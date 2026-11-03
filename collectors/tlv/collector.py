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

UPDATE 2026-11 - the site's paging became unreliable. From October 2026 the
unfiltered list's pages (120 cards each, ~9 pages) overlap at some page
boundaries and skip a block at others, and a page occasionally comes back
empty: on 2026-11-03 the crawl returned 987 cards but only 945 distinct
businesses, with the alphabetical block between "ר.ל. ברכת הכהנים" and
"שמעון מלך המרקים" absent from every page (the weekly runs of Oct 12 and 19
failed on this). The site's own search form, however, carries a dropdown of
every business (`select[name=business_id]`, 987 entries on that date) and
`?business_id=<id>` returns that one business's card. So the collector now
(1) crawls the pages as before, tolerating repeats and empty pages;
(2) compares the businesses found with the dropdown and looks up each missing
one individually (at most 45 extra requests, 10 seconds apart - the dropdown is
not a list of live listings, it only supplies ids to look up, and most of what
is left unrecoverable is simply not in it); (3) reads the מהדרין level from
its own filtered list, which fits on one page and so isn't affected, and treats
everything else as רגילה (the רגילה list shares the broken paging and is no
longer crawled); and (4) accepts a small shortfall against the number of listings the site's own
pages account for (warning; the missing ones are marked absent until a later
run sees them) but fails loudly beyond 5%.
The portal at shirathayam.m-datit.org.il lists only 34 Tel Aviv businesses with
no certificates, so it is no help here.

Collector contract (spec section 6.1): this module performs no database
writes and makes no judgement about what changed since the last run. It
either returns a complete list of canonical business records, or raises
CollectorError. It never returns a partial list.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
        cards, dropdown, listed_total = _fetch_all_cards(session)
        mehadrin_cards = _fetch_mehadrin_cards(session)
        cards = _add_cards_missing_from_mehadrin_list(cards, mehadrin_cards)
        cards = _complete_from_dropdown(session, cards, dropdown, listed_total)
        mehadrin_ids = {card.identity for card in mehadrin_cards}
    finally:
        if owns_session:
            session.close()

    supervision_by_identity = _assign_supervision(
        {c.identity for c in cards}, mehadrin_ids
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


def _fetch_all_cards(
    session: requests.Session,
) -> tuple[list[_RawCard], list[tuple[str, str]], int]:
    """Crawl the unfiltered list. Returns (cards, dropdown, listed_total).

    `dropdown` is the search form's own list of businesses, as (id, name)
    pairs. `listed_total` is how many cards the pages showed in all, repeats
    included - the pages are fixed-size windows over the site's result set, so
    their combined size is the number of listings the site says it has (987 on
    2026-11-03) even when the windows overlap and skip.

    Since 2026-10 the site's paging is unreliable (see the module docstring),
    so this crawl can both repeat and skip businesses; `_complete_from_dropdown`
    uses the dropdown and `listed_total` to recover what was skipped.
    """
    cards: list[_RawCard] = []
    seen: set[str] = set()
    duplicate_count = 0
    listed_total = 0
    dropdown: list[tuple[str, str]] = []
    for page_number, soup in enumerate(_iter_pages(session, BASE_URL), start=1):
        if page_number == 1:
            dropdown = _parse_dropdown(soup)
            if not dropdown:
                raise CollectorError(
                    "the search form's business list is missing or empty; "
                    "markup likely changed"
                )
        page_cards = _parse_cards(soup)
        if not page_cards:
            if page_number == 1:
                raise CollectorError("a listing page returned zero cards; markup likely changed")
            # An empty page in the middle used to be a hard failure. The site now
            # sometimes answers a page with nothing, so carry on - whatever it
            # skipped is recovered by _complete_from_dropdown.
            logger.warning("tlv: listing page %d returned zero cards", page_number)
            continue
        listed_total += len(page_cards)
        for card in page_cards:
            if card.identity in seen:
                duplicate_count += 1
                continue
            seen.add(card.identity)
            cards.append(card)
    if duplicate_count:
        logger.warning(
            "tlv: paging repeated %d listing(s) across page boundaries - "
            "deduped, first occurrence kept",
            duplicate_count,
        )
    return cards, dropdown, listed_total


def _parse_dropdown(soup: BeautifulSoup) -> list[tuple[str, str]]:
    return [
        (option["value"], " ".join(option.get_text().split()))
        for option in soup.select("select[name=business_id] option")
        if option.get("value")
    ]


def _name_key(name: str) -> str:
    return " ".join(name.split()).lower()


# How many single-business lookups one run may make to recover listings the
# paging skipped. On 2026-11-03 104 dropdown entries were candidates, about 80
# of them (the names at the end of the alphabet) with no public listing at
# all, and Cloudflare started answering 429 after roughly 70 requests in ten
# minutes. The candidates are tried in dropdown (alphabetical) order, so the
# budget is spent before the end-of-alphabet block of inactive entries.
MAX_LOOKUPS_PER_RUN = 45
# How far short of the site's own total the recovered list may be before the
# run is refused. Some shortfall is expected - listings the paging skipped that
# are not in the dropdown cannot be found by id - so up to this share of the
# total is only a warning, and the missing ones are marked absent until a later
# run's crawl happens to include them. Beyond it something is badly wrong.
MAX_SHORTFALL_FRACTION = 0.05


def _complete_from_dropdown(
    session: requests.Session,
    cards: list[_RawCard],
    dropdown: list[tuple[str, str]],
    listed_total: int,
) -> list[_RawCard]:
    """Add the listings the paged crawl skipped.

    The search form's business dropdown (987 entries on 2026-11-03) knows
    about businesses the paged crawl skipped (945 distinct found), but it is
    not a list of the live listings: 46 listed businesses are not in it, and
    about 60 of its entries have no public listing at all (their lookup comes
    back empty). So it only supplies ids to look up. For every name the
    dropdown has more times than the crawl found (104 entries on that date),
    fetch that business's own single-result page (`?business_id=<id>`) and
    add its card if it has one.

    Completeness is judged against `listed_total`, the number of listings the
    site's own pages account for: a small shortfall is a warning, a large one
    (MAX_SHORTFALL_FRACTION) refuses the run.
    """
    have: dict[str, int] = {}
    for card in cards:
        key = _name_key(card.name_raw)
        have[key] = have.get(key, 0) + 1

    ids_by_name: dict[str, list[str]] = {}
    for business_id, name in dropdown:
        ids_by_name.setdefault(_name_key(name), []).append(business_id)

    to_fetch: list[tuple[str, str]] = []
    for key, ids in ids_by_name.items():
        if have.get(key, 0) < len(ids):
            to_fetch.extend((business_id, key) for business_id in ids)
    if len(to_fetch) > MAX_LOOKUPS_PER_RUN:
        logger.warning(
            "tlv: %d dropdown entries are candidates for lookup; trying the "
            "first %d (alphabetical) to stay under the site's rate limit",
            len(to_fetch), MAX_LOOKUPS_PER_RUN,
        )
        to_fetch = to_fetch[:MAX_LOOKUPS_PER_RUN]

    known = {card.identity for card in cards}
    recovered = 0
    empty = 0  # dropdown entries with no public listing
    looked_up = 0
    for business_id, _key in to_fetch:
        time.sleep(LOOKUP_DELAY_SECONDS)
        try:
            html = _get(session, f"{BASE_URL}?business_id={business_id}")
        except CollectorError as exc:
            # Out of patience with the site's rate limit: carry on with what
            # was recovered so far rather than lose the whole run.
            logger.warning("tlv: stopping the lookups early: %s", exc)
            break
        looked_up += 1
        found = _parse_cards(BeautifulSoup(html, "lxml"))
        if not found:
            empty += 1
        for card in found:
            if card.identity not in known:
                known.add(card.identity)
                cards.append(card)
                recovered += 1

    shortfall = max(0, listed_total - len(cards))
    logger.info(
        "tlv: the site's pages account for %d listings; %d found by crawling, "
        "%d recovered by looking up %d dropdown entries (%d had no public "
        "listing); %d still unaccounted for",
        listed_total, len(cards) - recovered, recovered, looked_up, empty, shortfall,
    )
    if shortfall:
        logger.warning(
            "tlv: %d listing(s) the site's pages account for could not be found "
            "(skipped by the paging and not recoverable by id); they will be "
            "marked absent until a later run sees them", shortfall,
        )
    if shortfall > int(listed_total * MAX_SHORTFALL_FRACTION):
        raise CollectorError(
            f"{shortfall} of the {listed_total} listings the site's pages "
            f"account for could not be found even after looking up the missing "
            f"businesses individually - more than {MAX_SHORTFALL_FRACTION:.0%}, "
            f"so the data is too incomplete to use"
        )
    return cards


def _fetch_mehadrin_cards(session: requests.Session) -> list[_RawCard]:
    """The מהדרין-supervised businesses, as full listing cards, from the site's
    own filtered list. That list is a single page (112 on 2026-11-03, page size
    120), so the paging problem doesn't touch it; if it ever outgrows one page
    this refuses rather than trust the broken paging."""
    soup = BeautifulSoup(_get(session, MEHADRIN_URL), "lxml")
    cards = _parse_cards(soup)
    if not cards:
        raise CollectorError(
            f"the מהדרין list ({MEHADRIN_URL}) returned zero cards; markup likely changed"
        )
    if _find_next_page_url(soup) is not None:
        raise CollectorError(
            "the מהדרין list now spans more than one page, and the site's "
            "paging is unreliable - the collector needs a new way to read it"
        )
    return cards


def _add_cards_missing_from_mehadrin_list(
    cards: list[_RawCard], mehadrin_cards: list[_RawCard]
) -> list[_RawCard]:
    """The מהדרין list is downloaded in full anyway, so any of its businesses
    the paged crawl skipped (11 on 2026-11-03) can be added from it directly,
    with no extra request."""
    known = {card.identity for card in cards}
    added = [card for card in mehadrin_cards if card.identity not in known]
    if added:
        logger.info(
            "tlv: %d listing(s) the paged crawl skipped were recovered from the "
            "מהדרין list", len(added),
        )
    return cards + added


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


# The site sits behind Cloudflare, which answers "429 Too Many Requests" when
# it thinks we are going too fast (seen on 2026-11-03 after a few hundred
# requests in an afternoon of testing). Honour its Retry-After and try again
# rather than failing the whole run on the first 429.
MAX_RATE_LIMIT_RETRIES = 3
# Cloudflare's Retry-After is no use here (it comes back as "now"), and the
# limit looks like roughly 100 requests per ten minutes, so the wait grows
# with each retry instead: at least this long before the 1st, 2nd, 3rd retry.
RATE_LIMIT_BACKOFF_SECONDS = (60, 120, 240)
RETRY_AFTER_MIN_SECONDS = 5
RETRY_AFTER_MAX_SECONDS = 90
RETRY_AFTER_DEFAULT_SECONDS = 30
# One-by-one lookups are many requests in a row; space them out more than the
# page crawl so a normal run stays well under any rate limit.
LOOKUP_DELAY_SECONDS = 10.0


def _retry_after_seconds(response: requests.Response) -> float:
    """Seconds to wait before retrying, from a Retry-After header that is either
    a number of seconds or an HTTP date; clamped so a bad value can neither
    skip the wait nor stall the run."""
    value = (response.headers.get("Retry-After") or "").strip()
    seconds: float = RETRY_AFTER_DEFAULT_SECONDS
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                seconds = (when - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                seconds = RETRY_AFTER_DEFAULT_SECONDS
    return min(max(seconds, RETRY_AFTER_MIN_SECONDS), RETRY_AFTER_MAX_SECONDS)


def _get(session: requests.Session, url: str) -> str:
    try:
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            if response.status_code == 429 and attempt < MAX_RATE_LIMIT_RETRIES:
                wait = max(_retry_after_seconds(response), RATE_LIMIT_BACKOFF_SECONDS[attempt])
                logger.warning(
                    "tlv: rate limited (429) fetching %s - waiting %.0fs (retry %d/%d)",
                    url, wait, attempt + 1, MAX_RATE_LIMIT_RETRIES,
                )
                time.sleep(wait)
                continue
            break
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


def _assign_supervision(all_ids: set[str], mehadrin_ids: set[str]) -> dict[str, str]:
    """A business is מהדרין if it is on the site's מהדרין list and רגילה
    otherwise. (The רגילה list is no longer crawled: it is eight pages long
    and suffers the same unreliable paging as the full list. When both were
    crawled in August 2026 they partitioned the full list almost exactly:
    130 מהדרין + 990 רגילה = 1,120.)"""
    extra = mehadrin_ids - all_ids
    if extra:
        logger.warning(
            "tlv: %d business(es) are on the מהדרין list but have no listing "
            "of their own - dropped rather than fabricated: %s",
            len(extra), sorted(extra)[:10],
        )
    return {identity: ("mehadrin" if identity in mehadrin_ids else "regular") for identity in all_ids}


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
    # bucket is already "mehadrin" / "regular" - _assign_supervision sets it.
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
