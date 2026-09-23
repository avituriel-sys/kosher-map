"""Keyword-based business-category matching shared by every collector
whose source publishes a free-text business type (the national m-datit
portal, and the plugin-based council sites that have such a field).

Per spec section 5.3: the raw text is always stored verbatim by the caller;
this only supplies the canonical mapping. Anything unmatched becomes
"other" so a human can extend the tables - never a guess. Matching is
keyword-based rather than an exact-value table because councils fill the
field in their own words ("מסעדה בשרית, שווארמה, שניצל", "מפעל מוצרי מאפה").
"""

# Order matters - the first matching group wins. The order encodes the
# collisions seen in the pilot data, e.g. "מפעל מוצרי מאפה" must be a
# factory rather than a bakery, "מסעדת סושי" a sushi place rather than a
# generic restaurant, and "פיצוחים" (nuts) must not match "פיצה".
DESCRIPTION_KEYWORDS = [
    ("event_hall", ["אולם", "אולמי", "ארועים", "אירועים", "שמחות"]),
    ("factory", ["מפעל", "בית מטבחיים"]),
    ("hotel", ["בית מלון", "מלון"]),
    ("catering", ["קייטרינג", "מגשי", "הכנת אוכל"]),
    ("institutional_kitchen", ["מטבח מוסד", "מטבחי מוסדות", "חדר אוכל", "מטבח האכסניה", "בית אבות", "בית חולים"]),
    ("sushi_asian", ["סושי", "אסיאתית", "אסייתית", "אסיאתי"]),
    ("restaurant", ["מסעד"]),
    ("pizzeria", ["פיצה", "פיצות", "פיצר", "פיציר", "פצרי"]),
    ("falafel_shawarma", ["פלאפל", "שווארמה", "שאוורמה", "חומוס"]),
    ("ice_cream", ["גלידה", "גלידר", "גילדר"]),
    ("bakery_patisserie", ["קונדיטור", "קונדטור", "קוניטור", "מאפ", "בית מאפה", "עוגות", "עוגיות", "שוקולד"]),
    ("cafe", ["בית קפה", "עגלת קפה", "קפה", "מיצים", "שייק"]),
    ("butcher", ["אטליז", "איטליז", "קצב"]),
    ("fishmonger", ["דגים"]),
    ("delicatessen", ["מעדני"]),
    ("nuts_dried_fruit", ["פיצוחים", "תבלינים", "קטניות"]),
    ("greengrocer", ["ירקות", "פירות", "ירקניה"]),
    ("grocery_supermarket", ["מרכול", "סופר", "מינימרקט", "מכולת"]),
    ("restaurant_fast", ["מזנון", "מזנונ", "קיוסק", "המבורגר", "בורגר", "סנדביצ", "סנדוויצ", "סנדויצ", "כריכ", "טוסט", "שניצל", "שנייצל", "קפיטריה", "גריל", "שיפוד"]),
    ("factory_prod", ["יצור", "ייצור"]),
]

# "restaurant_fast" is only a grouping label above - these all map onto
# the same canonical category as a restaurant, matching how the other
# collectors treat "מזנון" (snack bar).
_GROUP_TO_CANONICAL = {"restaurant_fast": "restaurant", "factory_prod": "factory"}

# A deliberately small set of unambiguous words used only when the
# description is blank, matched against the business *name*. Rishon in
# particular prefixes names with the type ("אולם - אדל", "מפעל - ...",
# "קונדיטוריה - ..."), which is more reliable than guessing from an
# arbitrary brand name - so anything that isn't clearly one of these
# stays "other" rather than being inferred.
NAME_KEYWORDS = [
    ("event_hall", ["אולם", "אולמי"]),
    ("factory", ["מפעל"]),
    ("catering", ["קייטרינג"]),
    ("sushi_asian", ["סושי"]),
    ("restaurant", ["מסעדת", "מסעדה"]),
    ("pizzeria", ["פיצה", "פיצריה"]),
    ("falafel_shawarma", ["פלאפל", "שווארמה", "חומוס"]),
    ("ice_cream", ["גלידה", "גלידרי"]),
    ("bakery_patisserie", ["קונדיטוריה", "מאפיית", "מאפיה"]),
    ("cafe", ["קפה"]),
    ("butcher", ["אטליז", "קצבייה"]),
    ("grocery_supermarket", ["מרכול", "מינימרקט"]),
]


def map_category(description: str | None, name: str | None) -> tuple[str, bool]:
    """Return (category_canonical, matched). `matched` is False when
    nothing recognisable was found and the record fell back to "other".
    """
    text = (description or "").strip()
    if text:
        for canonical, words in DESCRIPTION_KEYWORDS:
            if any(w in text for w in words):
                return _GROUP_TO_CANONICAL.get(canonical, canonical), True
        return "other", False

    label = (name or "").strip()
    for canonical, words in NAME_KEYWORDS:
        if any(w in label for w in words):
            return canonical, True
    return "other", False
