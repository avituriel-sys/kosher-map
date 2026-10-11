"""Tests for the Tzohar store-locator collector.

The stores below are SYNTHETIC: they follow the WP Store Locator JSON
shape the finder returns (id, store, address, city, lat, lng, terms,
address2) but are not real listings. Tzohar's terms forbid automated
retrieval without written consent, so nothing here - or anywhere in the
test suite - contacts the live site.
"""

import pytest
import requests

from collectors.common.schema import CollectorError
from collectors.tzohar import collector


def _store(id_, **overrides):
    store = {
        "id": str(id_),
        "store": "מסעדת הדוגמה",
        "address": "הרצל 1",
        "city": "תל אביב",
        "lat": "32.0650",
        "lng": "34.7700",
        "terms": "בשרי",
        "address2": "https://example.test/certs/1.pdf",
    }
    store.update(overrides)
    return store


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, params))
        return _FakeResponse(self.payloads.pop(0))

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)


@pytest.fixture
def permitted(monkeypatch):
    monkeypatch.setenv(collector.PERMISSION_ENV_VAR, "1")


def test_refuses_to_run_without_written_permission(monkeypatch):
    monkeypatch.delenv(collector.PERMISSION_ENV_VAR, raising=False)
    session = _FakeSession([[_store(1)]])
    with pytest.raises(CollectorError, match="written permission"):
        collector.collect(session=session, centres=[(32.0, 34.8)])
    assert session.requests == []  # not a single request was made


def test_permission_flag_must_be_exactly_one(monkeypatch):
    monkeypatch.setenv(collector.PERMISSION_ENV_VAR, "true")
    with pytest.raises(CollectorError):
        collector.collect(session=_FakeSession([[_store(1)]]), centres=[(32.0, 34.8)])


def test_canonical_record_fields():
    record = collector.to_canonical_record(_store(7))
    assert record["source_id"] == "tzohar"
    assert record["source_record_id"] == "7"
    assert record["name_raw"] == "מסעדת הדוגמה"
    assert record["address_raw"] == "הרצל 1"
    assert record["city"] == "תל אביב"
    assert record["certifying_authority"] == "כשרות צהר"
    assert record["supervision_level"] == "unknown"
    assert record["kosher_type"] == ["meat"]
    assert record["source_url"] == "https://example.test/certs/1.pdf"
    assert (record["lat"], record["lng"]) == (32.065, 34.77)
    assert record["location_source"] == "published"
    assert record["phone"] is None
    assert record["status"] == "active"


@pytest.mark.parametrize(
    "terms, expected",
    [("חלבי", ["dairy"]), ("בשרי", ["meat"]), ("פרווה", ["parve"]),
     ("חלבי, פרווה", ["dairy", "parve"]), ("", None)],
)
def test_terms_map_to_kosher_type(terms, expected):
    assert collector.to_canonical_record(_store(1, terms=terms))["kosher_type"] == expected


def test_html_in_name_is_unescaped_and_stripped():
    record = collector.to_canonical_record(_store(1, store="קפה &amp; בר <b>הים</b>"))
    assert record["name_raw"] == "קפה & בר הים"


def test_missing_certificate_link_falls_back_to_the_finder_page():
    record = collector.to_canonical_record(_store(1, address2=""))
    assert record["source_url"] == collector.PAGE_URL


def test_coordinates_outside_israel_are_dropped_not_trusted():
    record = collector.to_canonical_record(_store(1, lat="0", lng="0"))
    assert record["lat"] is None and record["lng"] is None
    assert record["location_source"] is None


def test_store_without_address_is_skipped():
    assert collector.to_canonical_record(_store(1, address="")) is None


def test_store_without_name_is_skipped_not_fatal():
    assert collector.to_canonical_record(_store(1, store="")) is None
    records = collector.parse_stores([_store(1, store=""), _store(2)])
    assert [r["source_record_id"] for r in records] == ["2"]


def test_category_comes_from_the_name_when_recognisable():
    record = collector.to_canonical_record(_store(1, store="פיצה הדוגמה"))
    assert record["category_canonical"] == collector.map_category(None, "פיצה הדוגמה")[0]
    unknown = collector.to_canonical_record(_store(2, store="שם בלי רמז"))
    assert unknown["category_canonical"] == "other"


def test_overlapping_circles_are_deduplicated_by_id(permitted):
    a = _store(1, store="קפה אלף", address="הרצל 1")
    b = _store(2, store="פיצה בית", address="דיזנגוף 2")
    c = _store(3, store="סושי גמל", address="אלנבי 3")
    session = _FakeSession([[a, b], [b, c]])
    records = collector.collect(session=session, centres=[(32.0, 34.8), (32.1, 34.9)])
    assert sorted(r["source_record_id"] for r in records) == ["1", "2", "3"]
    assert len(session.requests) == 2
    _, params = session.requests[0]
    assert params["action"] == "store_search"
    assert params["search_radius"] == collector.SEARCH_RADIUS_KM


def test_same_id_with_different_details_fails_loudly():
    with pytest.raises(CollectorError, match="differing"):
        collector.parse_stores([_store(1), _store(1, address="אחר 2")])


def test_full_response_may_be_truncated_and_fails(permitted):
    full = [_store(i) for i in range(collector.MAX_RESULTS)]
    with pytest.raises(CollectorError, match="truncated"):
        collector.collect(session=_FakeSession([full]), centres=[(32.0, 34.8)])


def test_non_list_response_fails(permitted):
    with pytest.raises(CollectorError, match="unexpected response"):
        collector.collect(session=_FakeSession([{"error": "nope"}]), centres=[(32.0, 34.8)])


@pytest.mark.parametrize("empty", ["", None, 0])
def test_circle_with_no_stores_may_answer_with_an_empty_value(permitted, empty):
    session = _FakeSession([empty, [_store(1)]])
    records = collector.collect(session=session, centres=[(31.25, 34.4), (32.0, 34.8)])
    assert [r["source_record_id"] for r in records] == ["1"]


def test_invalid_json_fails(permitted):
    with pytest.raises(CollectorError, match="failed to fetch"):
        collector.collect(session=_FakeSession([ValueError("bad")]), centres=[(32.0, 34.8)])


def test_zero_stores_is_not_a_census(permitted):
    with pytest.raises(CollectorError, match="zero usable"):
        collector.collect(session=_FakeSession([[]]), centres=[(32.0, 34.8)])


@pytest.mark.parametrize(
    "name, expected",
    [
        ("ארומה רוטשילד", "cafe"),
        ("בריא בריבוע בן גוריון", "institutional_kitchen"),
        ("מלון אינדיגו", "hotel"),
        ("עוגות דה לה פה אשדוד", "bakery_patisserie"),
        ("יקב כהנוב", "winery"),
        ("Family Baum Winery", "winery"),
        ("שם בלי רמז", "other"),
        ("פיצה הדוגמה", "pizzeria"),     # shared table still wins first
    ],
)
def test_extra_name_keywords(name, expected):
    assert collector.category_for_name(name) == expected


def _rec(id_, name, address="שעל 1", city="שעל", terms="פרווה", lat="32.0650"):
    return collector.to_canonical_record(
        _store(id_, store=name, address=address, city=city, terms=terms, lat=lat)
    )


def test_same_business_listed_twice_is_merged_into_the_oldest_id():
    records = [_rec(60196, "יקב פאמילי באום ווינרי Family Baum Winery"),
               _rec(58180, "פאמילי באום ווינרי Family Baum Winery"),
               _rec(58388, "יקב פאמילי באום ווינרי")]
    merged = collector.merge_duplicate_records(records)
    assert [r["source_record_id"] for r in merged] == ["58180"]
    assert merged[0]["name_raw"] == "יקב פאמילי באום ווינרי Family Baum Winery"  # longest
    assert merged[0]["category_canonical"] == "winery"


def test_name_contained_in_the_other_counts_as_duplicate():
    merged = collector.merge_duplicate_records([_rec(2, "אוסול ראש פינה"), _rec(1, "אוסול")])
    assert [r["source_record_id"] for r in merged] == ["1"]
    assert merged[0]["name_raw"] == "אוסול ראש פינה"


def test_address_text_variants_at_the_same_coordinates_are_merged():
    a = _rec(1, "יקב צ'יטה Cheetah winery", address="טכנו פארק ביתן 204", city="אבן יהודה")
    b = _rec(2, "יקב צ'יטה", address="אבן יהודה טכנו פארק ביתן 204", city="אבן יהודה")
    assert [r["source_record_id"] for r in collector.merge_duplicate_records([a, b])] == ["1"]


def test_different_businesses_at_one_address_are_not_merged():
    records = [_rec(1, "קיטשן 94"), _rec(2, "מקסימום קונספט – פסאדור"), _rec(3, "ארקפה מגדלי אלון")]
    assert len(collector.merge_duplicate_records(records)) == 3


def test_same_name_at_different_addresses_is_not_merged():
    records = [_rec(1, "ארומה", address="הרצל 1"),
               _rec(2, "ארומה", address="דיזנגוף 5", lat="32.0900")]
    assert len(collector.merge_duplicate_records(records)) == 2


def test_merging_unions_the_kosher_types():
    merged = collector.merge_duplicate_records(
        [_rec(1, "מסעדת הדוגמה", terms="חלבי"), _rec(2, "מסעדת הדוגמה", terms="בשרי")]
    )
    assert merged[0]["kosher_type"] == ["dairy", "meat"]


def test_parse_stores_merges_duplicates():
    records = collector.parse_stores([
        _store(2, store="יקב כהנוב", address="גדרה", city="גדרה"),
        _store(1, store="יקב כהנוב", address="גדרה", city="גדרה"),
        _store(3, store="קפה אחר", address="הרצל 9", city="גדרה", lat="32.2000"),
    ])
    assert sorted(r["source_record_id"] for r in records) == ["1", "3"]


def test_every_centre_is_inside_israel_and_spaced_for_coverage():
    lo_lat, hi_lat, lo_lng, hi_lng = collector.ISRAEL_BOUNDS
    for lat, lng in collector.CENTRES:
        assert lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng
    assert 25 <= len(collector.CENTRES) <= 45
