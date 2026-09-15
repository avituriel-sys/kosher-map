"""Helpers for the sanity-check feature: cross-referencing eateries
against outside sources and recording what's found (schema and
reasoning in db/migrations/006_sanity_check.sql).

This module doesn't do the actual lookup - there's no automated
scraper here by design (see that migration's comments, and the
conversation that shaped it): a human/Claude researches each business
and calls record_check()/record_anomaly() with the findings. Nothing
here writes to `business` or `business_override` directly; resolving
an anomaly (via the admin UI, not this module) is what does that.
"""

from __future__ import annotations

from typing import Literal

import psycopg

MatchStatus = Literal["matched", "not_found", "ambiguous"]
FieldName = Literal["name", "address", "phone"]

# What the sanity check covers, per the scoping discussion: "eateries"
# in the sense of somewhere a person walks in and eats, bakeries
# included - not catering, institutional kitchens, event halls, hotels,
# or the shop/factory categories. Kept here rather than in
# collectors/*/mappings.py since this is a property of the sanity-check
# feature, not of any one source's category taxonomy.
EATERY_CATEGORIES = [
    "restaurant", "cafe", "pizzeria", "falafel_shawarma",
    "sushi_asian", "ice_cream", "bakery_patisserie",
]


def record_check(
    conn: psycopg.Connection,
    source_id: str,
    source_record_id: str,
    match_status: MatchStatus,
    external_url: str | None = None,
    notes: str | None = None,
) -> int:
    """Record that a business was checked against an outside source.
    Always call this once per business checked, whether or not
    anything was wrong - a 'matched' check with no anomalies recorded
    against it is exactly how "verified clean" is represented. Returns
    the new check's id, for passing to record_anomaly().
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into business_sanity_check
                (source_id, source_record_id, match_status, external_url, notes)
            values (%s, %s, %s, %s, %s)
            returning id
            """,
            (source_id, source_record_id, match_status, external_url, notes),
        )
        check_id = cur.fetchone()[0]
    conn.commit()
    return check_id


def record_anomaly(
    conn: psycopg.Connection,
    sanity_check_id: int,
    source_id: str,
    source_record_id: str,
    field_name: FieldName,
    our_value: str | None,
    external_value: str | None,
) -> int:
    """Record one field discrepancy found during a check. Call after
    record_check() for the same business, passing its returned id.
    Leaves the anomaly status="pending" - resolving it (writing a
    business_override and marking accepted_external/kept_ours/
    manual_fix) is the admin UI's job, not this module's.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into business_anomaly
                (sanity_check_id, source_id, source_record_id, field_name, our_value, external_value)
            values (%s, %s, %s, %s, %s, %s)
            returning id
            """,
            (sanity_check_id, source_id, source_record_id, field_name, our_value, external_value),
        )
        anomaly_id = cur.fetchone()[0]
    conn.commit()
    return anomaly_id


def get_next_batch(
    conn: psycopg.Connection, limit: int = 30, source_id: str | None = None
) -> list[dict]:
    """Return up to `limit` eatery-category active businesses to check
    next: never-checked ones first, then whichever were checked
    longest ago. Doesn't mark anything as "in progress" - re-running
    this before recording results for the previous batch will return
    the same businesses again, which is fine for a human-paced,
    resumable workflow (this session's whole reason for existing) but
    worth knowing if this is ever scripted.

    `source_id` optionally scopes to one source (e.g. a themed batch of
    just Tel Aviv businesses) - also what tests use to stay isolated
    from the real, much larger, set of never-checked businesses.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select b.source_id, b.source_record_id, b.name_raw, b.address_raw,
                   b.city, b.phone, last_check.checked_at as last_checked_at
            from business b
            left join lateral (
                select checked_at from business_sanity_check sc
                where sc.source_id = b.source_id and sc.source_record_id = b.source_record_id
                order by checked_at desc
                limit 1
            ) last_check on true
            where b.status = 'active' and b.category_canonical = any(%s)
              and (%s::text is null or b.source_id = %s)
            order by last_check.checked_at asc nulls first
            limit %s
            """,
            (EATERY_CATEGORIES, source_id, source_id, limit),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
