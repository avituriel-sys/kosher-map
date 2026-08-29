"""Export active, geocoded businesses to web/businesses.json for the map.

Left-joins business_override so a manual correction (see
db/migrations/003_business_override.sql) shows up on the map without
waiting for the source to change - COALESCE prefers the override value
field-by-field, falling back to the collector's scraped value where no
override exists.

This is a static snapshot, not a live query: the map fetches this file
directly rather than calling Supabase at page-load time, so a change
here only reaches the public map after this script re-runs and the
result is committed and redeployed (see spec section 8/9 - the real app
would query live; this is Phase 1's lightweight stand-in for that).

    python -m scripts.export_map_data
"""

import json
import sys
from pathlib import Path

from db.connection import get_connection

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "web" / "businesses.json"


def export() -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                select
                    b.source_id,
                    b.source_record_id,
                    coalesce(o.name_clean, b.name_raw) as name_raw,
                    coalesce(o.category_canonical, b.category_canonical) as category_canonical,
                    coalesce(o.kosher_type, b.kosher_type) as kosher_type,
                    coalesce(o.supervision_level, b.supervision_level) as supervision_level,
                    b.address_raw,
                    b.city,
                    coalesce(o.lat, b.lat) as lat,
                    coalesce(o.lng, b.lng) as lng,
                    b.certifying_authority,
                    b.source_url,
                    coalesce(o.phone, b.phone) as phone
                from business b
                left join business_override o
                    on o.source_id = b.source_id and o.source_record_id = b.source_record_id
                where b.status = 'active'
                    and coalesce(o.lat, b.lat) is not null
                    and coalesce(o.lng, b.lng) is not null
            """)
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return len(rows)


if __name__ == "__main__":
    count = export()
    print(f"exported {count} businesses to {OUTPUT_PATH}", file=sys.stderr)
