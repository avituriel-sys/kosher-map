"""Tests for the national m-datit portal collector.

The JSON fixtures in tests/fixtures/mdatit/ are real responses captured
from the portal on 2026-09-23 (Tel Mond in full, the first rows of Rishon
LeZion, Kfar Saba and Ashdod), with supervisors' names/phones stripped.
Everything runs offline - nothing here hits the live portal.
"""

import json
from pathlib import Path

import pytest
import requests

from collectors.common.geocoding import GeocodeResult, Geocoder
from collectors.common.schema import CollectorError
from collectors.mdatit import collector
from collectors.mdatit.mappings import map_category

FIXTURES = Path(__file__).parent / "fixtures" / "mdatit"

TEL_MOND, RISHON, KFAR_SABA, ASHDOD = 80, 5, 16, 7


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, sites_by_authority, authorities=None):
        self.sites = sites_by_authority
        self.authorities = authorities or _load("authorities.json")
        self.posts = []

    def get(self, url, timeout=None):
        assert url.endswith("/Data/Site/GetAuthoritiesAndCitiesForPirsum")
        return _FakeResponse(self.authorities)

    def post(self, url, json=None, timeout=None):
        assert url.endswith("/Data/Site/SearchPirsumSite")
        self.posts.append(json)
        return _FakeResponse(self.sites[json["AuthorityID"]])

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)


@pytest.fixture
def session():
    return _FakeSession({
        TEL_MOND: _load("sites_80.json"),
        RISHON: _load("sites_5.json"),
        KFAR_SABA: _load("sites_16.json"),
        ASHDOD: _load("sites_7.json"),
    })


def test_tel_mond_certified_records(session):
    records = collector.collect([TEL_MOND], session=session, geocode=False)

    assert len(records) == 27
    first = records[0]
    assert first["source_id"] == "mdatit-80"
    assert first["name_raw"] == "אדם תל מונד"
    assert first["address_raw"] == "הדקל 83"
    assert first["city"] == "תל מונד"
    assert first["certifying_authority"] == "מועצה דתית תל מונד"
    assert first["kosher_type"] == ["meat"]
    assert first["supervision_level"] == "regular"
    # Uncertified listings stay in with unknown supervision.
    uncertified = [r for r in records if r["supervision_level"] == "unknown"]
    assert len(uncertified) == 6
    assert all(r["kosher_type"] is None for r in uncertified)


def test_records_never_carry_supervisor_personal_details(session):
    records = collector.collect([TEL_MOND, KFAR_SABA], session=session, geocode=False)
    assert all(r["supervisor_name"] is None and r["supervisor_phone"] is None for r in records)
    assert all(r["phone"] is None for r in records)


def test_council_without_certificates_still_lists_businesses(session):
    records = collector.collect([RISHON], session=session, geocode=False)

    assert len(records) == 40
    assert {r["supervision_level"] for r in records} == {"unknown"}
    assert {r["kosher_type"] for r in records} == {None}
    # Rishon names carry their type as a prefix, used when the description is blank.
    halls = [r for r in records if r["name_raw"].startswith("אולם")]
    assert halls and all(r["category_canonical"] == "event_hall" for r in halls)


def test_each_authority_gets_its_own_source_id(session):
    records = collector.collect([TEL_MOND, KFAR_SABA], session=session, geocode=False)
    assert {r["source_id"] for r in records} == {"mdatit-80", "mdatit-16"}
    assert [p["AuthorityID"] for p in session.posts] == [TEL_MOND, KFAR_SABA]


def test_kosher_type_codes_and_levels_decode(session):
    records = collector.collect([KFAR_SABA], session=session, geocode=False)
    by_name = {r["name_raw"]: r for r in records}
    assert by_name["לה פסטה דלה קאזה"]["kosher_type"] == ["dairy", "meat", "parve"]  # "1,2,3"
    assert by_name["אוקי דאקי בע\"מ"]["kosher_type"] == ["parve"]  # "3"
    assert by_name["אוקי דאקי בע\"מ"]["name_clean"] == "אוקי דאקי"


def test_blank_city_falls_back_to_authority_city(session):
    records = collector.collect([ASHDOD], session=session, geocode=False)
    assert "" not in {r["city"] for r in records}
    assert "אשדוד" in {r["city"] for r in records}


def test_listing_without_address_is_skipped(session):
    sites = _load("sites_80.json")
    sites["RetVal"][0]["SiteAddress"] = "   "
    session.sites[TEL_MOND] = sites
    records = collector.collect([TEL_MOND], session=session, geocode=False)
    assert len(records) == 26


def test_multiple_certificates_take_strictest_level_and_union_of_types():
    facts = collector._certificate_facts([
        {"KosherLevel": {"Name": "רגיל"}, "KosherType": {"Name": "2"}},
        {"KosherLevel": {"Name": "מהדרין"}, "KosherType": {"Name": "1"}},
    ])
    assert facts == (["dairy", "meat"], "mehadrin")


def test_geocoding_populates_location_when_enabled(session, tmp_path):
    class _FixedGeocoder(Geocoder):
        def geocode(self, query):
            return GeocodeResult(lat=32.25, lng=34.92, confidence="high", out_of_bounds=False)

    records = collector.collect(
        [TEL_MOND], session=session, geocoder=_FixedGeocoder(),
        geocode_cache_path=tmp_path / "cache.json",
    )
    assert all(r["lat"] == 32.25 and r["location_source"] == "geocoded" for r in records)
    assert all(r["location_confidence"] == "high" for r in records)


def test_geocode_disabled_leaves_no_coordinates(session):
    records = collector.collect([TEL_MOND], session=session, geocode=False)
    assert all(r["lat"] is None and r["location_source"] is None for r in records)


def test_unknown_authority_id_fails_loudly(session):
    with pytest.raises(CollectorError, match="not in the portal's authority list"):
        collector.collect([99999], session=session, geocode=False)


def test_empty_authority_is_not_treated_as_a_complete_census(session):
    session.sites[TEL_MOND] = {"RetVal": []}
    with pytest.raises(CollectorError, match="zero businesses"):
        collector.collect([TEL_MOND], session=session, geocode=False)


def test_unexpected_response_shape_fails_loudly(session):
    session.sites[TEL_MOND] = {"Oops": []}
    with pytest.raises(CollectorError, match="unexpected response shape"):
        collector.collect([TEL_MOND], session=session, geocode=False)


def test_duplicate_site_ids_fail_loudly(session):
    sites = _load("sites_80.json")
    sites["RetVal"][1]["ID"] = sites["RetVal"][0]["ID"]
    session.sites[TEL_MOND] = sites
    with pytest.raises(CollectorError, match="duplicate site ids"):
        collector.collect([TEL_MOND], session=session, geocode=False)


def test_http_failure_fails_loudly(session):
    session.get = lambda url, timeout=None: _FakeResponse({}, status=500)
    with pytest.raises(CollectorError, match="failed to fetch"):
        collector.collect([TEL_MOND], session=session, geocode=False)


def test_no_authorities_requested_fails_loudly(session):
    with pytest.raises(CollectorError):
        collector.collect([], session=session, geocode=False)


@pytest.mark.parametrize(
    "description, name, expected",
    [
        ("מסעדה בשרית, שווארמה, שניצל", "x", "restaurant"),
        ("מסעדת סושי", "x", "sushi_asian"),
        ("מפעל מוצרי מאפה", "x", "factory"),          # factory beats bakery
        ("בית מאפה עובד 24/6", "x", "bakery_patisserie"),
        ("פיצוחים ותבלינים", "x", "nuts_dried_fruit"),  # must not match pizza
        ("פיצריה", "x", "pizzeria"),
        ("איטליז", "x", "butcher"),                    # yod spelling variant
        ("איטליזים", "x", "butcher"),
        ("מזנונים", "x", "restaurant"),
        ("חנות ירקות", "x", "greengrocer"),
        ("אולם אירועים", "x", "event_hall"),
        ("יצור קינוחים קפואים", "x", "factory"),
        ("", "אולם - אדל", "event_hall"),               # blank description: name prefix
        ("", "קונדיטוריה - רוני ובניו", "bakery_patisserie"),
        ("", "שם שאינו אומר כלום", "other"),
        ("סוג לא מוכר לחלוטין", "x", "other"),
    ],
)
def test_map_category(description, name, expected):
    assert map_category(description, name)[0] == expected


def test_unmatched_description_is_reported_as_unmatched():
    assert map_category("סוג לא מוכר לחלוטין", "x") == ("other", False)
    # A blank description with an unrecognisable name is expected, not a warning case.
    assert map_category("", "שם שאינו אומר כלום") == ("other", False)
