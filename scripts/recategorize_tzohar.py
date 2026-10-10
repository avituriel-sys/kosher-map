"""One-off: re-derive category_canonical for stored Tzohar rows from their
names using collectors.tzohar.collector.category_for_name, without
re-fetching anything from Tzohar. Safe to re-run; prints what changed.

    python -m scripts.recategorize_tzohar
"""

import collections
import sys

from collectors.tzohar.collector import SOURCE_ID, category_for_name
from db.connection import get_connection

sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    conn = get_connection()
    changed = 0
    totals: collections.Counter = collections.Counter()
    with conn.cursor() as cur:
        cur.execute(
            "select source_record_id, name_raw, category_canonical "
            "from business where source_id = %s",
            (SOURCE_ID,),
        )
        for source_record_id, name_raw, current in cur.fetchall():
            new = category_for_name(name_raw)
            totals[new] += 1
            if new != current:
                cur.execute(
                    "update business set category_canonical = %s "
                    "where source_id = %s and source_record_id = %s",
                    (new, SOURCE_ID, source_record_id),
                )
                changed += 1
    conn.commit()
    conn.close()
    print(f"updated {changed} row(s)")
    print(dict(totals))


if __name__ == "__main__":
    main()
