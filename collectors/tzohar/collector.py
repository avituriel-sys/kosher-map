"""Collector for Tzohar's (כשרות צהר) restaurant finder.

Source page: https://www.tzohar.org.il/?page_id=18700 . Unlike the municipal
directories this is a private hechsher's finder built on the WP Store
Locator plugin, which answers plain JSON:

    GET /wp-admin/admin-ajax.php?action=store_search&lat=..&lng=..
        &max_results=..&search_radius=..

Each store: id, store (HTML-escaped name), address, city, lat, lng,
terms (חלבי / בשרי / פרווה), address2 (link to the certificate PDF).
Coordinates are published, so nothing is geocoded. No phone numbers.

The server silently caps the search radius at about 25 km, so a census
needs a grid of circles across the country (CENTRES below, ~35 requests),
deduplicated by store id.

PERMISSION GATE - read before running. Tzohar's terms of use
(https://www.tzohar.org.il/?page_id=50581) prohibit automated search,
scanning, copying or retrieval of the site's content and building a
database from it without explicit prior written consent. `collect()`
therefore refuses to make any request unless the environment variable
TZOHAR_PERMISSION_CONFIRMED=1 is set, which should only happen once
that written consent exists. The parsing half (`parse_stores`,
`to_canonical_record`) makes no requests and is fully tested offline.

Supervision: Tzohar is a private hechsher, not the regular / mehadrin /
badatz scale, so supervision_level is "unknown" and certifying_authority
is "כשרות צהר". The map should describe these as "under Tzohar
supervision", not "kosher-certified" (their certificates avoid the word
"kosher" for trademark reasons). The list of businesses Tzohar stopped
supervising is not collected yet.

Collector contract (spec section 6.1): no database writes, no judgement
about what changed. It either returns the complete list of canonical
records or raises CollectorError.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time

import requests

from collectors.common.category_keywords import map_category
from collectors.common.schema import CollectorError, validate_record

logger = logging.getLogger(__name__)

SOURCE_ID = "tzohar"
CERTIFYING_AUTHORITY = "כשרות צהר"
PAGE_URL = "https://www.tzohar.org.il/?page_id=18700"
AJAX_URL = "https://www.tzohar.org.il/wp-admin/admin-ajax.php"

PERMISSION_ENV_VAR = "TZOHAR_PERMISSION_CONFIRMED"

USER_AGENT = (
    "KosherMapBot/0.1 (public kashrut directory aggregator; "
    "+https://github.com/avituriel-sys/kosher-map; "
    "mailto:avituriel@gmail.com)"
)

REQUEST_TIMEOUT_SECONDS = 60
POLITE_DELAY_SECONDS = 3.0

SEARCH_RADIUS_KM = 25
# Asked for per circle. If a response comes back with exactly this many
# stores the answer may have been truncated, so the run fails rather than
# quietly dropping the rest.
MAX_RESULTS = 500

ISRAEL_BOUNDS = (29.4, 33.4, 34.2, 35.9)  # min_lat, max_lat, min_lng, max_lng

# Circle centres, ~35 km apart so 25 km circles overlap with no gaps over
# the populated parts of the country (overlap is removed by store id).
CENTRES: list[tuple[float, float]] = [
    # Galilee and the north
    (33.21, 35.57), (32.96, 35.50), (32.80, 35.00), (32.79, 35.53),
    (32.70, 35.30), (32.60, 35.57), (32.92, 35.30),
    # Coastal plain
    (32.82, 34.98), (32.60, 34.93), (32.45, 34.90), (32.33, 34.86),
    (32.17, 34.85), (32.08, 34.78), (31.95, 34.80), (31.80, 34.75),
    (31.67, 34.58), (31.52, 34.60),
    # Inland centre and Jerusalem area
    (32.25, 35.00), (32.10, 35.00), (32.00, 34.90), (31.90, 35.00),
    (31.78, 35.21), (31.68, 35.00), (31.55, 35.10), (32.20, 35.28),
    # Dead Sea, Negev and Arava
    (31.25, 34.80), (31.25, 35.20), (31.25, 34.40), (30.85, 34.78),
    (30.60, 34.80), (30.20, 35.00), (29.56, 34.95), (31.78, 35.45),
]

# Tzohar publishes no business type, so the category comes from the name.
# The shared name table covers the obvious words; these are extra, Tzohar-
# specific words (school canteens, café chains, bakeries, resorts) checked
# only when the shared table finds nothing. Wineries and similar stay
# "other" on purpose - they are not eateries.
_EXTRA_NAME_KEYWORDS = [
    ("hotel", ["מלון", "אירוח כפרי", "כפר הנופש", "אתרי לינה"]),
    ("institutional_kitchen", ["חדר אוכל", "מזנון", "בריא בריבוע", "מטבח קיבוץ", "דיור מוגן"]),
    ("butcher", ["קצביה", "קצביית", "מעדני בשר"]),
    ("fishmonger", ["דגים"]),
    ("greengrocer", ["פירות וירקות"]),
    ("bakery_patisserie", ["פטיסרי", "בייקרי", "עוגות", "בייגלס", "מאפייה", "מאפיית"]),
    ("cafe", ["קפיטריה", "ארומה", "רוסטרס", "קופי", "קפאינה", "בית התה", "פיקה", "Fika"]),
    ("restaurant", ["פודטראק", "ביסטרו", "קולינרי"]),
]


def category_for_name(name: str) -> str:
    category, matched = map_category(None, name)
    if matched:
        return category
    for canonical, words in _EXTRA_NAME_KEYWORDS:
        if any(w in name for w in words):
            return canonical
    return "other"


_TERM_BY_WORD ={"חלבי": "dairy", "בשרי": "meat", "פרווה": "parve", "פרוה": "parve"}
_TAG_RE = re.compile(r"<[^>]+>")


def permission_confirmed() -> bool:
    return os.environ.get(PERMISSION_ENV_VAR) == "1"


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def collect(
    session: requests.Session | None = None,
    centres: list[tuple[float, float]] | None = None,
) -> list[dict]:
    """Return canonical records for every store Tzohar lists."""
    if not permission_confirmed():
        raise CollectorError(
            f"Tzohar's terms of use forbid automated retrieval without written "
            f"permission; set {PERMISSION_ENV_VAR}=1 only once that consent exists"
        )

    centres = centres if centres is not None else CENTRES
    owns_session = session is None
    session = session or new_session()
    stores_by_id: dict[str, dict] = {}
    try:
        for index, (lat, lng) in enumerate(centres):
            if index:
                time.sleep(POLITE_DELAY_SECONDS)
            for store in _fetch_circle(session, lat, lng):
                stores_by_id.setdefault(str(store["id"]), store)
    finally:
        if owns_session:
            session.close()

    return parse_stores(list(stores_by_id.values()))


def _fetch_circle(session: requests.Session, lat: float, lng: float) -> list[dict]:
    params = {
        "action": "store_search",
        "lat": lat,
        "lng": lng,
        "max_results": MAX_RESULTS,
        "search_radius": SEARCH_RADIUS_KM,
    }
    try:
        response = session.get(AJAX_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise CollectorError(f"failed to fetch Tzohar stores near {lat},{lng}: {exc}") from exc

    # WP Store Locator answers a circle with no stores as an empty string
    # (or 0 / null), not an empty list - still a complete, valid answer.
    if data in ("", None, 0, False):
        return []
    if not isinstance(data, list):
        raise CollectorError(
            f"unexpected response shape near {lat},{lng} (expected a JSON list): "
            f"{str(data)[:200]!r}"
        )
    if len(data) >= MAX_RESULTS:
        raise CollectorError(
            f"circle near {lat},{lng} returned {len(data)} stores, the requested "
            f"maximum - the answer may be truncated; use a smaller circle"
        )
    return data


def parse_stores(stores: list[dict]) -> list[dict]:
    """Turn raw store dicts (already deduplicated or not) into validated
    canonical records. Makes no requests."""
    records: dict[str, dict] = {}
    skipped = 0
    for store in stores:
        record = to_canonical_record(store)
        if record is None:
            skipped += 1
            continue
        existing = records.get(record["source_record_id"])
        if existing is not None and existing != record:
            raise CollectorError(
                f"store {record['source_record_id']} came back with differing "
                f"details from different circles: {existing!r} vs {record!r}"
            )
        records[record["source_record_id"]] = record

    if skipped:
        logger.warning(
            "tzohar: skipped %d store(s) with no name, address or city",
            skipped,
        )
    result = merge_duplicate_records(list(records.values()))
    if not result:
        raise CollectorError("Tzohar returned zero usable stores; refusing to treat as a census")
    for record in result:
        validate_record(record)
    return result


def _name_tokens(name: str) -> frozenset[str]:
    """Lower-cased word set of a business name, ignoring punctuation, so
    'יקב פאמילי באום ווינרי' and 'פאמילי באום ווינרי Family Baum Winery'
    can be compared."""
    stripped = re.sub(r"['\"״׳]", "", name.lower())
    return frozenset(re.findall(r"\w+", stripped))


def _address_key(record: dict) -> tuple[str, str]:
    squash = lambda s: re.sub(r"[\s\-–]+", "", s or "").lower()
    return squash(record["address_raw"]), squash(record["city"])


def find_duplicate_clusters(records: list[dict]) -> list[list[dict]]:
    """Group records that are the same business listed more than once.

    Tzohar lists a business once per certificate, so the same place turns up
    several times, often under slightly different names. Two records are the
    same business when they share an address and city AND one name's words
    are all contained in the other's ('אוסול' / 'אוסול ראש פינה').
    Different businesses in one building (a tower with three cafés, a kibbutz
    resort and its kitchen) share an address but not their name words, so
    they stay separate. Returns only clusters of two or more records.
    """
    tokens = [_name_tokens(r["name_raw"]) for r in records]
    parent = list(range(len(records)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # "Same place" means the same address text or the same published
    # coordinates (the address text sometimes differs only by a repeated
    # town name); the name rule below decides whether it is the same business.
    buckets: dict[tuple, list[int]] = {}
    for i, record in enumerate(records):
        buckets.setdefault(("addr",) + _address_key(record), []).append(i)
        if record.get("lat") is not None and record.get("lng") is not None:
            buckets.setdefault(
                ("geo", round(float(record["lat"]), 5), round(float(record["lng"]), 5)), []
            ).append(i)

    for indexes in buckets.values():
        for x in range(len(indexes)):
            for y in range(x + 1, len(indexes)):
                a, b = tokens[indexes[x]], tokens[indexes[y]]
                if a and b and (a <= b or b <= a):
                    parent[find(indexes[x])] = find(indexes[y])

    members: dict[int, list[dict]] = {}
    for i, record in enumerate(records):
        members.setdefault(find(i), []).append(record)
    return [c for c in members.values() if len(c) > 1]


def _id_order(record: dict):
    sid = record["source_record_id"]
    return (0, int(sid)) if sid.isdigit() else (1, sid)


def pick_survivor(cluster: list[dict]) -> dict:
    """The record that stands for a cluster: the lowest (oldest) store id, so
    the id stays stable from one run to the next."""
    return min(cluster, key=_id_order)


def merge_duplicate_records(records: list[dict]) -> list[dict]:
    """Collapse each duplicate cluster into its survivor, taking the most
    descriptive (longest) name and the union of the kosher types. The other
    ids simply stop being listed, so change detection marks them absent."""
    dropped: set[str] = set()
    merged: dict[str, dict] = {}
    for cluster in find_duplicate_clusters(records):
        survivor = dict(pick_survivor(cluster))
        longest = max(cluster, key=lambda r: len(r["name_raw"]))
        survivor["name_raw"] = survivor["name_clean"] = longest["name_raw"]
        types = {t for r in cluster for t in (r["kosher_type"] or [])}
        survivor["kosher_type"] = sorted(types) or None
        merged[survivor["source_record_id"]] = survivor
        dropped.update(r["source_record_id"] for r in cluster)
        logger.info(
            "tzohar: merged %d duplicate listings of %r (ids %s)",
            len(cluster), survivor["name_raw"],
            sorted((r["source_record_id"] for r in cluster), key=lambda s: (len(s), s)),
        )
    result = []
    for record in records:
        sid = record["source_record_id"]
        if sid in merged:
            result.append(merged[sid])
        elif sid not in dropped:
            result.append(record)
    return result


def _clean(text: object) -> str:
    return " ".join(html.unescape(_TAG_RE.sub("", str(text or ""))).split())


def _to_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _kosher_types(terms: str) -> list[str] | None:
    found = {canonical for word, canonical in _TERM_BY_WORD.items() if word in terms}
    if terms and not found:
        logger.warning("tzohar: unrecognised terms %r - extend _TERM_BY_WORD", terms)
    return sorted(found) or None


def to_canonical_record(store: dict) -> dict | None:
    if store.get("id") in (None, ""):
        raise CollectorError(f"store without an id: {store!r}")
    name_raw = _clean(store.get("store"))
    if not name_raw:
        logger.warning("tzohar: store %s has no name - skipped", store["id"])
        return None

    address = _clean(store.get("address"))
    city = _clean(store.get("city"))
    lat, lng = _to_float(store.get("lat")), _to_float(store.get("lng"))
    in_israel = (
        lat is not None and lng is not None
        and ISRAEL_BOUNDS[0] <= lat <= ISRAEL_BOUNDS[1]
        and ISRAEL_BOUNDS[2] <= lng <= ISRAEL_BOUNDS[3]
    )
    if not address or not city:
        return None
    if not in_israel:
        lat = lng = None

    certificate = _clean(store.get("address2"))
    source_url = certificate if certificate.startswith("http") else PAGE_URL
    category = category_for_name(name_raw)

    return {
        "source_id": SOURCE_ID,
        "source_record_id": str(store["id"]),
        "source_url": source_url,
        "name_raw": name_raw,
        "name_clean": name_raw,
        "branch": None,
        "category_raw": None,
        "category_canonical": category,
        "kosher_type": _kosher_types(_clean(store.get("terms"))),
        "supervision_level": "unknown",
        "certifying_authority": CERTIFYING_AUTHORITY,
        "additional_hechsher": None,
        "address_raw": address,
        "city": city,
        "lat": lat,
        "lng": lng,
        "location_source": "published" if lat is not None else None,
        "location_confidence": "high" if lat is not None else None,
        "phone": None,
        "supervisor_name": None,
        "supervisor_phone": None,
        "status": "active",
    }
