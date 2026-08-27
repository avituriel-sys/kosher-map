"""Pluggable geocoding with on-disk caching, per spec section 7.

- Geocode each distinct address string once, ever - results are cached
  on disk keyed on the exact input string.
- location_confidence is "high" for an exact street-number match,
  "medium" for street-level only, "low" for anything coarser.
- A result that is "low" confidence, or falls outside a sanity bounding
  box for the relevant city, is not something this module hides - it
  still returns it, with confidence "low" and/or out_of_bounds=True, so
  the caller can route it to a manual review queue rather than silently
  either trusting it or dropping it (spec section 7).

The provider is swappable: Geocoder is the abstraction, NominatimGeocoder
is the only implementation today (free/open, matches the project's
OpenStreetMap-based approach elsewhere - see spec section 4).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

# Nominatim's usage policy (https://operations.osmfoundation.org/policies/nominatim/)
# caps unauthenticated use at 1 request/second and requires a real
# identifying User-Agent.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = (
    "KosherMapBot/0.1 (public kashrut directory aggregator; "
    "+https://github.com/avituriel-sys/kosher-map; "
    "mailto:avituriel@gmail.com)"
)
NOMINATIM_MIN_INTERVAL_SECONDS = 1.0

# A production bulk backfill (~1000+ addresses at 1/second) found the
# public Nominatim instance is genuinely flaky under that kind of
# sustained load: the exact same query, retried seconds later, went
# from a zero-result response to a real match. Confirmed with a raw
# HTTP body of "[]" (not an error, not a block) followed by a normal
# 200 with a match on retry. Since geocode_cached (below) caches
# whatever comes back from here - including "no result" - forever,
# a few retries here matter: without them, ordinary service flakiness
# gets permanently misfiled as "this address doesn't exist."
NOMINATIM_EMPTY_RESULT_RETRIES = 2
NOMINATIM_RETRY_BACKOFF_SECONDS = 2.0


@dataclass
class GeocodeResult:
    lat: float | None
    lng: float | None
    confidence: str | None  # "high" | "medium" | "low" | None (failed)
    out_of_bounds: bool


class Geocoder:
    """Abstract geocoding provider. Swap implementations without
    touching collector code.
    """

    def geocode(self, query: str) -> GeocodeResult:
        raise NotImplementedError


class NominatimGeocoder(Geocoder):
    def __init__(self, session: requests.Session | None = None):
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = NOMINATIM_USER_AGENT
        self._last_request_at: float = 0.0

    def geocode(self, query: str) -> GeocodeResult:
        attempts = NOMINATIM_EMPTY_RESULT_RETRIES + 1
        for attempt in range(attempts):
            results = self._search(query)
            if results:
                break
            if attempt < attempts - 1:
                time.sleep(NOMINATIM_RETRY_BACKOFF_SECONDS)
        if not results:
            return GeocodeResult(lat=None, lng=None, confidence=None, out_of_bounds=False)

        top = results[0]
        lat, lng = float(top["lat"]), float(top["lon"])
        address = top.get("address", {})
        if address.get("house_number"):
            confidence = "high"
        elif address.get("road"):
            confidence = "medium"
        else:
            confidence = "low"
        return GeocodeResult(lat=lat, lng=lng, confidence=confidence, out_of_bounds=False)

    def _search(self, query: str) -> list[dict]:
        self._respect_rate_limit()
        response = self._session.get(
            NOMINATIM_URL,
            params={
                "q": query,
                "format": "jsonv2",
                "addressdetails": 1,
                "limit": 1,
            },
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < NOMINATIM_MIN_INTERVAL_SECONDS:
            time.sleep(NOMINATIM_MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()


class GeocodeCache:
    """On-disk cache keyed on the exact query string, per spec section 7
    ("geocode each distinct address once, ever - never on every run").
    """

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict] = {}
        if path.exists():
            self._data = json.loads(path.read_text(encoding="utf-8"))

    def get(self, query: str) -> GeocodeResult | None:
        raw = self._data.get(query)
        if raw is None:
            return None
        return GeocodeResult(**raw)

    def set(self, query: str, result: GeocodeResult) -> None:
        self._data[query] = asdict(result)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def geocode_cached(
    query: str, geocoder: Geocoder, cache: GeocodeCache, bounds: tuple[float, float, float, float]
) -> GeocodeResult:
    """Geocode `query`, using and populating `cache`. `bounds` is
    (min_lat, max_lat, min_lng, max_lng) - a sanity box for the city
    being collected; a result outside it is flagged out_of_bounds rather
    than trusted (spec section 7: "resolving outside a sanity bounding
    box... goes into a manual review queue rather than onto the map").

    Saves the cache to disk immediately after every genuinely new lookup
    (not on cache hits). A first-time backfill can mean hundreds of
    real geocoder calls at ~1/second; without saving as it goes, killing
    or crashing the process partway through would silently discard
    every lookup done so far and force starting over from nothing.
    """
    cached = cache.get(query)
    if cached is not None:
        return cached

    result = geocoder.geocode(query)
    if result.lat is not None and result.lng is not None:
        min_lat, max_lat, min_lng, max_lng = bounds
        if not (min_lat <= result.lat <= max_lat and min_lng <= result.lng <= max_lng):
            result = GeocodeResult(
                lat=result.lat, lng=result.lng, confidence="low", out_of_bounds=True
            )

    cache.set(query, result)
    cache.save()
    return result
