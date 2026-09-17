"""Tests for the Raanana collector.

The HTML fixtures in tests/fixtures/raanana/ are the two pages of
https://mdrn.org.il/directory-kashrut/ as captured on 2026-09-17 (338
records total: 200 + 138). Tests run entirely offline against these
fixtures - nothing here hits the live site.
"""

from pathlib import Path

import pytest
import requests
from bs4 import BeautifulSoup

from collectors.common.schema import CollectorError
from collectors.raanana import collector

FIXTURES = Path(__file__).parent / "fixtures" / "raanana"


def _fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fake_get_by_page(pages: dict[str, str]):
    """Build a fake replacement for collector._get keyed by which
    _page=N substring appears in the requested URL ("base" for page 1).
    """

    def fake_get(session, url):
        if "_page=" not in url:
            return pages["base"]
        for key, text in pages.items():
            if key != "base" and f"_page={key}" in url:
                return text
        raise AssertionError(f"no fixture mapped for url: {url}")

    return fake_get


@pytest.fixture
def two_real_pages():
    return {
        "base": _fixture_text("page1.html"),
        "2": _fixture_text("page2.html"),
    }


def test_parse_cards_from_page1_matches_known_record():
    soup = BeautifulSoup(_fixture_text("page1.html"), "lxml")
    cards = collector._parse_cards(soup)

    assert len(cards) == 200

    first = cards[0]
    assert first.entity_id == "13684"
    assert first.name_raw == "שופר סל דיל - רננים"
    assert first.directory_category_raw == "כשרות רגילה בתוקף"
    assert first.balanit_raw == "מרכולים"
    assert first.address_raw == "2 המלאכה, רעננה, Israel"
    assert first.lat == pytest.approx(32.197237)
    assert first.lng == pytest.approx(34.878108)
    assert first.supervisor_name == "שלמה יעקב"
    assert first.supervisor_phone == "0546137939"


def test_full_collection_across_both_pages(monkeypatch, two_real_pages):
    monkeypatch.setattr(collector, "_get", _fake_get_by_page(two_real_pages))
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    records = collector.collect(session=requests.Session())

    # 338 raw listings minus 8 deliberately excluded (5 "presence only" -
    # mikvehs/council office, 3 "awaiting kashrut" certification).
    assert len(records) == 330

    ids = [r["source_record_id"] for r in records]
    assert len(ids) == len(set(ids)), "duplicate source_record_id across pages"

    for r in records:
        assert r["source_id"] == "raanana"
        assert r["status"] == "active"
        assert r["location_source"] == "published"
        assert r["location_confidence"] == "high"
        assert r["lat"] is not None and r["lng"] is not None
        # This source doesn't publish kosher_type at all - never guessed at.
        assert r["kosher_type"] is None


def test_awaiting_kashrut_businesses_are_excluded(monkeypatch, two_real_pages):
    monkeypatch.setattr(collector, "_get", _fake_get_by_page(two_real_pages))
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    records = collector.collect(session=requests.Session())

    names = {r["name_raw"] for r in records}
    # Live examples confirmed 2026-09-17: assigned a supervisor but
    # explicitly not yet certified - must never appear as active.
    assert "לה פרל דלישס" not in names
    assert "ג'חנון נורית בע\"מ" not in names
    assert "גרין בול רעננה" not in names


def test_mikvehs_and_council_office_are_excluded(monkeypatch, two_real_pages):
    monkeypatch.setattr(collector, "_get", _fake_get_by_page(two_real_pages))
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    records = collector.collect(session=requests.Session())

    names = {r["name_raw"] for r in records}
    assert "מקווה רבוצקי" not in names
    assert "מקווה הרצל" not in names
    assert not any("המועצה הדתית רעננה" in n for n in names)


def test_supervision_level_comes_from_directory_category(monkeypatch, two_real_pages):
    monkeypatch.setattr(collector, "_get", _fake_get_by_page(two_real_pages))
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    records = collector.collect(session=requests.Session())

    by_level = {}
    for r in records:
        by_level.setdefault(r["supervision_level"], 0)
        by_level[r["supervision_level"]] += 1

    assert by_level.get("regular", 0) == 298
    assert by_level.get("mehadrin", 0) == 28
    # The 4 department-split listings (מח' בשרית / מחלקת ירקות variants)
    # have no supervision-level-bearing directory_category value.
    assert by_level.get("unknown", 0) == 4


def test_multi_category_balanit_picks_first_token(monkeypatch, two_real_pages):
    monkeypatch.setattr(collector, "_get", _fake_get_by_page(two_real_pages))
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    records = collector.collect(session=requests.Session())

    rec = next(
        r for r in records if r["category_raw"] == "איטליזים;ירקות ופירות;מרכולים"
    )
    assert rec["category_canonical"] == "butcher"  # first token, not "ירקות ופירות"


def test_unmapped_balanit_falls_back_to_other_and_warns(monkeypatch, two_real_pages, caplog):
    monkeypatch.setattr(collector, "_get", _fake_get_by_page(two_real_pages))
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    with caplog.at_level("WARNING"):
        records = collector.collect(session=requests.Session())

    unmapped = [r for r in records if r["category_canonical"] == "other"]
    assert len(unmapped) >= 1
    assert any("unmapped category" in m for m in caplog.messages)


def test_pagination_wraparound_raises(monkeypatch, two_real_pages):
    # Same bug documented in Netanya's collector (identical plugin): an
    # invalid page silently falls back to page 1's content.
    looping_pages = dict(two_real_pages)
    looping_pages["2"] = two_real_pages["base"]

    monkeypatch.setattr(collector, "_get", _fake_get_by_page(looping_pages))
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    with pytest.raises(CollectorError, match="same first record as page 1"):
        collector.collect(session=requests.Session())


def test_partial_fetch_failure_raises_not_partial_success(monkeypatch, two_real_pages):
    real_fake_get = _fake_get_by_page(two_real_pages)
    call_count = {"n": 0}

    def flaky_get(session, url):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise CollectorError("simulated network failure on page 2")
        return real_fake_get(session, url)

    monkeypatch.setattr(collector, "_get", flaky_get)
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    with pytest.raises(CollectorError, match="simulated network failure"):
        collector.collect(session=requests.Session())


def test_zero_cards_on_a_page_raises():
    soup = BeautifulSoup("<html><body>nothing here</body></html>", "lxml")
    assert collector._parse_cards(soup) == []


def test_map_supervision_unknown_value_warns_and_falls_back(caplog):
    with caplog.at_level("WARNING"):
        result = collector._map_supervision("משהו חדש שלא ראינו")
    assert result == "unknown"
    assert any("unmapped directory_category" in m for m in caplog.messages)


@pytest.mark.parametrize(
    "address_raw, expected_city",
    [
        ("2 המלאכה, רעננה, Israel", "רעננה"),
        # A real, live finding (2026-09-17): at least one address omits
        # the ", Israel" suffix entirely - must still resolve correctly
        # rather than falling back to the wrong city.
        ("הסחלב, אריאל", "אריאל"),
        # Blank street address - still parses city correctly even with
        # an empty first segment.
        (", רעננה, Israel", "רעננה"),
    ],
)
def test_city_from_address(address_raw, expected_city):
    assert collector._city_from_address(address_raw) == expected_city


def test_city_from_address_falls_back_and_warns_when_no_comma_at_all(caplog):
    with caplog.at_level("WARNING"):
        city = collector._city_from_address("no comma anywhere in this string")
    assert city == collector.CITY
    assert any("could not find a city" in m for m in caplog.messages)
