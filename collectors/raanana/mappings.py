"""Per-source lookup tables for the Raanana (mdrn.org.il) collector.

Per spec section 5.3: sources are never forced to agree with each other.
category_raw is always stored verbatim; this table only supplies the
canonical mapping. Unmapped values are intentionally left out rather
than guessed at - the collector logs a warning for anything not listed
here so a human can extend the table.

Unlike Netanya's directory (same underlying plugin, different council
configuration - see collector.py's docstring), this source's
"directory category" field is actually a supervision-status label, not
a business type. Business type instead comes from the oddly-named
"balanit_name" field, surveyed 2026-09-17 across all 338 live listings
(31 distinct values, some semicolon-separated combinations - the
collector takes the first token as primary per that day's decision).
"""

CATEGORY_MAP = {
    "מרכולים": "grocery_supermarket",
    "מסעדות-- בשרי": "restaurant",
    "מסעדות ---חלבי---פרווה": "restaurant",
    "בתי מאפה - מכירה": "bakery_patisserie",
    "בתי מאפה - ייצור": "bakery_patisserie",
    "בתי מאפה - ייצור ומכירה": "bakery_patisserie",
    "מזנונים": "restaurant",
    "בתי קפה": "cafe",
    "ירקות ופירות": "greengrocer",
    "פיצריות": "pizzeria",
    "איטליזים": "butcher",
    "בשרים שיווק +איטליז": "butcher",
    "מפעלים": "factory",
    "קפיטריה": "restaurant",
    "קייטרינג": "catering",
    "פיצוחים": "nuts_dried_fruit",
    "חומוסיות ופלאפל": "falafel_shawarma",
    "מטבח + חדר אוכל": "institutional_kitchen",
    "אולמות -- גן אירועים": "event_hall",
    "גלידריות": "ice_cream",
    "המבורגר = שווארמה": "falafel_shawarma",
    # "ברים" ("bars") and "חנות קימעונאי / משווק" ("retail store /
    # distributor") are genuinely ambiguous - deliberately left unmapped
    # so the health alert fires and a human decides, per spec section
    # 5.3 (same treatment as Netanya's two intentionally-unmapped values).
}

# The "directory category" field's two normal values encode supervision
# level, not business type - see module docstring. Every other observed
# value (מח' בשרית, מחלקת ירקות, מחלקת פירות וירקות, פרש מרקט - מחלקת
# ירקות) is a department-split artifact of a single supermarket listing,
# not a supervision level, and is deliberately left out here so it falls
# through to supervision_level="unknown" rather than being guessed at.
SUPERVISION_MAP = {
    "כשרות רגילה בתוקף": "regular",
    "כשר למהדרין בתוקף": "mehadrin",
}

# Directory-category values meaning "do not include this record at all",
# confirmed against every live example on 2026-09-17:
# - "עסק בהמתנה לקבלת כשרות" (business awaiting kashrut certification):
#   3 live examples, all with an assigned supervisor but explicitly NOT
#   yet certified - listing them as active kosher businesses would
#   misrepresent their actual status.
# - "נוכחות בלבד" (presence only): 5 live examples - 4 are mikvehs
#   (ritual baths) and 1 is the Religious Council's own office, none of
#   them food businesses at all, regardless of certification status.
EXCLUDED_DIRECTORY_CATEGORIES = {
    "עסק בהמתנה לקבלת כשרות",
    "נוכחות בלבד",
}
