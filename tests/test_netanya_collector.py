"""Tests for the Netanya collector.

The HTML fixtures in tests/fixtures/netanya/ are the four pages of
https://mdn.org.il/directory-kashrut/ as captured on 2026-08-11 (605
records total: 200 + 200 + 200 + 5). Tests run entirely offline against
these fixtures - nothing here hits the live site.
"""

from pathlib import Path

import pytest
import requests
from bs4 import BeautifulSoup

from collectors.common.schema import CollectorError
from collectors.netanya import collector

FIXTURES = Path(__file__).parent / "fixtures" / "netanya"


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
def four_real_pages():
    return {
        "base": _fixture_text("page1.html"),
        "2": _fixture_text("page2.html"),
        "3": _fixture_text("page3.html"),
        "4": _fixture_text("page4.html"),
    }


def test_parse_cards_from_page1_matches_known_record():
    soup = BeautifulSoup(_fixture_text("page1.html"), "lxml")
    cards = collector._parse_cards(soup)

    assert len(cards) == 200

    first = cards[0]
    assert first.entity_id == "13758"
    assert first.name_raw == "גלאט אקספרס"
    assert first.category_raw == "קייטרינג"
    assert first.address_raw == "7 סמילנסקי, נתניה, Israel"
    assert first.lat == pytest.approx(32.327601)
    assert first.lng == pytest.approx(34.855627)
    assert first.supervisor_name == "יוסף נואמה"
    assert first.supervisor_phone == "054-8460693"
    assert first.kosher_type_raw == "בשרי"
    assert first.supervision_raw == "רגילה"


def test_full_collection_across_all_four_pages(monkeypatch, four_real_pages):
    monkeypatch.setattr(collector, "_get", _fake_get_by_page(four_real_pages))
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    records = collector.collect(session=requests.Session())

    assert len(records) == 605

    ids = [r["source_record_id"] for r in records]
    assert len(ids) == len(set(ids)), "duplicate source_record_id across pages"

    for r in records:
        assert r["source_id"] == "netanya"
        assert r["status"] == "active"
        assert r["location_source"] == "published"
        assert r["location_confidence"] == "high"
        assert r["lat"] is not None and r["lng"] is not None


def test_known_branch_name_is_split(monkeypatch, four_real_pages):
    monkeypatch.setattr(collector, "_get", _fake_get_by_page(four_real_pages))
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    records = collector.collect(session=requests.Session())

    rec = next(
        r for r in records if r["name_raw"] == "בית רצון / שער הגיא"
    )
    assert rec["name_clean"] == "בית רצון"
    assert rec["branch"] == "שער הגיא"


def test_dirty_kosher_type_without_comma_is_flagged_not_guessed(monkeypatch, four_real_pages, caplog):
    monkeypatch.setattr(collector, "_get", _fake_get_by_page(four_real_pages))
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    with caplog.at_level("WARNING"):
        records = collector.collect(session=requests.Session())

    dirty = [r for r in records if r["kosher_type"] is None]
    assert len(dirty) >= 1
    assert any("unrecognised kosher_type_raw" in m for m in caplog.messages)


def test_unmapped_categories_fall_back_to_other_and_warn(monkeypatch, four_real_pages, caplog):
    monkeypatch.setattr(collector, "_get", _fake_get_by_page(four_real_pages))
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    with caplog.at_level("WARNING"):
        records = collector.collect(session=requests.Session())

    unmapped = [r for r in records if r["category_canonical"] == "other"]
    assert len(unmapped) >= 1
    assert any("unmapped category_raw" in m for m in caplog.messages)


def test_pagination_wraparound_raises(monkeypatch, four_real_pages):
    # Simulate the real bug found on the live site: an invalid page
    # silently falls back to page 1's content instead of erroring.
    looping_pages = dict(four_real_pages)
    looping_pages["2"] = four_real_pages["base"]

    monkeypatch.setattr(collector, "_get", _fake_get_by_page(looping_pages))
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    with pytest.raises(CollectorError, match="same first record as page 1"):
        collector.collect(session=requests.Session())


def test_partial_fetch_failure_raises_not_partial_success(monkeypatch, four_real_pages):
    real_fake_get = _fake_get_by_page(four_real_pages)
    call_count = {"n": 0}

    def flaky_get(session, url):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise CollectorError("simulated network failure on page 3")
        return real_fake_get(session, url)

    monkeypatch.setattr(collector, "_get", flaky_get)
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)

    with pytest.raises(CollectorError, match="simulated network failure"):
        collector.collect(session=requests.Session())


def test_zero_cards_on_a_page_raises():
    soup = BeautifulSoup("<html><body>nothing here</body></html>", "lxml")
    assert collector._parse_cards(soup) == []


@pytest.mark.parametrize(
    "name_raw, expected_clean, expected_branch",
    [
        ("טאג'ין", "טאג'ין", None),
        ("בית רצון / שער הגיא", "בית רצון", "שער הגיא"),
        ('פיצה שופרסל בע"מ', "פיצה שופרסל", None),
    ],
)
def test_clean_name(name_raw, expected_clean, expected_branch):
    clean, branch = collector._clean_name(name_raw)
    assert clean == expected_clean
    assert branch == expected_branch


def test_map_supervision_unknown_value_warns_and_falls_back(caplog):
    with caplog.at_level("WARNING"):
        result = collector._map_supervision("משהו חדש שלא ראינו")
    assert result == "unknown"
    assert any("unmapped supervision_level" in m for m in caplog.messages)


@pytest.mark.parametrize(
    "address_raw, expected_city",
    [
        ("7 סמילנסקי, נתניה, Israel", "נתניה"),
        # A real, live finding (2026-08-29): the council also certifies
        # some businesses in neighbouring Kfar Yona - these must not be
        # mislabeled as Netanya just because the source is Netanya's.
        ("10 וייצמן, כפר יונה, Israel", "כפר יונה"),
        # A handful of live records publish the city in English rather
        # than Hebrew - normalizes to match everything else.
        ("17 Sderot Giborei Israel, Netanya, Israel", "נתניה"),
        # Blank street address (spec section 6.2) - still parses city
        # correctly even with an empty first segment.
        (", נתניה, Israel", "נתניה"),
    ],
)
def test_city_from_address(address_raw, expected_city):
    assert collector._city_from_address(address_raw) == expected_city


def test_city_from_address_falls_back_and_warns_on_unrecognized_format(caplog):
    with caplog.at_level("WARNING"):
        city = collector._city_from_address("some address with no country suffix")
    assert city == collector.CITY
    assert any("could not find a city" in m for m in caplog.messages)
