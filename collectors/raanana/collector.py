"""Collector for the Raanana Religious Council kashrut directory.

Source: https://mdrn.org.il/directory-kashrut/
Spec: kosher-map-technical-spec-v0.1.md section 6.2 pattern (source A style).

Site survey (2026-09-17) found this source runs the exact same
WordPress directory plugin as Netanya's (mdn.org.il) - identical
`kashrut_dir_ltg` markup, identical pagination control, identical
Waze-coordinate-link pattern in the address field - but the council
configured the plugin's custom fields to mean different things:

- The field that *looks* like "category" (`entity_field_directory_
  category`) actually carries a supervision-status label
  ("כשרות רגילה בתוקף" / "כשר למהדרין בתוקף" = regular/mehadrin, "in
  effect") - this is what drives supervision_level here, not
  category_canonical. Two of its values mean "exclude this record
  entirely" rather than any supervision level at all - see
  EXCLUDED_DIRECTORY_CATEGORIES in mappings.py for why.
- The field named `entity_field_field_balanit_name` (a leftover name
  from the plugin template, unrelated to its actual content here) is
  what actually carries business type (מרכולים, מסעדות, בתי מאפה, ...) -
  this drives category_canonical instead.
- There is no separate kosher_type (meat/dairy/parve) or clean
  supervision-level field at all, unlike Netanya. kosher_type is left
  None for every record from this source rather than guessed at from
  category text (e.g. "מסעדות ---חלבי---פרווה" does encode it for some
  restaurants, but not consistently enough across categories to trust).

A handful of balanit_name values are semicolon-separated combinations
(e.g. "איטליזים;ירקות ופירות;מרכולים") - per the 2026-09-17 decision,
the first token is taken as the primary category_canonical.

Collector contract (spec section 6.1): this module performs no database
writes and makes no judgement about what changed since the last run. It
either returns a complete list of canonical business records, or raises
CollectorError. It never returns a partial list.
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
from collectors.raanana.mappings import (
    CATEGORY_MAP,
    EXCLUDED_DIRECTORY_CATEGORIES,
    SUPERVISION_MAP,
)

logger = logging.getLogger(__name__)

SOURCE_ID = "raanana"
CERTIFYING_AUTHORITY = "המועצה הדתית רעננה"
CITY = "רעננה"
BASE_URL = "https://mdrn.org.il/directory-kashrut/"

# Same idea as Netanya's collector: most published address_raw values
# end "..., <city>, Israel", and the council's directory isn't limited
# to its own city - כפר סבא, גבעת חן and טייבה addresses were all
# observed in the 2026-09-17 survey. Deriving city from the address
# rather than hardcoding CITY handles that. Unlike Netanya, at least one
# live address ("הסחלב, אריאל") omits the ", Israel" suffix entirely -
# the trailing ", Israel" is therefore optional here, so both formats
# resolve to the same last comma-separated segment as the city. Extend
# _CITY_NORMALIZE if an English/transliterated spelling variant turns up
# (none seen yet).
_ADDRESS_CITY_RE = re.compile(r",\s*([^,]+?)(?:,\s*Israel)?$")
_CITY_NORMALIZE: dict[str, str] = {}

# Spec section 11: identify the collector and give a contact address.
USER_AGENT = (
    "KosherMapBot/0.1 (public kashrut directory aggregator; "
    "+https://github.com/avituriel-sys/kosher-map; "
    "mailto:avituriel@gmail.com)"
)

REQUEST_TIMEOUT_SECONDS = 30
POLITE_DELAY_SECONDS = 1.5
MAX_PAGES = 50  # sanity cap; a real run should never get near this

_WAZE_LL_RE = re.compile(r"[?&]ll=([\-0-9.]+)%2C([\-0-9.]+)")


@dataclass
class _RawCard:
    entity_id: str
    name_raw: str
    source_url: str
    directory_category_raw: str
    balanit_raw: str
    address_raw: str
    lat: float | None
    lng: float | None
    supervisor_name: str | None
    supervisor_phone: str | None


def collect(session: requests.Session | None = None) -> list[dict]:
    """Fetch every currently published, currently-certified Raanana
    kashrut listing (see EXCLUDED_DIRECTORY_CATEGORIES for what's
    deliberately left out and why).

    Returns a list of canonical `business` records (spec section 5.1).
    Raises CollectorError if any page fails to fetch, if pagination
    cannot be trusted to have reached the end, or if a record fails
    schema validation.
    """
    owns_session = session is None
    session = session or requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    try:
        cards = _fetch_all_cards(session)
    finally:
        if owns_session:
            session.close()

    records = []
    for card in cards:
        record = _to_canonical_record(card)
        if record is not None:
            records.append(record)
    for record in records:
        validate_record(record)
    return records


def _fetch_all_cards(session: requests.Session) -> list[_RawCard]:
    cards: list[_RawCard] = []
    seen_entity_ids: set[str] = set()
    seen_first_entity_id: str | None = None

    page_num = 1
    next_url = BASE_URL
    while True:
        if page_num > MAX_PAGES:
            raise CollectorError(
                f"exceeded MAX_PAGES={MAX_PAGES} without pagination reporting "
                f"completion - refusing to loop forever"
            )

        html = _get(session, next_url)
        soup = BeautifulSoup(html, "lxml")

        page_cards = _parse_cards(soup)
        if not page_cards:
            raise CollectorError(
                f"page {page_num} ({next_url}) returned zero listing cards; "
                f"the site markup likely changed"
            )

        first_id = page_cards[0].entity_id
        if seen_first_entity_id is None:
            seen_first_entity_id = first_id
        elif page_num > 1 and first_id == seen_first_entity_id:
            # Same failure mode documented in Netanya's collector (same
            # plugin): an out-of-range `_page` silently falls back to
            # page 1's content instead of erroring or returning empty.
            raise CollectorError(
                f"page {page_num} ({next_url}) returned the same first "
                f"record as page 1 (entity id {first_id}); the site's "
                f"pagination likely wrapped around instead of ending, "
                f"which means the 'has next page' check below is wrong "
                f"or the site changed"
            )

        duplicate_ids = seen_entity_ids & {c.entity_id for c in page_cards}
        if duplicate_ids:
            raise CollectorError(
                f"page {page_num} ({next_url}) re-returned already-seen "
                f"entity id(s) {duplicate_ids}"
            )
        seen_entity_ids.update(c.entity_id for c in page_cards)
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
    """Same pagination control as Netanya's site (identical plugin) -
    take the pagination block's last link and check whether it's
    disabled, rather than guessing from icon direction.
    """
    pagination = soup.select_one("div.drts-pagination")
    if pagination is None:
        return None

    links = pagination.find_all("a", recursive=False)
    if not links:
        return None

    next_link = links[-1]
    classes = next_link.get("class") or []
    if "drts-bs-disabled" in classes:
        return None

    href = next_link.get("data-ajax-url") or next_link.get("href")
    if not href or href == "#":
        return None
    return href


def _parse_cards(soup: BeautifulSoup) -> list[_RawCard]:
    cards: list[_RawCard] = []
    entities = soup.select(
        'div[data-content-name="kashrut_dir_ltg"][data-display-name="summary-custom_list"]'
    )
    for entity in entities:
        entity_id = entity.get("data-entity-id")
        if not entity_id:
            continue
        cards.append(_parse_one_card(entity_id, entity))
    return cards


def _field_value_text(entity, field_name: str) -> str | None:
    field = entity.select_one(f'div[data-name="{field_name}"]')
    if field is None:
        return None
    value = field.select_one("div.drts-entity-field-value")
    text = (value or field).get_text(strip=True)
    return text or None


def _parse_one_card(entity_id: str, entity) -> _RawCard:
    title_field = entity.select_one('div[data-name="entity_field_post_title"] a')
    if title_field is None:
        raise CollectorError(f"listing {entity_id} has no title/link - markup changed")
    name_raw = title_field.get_text(strip=True)
    source_url = title_field.get("href", "")

    address_field = entity.select_one('div[data-name="entity_field_location_address"]')
    if address_field is None:
        raise CollectorError(f"listing {entity_id} has no address field - markup changed")
    address_value = address_field.select_one("div.drts-entity-field-value")
    address_raw = (address_value or address_field).get_text(strip=True)

    waze_link = address_field.select_one('a[href*="waze.com"]')
    lat = lng = None
    if waze_link is not None:
        match = _WAZE_LL_RE.search(waze_link.get("href", ""))
        if match:
            lat, lng = float(match.group(1)), float(match.group(2))

    directory_category_field = entity.select_one('div[data-name="entity_field_directory_category"] a')
    directory_category_raw = (
        directory_category_field.get_text(strip=True) if directory_category_field else ""
    )

    return _RawCard(
        entity_id=entity_id,
        name_raw=name_raw,
        source_url=source_url,
        directory_category_raw=directory_category_raw,
        balanit_raw=_field_value_text(entity, "entity_field_field_balanit_name") or "",
        address_raw=address_raw,
        lat=lat,
        lng=lng,
        supervisor_name=_field_value_text(entity, "entity_field_field_supervisor_name"),
        supervisor_phone=_field_value_text(entity, "entity_field_field_supervisor_phone"),
    )


def _clean_name(name_raw: str) -> tuple[str, str | None]:
    """Return (name_clean, branch). Spec section 5.1: trim, strip בע"מ,
    and split a branch name that follows a '/' or '-' separator.
    """
    name = name_raw.strip()
    branch = None
    if "/" in name:
        head, _, tail = name.partition("/")
        name, branch = head.strip(), tail.strip() or None
    name = re.sub(r'\s*בע"מ\s*$', "", name).strip()
    return name, branch


def _map_category(balanit_raw: str) -> str:
    primary = balanit_raw.split(";")[0].strip() if balanit_raw else ""
    canonical = CATEGORY_MAP.get(primary)
    if canonical is None:
        logger.warning(
            "raanana: unmapped category (balanit_name) %r - extend "
            "CATEGORY_MAP (health alert per spec section 5.3)",
            primary,
        )
        canonical = "other"
    return canonical


def _map_supervision(directory_category_raw: str) -> str:
    canonical = SUPERVISION_MAP.get(directory_category_raw)
    if canonical is None:
        logger.warning(
            "raanana: unmapped directory_category %r treated as "
            "supervision_level=unknown - extend SUPERVISION_MAP or "
            "EXCLUDED_DIRECTORY_CATEGORIES if this is a new pattern "
            "(health alert per spec section 5.3)",
            directory_category_raw,
        )
        return "unknown"
    return canonical


def _city_from_address(address_raw: str) -> str:
    match = _ADDRESS_CITY_RE.search(address_raw)
    if match is None:
        logger.warning(
            "raanana: could not find a city in address_raw %r - "
            "falling back to %r (health alert per spec section 5.3)",
            address_raw,
            CITY,
        )
        return CITY
    city = match.group(1).strip()
    return _CITY_NORMALIZE.get(city, city)


def _to_canonical_record(card: _RawCard) -> dict | None:
    if card.directory_category_raw in EXCLUDED_DIRECTORY_CATEGORIES:
        logger.info(
            "raanana: skipping %r (entity %s) - directory_category %r "
            "means not-yet-certified or not a food business",
            card.name_raw,
            card.entity_id,
            card.directory_category_raw,
        )
        return None

    name_clean, branch = _clean_name(card.name_raw)
    slug = urllib.parse.unquote(card.source_url.rstrip("/").rsplit("/", 1)[-1])

    return {
        "source_id": SOURCE_ID,
        "source_record_id": slug or card.entity_id,
        "source_url": card.source_url,
        "name_raw": card.name_raw,
        "name_clean": name_clean,
        "branch": branch,
        "category_raw": card.balanit_raw or None,
        "category_canonical": _map_category(card.balanit_raw),
        # Not published by this source at all (see module docstring) -
        # left unset rather than guessed at from category text.
        "kosher_type": None,
        "supervision_level": _map_supervision(card.directory_category_raw),
        "certifying_authority": CERTIFYING_AUTHORITY,
        "additional_hechsher": None,
        "address_raw": card.address_raw,
        "city": _city_from_address(card.address_raw),
        "lat": card.lat,
        "lng": card.lng,
        "location_source": "published" if card.lat is not None else None,
        "location_confidence": "high" if card.lat is not None else None,
        "phone": None,
        "supervisor_name": card.supervisor_name,
        "supervisor_phone": card.supervisor_phone,
        "status": "active",
    }


if __name__ == "__main__":
    import json
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        results = collect()
    except CollectorError as exc:
        print(f"COLLECTOR FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"collected {len(results)} records", file=sys.stderr)
    json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
