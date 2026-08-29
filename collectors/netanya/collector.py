"""Collector for the Netanya Religious Council kashrut directory.

Source: https://mdn.org.il/directory-kashrut/
Spec: kosher-map-technical-spec-v0.1.md section 6.2, source A.

Site survey (2026-08-11) found the WordPress REST API exposes the
`kashrut_dir_ltg` post type (wp-json/wp/v2/kashrut_dir_ltg) but the
directory plugin's custom fields (address, phone, kashrut type,
supervision level) are not registered for REST and come back empty. The
data instead lives in the server-rendered list view at
/directory-kashrut/, which - unlike a per-listing detail page - already
carries all the fields this collector needs (title, category, address
with an embedded Waze coordinate link, supervisor name/phone, kosher
type, supervision level) for up to ~200 businesses per page. That view
is paginated via a `_page` query parameter; the collector follows it to
completion rather than assuming a fixed page count.

One pagination trap found during the survey: requesting a `_page` value
past the last real page does not error or return empty - the server
silently falls back to page 1's content. A naive "stop when the page is
empty" loop would therefore hang, and a naive "stop after N pages" loop
would silently duplicate page 1 forever. This collector instead reads
the page's own pagination control to find out whether a next page
exists, and additionally treats a repeated first-entity-id as a hard
error (see _COLLECTOR CONTRACT_ below).

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
from collectors.netanya.mappings import CATEGORY_MAP, KOSHER_TYPE_MAP, SUPERVISION_MAP

logger = logging.getLogger(__name__)

SOURCE_ID = "netanya"
CERTIFYING_AUTHORITY = "הרבנות הראשית נתניה"
CITY = "נתניה"
BASE_URL = "https://mdn.org.il/directory-kashrut/"

# Every published address_raw ends "..., <city>, Israel" (confirmed
# against all 605 live records during a 2026-08-29 audit) - the city
# named there is not always Netanya. The council apparently also
# certifies some businesses in neighbouring כפר יונה (Kfar Yona), and a
# handful of Netanya addresses are published in English/transliterated
# rather than Hebrew. Deriving city from the address rather than
# hardcoding CITY fixes both: businesses genuinely in Kfar Yona get
# labelled correctly, and English "Netanya" normalizes to the Hebrew
# spelling used everywhere else. Extend _CITY_NORMALIZE if more
# spelling variants turn up.
_ADDRESS_CITY_RE = re.compile(r",\s*([^,]+),\s*Israel$")
_CITY_NORMALIZE = {"Netanya": CITY}

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
    category_raw: str
    address_raw: str
    lat: float | None
    lng: float | None
    supervisor_name: str | None
    supervisor_phone: str | None
    kosher_type_raw: str | None
    supervision_raw: str | None


def collect(session: requests.Session | None = None) -> list[dict]:
    """Fetch every currently published Netanya kashrut listing.

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

    records = [_to_canonical_record(card) for card in cards]
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
            # Observed failure mode: an out-of-range `_page` silently
            # falls back to page 1's content instead of erroring or
            # returning empty. Treat a repeat of page 1's first record
            # as proof we've wrapped around, not as a legitimate page.
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
    """Return the URL of the next pagination page, or None if this is the
    last page. Netanya's directory is RTL, so the visually-forward
    "next" control is the left-pointing double-angle link; we identify
    it by finding the pagination block and taking its last <a>, then
    checking whether that link is disabled rather than guessing from
    icon direction (which would be a much more fragile signal).
    """
    pagination = soup.select_one("div.drts-pagination")
    if pagination is None:
        # No pagination control at all means there is only one page.
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

    category_field = entity.select_one('div[data-name="entity_field_directory_category"] a')
    category_raw = category_field.get_text(strip=True) if category_field else ""

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

    return _RawCard(
        entity_id=entity_id,
        name_raw=name_raw,
        source_url=source_url,
        category_raw=category_raw,
        address_raw=address_raw,
        lat=lat,
        lng=lng,
        supervisor_name=_field_value_text(entity, "entity_field_field_supervisor_name"),
        supervisor_phone=_field_value_text(entity, "entity_field_field_supervisor_phone"),
        kosher_type_raw=_field_value_text(entity, "entity_field_field_balanit_name"),
        supervision_raw=_field_value_text(entity, "entity_field_field_type_of_supervision"),
    )


def _clean_name(name_raw: str) -> tuple[str, str | None]:
    """Return (name_clean, branch). Spec section 5.1: trim, strip בע"מ,
    and split a branch name that follows a '/' separator.
    """
    name = name_raw.strip()
    branch = None
    if "/" in name:
        head, _, tail = name.partition("/")
        name, branch = head.strip(), tail.strip() or None
    name = re.sub(r'\s*בע"מ\s*$', "", name).strip()
    return name, branch


def _map_category(category_raw: str) -> str:
    canonical = CATEGORY_MAP.get(category_raw)
    if canonical is None:
        logger.warning(
            "netanya: unmapped category_raw %r - extend CATEGORY_MAP "
            "(health alert per spec section 5.3)",
            category_raw,
        )
        canonical = "other"
    return canonical


def _map_kosher_type(kosher_type_raw: str | None) -> list[str] | None:
    if not kosher_type_raw:
        return None
    tokens = [t.strip() for t in kosher_type_raw.split(",") if t.strip()]
    if len(tokens) == 1 and tokens[0] not in KOSHER_TYPE_MAP:
        # Guards against the observed data glitch where multiple values
        # are whitespace-separated instead of comma-separated (e.g.
        # "בשרי     פרווה"). Don't guess at a split - flag it.
        logger.warning(
            "netanya: unrecognised kosher_type_raw %r (possibly missing "
            "comma separator) - leaving kosher_type empty for this record",
            kosher_type_raw,
        )
        return None
    mapped = []
    for token in tokens:
        canonical = KOSHER_TYPE_MAP.get(token)
        if canonical is None:
            logger.warning(
                "netanya: unmapped kosher_type token %r in %r - extend "
                "KOSHER_TYPE_MAP (health alert per spec section 5.3)",
                token,
                kosher_type_raw,
            )
            continue
        mapped.append(canonical)
    return mapped or None


def _map_supervision(supervision_raw: str | None) -> str:
    if not supervision_raw:
        return "unknown"
    canonical = SUPERVISION_MAP.get(supervision_raw)
    if canonical is None:
        logger.warning(
            "netanya: unmapped supervision_level %r - extend "
            "SUPERVISION_MAP (health alert per spec section 5.3)",
            supervision_raw,
        )
        return "unknown"
    return canonical


def _city_from_address(address_raw: str) -> str:
    match = _ADDRESS_CITY_RE.search(address_raw)
    if match is None:
        logger.warning(
            "netanya: could not find a city in address_raw %r - "
            "falling back to %r (health alert per spec section 5.3)",
            address_raw,
            CITY,
        )
        return CITY
    city = match.group(1).strip()
    return _CITY_NORMALIZE.get(city, city)


def _to_canonical_record(card: _RawCard) -> dict:
    name_clean, branch = _clean_name(card.name_raw)
    slug = urllib.parse.unquote(card.source_url.rstrip("/").rsplit("/", 1)[-1])

    return {
        "source_id": SOURCE_ID,
        "source_record_id": slug or card.entity_id,
        "source_url": card.source_url,
        "name_raw": card.name_raw,
        "name_clean": name_clean,
        "branch": branch,
        "category_raw": card.category_raw,
        "category_canonical": _map_category(card.category_raw),
        "kosher_type": _map_kosher_type(card.kosher_type_raw),
        "supervision_level": _map_supervision(card.supervision_raw),
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
