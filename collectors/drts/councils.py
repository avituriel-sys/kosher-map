"""Per-council configuration for the generic drts-plugin collector.

Each council configures the plugin's custom fields differently (the
Raanana lesson), so what a field *means* lives here, one entry per
council, surveyed by reading the live site. An unmapped value is never
guessed at: supervision falls back to "unknown" and a business type falls
back to "other", each with a logged warning so a human can extend this.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from collectors.common.category_keywords import map_category

logger = logging.getLogger(__name__)

Fields = dict[str, str]


@dataclass(frozen=True)
class CouncilConfig:
    source_id: str
    authority: str        # certifying_authority
    default_city: str
    base_url: str
    supervision: Callable[[Fields], str]
    # fields -> ["meat", "dairy", "parve"] subset, or None if not published
    kosher_type: Callable[[Fields], list[str] | None]
    # (fields, name) -> (category_canonical, matched)
    category: Callable[[Fields, str], tuple[str, bool]]
    # data-name of the field holding the raw business-type text, if any
    category_field: str | None = None
    # (name, fields) -> True to leave the listing out entirely
    is_excluded: Callable[[str, Fields], bool] = lambda name, fields: False
    city_normalize: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------- Givat Shmuel
# mdgs.org.il/directory-kashrut/ (surveyed 2026-09-24, 65 listings, 1 page).
# Three separate fields, one of them oddly named:
#   entity_field_field_type_of_supervision  "כשרות רגילה [- note]" / "כשרות מהדרין [- note]"
#   entity_field_field_balanit_name         kosher TYPE: "בשרי / חלבי / פרווה [- note]"
#   entity_field_field_gabaddress           business type (free text): "מסעדה", "בית קפה" ...
GS_SUPERVISION_FIELD = "entity_field_field_type_of_supervision"
GS_KOSHER_TYPE_FIELD = "entity_field_field_balanit_name"
GS_CATEGORY_FIELD = "entity_field_field_gabaddress"

_GS_SUPERVISION_PREFIXES = (("כשרות מהדרין", "mehadrin"), ("כשרות רגילה", "regular"))


def _gs_supervision(fields: Fields) -> str | None:
    value = fields.get(GS_SUPERVISION_FIELD, "")
    for prefix, level in _GS_SUPERVISION_PREFIXES:
        if value.startswith(prefix):
            return level
    logger.warning("givat_shmuel: unmapped supervision %r treated as unknown", value)
    return "unknown"


def _gs_kosher_type(fields: Fields) -> list[str] | None:
    value = fields.get(GS_KOSHER_TYPE_FIELD, "")
    types = [
        canonical for word, canonical in (("בשרי", "meat"), ("חלבי", "dairy"), ("פרווה", "parve"))
        if word in value
    ]
    if value and not types:
        logger.warning("givat_shmuel: kosher type %r not understood - left unset", value)
    return types or None


def _gs_category(fields: Fields, name: str) -> tuple[str, bool]:
    return map_category(fields.get(GS_CATEGORY_FIELD), name)


GIVAT_SHMUEL = CouncilConfig(
    source_id="givat_shmuel",
    authority="המועצה הדתית גבעת שמואל",
    default_city="גבעת שמואל",
    base_url="https://mdgs.org.il/directory-kashrut/",
    supervision=_gs_supervision,
    kosher_type=_gs_kosher_type,
    category=_gs_category,
    category_field=GS_CATEGORY_FIELD,
)


# --------------------------------------------------------------- Petah Tikva
# mpt.org.il/directory-kashrut/ (surveyed 2026-09-24, ~580 listings, 3 pages).
# The only classifying field is entity_field_directory_category, a
# supervision-STATUS label. There is no business-type or kosher-type field
# at all, so category comes from the name alone (mostly "other") and
# kosher_type stays unset rather than being guessed.
PT_STATUS_FIELD = "entity_field_directory_category"

PT_SUPERVISION_MAP = {
    "כשרות רגילה בתוקף": "regular",
    "כשר למהדרין בתוקף": "mehadrin",
    "כשר למהדרין - השכרת כלים": "mehadrin",  # a dish-rental business
    "כשרות רגילה - חלבי , פרווה": "regular",
    # Present on the live site but their meaning isn't confirmed, so they
    # are deliberately "unknown" rather than mapped to a level:
    "הידור הכשרות": "unknown",
    "פיקוח בלבד": "unknown",
    "דוח אישי": "unknown",
    "קונדיטוריה בלבד!": "unknown",
    "מחלקת ירקות ומחלקת אפיה - בלבד": "unknown",
    'כשר לפסח תשפ"ו': "unknown",
}
# Same decision as Raanana: a business still awaiting certification is not
# yet certified, so listing it as an active kosher business would mislead.
PT_EXCLUDED_STATUSES = {"עסק בהמתנה לקבלת כשרות"}
# "דוח אישי - <person's name>" listings (a "personal report" filed under an
# individual's name) are people, not public eateries - 6 of them on the
# 2026-09-24 survey, most with no street address - so they are left out.
_PERSONAL_REPORT_RE = re.compile(r'דו"?ח אישי')


def _pt_is_excluded(name: str, fields: Fields) -> bool:
    return (
        fields.get(PT_STATUS_FIELD, "") in PT_EXCLUDED_STATUSES
        or bool(_PERSONAL_REPORT_RE.search(name))
    )


def _pt_supervision(fields: Fields) -> str:
    status = fields.get(PT_STATUS_FIELD, "")
    level = PT_SUPERVISION_MAP.get(status)
    if level is None:
        logger.warning("petah_tikva: unmapped status %r treated as unknown", status)
        return "unknown"
    return level


PETAH_TIKVA = CouncilConfig(
    source_id="petah_tikva",
    authority="המועצה הדתית פתח תקווה",
    default_city="פתח תקווה",
    base_url="https://mpt.org.il/directory-kashrut/",
    supervision=_pt_supervision,
    kosher_type=lambda fields: None,
    category=lambda fields, name: map_category(None, name),
    category_field=None,
    city_normalize={"Petah Tikva": "פתח תקווה"},
    is_excluded=_pt_is_excluded,
)


COUNCILS = {"givat_shmuel": GIVAT_SHMUEL, "petah_tikva": PETAH_TIKVA}
