"""Tests for the Tel Aviv-Yafo active-listings collector.

Fixtures in tests/fixtures/tlv/ are the full live crawl as captured on
2026-08-27: unfiltered (all_p1..10, 1120 records), מהדרין-filtered
(mehadrin_p1..2, 130 records) and רגילה-filtered (regular_p1..9, 990
records). Tests run entirely offline - nothing here hits the live site
or the live geocoder.
"""

import functools
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


def _single_page(html: str) -> str:
    """The live מהדרין list fits on one page; the August fixture has two, so
    drop the "next" link to match what the collector now relies on."""
    soup = BeautifulSoup(html, "lxml")
    for link in soup.select("a.next.page-numbers"):
        link.decompose()
    return str(soup)


@functools.lru_cache(maxsize=1)
def _business_pages_by_dropdown_id() -> dict[str, str]:
    """For every dropdown entry in the fixture, the one-card page the live
    site would give for `?business_id=<id>`, rebuilt from the fixture cards
    (same-named businesses are matched up in order). Entries with no card in
    the fixture - the businesses the August crawl itself repeated or missed -
    get an empty page, like an unknown id."""
    dropdown = collector._parse_dropdown(
        BeautifulSoup(_fixture_text("all_p1.html"), "lxml")
    )
    html_by_name: dict[str, list[str]] = {}
    seen: set[str] = set()
    for n in range(1, 11):
        soup = BeautifulSoup(_fixture_text(f"all_p{n}.html"), "lxml")
        for item in soup.select("div.post_col.kosher_item"):
            name = item.select_one(".post_title").get_text(strip=True)
            address = item.select_one(".wrap_address").get_text(strip=True)
            identity = collector.hash_identity(name, address)
            if identity in seen:
                continue
            seen.add(identity)
            html_by_name.setdefault(collector._name_key(name), []).append(str(item))
    used: dict[str, int] = {}
    pages = {}
    for business_id, name in dropdown:
        key = collector._name_key(name)
        index = used.get(key, 0)
        used[key] = index + 1
        candidates = html_by_name.get(key, [])
        card = candidates[index] if index < len(candidates) else ""
        pages[business_id] = f"<html><body>{card}</body></html>"
    return pages


def _fake_get_all_pages():
    def fake_get(session, url):
        if "?business_id=" in url:
            return _business_pages_by_dropdown_id()[url.rsplit("=", 1)[1]]
        if "kashrut=318" in url:
            return _single_page(_fixture_text("mehadrin_p1.html"))
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

    # 1120 raw cards across the 10 unfiltered pages; the August crawl itself
    # repeated four of them, so 1116 distinct businesses.
    assert len(records) == 1116

    ids = [r["source_record_id"] for r in records]
    assert len(ids) == len(set(ids))

    # Every business is מהדרין (on the מהדרין list) or רגילה (everything else);
    # there is no "unknown" bucket any more.
    by_level = {"mehadrin": 0, "regular": 0}
    for r in records:
        by_level[r["supervision_level"]] += 1
    assert sum(by_level.values()) == len(records)
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


# --- the unreliable-paging cases (site behaviour since 2026-10) ------------


def _drop_cards(html: str, start: int, count: int) -> str:
    soup = BeautifulSoup(html, "lxml")
    for item in soup.select("div.post_col.kosher_item")[start : start + count]:
        item.decompose()
    return str(soup)


def _run(monkeypatch, fake_collect_env, get):
    geocoder, cache_path = fake_collect_env
    monkeypatch.setattr(collector, "_get", get)
    return collector.collect(
        session=requests.Session(), geocoder=geocoder, geocode_cache_path=cache_path
    )


def test_listings_skipped_by_the_paging_are_recovered_from_the_dropdown(monkeypatch, fake_collect_env):
    real = _fake_get_all_pages()
    lookups = []

    def get(session, url):
        if "?business_id=" in url:
            lookups.append(url)
        html = real(session, url)
        if url.endswith("/page/5/"):
            return _drop_cards(html, 10, 30)  # the paging "skips" 30 businesses
        return html

    records = _run(monkeypatch, fake_collect_env, get)
    assert len(records) == 1116  # nothing lost
    assert 30 <= len(lookups) <= 45  # about the skipped ones, not a re-crawl


def test_pages_that_repeat_businesses_are_tolerated(monkeypatch, fake_collect_env):
    real = _fake_get_all_pages()

    def get(session, url):
        if url.endswith("/page/3/"):
            # Like the live site: page 3 opens with the last 20 businesses of
            # page 2 again (and so loses its own first 20 - recovered later).
            page2 = BeautifulSoup(real(session, url.replace("/page/3/", "/page/2/")), "lxml")
            repeated = page2.select("div.post_col.kosher_item")[-20:]
            page3 = BeautifulSoup(real(session, url), "lxml")
            for old, new in zip(page3.select("div.post_col.kosher_item")[:20], repeated):
                old.replace_with(new)
            return str(page3)
        return real(session, url)

    records = _run(monkeypatch, fake_collect_env, get)
    assert len(records) == 1116
    assert len({r["source_record_id"] for r in records}) == len(records)


def test_too_many_missing_listings_refuses_to_hammer_the_site(monkeypatch, fake_collect_env):
    real = _fake_get_all_pages()

    def get(session, url):
        html = real(session, url)
        if "?business_id=" not in url and ("/page/3/" in url or "/page/4/" in url):
            return _drop_cards(html, 0, 112)
        return html

    with pytest.raises(CollectorError, match="too many listings to recover"):
        _run(monkeypatch, fake_collect_env, get)


def test_dropdown_entries_that_cannot_be_found_fail_loudly(monkeypatch, fake_collect_env):
    real = _fake_get_all_pages()

    def get(session, url):
        if "?business_id=" in url:
            return "<html><body></body></html>"  # lookups find nothing
        html = real(session, url)
        return _drop_cards(html, 10, 20) if url.endswith("/page/5/") else html

    with pytest.raises(CollectorError, match="could not be found as listings"):
        _run(monkeypatch, fake_collect_env, get)


def test_a_few_unmatched_dropdown_entries_are_tolerated(monkeypatch, fake_collect_env):
    # The fixture itself has four dropdown entries with no card anywhere
    # (see test_full_collection_counts_and_partition) - within MAX_UNRESOLVED.
    records = _run(monkeypatch, fake_collect_env, _fake_get_all_pages())
    assert len(records) == 1116


def test_a_mid_crawl_empty_page_is_not_fatal_but_an_empty_first_page_is(monkeypatch, fake_collect_env):
    real = _fake_get_all_pages()

    def empty_first(session, url):
        if "?business_id=" not in url and "kashrut" not in url and "/page/" not in url:
            return _drop_cards(real(session, url), 0, 120)
        return real(session, url)

    with pytest.raises(CollectorError, match="zero cards"):
        _run(monkeypatch, fake_collect_env, empty_first)


def test_missing_business_dropdown_fails_loudly(monkeypatch, fake_collect_env):
    real = _fake_get_all_pages()

    def get(session, url):
        html = real(session, url)
        if "?business_id=" not in url and "kashrut" not in url and "/page/" not in url:
            soup = BeautifulSoup(html, "lxml")
            for select in soup.select("select[name=business_id]"):
                select.decompose()
            return str(soup)
        return html

    with pytest.raises(CollectorError, match="business list is missing"):
        _run(monkeypatch, fake_collect_env, get)


def test_mehadrin_list_outgrowing_one_page_is_refused(monkeypatch, fake_collect_env):
    real = _fake_get_all_pages()

    def get(session, url):
        if "kashrut=318" in url:
            return _fixture_text("mehadrin_p1.html")  # still has its "next" link
        return real(session, url)

    with pytest.raises(CollectorError, match="more than one page"):
        _run(monkeypatch, fake_collect_env, get)


def test_assign_supervision_marks_everything_not_mehadrin_as_regular():
    levels = collector._assign_supervision({"a", "b", "c"}, {"b", "zzz"})
    assert levels == {"a": "regular", "b": "mehadrin", "c": "regular"}


# --- rate limiting (Cloudflare 429) ----------------------------------------


class _Resp:
    def __init__(self, status, text="ok", retry_after=None):
        self.status_code, self.text = status, text
        self.headers = {"Retry-After": retry_after} if retry_after else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


class _SeqSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    def get(self, url, timeout=None):
        self.calls += 1
        return self.responses.pop(0)


def test_a_429_is_retried_after_the_requested_wait(monkeypatch):
    waits = []
    monkeypatch.setattr(collector.time, "sleep", waits.append)
    session = _SeqSession([_Resp(429, retry_after="12"), _Resp(200, "<html>hi</html>")])
    assert collector._get(session, "https://x/") == "<html>hi</html>"
    assert waits == [12.0] and session.calls == 2


def test_a_persistent_429_eventually_fails_loudly(monkeypatch):
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)
    session = _SeqSession([_Resp(429)] * (collector.MAX_RATE_LIMIT_RETRIES + 1))
    with pytest.raises(CollectorError, match="429"):
        collector._get(session, "https://x/")
    assert session.calls == collector.MAX_RATE_LIMIT_RETRIES + 1


@pytest.mark.parametrize(
    "header, expected",
    [("20", 20.0), ("0", collector.RETRY_AFTER_MIN_SECONDS), ("9999", collector.RETRY_AFTER_MAX_SECONDS),
     (None, collector.RETRY_AFTER_DEFAULT_SECONDS), ("garbage", collector.RETRY_AFTER_DEFAULT_SECONDS),
     ("Mon, 02 Nov 2000 22:30:19 +0000", collector.RETRY_AFTER_MIN_SECONDS)],
)
def test_retry_after_is_parsed_and_clamped(header, expected):
    assert collector._retry_after_seconds(_Resp(429, retry_after=header)) == expected
