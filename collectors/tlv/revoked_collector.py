"""Collector for the Tel Aviv-Yafo revoked-certification feed.

Source: https://rabanut.co.il/עסקים-שכשרותם-הוסרה/
Spec: kosher-map-technical-spec-v0.1.md section 6.2: "Second collector
required for the same source... Collect it as its own feed and use it to
set status = revoked. This is high-value data and should be treated as a
first-class feature, not an afterthought."

Site survey (2026-08-27): a single unpaginated page, 14 businesses at the
time of the survey. Each card gives only a name and a bare street
address - no business type, no phone, no permalink to a business detail
page (the card's href is a dead "#" - clicking it does nothing). Because
there's no permalink there's no stable slug, so source_record_id here
follows the spec's explicit fallback (section 5.1): a hash of name +
address. There is a per-business removal date shown on some cards, but
it's frequently a 01.01.1970 placeholder (epoch zero, i.e. "no real date
recorded") rather than a genuine one, and the canonical schema has no
field for a source-published revocation date anyway - status_changed_at
is defined as something spec section 6.3's shared change-detection logic
sets from our own run history, not copied from a source. So that date is
intentionally not collected here.

No coordinates are geocoded for this feed. Per spec section 6.3, revoked
businesses must stay visible in the data export and reachable in the
interface but must not appear as ordinary map pins - so precise
coordinates for what may now be a closed or relocated business aren't
needed to satisfy that requirement.

Reconciling a revoked-feed entry against an existing *active* record
from collector.py (so the shared record's status flips to "revoked"
instead of a duplicate row being created) is exactly the kind of
cross-run matching spec section 6.3 assigns to shared change-detection
logic, written once for every source - not to an individual collector.
This module only returns what the feed itself publishes.

Collector contract (spec section 6.1): performs no database writes and
makes no judgement about what changed. Returns a complete list or raises
CollectorError - never a partial list.
"""

from __future__ import annotations

import logging

import requests
from bs4 import BeautifulSoup

from collectors.common.schema import CollectorError, hash_identity, validate_record

logger = logging.getLogger(__name__)

SOURCE_ID = "tlv"
CERTIFYING_AUTHORITY = "המועצה הדתית תל אביב-יפו"
CITY = "תל אביב-יפו"
REVOKED_URL = "https://rabanut.co.il/%d7%a2%d7%a1%d7%a7%d7%99%d7%9d-%d7%a9%d7%9b%d7%a9%d7%a8%d7%95%d7%aa%d7%9d-%d7%94%d7%95%d7%a1%d7%a8%d7%94/"

USER_AGENT = (
    "KosherMapBot/0.1 (public kashrut directory aggregator; "
    "+https://github.com/avituriel-sys/kosher-map; "
    "mailto:avituriel@gmail.com)"
)
REQUEST_TIMEOUT_SECONDS = 30


def collect_revoked(session: requests.Session | None = None) -> list[dict]:
    """Fetch every currently published Tel Aviv-Yafo revoked-certification
    listing. Returns a list of canonical `business` records with
    status="revoked". Raises CollectorError on any fetch failure or if
    the page's markup doesn't match what the survey found.
    """
    owns_session = session is None
    session = session or requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    try:
        html = _get(session, REVOKED_URL)
    finally:
        if owns_session:
            session.close()

    soup = BeautifulSoup(html, "lxml")
    cards = _parse_cards(soup)
    # An empty revoked list is a plausible real state (no businesses
    # currently revoked) - unlike the active directory, zero here is not
    # on its own proof of a broken collector. Still worth flagging.
    if not cards:
        logger.warning("tlv revoked feed returned zero entries - confirm this is expected")

    records = [_to_canonical_record(c) for c in cards]
    for record in records:
        validate_record(record)
    return records


def _get(session: requests.Session, url: str) -> str:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise CollectorError(f"failed to fetch {url}: {exc}") from exc
    return response.text


def _parse_cards(soup: BeautifulSoup) -> list[tuple[str, str]]:
    cards = []
    for item in soup.select('div.post_col[data-type="is-not-kosher"]'):
        title = item.select_one(".post_title")
        name_raw = title.get_text(strip=True) if title else ""
        address_el = item.select_one(".wrap_address")
        address_raw = address_el.get_text(strip=True) if address_el else ""
        if not name_raw or not address_raw:
            raise CollectorError(
                "a revoked-listing card is missing a name or address - markup changed"
            )
        cards.append((name_raw, address_raw))
    return cards


def _to_canonical_record(card: tuple[str, str]) -> dict:
    name_raw, address_raw = card
    return {
        "source_id": SOURCE_ID,
        "source_record_id": hash_identity(name_raw, address_raw),
        "source_url": None,
        "name_raw": name_raw,
        "name_clean": name_raw.strip(),
        "branch": None,
        "category_raw": None,
        "category_canonical": None,
        "kosher_type": None,
        "supervision_level": None,
        "certifying_authority": CERTIFYING_AUTHORITY,
        "additional_hechsher": None,
        "address_raw": address_raw,
        "city": CITY,
        "lat": None,
        "lng": None,
        "location_source": None,
        "location_confidence": None,
        "phone": None,
        "supervisor_name": None,
        "supervisor_phone": None,
        "status": "revoked",
    }


if __name__ == "__main__":
    import json
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        results = collect_revoked()
    except CollectorError as exc:
        print(f"COLLECTOR FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"collected {len(results)} revoked records", file=sys.stderr)
    json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
