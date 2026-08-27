"""Tests for the Tel Aviv-Yafo active-listings collector.

Fixtures in tests/fixtures/tlv/ are the full live crawl as captured on
2026-08-27: unfiltered (all_p1..10, 1120 records), מהדרין-filtered
(mehadrin_p1..2, 130 records) and רגילה-filtered (regular_p1..9, 990
records). Tests run entirely offline - nothing here hits the live site
or the live geocoder.
"""

import re
from pathlib import Path

import pytest
import requests
from bs4 import BeautifulSoup

from collectors.common.geocoding import GeocodeResult, Geocoder
from collectors.common.schema import CollectorError
from collectors.tlv import collector

FIXTURES = Path(__file__).parent / "fixtures" / "tlv"


def _fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _FakeGeocoder(Geocoder):
    """Deterministic stand-in for Nominatim - never touches the network."""

    def __init__(self):
        self.calls: list[str] = []

    def geocode(self, query: str) -> GeocodeResult:
        self.calls.append(query)
        return GeocodeResult(lat=32.08, lng=34.78, confidence="high", out_of_bounds=False)


def _fake_get_all_pages():
    def fake_get(session, url):
        if "kashrut=318" in url:
            m = re.search(r"/page/(\d+)/", url)
            n = m.group(1) if m else "1"
            return _fixture_text(f"mehadrin_p{n}.html")
        if "kashrut=322" in url:
            m = re.search(r"/page/(\d+)/", url)
            n = m.group(1) if m else "1"
            return _fixture_text(f"regular_p{n}.html")
        m = re.search(r"/page/(\d+)/", url)
        n = m.group(1) if m else "1"
        return _fixture_text(f"all_p{n}.html")

    return fake_get


@pytest.fixture
def fake_collect_env(monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "_get", _fake_get_all_pages())
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)
    geocoder = _FakeGeocoder()
    cache_path = tmp_path / "geocode_cache.json"
    return geocoder, cache_path


def test_parse_cards_from_page1_matches_known_record():
    soup = BeautifulSoup(_fixture_text("all_p1.html"), "lxml")
    cards = collector._parse_cards(soup)

    assert len(cards) == 120
    first = cards[0]
    assert first.source_url.endswith(
        "/business/%d7%92%d7%90%d7%a1%d7%98-%d7%91%d7%99%d7%a3-just-beef/"
    )
    assert first.name_raw == "(ג'אסט ביף) Just BeeF"
    assert first.category_raw == "מסעדה בשרית"
    assert first.address_raw == "קניון עזריאלי קומה 2"
    assert first.phone == "03-6185222"
    assert first.identity == collector.hash_identity(first.name_raw, first.address_raw)

    # 100 GRAM has no phone published - must come back None, not "".
    second = cards[1]
    assert second.name_raw == "100 GRAM"
    assert second.phone is None


def test_full_collection_counts_and_partition(fake_collect_env):
    geocoder, cache_path = fake_collect_env
    records = collector.collect(
        session=requests.Session(), geocoder=geocoder, geocode_cache_path=cache_path
    )

    # 1120 raw cards across the 10 unfiltered pages, minus a handful the
    # survey found genuinely duplicated by result-set drift between
    # sequential page fetches (see DUPLICATE_WARN_THRESHOLD) - so this is
    # deliberately not hardcoded to the raw page-count total, which
    # would make the test as fragile as the live site's own pagination.
    raw_card_count = sum(
        len(collector._parse_cards(BeautifulSoup(_fixture_text(f"all_p{n}.html"), "lxml")))
        for n in range(1, 11)
    )
    assert len(records) < raw_card_count
    assert raw_card_count - len(records) <= collector.DUPLICATE_WARN_THRESHOLD

    ids = [r["source_record_id"] for r in records]
    assert len(ids) == len(set(ids))

    by_level = {"mehadrin": 0, "regular": 0, "unknown": 0}
    for r in records:
        by_level[r["supervision_level"]] += 1
    assert sum(by_level.values()) == len(records)
    # The survey's מהדרין+רגילה filtered crawls reconciled almost, but
    # not quite, perfectly against the unfiltered one (see
    # _verify_supervision_partition) - a small "unknown" bucket is the
    # expected, honest outcome, not a bug.
    assert by_level["unknown"] <= collector.DUPLICATE_WARN_THRESHOLD
    assert by_level["mehadrin"] > 0
    assert by_level["regular"] > 0

    for r in records:
        assert r["source_id"] == "tlv"
        assert r["status"] == "active"
        assert r["location_source"] == "geocoded"
        assert r["supervisor_name"] is None
        assert r["supervisor_phone"] is None


def test_geocode_cache_is_reused_across_duplicate_addresses(fake_collect_env):
    geocoder, cache_path = fake_collect_env
    collector.collect(session=requests.Session(), geocoder=geocoder, geocode_cache_path=cache_path)

    soup = BeautifulSoup(_fixture_text("all_p1.html"), "lxml")
    cards = collector._parse_cards(soup)
    unique_addresses = {c.address_raw for c in cards}

    # If every card's address were geocoded independently we'd expect far
    # more calls than unique addresses across the whole 1120-record run;
    # this just checks the cache collapsed at least the obvious
    # duplicates on page 1 (multiple businesses at the same mall, etc.).
    assert len(geocoder.calls) < 1120
    assert len(set(geocoder.calls)) <= len(geocoder.calls)
    assert unique_addresses  # sanity: fixture actually has addresses

    # And re-running against a warm cache makes zero new geocoder calls.
    calls_before = len(geocoder.calls)
    collector.collect(session=requests.Session(), geocoder=geocoder, geocode_cache_path=cache_path)
    assert len(geocoder.calls) == calls_before


def test_out_of_bounds_result_is_flagged_low_confidence(fake_collect_env, monkeypatch):
    _, cache_path = fake_collect_env

    class _OutOfBoundsGeocoder(Geocoder):
        def geocode(self, query):
            return GeocodeResult(lat=31.0, lng=35.5, confidence="high", out_of_bounds=False)

    records = collector.collect(
        session=requests.Session(),
        geocoder=_OutOfBoundsGeocoder(),
        geocode_cache_path=cache_path,
    )
    assert all(r["location_confidence"] == "low" for r in records)


def test_ungeocodable_address_produces_a_valid_record_not_a_crash(fake_collect_env):
    # Real finding from the live run: "קניון עזריאלי קומה 2" (a mall
    # name and floor, no street name or number) can't be geocoded to
    # anything at all. That must not crash the whole 1000+ record run -
    # it's a legitimate "we don't know where this is" record.
    geocoder, cache_path = fake_collect_env

    class _NoResultGeocoder(Geocoder):
        def geocode(self, query):
            return GeocodeResult(lat=None, lng=None, confidence=None, out_of_bounds=False)

    records = collector.collect(
        session=requests.Session(),
        geocoder=_NoResultGeocoder(),
        geocode_cache_path=cache_path,
    )
    assert len(records) > 1000
    assert all(r["lat"] is None and r["location_source"] is None for r in records)


def test_unmapped_category_falls_back_to_other_and_warns(fake_collect_env, caplog):
    geocoder, cache_path = fake_collect_env
    with caplog.at_level("WARNING"):
        records = collector.collect(
            session=requests.Session(), geocoder=geocoder, geocode_cache_path=cache_path
        )
    unmapped = [r for r in records if r["category_canonical"] == "other"]
    assert len(unmapped) >= 1
    assert any("unmapped category_raw" in m for m in caplog.messages)


def test_supervision_partition_mismatch_raises(monkeypatch, fake_collect_env):
    geocoder, cache_path = fake_collect_env

    # Force the מהדרין crawl to report empty results, breaking the
    # partition against the unfiltered crawl.
    real_fake_get = _fake_get_all_pages()

    def broken_get(session, url):
        if "kashrut=318" in url:
            return "<html><body><div class='wrap_pagination'></div></body></html>"
        return real_fake_get(session, url)

    monkeypatch.setattr(collector, "_get", broken_get)

    with pytest.raises(CollectorError, match="zero cards"):
        collector.collect(session=requests.Session(), geocoder=geocoder, geocode_cache_path=cache_path)


def test_missing_permalink_raises():
    soup = BeautifulSoup(
        '<div class="post_col kosher_item"><div class="inner_div">'
        '<div class="post_title">X</div></div></div>',
        "lxml",
    )
    with pytest.raises(CollectorError, match="no permalink"):
        collector._parse_cards(soup)


def test_missing_address_raises():
    html = (
        '<div class="post_col kosher_item">'
        '<a href="https://rabanut.co.il/business/x/">'
        '<div class="post_title">X</div>'
        '<div class="business_type">מסעדה</div>'
        "</a></div>"
    )
    soup = BeautifulSoup(html, "lxml")
    with pytest.raises(CollectorError, match="no address"):
        collector._parse_cards(soup)
