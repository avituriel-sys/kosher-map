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

# "other" is included on purpose (2026-09-24): sources that publish no
# business type at all (Petah Tikva's council site, most of the m-datit
# portal) leave most of their listings as "other", and Google's own
# subheading for the place is the best cheap signal we have for what they
# really are. Checking them records that subheading in external_category
# (and confirms they exist); a matched check can then be used to give the
# business a real category. Genuine non-eateries stay excluded via their
# own categories (hotel, factory, catering, event_hall, ...).
SANITY_CHECK_CATEGORIES = EATERY_CATEGORIES + ["other"]

# Cuisine type is prep for a future map-pin/list-view icon feature
# (discussed 2026-09-17) - it only exists to subdivide the two big,
# visually-uninformative category_canonical buckets ("restaurant",
# "cafe") into something an icon can actually distinguish. It's
# deliberately NOT a general-purpose cuisine taxonomy: category_canonical
# already distinguishes pizzeria/sushi_asian/bakery_patisserie/
# ice_cream/falafel_shawarma on its own, so those aren't repeated here -
# a badge-rendering step would check category_canonical first and only
# fall back to cuisine_type for "restaurant"/"cafe".
CUISINE_TYPES = {
    "burger", "italian", "asian", "middle_eastern", "grill_meat",
    "seafood", "cafe_bakery", "ethiopian", "generic",
}

# Maps Google Maps' own subheading text (business_sanity_check.
# external_category - see migration 007) to a CUISINE_TYPES value.
# Seeded from subheadings actually seen during real sanity-check
# research batches (2026-09 pilot batches) - like every mapping table
# in this project, deliberately incomplete: extend as new subheadings
# turn up rather than guessing at a translation. A caller should treat
# a miss here as "leave cuisine_type unset for now", not as license to
# invent a mapping.
CUISINE_TYPE_MAP = {
    "מסעדה איטלקית": "italian",
    "מסעדת המבורגרים": "burger",
    "מסעדה יפנית": "asian",
    "מסעדה אסייתית": "asian",
    "מסעדה מזרח תיכונית": "middle_eastern",
    "פיצרייה": "italian",
    "בית קפה": "cafe_bakery",
    "מאפייה": "cafe_bakery",
    "קונדיטוריה": "cafe_bakery",
    "פטיסרי": "cafe_bakery",
    "חנות עוגות": "cafe_bakery",
    "מסעדה אתיופית": "ethiopian",
}


def map_cuisine_type(external_category: str | None) -> str | None:
    """Best-effort translation of a Google Maps subheading into a
    CUISINE_TYPES value, or None if unmapped/absent. Pure lookup - does
    not write anything; whoever records the result (currently a batch
    script, same as every other sanity-check write) decides whether and
    where to store it, per this module's own no-writes-to-business*
    boundary (see module docstring).
    """
    if not external_category:
        return None
    return CUISINE_TYPE_MAP.get(external_category.strip())


def record_check(
    conn: psycopg.Connection,
    source_id: str,
    source_record_id: str,
    match_status: MatchStatus,
    external_url: str | None = None,
    notes: str | None = None,
    external_category: str | None = None,
) -> int:
    """Record that a business was checked against an outside source.
    Always call this once per business checked, whether or not
    anything was wrong - a 'matched' check with no anomalies recorded
    against it is exactly how "verified clean" is represented. Returns
    the new check's id, for passing to record_anomaly().

    `external_category` is the source's own free-text category label
    (e.g. Google Maps' subheading, "מסעדה איטלקית") - kept for later
    use, not compared against our category_canonical (see migration
    007 for why that's not an anomaly field).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into business_sanity_check
                (source_id, source_record_id, match_status, external_url, notes, external_category)
            values (%s, %s, %s, %s, %s, %s)
            returning id
            """,
            (source_id, source_record_id, match_status, external_url, notes, external_category),
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
    """Return up to `limit` eatery-category (or "other" - see
    SANITY_CHECK_CATEGORIES) active businesses to check
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
                   b.city, b.phone, b.category_canonical,
                   last_check.checked_at as last_checked_at
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
            (SANITY_CHECK_CATEGORIES, source_id, source_id, limit),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
