"""Shared change-detection logic, per spec section 6.3.

Written once here rather than duplicated per collector, per section 6.1:
a collector's only job is to fetch and normalize; this module is what
decides what changed since the last run and writes it to the database.

Two distinct kinds of collector output feed into this, and they must be
applied differently:

- A *full-census* run (Netanya's collector, Tel Aviv's active-listings
  collector) returns everything currently published by that source.
  Anything previously seen for that source_id but missing from this run
  gets marked "absent" - its absence *is* the signal.
- A *partial-feed* run (Tel Aviv's revoked_collector) returns only a
  small subset - the revoked list, not a census of the whole source. The
  businesses it doesn't mention say nothing about whether they're still
  active; "mark everything else absent" must never run for one of these,
  or every non-revoked Tel Aviv business would be wrongly marked absent
  the moment the revoked feed's own collection_run was applied. See
  `is_full_census`.

One rule this module enforces that spec doesn't spell out explicitly:
a business already marked "revoked" doesn't get silently flipped back to
"active" just because a later full-census run finds it still listed
(sources can be slow to update their own directory after revoking
someone). That would undermine the one thing revoked status is for. It
takes an explicit appearance in some future "un-revoked" signal - not
built yet, since no source publishes one - to move a business off
"revoked".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

logger = logging.getLogger(__name__)

# Columns compared to decide whether a change is worth a log line, per
# spec section 6.3: "if supervision_level or kosher_type changed, log it
# as a notable change."
NOTABLE_CHANGE_FIELDS = ("supervision_level", "kosher_type")

_UPSERT_COLUMNS = [
    "source_id",
    "source_record_id",
    "source_url",
    "name_raw",
    "name_clean",
    "branch",
    "category_raw",
    "category_canonical",
    "kosher_type",
    "supervision_level",
    "certifying_authority",
    "additional_hechsher",
    "address_raw",
    "city",
    "lat",
    "lng",
    "location_source",
    "location_confidence",
    "phone",
    "supervisor_name",
    "supervisor_phone",
    "status",
]


@dataclass
class CollectionRunResult:
    run_id: int
    records_found: int
    records_new: int
    records_absent: int
    notable_changes: int


def apply_collection_run(
    conn: psycopg.Connection,
    source_id: str,
    records: list[dict],
    is_full_census: bool,
) -> CollectionRunResult:
    """Apply one collector's output to the database as a single
    collection_run. Commits internally; raises and rolls back on error
    so a failed run never leaves the database half-updated (spec
    section 6.1's fail-loud principle applies here too - a partially
    applied run is exactly the kind of silent corruption the whole
    snapshot design in spec section 3 exists to avoid).
    """
    started_at = datetime.now(timezone.utc)
    seen_source_record_ids: set[str] = set()
    records_new = 0
    notable_changes = 0

    try:
        with conn.cursor() as cur:
            for record in records:
                seen_source_record_ids.add(record["source_record_id"])
                is_new, changed_fields = _upsert_business(cur, record)
                if is_new:
                    records_new += 1
                notable = changed_fields & set(NOTABLE_CHANGE_FIELDS)
                if notable:
                    notable_changes += 1
                    logger.info(
                        "%s: notable change on %s (%s): %s",
                        source_id,
                        record["source_record_id"],
                        record.get("name_raw"),
                        sorted(notable),
                    )

            records_absent = 0
            if is_full_census:
                records_absent = _mark_missing_as_absent(
                    cur, source_id, seen_source_record_ids
                )

            run_id = _insert_collection_run(
                cur,
                source_id=source_id,
                started_at=started_at,
                outcome="success",
                records_found=len(records),
                records_new=records_new,
                records_absent=records_absent,
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        with conn.cursor() as cur:
            _insert_collection_run(
                cur,
                source_id=source_id,
                started_at=started_at,
                outcome="failed",
                records_found=len(records),
                records_new=None,
                records_absent=None,
                error_detail=str(exc),
            )
        conn.commit()
        raise

    return CollectionRunResult(
        run_id=run_id,
        records_found=len(records),
        records_new=records_new,
        records_absent=records_absent,
        notable_changes=notable_changes,
    )


def _upsert_business(cur: psycopg.Cursor, record: dict) -> tuple[bool, set[str]]:
    """Insert or update one business row. Returns (is_new, changed_fields).

    Revoked status is sticky (see module docstring): an incoming record
    that would set status="active" or "absent" over an existing
    status="revoked" row is applied to every other column but leaves
    status alone.
    """
    cur.execute(
        "select status, supervision_level, kosher_type from business "
        "where source_id = %s and source_record_id = %s",
        (record["source_id"], record["source_record_id"]),
    )
    existing = cur.fetchone()

    values = {col: record.get(col) for col in _UPSERT_COLUMNS}
    if existing is not None:
        existing_status, existing_supervision, existing_kosher_type = existing
        if existing_status == "revoked" and values["status"] != "revoked":
            logger.warning(
                "%s: source still lists %s as %s but it's marked revoked here - "
                "keeping revoked (source may not have updated its own listing yet)",
                record["source_id"],
                record["source_record_id"],
                values["status"],
            )
            values["status"] = "revoked"

        changed_fields = set()
        if values["supervision_level"] != existing_supervision:
            changed_fields.add("supervision_level")
        if (values["kosher_type"] or []) != (existing_kosher_type or []):
            changed_fields.add("kosher_type")

        set_clause = ", ".join(f"{col} = %({col})s" for col in _UPSERT_COLUMNS)
        cur.execute(
            f"""
            update business
            set {set_clause}, last_seen = now(), status_changed_at =
                case when status is distinct from %(status)s then now()
                     else status_changed_at end
            where source_id = %(source_id)s
              and source_record_id = %(source_record_id)s
            """,
            values,
        )
        return False, changed_fields

    columns = ", ".join(_UPSERT_COLUMNS)
    placeholders = ", ".join(f"%({col})s" for col in _UPSERT_COLUMNS)
    cur.execute(
        f"""
        insert into business ({columns}, first_seen, last_seen, status_changed_at)
        values ({placeholders}, now(), now(), now())
        """,
        values,
    )
    return True, set()


def _mark_missing_as_absent(
    cur: psycopg.Cursor, source_id: str, seen_source_record_ids: set[str]
) -> int:
    """Spec section 6.3: a record previously seen but absent from this
    run gets status="absent" - never deleted. Only applies within one
    source_id, and only ever called for a full-census run (see module
    docstring).
    """
    seen = list(seen_source_record_ids)
    cur.execute(
        """
        update business
        set status = 'absent', status_changed_at = now()
        where source_id = %s
          and status = 'active'
          and not (source_record_id = any(%s))
        """,
        (source_id, seen),
    )
    return cur.rowcount


def _insert_collection_run(
    cur: psycopg.Cursor,
    *,
    source_id: str,
    started_at: datetime,
    outcome: str,
    records_found: int | None,
    records_new: int | None,
    records_absent: int | None,
    error_detail: str | None = None,
) -> int:
    cur.execute(
        """
        insert into collection_run
            (source_id, started_at, finished_at, outcome,
             records_found, records_new, records_absent, error_detail)
        values (%s, %s, now(), %s, %s, %s, %s, %s)
        returning id
        """,
        (
            source_id,
            started_at,
            outcome,
            records_found,
            records_new,
            records_absent,
            error_detail,
        ),
    )
    return cur.fetchone()[0]
