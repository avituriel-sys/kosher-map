"""Collector for the Tel Aviv-Yafo Religious Council kashrut directory
(the *active* listings - see revoked_collector.py for the separate
revocation feed required by spec section 6.2).

Source: https://rabanut.co.il/חיפוש-עסקים-כשרים/
Spec: kosher-map-technical-spec-v0.1.md section 6.2, source B.

Site survey (2026-08-27): no REST API exposes this data (no custom
namespace, and wp/v2 doesn't list a kashrut post type at all - unlike
Netanya, this council's plugin isn't even trying). The rendered list
view at /חיפוש-עסקים-כשרים/ carries name, business type, street address
(no city, no coordinates) and phone; it's paginated via standard
WordPress /page/N/ URLs with an explicit "next" link, which is a much
simpler termination signal than Netanya's site.

The one field the list view does *not* carry is kashrut level (רגילה /
מהדרין) - that only appears on each business's own detail page. Visiting
~1120 detail pages daily isn't consistent with spec section 3's "minimise
recurring cost" or section 11's politeness requirement. Instead, the
search form's `kashrut` filter (values 322=רגילה, 318=מהדרין) can be
applied to the same list view, so the collector crawls it a second and
third time filtered by each level and merges the result. That's
~10+2+9=21 page fetches total instead of 1120.

IMPORTANT - why matching is done by (name, address) and not by URL slug:
the survey found that the permalink each card links to is NOT a stable
per-business identifier across these three crawls. One business (a
hospital kitchen) appeared in the unfiltered crawl linked to
.../בית-חולים-רפאל-3/, and in the מהדרין-filtered crawl linked to
.../בית-חולים-רפאל/ - different hrefs for the same real business.
Fetching both live settled which was real: the "-3" URL 404s: it was
never a valid page. The plain slug is the business's actual permanent
permalink. The site appears to compute that numeric disambiguation
suffix from how many same-titled businesses have appeared so far *within
that specific query's result set* rather than reading each post's real
stored slug, so the suffix (and therefore the href) can come out
differently depending on which filter is active. In the survey's data
this affected roughly 1 in 5 businesses. A collector that joined the
three crawls - or set source_record_id - on that href would silently
mismatch supervision levels for a fifth of the city and would make
first_seen/last_seen churn every time some unrelated same-titled
business was added or removed elsewhere in the corpus, since the
suffix (and hence the "identity") would shift out from under a business
that hadn't actually changed. So identity here is computed the way spec
section 5.1 already prescribes for a source with no stable slug: a hash
of (name, address). source_url is still recorded as a best-effort deep
link (it *is* a real link to somewhere on the site, and it's correct
whenever there's no title collision), but it is not trustworthy enough
to build identity on, and a human polishing this later should expect a
minority of source_url values to 404.

No coordinates are published anywhere for this source (every business's
detail page carries a literal `data-lat="0" data-lng="0"`); geocoding is
mandatory here, per spec section 7. A full backfill (2026-08-27/28) left
~9% of records (98/1116) with no coordinates at all even after
NominatimGeocoder's built-in retries. Two real causes, not a bug here:
(1) some addresses are landmarks or complex names rather than
street+number ("קניון עזריאלי קומה 2", "תחנה מרכזית", "אוניברסיטת תל
אביב בית גילמן") - not something a street geocoder can resolve; and
(2) free Nominatim/OSM appears to have a real, reproducible coverage gap
for Jaffa (יפו) street-level addresses specifically - roughly half the
failures cluster on a handful of Jaffa streets (שד' ירושלים, חשמונאים,
המבשר, העליה, שד' יהודית...), streets this source's own UI treats as a
separate "יפו" region from the rest of the city. These records still
collect and validate fine (schema.py allows a record with no location
data at all) - per spec section 7 they're exactly what the not-yet-built
manual review queue is for, not something to force onto the map.

Collector contract (spec section 6.1): this module performs no database
writes and makes no judgement about what changed since the last run. It
either returns a complete list of canonical business records, or raises
CollectorError. It never returns a partial list.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from collectors.common.geocoding import (
    GeocodeCache,
    Geocoder,
    NominatimGeocoder,
    geocode_cached,
)
from collectors.common.schema import CollectorError, hash_identity, validate_record
from collectors.tlv.mappings import CATEGORY_MAP, KOSHER_TYPE_FROM_BUSINESS_TYPE

logger = logging.getLogger(__name__)

SOURCE_ID = "tlv"
CERTIFYING_AUTHORITY = "המועצה הדתית תל אביב-יפו"
CITY = "תל אביב-יפו"
BASE_URL = "https://rabanut.co.il/%D7%97%D7%99%D7%A4%D7%95%D7%A9-%D7%A2%D7%A1%D7%A7%D7%99%D7%9D-%D7%9B%D7%A9%D7%A8%D7%99%D7%9D/"
MEHADRIN_URL = f"{BASE_URL}?kashrut=318"
REGULAR_URL = f"{BASE_URL}?kashrut=322"

USER_AGENT = (
    "KosherMapBot/0.1 (public kashrut directory aggregator; "
    "+https://github.com/avituriel-sys/kosher-map; "
    "mailto:avituriel@gmail.com)"
)

REQUEST_TIMEOUT_SECONDS = 30
POLITE_DELAY_SECONDS = 1.5
MAX_PAGES = 50

# Generous sanity box around Tel Aviv-Yafo municipal area.
TLV_BOUNDS = (32.00, 32.15, 34.72, 34.87)

DEFAULT_GEOCODE_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "geocode_cache" / "tlv.json"


@dataclass
class _RawCard:
    identity: str  # hash_identity(name_raw, address_raw) - see module docstring
    source_url: str
    name_raw: str
    category_raw: str
    address_raw: str
    phone: str | None


def collect(
    session: requests.Session | None = None,
    geocoder: Geocoder | None = None,
    geocode_cache_path: Path = DEFAULT_GEOCODE_CACHE_PATH,
) -> list[dict]:
    """Fetch every currently published Tel Aviv-Yafo kashrut listing
    (active only - see collect_revoked in revoked_collector.py).

    Returns a list of canonical `business` records (spec section 5.1).
    Raises CollectorError if any page fails to fetch, if pagination
    cannot be trusted to have reached the end, if the three supervision-
    level crawls don't reconcile, or if a record fails schema
    validation.
    """
    owns_session = session is None
    session = session or requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    try:
        cards = _fetch_all_cards(session)
        mehadrin_ids = _fetch_identity_set(session, MEHADRIN_URL)
        regular_ids = _fetch_identity_set(session, REGULAR_URL)
    finally:
        if owns_session:
            session.close()

    supervision_by_identity = _verify_supervision_partition(
        {c.identity for c in cards}, mehadrin_ids, regular_ids
    )

    geocoder = geocoder or NominatimGeocoder()
    cache = GeocodeCache(geocode_cache_path)
    records = []
    try:
        for card in cards:
            records.append(
                _to_canonical_record(
                    card, supervision_by_identity[card.identity], geocoder, cache
                )
            )
    finally:
        cache.save()

    for record in records:
        validate_record(record)
    return records


# The result set for a given crawl can shift slightly between two
# sequential page requests on this live, frequently-edited site (the
# survey saw one business move from page 6 to page 5 between requests
# about a second apart, so it was captured on both). That's ordinary
# churn, not the kind of systemic failure Netanya's page-wraparound bug
# was - a handful of re-seen identities gets deduped with a warning
# rather than failing the whole run. A large number is a different
# story - that would suggest something is actually broken, and the
# supervision-partition check in _verify_supervision_partition provides
# a second, independent completeness signal on top of this one.
DUPLICATE_WARN_THRESHOLD = 20


def _fetch_all_cards(session: requests.Session) -> list[_RawCard]:
    cards: list[_RawCard] = []
    seen: set[str] = set()
    duplicate_count = 0
    for soup in _iter_pages(session, BASE_URL):
        page_cards = _parse_cards(soup)
        if not page_cards:
            raise CollectorError("a listing page returned zero cards; markup likely changed")
        for card in page_cards:
            if card.identity in seen:
                duplicate_count += 1
                continue
            seen.add(card.identity)
            cards.append(card)
    _check_duplicate_count(duplicate_count)
    return cards


def _fetch_identity_set(session: requests.Session, start_url: str) -> set[str]:
    identities: set[str] = set()
    duplicate_count = 0
    for soup in _iter_pages(session, start_url):
        page_identities = _parse_identities_only(soup)
        if not page_identities:
            raise CollectorError(
                f"a filtered listing page ({start_url}) returned zero cards; "
                f"markup likely changed"
            )
        new_ids = page_identities - identities
        duplicate_count += len(page_identities) - len(new_ids)
        identities.update(new_ids)
    _check_duplicate_count(duplicate_count)
    return identities


def _check_duplicate_count(duplicate_count: int) -> None:
    if duplicate_count == 0:
        return
    if duplicate_count > DUPLICATE_WARN_THRESHOLD:
        raise CollectorError(
            f"pagination re-returned {duplicate_count} already-seen records, "
            f"well above the {DUPLICATE_WARN_THRESHOLD} expected from "
            f"ordinary result-set drift - treating this as untrustworthy"
        )
    logger.warning(
        "tlv: pagination re-returned %d already-seen record(s), likely the "
        "result set shifting between page requests - deduped, first "
        "occurrence kept",
        duplicate_count,
    )


def _iter_pages(session: requests.Session, start_url: str):
    url = start_url
    page_num = 1
    while url:
        if page_num > MAX_PAGES:
            raise CollectorError(
                f"exceeded MAX_PAGES={MAX_PAGES} without pagination reporting "
                f"completion - refusing to loop forever"
            )
        html = _get(session, url)
        soup = BeautifulSoup(html, "lxml")
        yield soup
        url = _find_next_page_url(soup)
        page_num += 1
        if url:
            time.sleep(POLITE_DELAY_SECONDS)


def _get(session: requests.Session, url: str) -> str:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise CollectorError(f"failed to fetch {url}: {exc}") from exc
    return response.text


def _find_next_page_url(soup: BeautifulSoup) -> str | None:
    next_link = soup.select_one("a.next.page-numbers")
    if next_link is None:
        return None
    return next_link.get("href")


def _parse_cards(soup: BeautifulSoup) -> list[_RawCard]:
    cards = []
    for item in soup.select("div.post_col.kosher_item"):
        link = item.select_one("a")
        if link is None or not link.get("href"):
            raise CollectorError("a kosher_item card has no permalink - markup changed")
        source_url = link["href"]

        title = item.select_one(".post_title")
        name_raw = title.get_text(strip=True) if title else ""
        if not name_raw:
            raise CollectorError(f"listing at {source_url} has no title - markup changed")

        category_el = item.select_one(".business_type")
        category_raw = category_el.get_text(strip=True) if category_el else ""

        address_el = item.select_one(".wrap_address")
        address_raw = address_el.get_text(strip=True) if address_el else ""
        if not address_raw:
            raise CollectorError(f"listing {name_raw!r} has no address - markup changed")

        phone_el = item.select_one(".wrap_phone span")
        phone = phone_el.get_text(strip=True) if phone_el else None

        cards.append(
            _RawCard(
                identity=hash_identity(name_raw, address_raw),
                source_url=source_url,
                name_raw=name_raw,
                category_raw=category_raw,
                address_raw=address_raw,
                phone=phone,
            )
        )
    return cards


def _parse_identities_only(soup: BeautifulSoup) -> set[str]:
    identities = set()
    for item in soup.select("div.post_col.kosher_item"):
        title = item.select_one(".post_title")
        address_el = item.select_one(".wrap_address")
        if title is None or address_el is None:
            continue
        name_raw = title.get_text(strip=True)
        address_raw = address_el.get_text(strip=True)
        if name_raw and address_raw:
            identities.add(hash_identity(name_raw, address_raw))
    return identities


def _verify_supervision_partition(
    all_ids: set[str], mehadrin_ids: set[str], regular_ids: set[str]
) -> dict[str, str]:
    """Reconcile the two supervision-filtered crawls against the
    unfiltered one. In an ideal world מהדרין and רגילה partition the full
    set exactly (and the survey found they do, almost always: 1120 =
    130 + 990). But the three crawls are three separate sequential trips
    over a live site, seconds to tens of seconds apart, and the survey
    also caught a single business appearing in *both* filtered crawls -
    most likely its level was edited mid-survey, not a scraping bug.
    Small mismatches get logged and resolved to supervision_level
    "unknown" (an honest answer given the source's own filters
    disagreed, per schema.py's SUPERVISION_LEVELS) rather than failing
    the whole run; a mismatch too large to plausibly be page-to-page
    drift raises instead, per section 6.1's fail-loud requirement.
    """
    overlap = mehadrin_ids & regular_ids
    covered = mehadrin_ids | regular_ids
    missing = all_ids - covered  # in the unfiltered crawl, in neither filtered one
    extra = covered - all_ids  # in a filtered crawl, absent from the unfiltered one

    total_anomalies = len(overlap) + len(missing) + len(extra)
    if total_anomalies > DUPLICATE_WARN_THRESHOLD:
        raise CollectorError(
            f"supervision-level crawls don't reconcile with the unfiltered "
            f"crawl: {len(overlap)} in both filters, {len(missing)} in "
            f"neither, {len(extra)} in a filter but not the unfiltered "
            f"crawl - {total_anomalies} total, well above the "
            f"{DUPLICATE_WARN_THRESHOLD} expected from ordinary drift"
        )
    if overlap:
        logger.warning(
            "tlv: %d business(es) matched both מהדרין and רגילה filters - "
            "supervision_level set to 'unknown' for these: %s",
            len(overlap),
            sorted(overlap)[:10],
        )
    if missing:
        logger.warning(
            "tlv: %d business(es) from the unfiltered crawl matched "
            "neither supervision filter - supervision_level set to "
            "'unknown' for these: %s",
            len(missing),
            sorted(missing)[:10],
        )
    if extra:
        logger.warning(
            "tlv: %d business(es) matched a supervision filter but weren't "
            "in the unfiltered crawl - no listing data for these, so "
            "they're dropped rather than fabricated: %s",
            len(extra),
            sorted(extra)[:10],
        )

    result = {identity: "mehadrin" for identity in mehadrin_ids - overlap}
    result.update({identity: "regular" for identity in regular_ids - overlap})
    result.update({identity: "unknown" for identity in overlap | missing})
    return result


def _map_category(category_raw: str) -> str:
    canonical = CATEGORY_MAP.get(category_raw)
    if canonical is None:
        logger.warning(
            "tlv: unmapped category_raw %r - extend CATEGORY_MAP "
            "(health alert per spec section 5.3)",
            category_raw,
        )
        canonical = "other"
    return canonical


def _map_kosher_type(category_raw: str) -> list[str] | None:
    return KOSHER_TYPE_FROM_BUSINESS_TYPE.get(category_raw)


def _map_supervision(bucket: str) -> str:
    # bucket is already "mehadrin" / "regular" / "unknown" -
    # _verify_supervision_partition assigns these directly.
    assert bucket in ("mehadrin", "regular", "unknown")
    return bucket


def _to_canonical_record(
    card: _RawCard, supervision_bucket: str, geocoder: Geocoder, cache: GeocodeCache
) -> dict:
    geocode_query = f"{card.address_raw}, {CITY}, ישראל"
    geo = geocode_cached(geocode_query, geocoder, cache, TLV_BOUNDS)

    return {
        "source_id": SOURCE_ID,
        "source_record_id": card.identity,
        "source_url": card.source_url,
        "name_raw": card.name_raw,
        "name_clean": card.name_raw.strip(),
        "branch": None,
        "category_raw": card.category_raw,
        "category_canonical": _map_category(card.category_raw),
        "kosher_type": _map_kosher_type(card.category_raw),
        "supervision_level": _map_supervision(supervision_bucket),
        "certifying_authority": CERTIFYING_AUTHORITY,
        "additional_hechsher": None,
        "address_raw": card.address_raw,
        "city": CITY,
        "lat": geo.lat,
        "lng": geo.lng,
        "location_source": "geocoded" if geo.lat is not None else None,
        "location_confidence": geo.confidence,
        "phone": card.phone,
        "supervisor_name": None,
        "supervisor_phone": None,
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
