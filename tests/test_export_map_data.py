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
        coalesce(o.address_raw, b.address_raw) as address_raw,
        coalesce(o.lat, b.lat) as lat,
        coalesce(o.lng, b.lng) as lng,
        coalesce(o.phone, b.phone) as phone,
        o.cuisine_type,
        o.cuisine_type_source
    from business b
    left join business_override o
        on o.source_id = b.source_id and o.source_record_id = b.source_record_id
    where b.status = 'active' and b.source_id = %s
        and not coalesce(o.hidden, false)
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
        "address_raw": "1 Test St", "lat": 32.0, "lng": 34.0, "phone": "050-0000000",
        "cuisine_type": None, "cuisine_type_source": None,
    }]


def test_override_position_wins_over_scraped_position(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    _insert_override(conn, source_id, "biz-1", lat=32.5, lng=34.5, note="corrected pin")
    rows = _fetch(conn, source_id)
    assert rows[0]["lat"] == 32.5
    assert rows[0]["lng"] == 34.5
    # Non-overridden fields still come from the scraped row.
    assert rows[0]["name_raw"] == "Original Name"


def test_override_address_wins_over_scraped_address(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    _insert_override(conn, source_id, "biz-1", address_raw="2 Test St", note="corrected street number")
    rows = _fetch(conn, source_id)
    assert rows[0]["address_raw"] == "2 Test St"
    # Non-overridden fields still come from the scraped row.
    assert rows[0]["name_raw"] == "Original Name"


def test_override_cuisine_type_has_no_raw_fallback(conn, source_id):
    # Unlike every other override field, cuisine_type has no business.*
    # counterpart to fall back to - the business table never has one at
    # all, so it's simply null until something (a sanity-check batch,
    # eventually an admin) sets it.
    _insert_business(conn, source_id, "biz-1")
    _insert_override(
        conn, source_id, "biz-1",
        cuisine_type="burger", cuisine_type_source="sanity_check", note="from Google subheading",
    )
    rows = _fetch(conn, source_id)
    assert rows[0]["cuisine_type"] == "burger"
    assert rows[0]["cuisine_type_source"] == "sanity_check"


def test_cuisine_type_source_without_cuisine_type_is_rejected(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    with pytest.raises(Exception):
        _insert_override(conn, source_id, "biz-1", cuisine_type_source="admin", note="dangling source")
    conn.rollback()


def test_cuisine_type_source_must_be_a_known_value(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    with pytest.raises(Exception):
        _insert_override(
            conn, source_id, "biz-1",
            cuisine_type="burger", cuisine_type_source="made_up_source", note="bad source",
        )
    conn.rollback()


def test_hidden_business_is_excluded_from_export(conn, source_id):
    _insert_business(conn, source_id, "biz-hidden")
    _insert_business(conn, source_id, "biz-visible")
    _insert_override(conn, source_id, "biz-hidden", hidden=True, hidden_reason="closed", note="closed down")
    rows = _fetch(conn, source_id)
    assert [r["source_record_id"] for r in rows] == ["biz-visible"]


def test_hidden_false_alone_is_an_empty_override(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    with pytest.raises(Exception):
        _insert_override(conn, source_id, "biz-1", hidden=False, note="no actual change")
    conn.rollback()


def test_hidden_reason_requires_hidden(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    with pytest.raises(Exception):
        _insert_override(conn, source_id, "biz-1", phone="050-1111111", hidden_reason="orphan reason")
    conn.rollback()


def test_hidden_export_query_matches_real_export():
    # The mirrored _QUERY above is what the tests exercise; make sure the
    # real script filters hidden rows too, so the two can't drift apart.
    from scripts import export_map_data
    import inspect
    assert "coalesce(o.hidden, false)" in inspect.getsource(export_map_data.export)


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
