"""Canonical `business` record shape shared by every collector.

See kosher-map-technical-spec-v0.1.md section 5.1. Every collector must
return a list of dicts matching REQUIRED_FIELDS at minimum; optional
fields may be omitted or set to None.
"""

import hashlib


def hash_identity(*parts: str) -> str:
    """Spec section 5.1's fallback for source_record_id when a source
    doesn't give a stable per-record slug: "a hash of name+address".
    Shared so every collector that needs it hashes the same way.
    """
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]

REQUIRED_FIELDS = [
    "source_id",
    "source_record_id",
    "name_raw",
    "category_canonical",
    "certifying_authority",
    "address_raw",
    "city",
    "status",
]

# A record with status="revoked" may come from a source's revocation
# feed rather than its main directory (e.g. Tel Aviv's, spec section
# 6.2) - such a feed may publish only a name and address, with no
# category for what's now a delisted business. This field stays
# required for "active"/"absent" records, where the data genuinely came
# from the source's live listing.
REQUIRED_UNLESS_REVOKED = {"category_canonical"}

OPTIONAL_FIELDS = [
    "source_url",
    "name_clean",
    "branch",
    # category_raw is usually present but not guaranteed: the Tel Aviv
    # survey (spec section 6.2, source B) found at least one active,
    # otherwise-complete listing with no business type published at all.
    # category_canonical still always gets a value (falls back to
    # "other" via each collector's mapping function), so that one stays
    # required; the raw source string doesn't.
    "category_raw",
    "kosher_type",
    "supervision_level",
    "additional_hechsher",
    # lat/lng/location_source/location_confidence are all optional
    # together: the same TLV survey found a real active listing at a
    # mall address ("קניון עזריאלי קומה 2" - no street name or number)
    # that a geocoder can't resolve to any coordinates at all. That's a
    # legitimate "we don't know where this is" state - not a scraping
    # failure - so a record must be allowed to carry no location data
    # whatsoever. See the location_source/lat consistency check in
    # validate_record for the one rule that *is* enforced: coordinates
    # never appear without a location_source explaining where they came
    # from.
    "lat",
    "lng",
    "location_source",
    "location_confidence",
    "phone",
    "supervisor_name",
    "supervisor_phone",
    "first_seen",
    "last_seen",
    "status_changed_at",
]

ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

KOSHER_TYPES = {"meat", "dairy", "parve"}
SUPERVISION_LEVELS = {"regular", "mehadrin", "badatz", "unknown"}
LOCATION_SOURCES = {"published", "geocoded"}
LOCATION_CONFIDENCES = {"high", "medium", "low"}
STATUSES = {"active", "absent", "revoked"}


class CollectorError(Exception):
    """Raised when a collector cannot retrieve a complete, trustworthy dataset.

    Per spec section 6.1, a collector must fail loudly rather than return
    partial data - callers should treat this as a failed collection_run,
    not silently ignore missing records.
    """


def validate_record(record: dict) -> None:
    """Raise CollectorError if a record is missing a required field or
    uses a value outside the canonical enums. Does not mutate the record.
    """
    is_revoked = record.get("status") == "revoked"
    required = [
        f for f in REQUIRED_FIELDS
        if not (is_revoked and f in REQUIRED_UNLESS_REVOKED)
    ]
    missing = [f for f in required if not record.get(f)]
    if missing:
        raise CollectorError(
            f"record missing required field(s) {missing}: {record!r}"
        )

    kosher_type = record.get("kosher_type")
    if kosher_type is not None:
        bad = set(kosher_type) - KOSHER_TYPES
        if bad:
            raise CollectorError(f"unknown kosher_type value(s) {bad}: {record!r}")

    supervision_level = record.get("supervision_level")
    if supervision_level is not None and supervision_level not in SUPERVISION_LEVELS:
        raise CollectorError(
            f"unknown supervision_level {supervision_level!r}: {record!r}"
        )

    location_source = record.get("location_source")
    if location_source is not None and location_source not in LOCATION_SOURCES:
        raise CollectorError(
            f"invalid location_source {location_source!r}: {record!r}"
        )
    has_coords = record.get("lat") is not None or record.get("lng") is not None
    if has_coords and location_source is None:
        raise CollectorError(
            f"record has coordinates but no location_source explaining "
            f"where they came from: {record!r}"
        )

    location_confidence = record.get("location_confidence")
    if location_confidence is not None and location_confidence not in LOCATION_CONFIDENCES:
        raise CollectorError(
            f"invalid location_confidence {location_confidence!r}: {record!r}"
        )

    status = record.get("status")
    if status not in STATUSES:
        raise CollectorError(f"invalid status {status!r}: {record!r}")
