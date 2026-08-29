"""Tests for the business_override merge logic in scripts/export_map_data.py.

Runs the same LEFT JOIN + COALESCE query the real export uses, scoped to
a throwaway source_id so this never touches or depends on real
Netanya/Tel Aviv data. Doesn't call export() itself (that writes the
real web/businesses.json against the whole table) - see
test_export_writes_real_file for the one test that does, using a
tmp_path redirect.
"""

import uuid
from pathlib import Path

import pytest

from db.connection import get_connection

# Same query as scripts/export_map_data.py, with a source_id filter
# added so tests never scan/export real production data.
_QUERY = """
    select
        b.source_id,
        b.source_record_id,
        coalesce(o.name_clean, b.name_raw) as name_raw,
        coalesce(o.category_canonical, b.category_canonical) as category_canonical,
        coalesce(o.kosher_type, b.kosher_type) as kosher_type,
        coalesce(o.supervision_level, b.supervision_level) as supervision_level,
        coalesce(o.lat, b.lat) as lat,
        coalesce(o.lng, b.lng) as lng,
        coalesce(o.phone, b.phone) as phone
    from business b
    left join business_override o
        on o.source_id = b.source_id and o.source_record_id = b.source_record_id
    where b.status = 'active' and b.source_id = %s
    order by b.source_record_id
"""


@pytest.fixture
def conn():
    connection = get_connection()
    yield connection
    connection.close()


@pytest.fixture
def source_id():
    return f"test_{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def _cleanup(conn, source_id):
    yield
    with conn.cursor() as cur:
        cur.execute("delete from business_override where source_id = %s", (source_id,))
        cur.execute("delete from business where source_id = %s", (source_id,))
    conn.commit()


def _insert_business(conn, source_id, source_record_id, **overrides):
    values = {
        "source_id": source_id,
        "source_record_id": source_record_id,
        "name_raw": "Original Name",
        "category_canonical": "restaurant",
        "kosher_type": ["meat"],
        "supervision_level": "regular",
        "certifying_authority": "Test Authority",
        "address_raw": "1 Test St",
        "city": "Test City",
        "lat": 32.0,
        "lng": 34.0,
        "location_source": "geocoded",
        "phone": "050-0000000",
        "status": "active",
    }
    values.update(overrides)
    with conn.cursor() as cur:
        cols = list(values)
        cur.execute(
            f"insert into business ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})",
            [values[c] for c in cols],
        )
    conn.commit()


def _insert_override(conn, source_id, source_record_id, **fields):
    with conn.cursor() as cur:
        cols = ["source_id", "source_record_id"] + list(fields)
        values = [source_id, source_record_id] + list(fields.values())
        cur.execute(
            f"insert into business_override ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})",
            values,
        )
    conn.commit()


def _fetch(conn, source_id):
    with conn.cursor() as cur:
        cur.execute(_QUERY, (source_id,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def test_no_override_falls_back_to_business_values(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    rows = _fetch(conn, source_id)
    assert rows == [{
        "source_id": source_id, "source_record_id": "biz-1", "name_raw": "Original Name",
        "category_canonical": "restaurant", "kosher_type": ["meat"], "supervision_level": "regular",
        "lat": 32.0, "lng": 34.0, "phone": "050-0000000",
    }]


def test_override_position_wins_over_scraped_position(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    _insert_override(conn, source_id, "biz-1", lat=32.5, lng=34.5, note="corrected pin")
    rows = _fetch(conn, source_id)
    assert rows[0]["lat"] == 32.5
    assert rows[0]["lng"] == 34.5
    # Non-overridden fields still come from the scraped row.
    assert rows[0]["name_raw"] == "Original Name"


def test_override_partial_fields_only_replaces_those_fields(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    _insert_override(conn, source_id, "biz-1", category_canonical="cafe", note="wrong category")
    rows = _fetch(conn, source_id)
    assert rows[0]["category_canonical"] == "cafe"
    assert rows[0]["kosher_type"] == ["meat"]  # untouched
    assert rows[0]["lat"] == 32.0  # untouched


def test_override_deleted_when_business_deleted(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    _insert_override(conn, source_id, "biz-1", phone="050-1111111", note="fixed phone")
    with conn.cursor() as cur:
        cur.execute("delete from business where source_id = %s and source_record_id = 'biz-1'", (source_id,))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from business_override where source_id = %s and source_record_id = 'biz-1'",
            (source_id,),
        )
        assert cur.fetchone()[0] == 0  # cascaded


def test_empty_override_row_is_rejected(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    with pytest.raises(Exception):
        _insert_override(conn, source_id, "biz-1", note="no actual field changed")
    conn.rollback()  # the failed insert leaves the transaction aborted


def test_export_writes_real_file(conn, source_id, tmp_path, monkeypatch):
    from scripts import export_map_data

    _insert_business(conn, source_id, "biz-1")
    fake_output = tmp_path / "businesses.json"
    monkeypatch.setattr(export_map_data, "OUTPUT_PATH", fake_output)

    count = export_map_data.export()

    assert count > 0
    assert fake_output.exists()
    import json
    data = json.loads(fake_output.read_text(encoding="utf-8"))
    assert any(r["source_id"] == source_id for r in data)
