"""One-off clean-up after adding the `winery` category and the Tzohar
duplicate merge, applied to rows already in the database so Tzohar doesn't
have to be fetched again:

1. Any "other" business whose name says it is a winery (יקב / ווינרי /
   Winery) gets category_canonical = "winery", from any source.
2. Tzohar rows that are the same business listed more than once are merged:
   the oldest store id survives (taking the longest name and the union of the
   kosher types) and the others are marked absent - exactly what the next
   Tzohar collection run would do.

Dry run by default; pass --apply to write.

    python -m scripts.tidy_tzohar_and_wineries
    python -m scripts.tidy_tzohar_and_wineries --apply
"""

import sys

from collectors.common.category_keywords import map_category
from collectors.tzohar.collector import (
    SOURCE_ID,
    find_duplicate_clusters,
    pick_survivor,
)
from db.connection import get_connection

sys.stdout.reconfigure(encoding="utf-8")


def main(apply: bool) -> None:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "select source_id, source_record_id, name_raw from business "
        "where category_canonical = 'other' and status = 'active'"
    )
    wineries = [
        (source_id, record_id, name)
        for source_id, record_id, name in cur.fetchall()
        if map_category(None, name)[0] == "winery"
    ]
    print(f"{len(wineries)} 'other' business(es) become winery")
    for source_id, record_id, name in wineries:
        print(f"   {source_id}/{record_id}: {name}")
        if apply:
            cur.execute(
                "update business set category_canonical = 'winery' "
                "where source_id = %s and source_record_id = %s",
                (source_id, record_id),
            )

    cur.execute(
        "select source_record_id, name_raw, address_raw, city, kosher_type, lat, lng "
        "from business where source_id = %s and status = 'active'",
        (SOURCE_ID,),
    )
    records = [
        {"source_record_id": r[0], "name_raw": r[1], "address_raw": r[2],
         "city": r[3], "kosher_type": r[4], "lat": r[5], "lng": r[6]}
        for r in cur.fetchall()
    ]
    clusters = find_duplicate_clusters(records)
    absent = 0
    for cluster in clusters:
        survivor = pick_survivor(cluster)
        longest = max(cluster, key=lambda r: len(r["name_raw"]))["name_raw"]
        types = sorted({t for r in cluster for t in (r["kosher_type"] or [])}) or None
        others = [r for r in cluster if r is not survivor]
        absent += len(others)
        print(f"merge into {survivor['source_record_id']} ({longest}): "
              f"drop {[r['source_record_id'] for r in others]}")
        if apply:
            cur.execute(
                "update business set name_raw = %s, name_clean = %s, kosher_type = %s "
                "where source_id = %s and source_record_id = %s",
                (longest, longest, types, SOURCE_ID, survivor["source_record_id"]),
            )
            cur.execute(
                "update business set status = 'absent', status_changed_at = now() "
                "where source_id = %s and source_record_id = any(%s)",
                (SOURCE_ID, [r["source_record_id"] for r in others]),
            )
    print(f"{len(clusters)} duplicate group(s); {absent} listing(s) marked absent")

    if apply:
        conn.commit()
        print("applied")
    else:
        conn.rollback()
        print("dry run - nothing written (use --apply)")
    conn.close()


if __name__ == "__main__":
    main("--apply" in sys.argv)
