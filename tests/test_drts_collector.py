"""Tests for the generic drts-plugin collector and its Givat Shmuel and
Petah Tikva configs.

The HTML fixtures in tests/fixtures/drts/ are real cards from
https://mdgs.org.il/directory-kashrut/ and https://mpt.org.il/directory-kashrut/
as captured on 2026-09-24 (a hand-picked subset covering the edge cases),
with supervisors' names and phones removed. Everything runs offline.
"""

from pathlib import Path

import pytest
import requests
from bs4 import BeautifulSoup

from collectors.common.schema import CollectorError
from collectors.drts import collector
from collectors.drts.councils import GIVAT_SHMUEL, PETAH_TIKVA

FIXTURES = Path(__file__).parent / "fixtures" / "drts"


def _fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)


@pytest.fixture
def gs_records(monkeypatch):
    monkeypatch.setattr(collector, "_get", lambda session, url: _fixture("gs_page.html"))
    return {r["name_raw"]: r for r in collector.collect(GIVAT_SHMUEL)}


@pytest.fixture
def pt_records(monkeypatch):
    def fake_get(session, url):
        return _fixture("pt_page2.html" if "_page=2" in url else "pt_page1.html")

    monkeypatch.setattr(collector, "_get", fake_get)
    return collector.collect(PETAH_TIKVA)


# ------------------------------------------------------------- Givat Shmuel

def test_gs_full_record(gs_records):
    assert len(gs_records) == 8
    r = gs_records['"אלמה" מרקט']
    assert r["source_id"] == "givat_shmuel"
    assert r["certifying_authority"] == "המועצה הדתית גבעת שמואל"
    assert r["address_raw"] == "האורנים 1, גבעת שמואל, ישראל"
    assert r["city"] == "גבעת שמואל"
    assert r["category_canonical"] == "grocery_supermarket"
    assert set(r["kosher_type"]) == {"meat", "dairy", "parve"}
    assert r["supervision_level"] == "regular"
    assert r["lat"] == pytest.approx(32.068178)
    assert r["lng"] == pytest.approx(34.845651)
    assert r["location_source"] == "published" and r["location_confidence"] == "high"


def test_gs_supervisors_and_phone_are_never_collected(gs_records):
    assert all(r["supervisor_name"] is None and r["supervisor_phone"] is None for r in gs_records.values())
    assert all(r["phone"] is None for r in gs_records.values())


def test_gs_kosher_type_understands_notes(gs_records):
    # "חלבי - כל המוצרים בבית העסק חלביים כולל פיצה טבעוני." is still just dairy
    assert gs_records["הגבעה האיטלקית"]["kosher_type"] == ["dairy"]


def test_gs_mehadrin_prefix_with_a_note_still_maps(gs_records):
    levels = {r["supervision_level"] for r in gs_records.values()}
    assert levels <= {"regular", "mehadrin"}
    assert "mehadrin" in levels


def test_gs_postal_code_is_not_mistaken_for_the_city(gs_records):
    r = gs_records["פיצה סטורי"]
    assert r["city"] == "גבעת שמואל"
    assert "5442116" in r["address_raw"]


def test_gs_listing_without_usable_coordinates_is_kept_but_unlocated(gs_records):
    r = gs_records["דודה קפה"]
    assert r["lat"] is None and r["lng"] is None
    assert r["location_source"] is None and r["location_confidence"] is None


# -------------------------------------------------------------- Petah Tikva

def test_pt_excludes_pending_and_personal_reports(pt_records):
    names = [r["name_raw"] for r in pt_records]
    assert 'ד"ר שווארמה אבו גוש' not in names          # awaiting certification
    assert "דוח אישי - אחיה וינברג" not in names        # a person, not a business
    assert 'אהרון כץ - דו"ח אישי' not in names
    assert len(pt_records) == 11


def test_pt_status_maps_to_supervision(pt_records):
    by_name = {r["name_raw"]: r for r in pt_records}
    assert by_name["CARREFOUR"]["supervision_level"] == "regular"
    assert by_name["COFFEE FRESH"]["supervision_level"] == "mehadrin"
    assert by_name["אליהו דגים"]["supervision_level"] == "unknown"  # "הידור הכשרות" - meaning unconfirmed


def test_pt_has_no_kosher_type_or_business_type_source(pt_records):
    assert all(r["kosher_type"] is None for r in pt_records)
    assert all(r["category_raw"] is None for r in pt_records)
    by_name = {r["name_raw"]: r for r in pt_records}
    assert by_name["אטליז קוזי"]["category_canonical"] in ("other", "butcher")


def test_pt_reads_published_coordinates_from_the_waze_link(pt_records):
    carrefour = next(r for r in pt_records if r["name_raw"] == "CARREFOUR")
    assert carrefour["lat"] == pytest.approx(32.088359)
    assert carrefour["lng"] == pytest.approx(34.868757)
    assert carrefour["location_source"] == "published"


def test_pt_city_comes_from_the_address_not_the_council(pt_records):
    by_name = {r["name_raw"]: r for r in pt_records}
    assert by_name["איליין טכנולוגיות - בשרי"]["city"] == "קרית אונו"
    assert by_name["אושי אושי"]["city"] == "פתח תקווה"  # "Petah Tikva" spelled in Latin


def test_pt_listing_without_a_street_keeps_its_city_as_the_address(pt_records):
    r = next(r for r in pt_records if r["name_raw"] == "גן סיפור פתח תקוה")
    assert r["address_raw"] == r["city"] == "פתח תקווה"


def test_pt_two_pages_are_followed_and_slugs_are_unique(pt_records):
    ids = [r["source_record_id"] for r in pt_records]
    assert len(ids) == len(set(ids))


def test_listing_with_no_address_field_and_no_coordinates_is_skipped(monkeypatch):
    html = _fixture("gs_page.html")
    soup = BeautifulSoup(html, "lxml")
    soup.select_one('div[data-name="entity_field_location_address"]').decompose()
    monkeypatch.setattr(collector, "_get", lambda session, url: str(soup))
    records = collector.collect(GIVAT_SHMUEL)
    assert len(records) == 7


# ------------------------------------------------------------------ safety

def test_zero_cards_fails_loudly(monkeypatch):
    monkeypatch.setattr(collector, "_get", lambda session, url: "<html><body></body></html>")
    with pytest.raises(CollectorError, match="zero listing cards"):
        collector.collect(GIVAT_SHMUEL)


def test_pagination_that_wraps_back_to_page_one_fails_loudly(monkeypatch):
    monkeypatch.setattr(collector, "_get", lambda session, url: _fixture("pt_page1.html"))
    with pytest.raises(CollectorError, match="same first record as page 1"):
        collector.collect(PETAH_TIKVA)


def test_http_failure_fails_loudly():
    class _Boom:
        def get(self, url, timeout=None):
            raise requests.ConnectionError("down")

    with pytest.raises(CollectorError, match="failed to fetch"):
        collector._get(_Boom(), "https://example.test/")


@pytest.mark.parametrize(
    "address, city, street",
    [
        ("13 קפלן, פתח תקווה, Israel", "פתח תקווה", "13 קפלן"),
        ("1 יצחק רבין, קרית אונו, Israel", "קרית אונו", "1 יצחק רבין"),
        ("72 ז׳בוטינסקי, Petah Tikva, Israel", "פתח תקווה", "72 ז׳בוטינסקי"),
    ],
)
def test_city_and_street_split(address, city, street):
    got_city, got_street = collector._city_and_street(address, PETAH_TIKVA)
    assert (got_city, got_street) == (city, street)


def test_blank_street_and_postcode_variants():
    assert collector._city_and_street(", פתח תקווה, Israel", PETAH_TIKVA) == ("פתח תקווה", "")
    assert collector._city_and_street("שדרות בגין 38, גבעת שמואל, 5442116", GIVAT_SHMUEL) == ("גבעת שמואל", "שדרות בגין 38")
