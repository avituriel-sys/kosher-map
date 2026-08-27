"""Tests for the Tel Aviv-Yafo revoked-certification feed collector.

Fixture: tests/fixtures/tlv/revoked_p1.html, the live page as captured on
2026-08-27 (28 entries, unpaginated). Offline only - no network calls.
"""

from pathlib import Path

import pytest
import requests

from collectors.common.schema import CollectorError, hash_identity
from collectors.tlv import revoked_collector

FIXTURES = Path(__file__).parent / "fixtures" / "tlv"


def _fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def fake_get(monkeypatch):
    def fake(session, url):
        return _fixture_text("revoked_p1.html")

    monkeypatch.setattr(revoked_collector, "_get", fake)


def test_collects_all_revoked_entries(fake_get):
    records = revoked_collector.collect_revoked(session=requests.Session())

    assert len(records) == 14
    ids = [r["source_record_id"] for r in records]
    assert len(ids) == len(set(ids))

    first = records[0]
    assert first["source_id"] == "tlv"
    assert first["status"] == "revoked"
    assert first["name_raw"] == "אסקייפ פלייס"
    assert first["address_raw"] == "נחלת בנימין 68"
    assert first["source_record_id"] == hash_identity("אסקייפ פלייס", "נחלת בנימין 68")
    # This feed genuinely doesn't publish category or coordinates - both
    # must come back None rather than a fabricated value.
    assert first["category_raw"] is None
    assert first["category_canonical"] is None
    assert first["lat"] is None
    assert first["location_source"] is None


def test_revoked_records_pass_schema_validation_despite_sparse_fields(fake_get):
    # collect_revoked already runs validate_record internally; this test
    # just documents why that doesn't raise even though category/location
    # are missing - see schema.py's REQUIRED_UNLESS_REVOKED.
    records = revoked_collector.collect_revoked(session=requests.Session())
    assert len(records) == 14


def test_missing_name_or_address_raises():
    # A card missing wrap_address entirely should fail loudly rather
    # than silently produce a record with an empty address.
    html = (
        '<div class="post_col" data-type="is-not-kosher">'
        '<div class="post_title">עסק ללא כתובת</div>'
        "</div>"
    )
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    with pytest.raises(CollectorError, match="missing a name or address"):
        revoked_collector._parse_cards(soup)
