"""Collector for the Ministry of Religious Services' national kashrut
portal (m-datit), covering whichever local religious councils are asked for.

Source: https://shirathayam.m-datit.org.il/pirsumsite  (the host name
really does say "shirathayam" - it is the portal's public-listing site).
Coverage survey: docs/mdatit-coverage-rabbanut-list.docx.

Unlike the three municipal-directory collectors this is one system for
many councils, so it is one collector, not one per town. The page is an
Angular app over plain JSON:

- GET  /Data/Site/GetAuthoritiesAndCitiesForPirsum  -> RetVal[0] is the
  list of authorities (id + name), RetVal[1] the list of localities.
- POST /Data/Site/SearchPirsumSite {AuthorityID, SiteName: ""} -> every
  business of that authority in one response.

Each authority becomes its own source_id ("mdatit-<AuthorityID>") so the
change-detection "absent" logic stays scoped to one council: running the
collector for a different set of authorities later can never mark another
council's businesses as gone.

What the portal does and does not give us (2026-09-23 pilot survey):

- name, address and city; a free-text business type in `SiteDescription`
  (well filled in by some councils, mostly blank for others - see
  mappings.py); per certificate a kashrut level, a kashrut type as
  comma-joined ids (1=dairy, 2=meat, 3=parve) and an expiry date.
- Councils differ in whether they publish certificates at all: Petah
  Tikva, Rishon, Ashdod and Givat Shmuel list their supervised
  businesses but carry no certificate data, so those records have
  supervision_level="unknown" and no kosher_type - still worth listing.
- No business phone number, and no coordinates (the CoorX/CoorY fields
  exist but are empty), so addresses are geocoded like Tel Aviv's.
- Each business lists its supervisors' names and phone numbers. Those
  are private individuals' details, so this collector deliberately does
  not collect them (supervisor_name/supervisor_phone stay unset).
- Certificate expiry is not part of the canonical schema, so it is not
  stored; no expired certificate was present in the pilot data.

The server negotiates TLS in a way OpenSSL 3's default security level
rejects (browsers connect fine; Python's default context fails with a
handshake error), so requests to this one host use a context relaxed to
SECLEVEL=1. That is scoped to this host's session adapter only.

Collector contract (spec section 6.1): no database writes, no judgement
about what changed. It either returns the complete list of canonical
records for every requested authority or raises CollectorError.
"""

from __future__ import annotations

import logging
import re
import ssl
import time
from collections.abc import Iterable
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from collectors.common.geocoding import (
    GeocodeCache,
    Geocoder,
    NominatimGeocoder,
    geocode_cached,
)
from collectors.common.schema import CollectorError, validate_record
from collectors.mdatit.mappings import (
    KOSHER_TYPE_BY_ID,
    SUPERVISION_BY_LEVEL_NAME,
    SUPERVISION_RANK,
    map_category,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://shirathayam.m-datit.org.il"
SOURCE_PREFIX = "mdatit-"
SOURCE_URL = f"{BASE_URL}/pirsumsite"

USER_AGENT = (
    "KosherMapBot/0.1 (public kashrut directory aggregator; "
    "+https://github.com/avituriel-sys/kosher-map; "
    "mailto:avituriel@gmail.com)"
)

REQUEST_TIMEOUT_SECONDS = 60
POLITE_DELAY_SECONDS = 1.5

# The portal spans the whole country, so the per-city sanity box the
# single-city collectors use doesn't apply; this only catches results
# that land outside Israel altogether.
ISRAEL_BOUNDS = (29.4, 33.4, 34.2, 35.9)

DEFAULT_GEOCODE_CACHE_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "geocode_cache" / "mdatit.json"
)

_AUTHORITY_NAME_PREFIXES = ("מועצה דתית ", "מחלקת דת ", "אגף דת ")
_BAAL_RE = re.compile(r'\s*בע"מ\s*$')


class _LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        context.set_ciphers("DEFAULT@SECLEVEL=1")
        kwargs["ssl_context"] = context
        super().init_poolmanager(*args, **kwargs)


def new_session() -> requests.Session:
    session = requests.Session()
    session.mount(BASE_URL, _LegacyTLSAdapter())
    session.headers["User-Agent"] = USER_AGENT
    return session


def source_id_for(authority_id: int) -> str:
    return f"{SOURCE_PREFIX}{authority_id}"


def collect(
    authority_ids: Iterable[int],
    session: requests.Session | None = None,
    geocoder: Geocoder | None = None,
    geocode_cache_path: Path = DEFAULT_GEOCODE_CACHE_PATH,
    geocode: bool = True,
) -> list[dict]:
    """Return canonical records for every business of each requested
    authority. `geocode=False` skips address lookups (records carry no
    coordinates) - useful for a dry run that shouldn't spend 1 request per
    second per address on Nominatim.
    """
    authority_ids = list(authority_ids)
    if not authority_ids:
        raise CollectorError("no authority ids requested")

    owns_session = session is None
    session = session or new_session()
    try:
        authorities = _fetch_authorities(session)
        missing = [a for a in authority_ids if a not in authorities]
        if missing:
            raise CollectorError(
                f"authority id(s) {missing} not in the portal's authority list - "
                f"the ids changed or the list is incomplete"
            )

        sites_by_authority: dict[int, list[dict]] = {}
        for index, authority_id in enumerate(authority_ids):
            if index:
                time.sleep(POLITE_DELAY_SECONDS)
            sites_by_authority[authority_id] = _fetch_sites(session, authority_id)
    finally:
        if owns_session:
            session.close()

    cache = geocoder_instance = None
    if geocode:
        geocoder_instance = geocoder or NominatimGeocoder()
        cache = GeocodeCache(geocode_cache_path)

    records: list[dict] = []
    try:
        for authority_id in authority_ids:
            authority_name = authorities[authority_id]
            skipped = 0
            for site in sites_by_authority[authority_id]:
                record = _to_canonical_record(
                    site, authority_id, authority_name, geocoder_instance, cache
                )
                if record is None:
                    skipped += 1
                else:
                    records.append(record)
            if skipped:
                logger.warning(
                    "mdatit: %s: skipped %d listing(s) with no address (nothing to "
                    "place on a map)", authority_name, skipped,
                )
    finally:
        if cache is not None:
            cache.save()

    for record in records:
        validate_record(record)
    return records


def _post_json(session: requests.Session, path: str, payload: dict) -> object:
    url = f"{BASE_URL}{path}"
    try:
        response = session.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise CollectorError(f"failed to fetch {url}: {exc}") from exc
    return data


def _get_json(session: requests.Session, path: str) -> object:
    url = f"{BASE_URL}{path}"
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise CollectorError(f"failed to fetch {url}: {exc}") from exc
    return data


def _fetch_authorities(session: requests.Session) -> dict[int, str]:
    data = _get_json(session, "/Data/Site/GetAuthoritiesAndCitiesForPirsum")
    try:
        authorities = data["RetVal"][0]
        return {int(a["ID"]): a["Name"].strip() for a in authorities}
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise CollectorError(
            f"unexpected authority-list shape from the portal: {exc!r}"
        ) from exc


def _fetch_sites(session: requests.Session, authority_id: int) -> list[dict]:
    data = _post_json(
        session, "/Data/Site/SearchPirsumSite",
        {"AuthorityID": authority_id, "SiteName": ""},
    )
    sites = data.get("RetVal") if isinstance(data, dict) else None
    if not isinstance(sites, list):
        raise CollectorError(
            f"authority {authority_id}: unexpected response shape (no RetVal list)"
        )
    if not sites:
        raise CollectorError(
            f"authority {authority_id}: zero businesses returned; refusing to "
            f"treat an empty list as a complete census"
        )
    ids = [s.get("ID") for s in sites]
    if len(set(ids)) != len(ids):
        raise CollectorError(f"authority {authority_id}: duplicate site ids in response")
    return sites


def _authority_city(authority_name: str) -> str:
    for prefix in _AUTHORITY_NAME_PREFIXES:
        if authority_name.startswith(prefix):
            return authority_name[len(prefix):].strip()
    return authority_name


def _clean_address(address: str | None) -> str:
    return " ".join((address or "").split())


def _certificate_facts(certificates: list[dict]) -> tuple[list[str] | None, str]:
    """(kosher_type, supervision_level) across a site's certificates."""
    if not certificates:
        return None, "unknown"

    kosher_types: set[str] = set()
    level = "unknown"
    for cert in certificates:
        type_name = ((cert.get("KosherType") or {}).get("Name") or "")
        for code in (c.strip() for c in type_name.split(",") if c.strip()):
            canonical = KOSHER_TYPE_BY_ID.get(code)
            if canonical is None:
                logger.warning("mdatit: unknown KosherType code %r - extend KOSHER_TYPE_BY_ID", code)
            else:
                kosher_types.add(canonical)

        level_name = ((cert.get("KosherLevel") or {}).get("Name") or "").strip()
        canonical_level = SUPERVISION_BY_LEVEL_NAME.get(level_name)
        if canonical_level is None:
            logger.warning(
                "mdatit: unmapped KosherLevel %r treated as unknown - extend "
                "SUPERVISION_BY_LEVEL_NAME", level_name,
            )
            canonical_level = "unknown"
        if SUPERVISION_RANK[canonical_level] > SUPERVISION_RANK[level]:
            level = canonical_level

    return (sorted(kosher_types) or None), level


def _to_canonical_record(
    site: dict,
    authority_id: int,
    authority_name: str,
    geocoder: Geocoder | None,
    cache: GeocodeCache | None,
) -> dict | None:
    name_raw = (site.get("SiteName") or "").strip()
    address = _clean_address(site.get("SiteAddress"))
    if not name_raw:
        raise CollectorError(f"authority {authority_id}: site {site.get('ID')} has no name")
    if not address:
        return None

    city = (site.get("CityName") or "").strip() or _authority_city(authority_name)
    description = (site.get("SiteDescription") or "").strip()
    category, matched = map_category(description, name_raw)
    if description and not matched:
        logger.warning(
            "mdatit: unmapped business type %r (%s) - extend "
            "DESCRIPTION_KEYWORDS (health alert per spec section 5.3)",
            description, authority_name,
        )

    kosher_type, supervision_level = _certificate_facts(
        site.get("lstKosherCertificateIssued") or []
    )

    lat = lng = confidence = None
    if geocoder is not None and cache is not None:
        geo = geocode_cached(
            f"{address}, {city}, ישראל", geocoder, cache, ISRAEL_BOUNDS
        )
        lat, lng, confidence = geo.lat, geo.lng, geo.confidence

    return {
        "source_id": source_id_for(authority_id),
        "source_record_id": str(site["ID"]),
        "source_url": SOURCE_URL,
        "name_raw": name_raw,
        "name_clean": _BAAL_RE.sub("", name_raw).strip(),
        "branch": None,
        "category_raw": description or None,
        "category_canonical": category,
        "kosher_type": kosher_type,
        "supervision_level": supervision_level,
        "certifying_authority": authority_name,
        "additional_hechsher": None,
        "address_raw": address,
        "city": city,
        "lat": lat,
        "lng": lng,
        "location_source": "geocoded" if lat is not None else None,
        "location_confidence": confidence if lat is not None else None,
        "phone": None,
        "supervisor_name": None,
        "supervisor_phone": None,
        "status": "active",
    }


if __name__ == "__main__":
    import json
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ids = [int(a) for a in sys.argv[1:]] or [80]
    try:
        results = collect(ids, geocode=False)
    except CollectorError as exc:
        print(f"COLLECTOR FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"collected {len(results)} records", file=sys.stderr)
    json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
