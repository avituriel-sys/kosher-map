"""Collector for religious-council sites that run the WordPress "drts"
directory plugin (the same plugin behind mdn.org.il, mdrn.org.il, ...).

Netanya's and Raanana's collectors are copies of this idea, each with its
own field meanings hard-coded. About 18 councils use this plugin (see
docs/council-site-survey-2026-09-23.csv), and - the Raanana lesson - each
one configures the plugin's custom fields to mean something different, so
this collector is generic and each council supplies a small config
(collectors/drts/councils.py) saying what its fields mean.

What is generic across councils (verified on Givat Shmuel and Petah Tikva,
2026-09-24): listings are `div[data-content-name="kashrut_dir_ltg"]` cards
with a title link, an address field carrying a Waze link with the published
coordinates (`ll=<lat>,<lng>`), and any number of extra fields identified by
their `data-name`. Pagination is the plugin's `div.drts-pagination`, and an
out-of-range `_page` silently wraps back to page 1 (see Netanya's collector)
- guarded against below.

Deliberately not collected: supervisors' names and phone numbers. They are
private individuals' details and nothing here needs them.

Collector contract (spec section 6.1): no database writes, no judgement
about what changed. It either returns the complete list of canonical records
or raises CollectorError.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from collectors.common.schema import CollectorError, validate_record
from collectors.drts.councils import CouncilConfig

logger = logging.getLogger(__name__)

USER_AGENT = (
    "KosherMapBot/0.1 (public kashrut directory aggregator; "
    "+https://github.com/avituriel-sys/kosher-map; "
    "mailto:avituriel@gmail.com)"
)
REQUEST_TIMEOUT_SECONDS = 30
POLITE_DELAY_SECONDS = 1.5
MAX_PAGES = 50

ISRAEL_BOUNDS = (29.4, 33.4, 34.2, 35.9)  # min_lat, max_lat, min_lng, max_lng
_WAZE_LL_RE = re.compile(r"[?&]ll=([\-0-9.]+)%2C([\-0-9.]+)")
_TRAILING_COUNTRY_RE = re.compile(r",\s*(?:Israel|ישראל)\s*$")
_TRAILING_POSTCODE_RE = re.compile(r",\s*\d{5,7}\s*$")
_BAAL_RE = re.compile(r'\s*בע"מ\s*$')

CARD_SELECTOR = 'div[data-content-name="kashrut_dir_ltg"][data-display-name="summary-custom_list"]'


@dataclass
class _RawCard:
    entity_id: str
    name_raw: str
    source_url: str
    address_text: str | None  # None = the card has no address field at all
    lat: float | None
    lng: float | None
    fields: dict[str, str]  # data-name -> value text (labels stripped)


def collect(config: CouncilConfig, session: requests.Session | None = None) -> list[dict]:
    owns_session = session is None
    session = session or requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    try:
        cards = _fetch_all_cards(session, config)
    finally:
        if owns_session:
            session.close()

    records = []
    skipped_no_address = 0
    for card in cards:
        record = _to_canonical_record(card, config)
        if record is _NO_ADDRESS:
            skipped_no_address += 1
        elif record is not None:
            records.append(record)
    if skipped_no_address:
        logger.warning(
            "%s: skipped %d listing(s) with no address field and no coordinates "
            "(nothing to place on a map)", config.source_id, skipped_no_address,
        )
    for record in records:
        validate_record(record)
    return records


def _fetch_all_cards(session: requests.Session, config: CouncilConfig) -> list[_RawCard]:
    cards: list[_RawCard] = []
    seen_ids: set[str] = set()
    first_id_of_page_one: str | None = None

    page_num = 1
    next_url = config.base_url
    while True:
        if page_num > MAX_PAGES:
            raise CollectorError(
                f"{config.source_id}: exceeded MAX_PAGES={MAX_PAGES} without pagination "
                f"reporting completion - refusing to loop forever"
            )
        soup = BeautifulSoup(_get(session, next_url), "lxml")
        page_cards = _parse_cards(soup)
        if not page_cards:
            raise CollectorError(
                f"{config.source_id}: page {page_num} ({next_url}) returned zero listing "
                f"cards; the site markup likely changed"
            )

        first_id = page_cards[0].entity_id
        if first_id_of_page_one is None:
            first_id_of_page_one = first_id
        elif first_id == first_id_of_page_one:
            raise CollectorError(
                f"{config.source_id}: page {page_num} ({next_url}) returned the same first "
                f"record as page 1 (entity id {first_id}); pagination wrapped around "
                f"instead of ending"
            )
        duplicate_ids = seen_ids & {c.entity_id for c in page_cards}
        if duplicate_ids:
            raise CollectorError(
                f"{config.source_id}: page {page_num} re-returned already-seen entity "
                f"id(s) {duplicate_ids}"
            )
        seen_ids.update(c.entity_id for c in page_cards)
        cards.extend(page_cards)

        next_url = _find_next_page_url(soup)
        if next_url is None:
            break
        page_num += 1
        time.sleep(POLITE_DELAY_SECONDS)
    return cards


def _get(session: requests.Session, url: str) -> str:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise CollectorError(f"failed to fetch {url}: {exc}") from exc
    return response.text


def _find_next_page_url(soup: BeautifulSoup) -> str | None:
    pagination = soup.select_one("div.drts-pagination")
    if pagination is None:
        return None
    links = pagination.find_all("a", recursive=False)
    if not links:
        return None
    next_link = links[-1]
    if "drts-bs-disabled" in (next_link.get("class") or []):
        return None
    href = next_link.get("data-ajax-url") or next_link.get("href")
    if not href or href == "#":
        return None
    return href


def _parse_cards(soup: BeautifulSoup) -> list[_RawCard]:
    cards = []
    for entity in soup.select(CARD_SELECTOR):
        entity_id = entity.get("data-entity-id")
        if entity_id:
            cards.append(_parse_one_card(entity_id, entity))
    return cards


def _parse_one_card(entity_id: str, entity) -> _RawCard:
    title = entity.select_one('div[data-name="entity_field_post_title"] a')
    if title is None:
        raise CollectorError(f"listing {entity_id} has no title/link - markup changed")

    address_field = entity.select_one('div[data-name="entity_field_location_address"]')
    address_text = lat = lng = None
    if address_field is not None:
        value = address_field.select_one("div.drts-entity-field-value")
        address_text = " ".join((value or address_field).get_text(" ", strip=True).split())
        waze = address_field.select_one('a[href*="waze.com"]')
        match = _WAZE_LL_RE.search(waze.get("href", "")) if waze else None
        if match:
            lat, lng = float(match.group(1)), float(match.group(2))
            min_lat, max_lat, min_lng, max_lng = ISRAEL_BOUNDS
            if not (min_lat <= lat <= max_lat and min_lng <= lng <= max_lng):
                lat = lng = None  # e.g. a literal 0,0 placeholder

    fields: dict[str, str] = {}
    for field in entity.select("div[data-name]"):
        name = field["data-name"]
        if name in ("entity_field_post_title", "entity_field_location_address"):
            continue
        value = field.select_one("div.drts-entity-field-value")
        text = " ".join((value or field).get_text(" ", strip=True).split())
        if text:
            fields[name] = text

    return _RawCard(
        entity_id=entity_id,
        name_raw=title.get_text(strip=True),
        source_url=title.get("href", ""),
        address_text=address_text,
        lat=lat,
        lng=lng,
        fields=fields,
    )


_NO_ADDRESS = object()


def _city_and_street(address_text: str, config: CouncilConfig) -> tuple[str, str]:
    """Split "<street>, <city>, Israel" (also "...ישראל", and a missing
    ", Israel") into (city, street). City falls back to the council's own.
    """
    cleaned = _TRAILING_COUNTRY_RE.sub("", address_text).strip()
    cleaned = _TRAILING_POSTCODE_RE.sub("", cleaned).strip()  # "..., גבעת שמואל, 5442116"
    if "," in cleaned:
        street, _, city = cleaned.rpartition(",")
        street, city = street.strip(" ,"), city.strip()
    else:
        street, city = cleaned, ""
    city = config.city_normalize.get(city, city)
    if not city:
        city = config.default_city
    elif re.fullmatch(r"[A-Za-z .'\-]+", city):
        logger.warning(
            "%s: unrecognised Latin city name %r - extend the council's city_normalize",
            config.source_id, city,
        )
    return city, street


def _to_canonical_record(card: _RawCard, config: CouncilConfig):
    if config.is_excluded(card.name_raw, card.fields):
        logger.info("%s: skipping %r - excluded by council config", config.source_id, card.name_raw)
        return None
    supervision = config.supervision(card.fields)

    city, street = (
        _city_and_street(card.address_text, config)
        if card.address_text is not None else (config.default_city, "")
    )
    has_location = card.lat is not None
    if card.address_text is None and not has_location:
        return _NO_ADDRESS
    # A listing with coordinates but no street text keeps the city so the
    # required address_raw is never empty.
    address_raw = card.address_text if street else city

    category, matched = config.category(card.fields, card.name_raw)
    if not matched and config.category_field and card.fields.get(config.category_field):
        logger.warning(
            "%s: unmapped business type %r - extend the keyword lists "
            "(health alert per spec section 5.3)",
            config.source_id, card.fields[config.category_field],
        )

    name_clean = _BAAL_RE.sub("", card.name_raw).strip()
    slug = urllib.parse.unquote(card.source_url.rstrip("/").rsplit("/", 1)[-1])

    return {
        "source_id": config.source_id,
        "source_record_id": slug or card.entity_id,
        "source_url": card.source_url,
        "name_raw": card.name_raw,
        "name_clean": name_clean,
        "branch": None,
        "category_raw": card.fields.get(config.category_field) if config.category_field else None,
        "category_canonical": category,
        "kosher_type": config.kosher_type(card.fields),
        "supervision_level": supervision,
        "certifying_authority": config.authority,
        "additional_hechsher": None,
        "address_raw": address_raw,
        "city": city,
        "lat": card.lat,
        "lng": card.lng,
        "location_source": "published" if has_location else None,
        "location_confidence": "high" if has_location else None,
        "phone": None,
        "supervisor_name": None,
        "supervisor_phone": None,
        "status": "active",
    }


if __name__ == "__main__":
    import json
    import sys

    from collectors.drts.councils import COUNCILS

    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) != 2 or sys.argv[1] not in COUNCILS:
        print(f"usage: python -m collectors.drts.collector <{'|'.join(COUNCILS)}>", file=sys.stderr)
        sys.exit(2)
    try:
        results = collect(COUNCILS[sys.argv[1]])
    except CollectorError as exc:
        print(f"COLLECTOR FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"collected {len(results)} records", file=sys.stderr)
    json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
