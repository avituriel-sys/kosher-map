"""Tests for db/change_detection.py, run against the real database
(Supabase Postgres, per .env). Every test uses its own throwaway
source_id so nothing here ever touches or depends on real Netanya/Tel
Aviv rows, and the fixture deletes everything under that source_id
afterward regardless of pass/fail.
"""

import uuid

import pytest

from db.change_detection import apply_collection_run
from db.change_detection import _BATCH_SIZE
from db.connection import get_connection


def _record(source_id, source_record_id, **overrides):
    base = {
        "source_id": source_id,
        "source_record_id": source_record_id,
        "source_url": f"https://example.test/{source_record_id}",
        "name_raw": "Test Business",
        "name_clean": "Test Business",
        "branch": None,
        "category_raw": "restaurant",
        "category_canonical": "restaurant",
        "kosher_type": ["meat"],
        "supervision_level": "regular",
        "certifying_authority": "Test Authority",
        "additional_hechsher": None,
        "address_raw": "1 Test St",
        "city": "Test City",
        "lat": 32.08,
        "lng": 34.78,
        "location_source": "geocoded",
        "location_confidence": "high",
        "phone": None,
        "supervisor_name": None,
        "supervisor_phone": None,
        "status": "active",
    }
    base.update(overrides)
    return base


@pytest.fixture
def conn():
    connection = get_connection()
    yield connection
    connection.close()


@pytest.fixture
def source_id():
    sid = f"test_{uuid.uuid4().hex[:12]}"
    yield sid


@pytest.fixture(autouse=True)
def _cleanup(conn, source_id):
    yield
    with conn.cursor() as cur:
        cur.execute("delete from business where source_id = %s", (source_id,))
        cur.execute("delete from collection_run where source_id = %s", (source_id,))
    conn.commit()


def _fetch(conn, source_id, source_record_id):
    with conn.cursor() as cur:
        cur.execute(
            "select status, first_seen, last_seen, status_changed_at, "
            "supervision_level, lat, lng, location is not null "
            "from business where source_id = %s and source_record_id = %s",
            (source_id, source_record_id),
        )
        return cur.fetchone()


def test_new_business_is_inserted_active(conn, source_id):
    result = apply_collection_run(
        conn, source_id, [_record(source_id, "biz-1")], is_full_census=True
    )
    assert result.records_found == 1
    assert result.records_new == 1
    assert result.records_absent == 0

    row = _fetch(conn, source_id, "biz-1")
    assert row is not None
    status, first_seen, last_seen, status_changed_at, *_ = row
    assert status == "active"
    assert first_seen == last_seen
    assert status_changed_at is not None


def test_generated_location_column_populates_from_lat_lng(conn, source_id):
    apply_collection_run(
        conn, source_id, [_record(source_id, "biz-1")], is_full_census=True
    )
    row = _fetch(conn, source_id, "biz-1")
    has_location = row[-1]
    assert has_location is True


def test_reseen_business_updates_last_seen_not_first_seen(conn, source_id):
    apply_collection_run(
        conn, source_id, [_record(source_id, "biz-1")], is_full_census=True
    )
    first_row = _fetch(conn, source_id, "biz-1")

    result = apply_collection_run(
        conn, source_id, [_record(source_id, "biz-1")], is_full_census=True
    )
    assert result.records_new == 0
    second_row = _fetch(conn, source_id, "biz-1")

    assert second_row[1] == first_row[1]  # first_seen unchanged
    assert second_row[2] >= first_row[2]  # last_seen advanced or equal


def test_missing_business_marked_absent_only_on_full_census(conn, source_id):
    apply_collection_run(
        conn,
        source_id,
        [_record(source_id, "biz-1"), _record(source_id, "biz-2")],
        is_full_census=True,
    )

    # biz-2 no longer appears in this run.
    result = apply_collection_run(
        conn, source_id, [_record(source_id, "biz-1")], is_full_census=True
    )
    assert result.records_absent == 1
    assert _fetch(conn, source_id, "biz-1")[0] == "active"
    assert _fetch(conn, source_id, "biz-2")[0] == "absent"


def test_partial_feed_never_marks_others_absent(conn, source_id):
    apply_collection_run(
        conn,
        source_id,
        [_record(source_id, "biz-1"), _record(source_id, "biz-2")],
        is_full_census=True,
    )

    # A revoked-style partial feed mentions neither business - if this
    # were treated as a full census it would wrongly mark both absent.
    result = apply_collection_run(
        conn,
        source_id,
        [_record(source_id, "biz-3", status="revoked", name_raw="Other Biz")],
        is_full_census=False,
    )
    assert result.records_absent == 0
    assert _fetch(conn, source_id, "biz-1")[0] == "active"
    assert _fetch(conn, source_id, "biz-2")[0] == "active"
    assert _fetch(conn, source_id, "biz-3")[0] == "revoked"


def test_revoked_status_is_sticky_against_later_active_sighting(conn, source_id):
    apply_collection_run(
        conn, source_id, [_record(source_id, "biz-1")], is_full_census=True
    )
    apply_collection_run(
        conn,
        source_id,
        [_record(source_id, "biz-1", status="revoked")],
        is_full_census=False,
    )
    assert _fetch(conn, source_id, "biz-1")[0] == "revoked"

    # The source's own directory hasn't caught up yet and still lists
    # it as active - must not silently un-revoke it.
    apply_collection_run(
        conn, source_id, [_record(source_id, "biz-1", status="active")], is_full_census=True
    )
    assert _fetch(conn, source_id, "biz-1")[0] == "revoked"


def test_notable_change_counted_for_supervision_level_change(conn, source_id):
    apply_collection_run(
        conn,
        source_id,
        [_record(source_id, "biz-1", supervision_level="regular")],
        is_full_census=True,
    )
    result = apply_collection_run(
        conn,
        source_id,
        [_record(source_id, "biz-1", supervision_level="mehadrin")],
        is_full_census=True,
    )
    assert result.notable_changes == 1


def test_upsert_batching_spans_multiple_chunks_correctly(conn, source_id):
    # _BATCH_SIZE rows per SQL statement - use enough records to force
    # at least two chunks, so a bug scoped to "the last chunk" or "the
    # first chunk" would actually get caught.
    n = _BATCH_SIZE + 5
    first_batch = [_record(source_id, f"biz-{i}") for i in range(n)]
    result = apply_collection_run(conn, source_id, first_batch, is_full_census=True)
    assert result.records_found == n
    assert result.records_new == n

    with conn.cursor() as cur:
        cur.execute("select count(*) from business where source_id = %s", (source_id,))
        assert cur.fetchone()[0] == n

    # Re-run with the last record dropped and one changed - exercises
    # update, absent-marking, and notable-change detection across the
    # chunk boundary in the same pass.
    second_batch = [_record(source_id, f"biz-{i}") for i in range(n - 1)]
    second_batch[0] = _record(source_id, "biz-0", supervision_level="mehadrin")
    result = apply_collection_run(conn, source_id, second_batch, is_full_census=True)
    assert result.records_new == 0
    assert result.records_absent == 1
    assert result.notable_changes == 1
    assert _fetch(conn, source_id, f"biz-{n - 1}")[0] == "absent"
    assert _fetch(conn, source_id, "biz-0")[0] == "active"


def test_failed_run_rolls_back_and_records_failure(conn, source_id):
    bad_record = _record(source_id, "biz-1", status="not-a-real-status")
    with pytest.raises(Exception):
        apply_collection_run(conn, source_id, [bad_record], is_full_census=True)

    # Nothing from the failed attempt should have been committed.
    assert _fetch(conn, source_id, "biz-1") is None

    with conn.cursor() as cur:
        cur.execute(
            "select outcome, error_detail from collection_run "
            "where source_id = %s order by id desc limit 1",
            (source_id,),
        )
        outcome, error_detail = cur.fetchone()
    assert outcome == "failed"
    assert error_detail
