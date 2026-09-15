"""Tests for db/sanity_check.py, run against the real database. Every
test uses its own throwaway source_id so nothing here ever touches or
depends on real Netanya/Tel Aviv data.
"""

import uuid

import pytest

from db.connection import get_connection
from db.sanity_check import get_next_batch, record_anomaly, record_check


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
        cur.execute("delete from business_anomaly where source_id = %s", (source_id,))
        cur.execute("delete from business_sanity_check where source_id = %s", (source_id,))
        cur.execute("delete from business_override where source_id = %s", (source_id,))
        cur.execute("delete from business where source_id = %s", (source_id,))
    conn.commit()


def _insert_business(conn, source_id, source_record_id, **overrides):
    values = {
        "source_id": source_id,
        "source_record_id": source_record_id,
        "name_raw": "Original Name",
        "category_canonical": "restaurant",
        "certifying_authority": "Test Authority",
        "address_raw": "1 Test St",
        "city": "Test City",
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


def test_record_check_matched_requires_a_url(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    check_id = record_check(conn, source_id, "biz-1", "matched", external_url="https://example.test/biz-1")
    assert check_id > 0

    with pytest.raises(Exception):
        record_check(conn, source_id, "biz-1", "matched", external_url=None)
    conn.rollback()


def test_record_check_not_found_must_not_have_a_url(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    check_id = record_check(conn, source_id, "biz-1", "not_found")
    assert check_id > 0

    with pytest.raises(Exception):
        record_check(conn, source_id, "biz-1", "not_found", external_url="https://example.test/oops")
    conn.rollback()


def test_record_check_stores_external_category(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    check_id = record_check(
        conn, source_id, "biz-1", "matched",
        external_url="https://example.test/biz-1", external_category="Italian restaurant",
    )
    with conn.cursor() as cur:
        cur.execute("select external_category from business_sanity_check where id = %s", (check_id,))
        assert cur.fetchone()[0] == "Italian restaurant"


def test_record_anomaly_defaults_to_pending(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    check_id = record_check(conn, source_id, "biz-1", "matched", external_url="https://example.test/biz-1")
    anomaly_id = record_anomaly(conn, check_id, source_id, "biz-1", "address", "1 Test St", "2 Test St")
    assert anomaly_id > 0

    with conn.cursor() as cur:
        cur.execute(
            "select status, resolved_at, our_value, external_value from business_anomaly where id = %s",
            (anomaly_id,),
        )
        status, resolved_at, our_value, external_value = cur.fetchone()
    assert status == "pending"
    assert resolved_at is None
    assert our_value == "1 Test St"
    assert external_value == "2 Test St"


def test_anomaly_field_name_is_constrained(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    check_id = record_check(conn, source_id, "biz-1", "matched", external_url="https://example.test/biz-1")
    with pytest.raises(Exception):
        record_anomaly(conn, check_id, source_id, "biz-1", "kosher_type", "meat", "dairy")
    conn.rollback()


def test_resolved_at_must_match_status(conn, source_id):
    # Exercises the DB constraint directly - record_anomaly never sets
    # resolved_at itself (resolving is the admin UI's job), but the
    # constraint should still hold for anyone writing to the table.
    _insert_business(conn, source_id, "biz-1")
    check_id = record_check(conn, source_id, "biz-1", "matched", external_url="https://example.test/biz-1")
    with conn.cursor() as cur:
        with pytest.raises(Exception):
            cur.execute(
                """
                insert into business_anomaly
                    (sanity_check_id, source_id, source_record_id, field_name, status, resolved_at)
                values (%s, %s, %s, 'name', 'pending', now())
                """,
                (check_id, source_id, "biz-1"),
            )
    conn.rollback()


def test_deleting_business_cascades_to_check_and_anomaly(conn, source_id):
    _insert_business(conn, source_id, "biz-1")
    check_id = record_check(conn, source_id, "biz-1", "matched", external_url="https://example.test/biz-1")
    record_anomaly(conn, check_id, source_id, "biz-1", "name", "Original Name", "Different Name")

    with conn.cursor() as cur:
        cur.execute("delete from business where source_id = %s and source_record_id = 'biz-1'", (source_id,))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select count(*) from business_sanity_check where source_id = %s", (source_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("select count(*) from business_anomaly where source_id = %s", (source_id,))
        assert cur.fetchone()[0] == 0


def test_get_next_batch_prioritizes_never_checked(conn, source_id):
    _insert_business(conn, source_id, "biz-checked", category_canonical="cafe")
    _insert_business(conn, source_id, "biz-unchecked", category_canonical="cafe")
    record_check(conn, source_id, "biz-checked", "matched", external_url="https://example.test/checked")

    batch = get_next_batch(conn, limit=1000, source_id=source_id)
    ids = [r["source_record_id"] for r in batch]
    # Never-checked comes before already-checked (nulls first ordering).
    assert ids.index("biz-unchecked") < ids.index("biz-checked")


def test_get_next_batch_excludes_non_eatery_categories(conn, source_id):
    _insert_business(conn, source_id, "biz-hotel", category_canonical="hotel")
    _insert_business(conn, source_id, "biz-cafe", category_canonical="cafe")

    batch = get_next_batch(conn, limit=1000, source_id=source_id)
    ids = {r["source_record_id"] for r in batch}
    assert "biz-hotel" not in ids
    assert "biz-cafe" in ids
