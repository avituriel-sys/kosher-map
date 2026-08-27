"""Per-source lookup tables for the Netanya (mdn.org.il) collector.

Per spec section 5.3: sources are never forced to agree with each other.
category_raw is always stored verbatim; this table only supplies the
canonical mapping. Unmapped values are intentionally left out rather than
guessed at - the collector logs a warning for anything not listed here so
a human can extend the table. As of the 2026-08-11 site survey there were
24 distinct category_raw values; 22 are mapped below and 2 are left
unmapped on purpose (genuinely ambiguous, see comments).
"""

CATEGORY_MAP = {
    "מסעדות/מזנונים": "restaurant",
    "מסעדות/מזנונים - פיצריות": "pizzeria",
    "מסעדות/מזנונים - בתי קפה": "cafe",
    "מסעדות/מזנונים - שווארמיה": "falafel_shawarma",
    "מסעדות/מזנונים - פלאפליה": "falafel_shawarma",
    "מסעדות/מזנונים - גלידריות": "ice_cream",
    "מסעדות/מזנונים - מטבח אסיאתי": "sushi_asian",
    "מסעדות/מזנונים - שייקים": "cafe",
    "מאפיות / קונדיטוריות": "bakery_patisserie",
    "מאפיות/קונדיטוריות - בתי קפה": "bakery_patisserie",
    "קייטרינג": "catering",
    "מרכולים": "grocery_supermarket",
    "חנויות מזון": "grocery_supermarket",
    "חנויות מזון - מעדניות": "delicatessen",
    "חנויות מזון - איטליזים": "butcher",
    "חנויות מזון - דגים": "fishmonger",
    "חנויות מזון - פירות וירקות": "greengrocer",
    "חנויות מזון - פיצוחים": "nuts_dried_fruit",
    "מפעלים": "factory",
    "בתי מלון": "hotel",
    "אולמות אירועים": "event_hall",
    "מטבח מוסדי": "institutional_kitchen",
    # "בר אקטיבי" ("active bar"?) and "מטבח קצה" ("edge/satellite kitchen"?)
    # are genuinely ambiguous - deliberately left unmapped so the health
    # alert fires and a human decides, per spec section 5.3.
}

# סוג השגחה (supervision level) -> canonical enum. Spec section 6.2.
SUPERVISION_MAP = {
    "רגילה": "regular",
    "מהדרין": "mehadrin",
    'בד"ץ בהידור הכשרות': "badatz",
    "בד”ץ בהידור הכשרות": "badatz",  # curly-quote variant, seen in the wild
}

# סוג כשרות (kosher type) is published as a single free-text string that
# may combine multiple values, comma-separated. Split on comma and map
# each token. Spec section 6.2 lists the expected combinations.
KOSHER_TYPE_MAP = {
    "בשרי": "meat",
    "חלבי": "dairy",
    "פרווה": "parve",
}
