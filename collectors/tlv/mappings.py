"""Per-source lookup tables for the Tel Aviv-Yafo (rabanut.co.il) collector.

Per spec section 5.3: sources are never forced to agree with each other.
category_raw is always stored verbatim; these tables only supply the
canonical mapping. Unmapped values are intentionally left out rather than
guessed at - the collector logs a warning for anything not listed here so
a human can extend the table.

As of the 2026-08-27 site survey the site's business_type filter listed
66 distinct values, all 66 of which were actually in use across the 1120
live records. 60 are mapped below; 6 are deliberately left unmapped
because the label alone doesn't say what kind of place it is (see
comments).
"""

CATEGORY_MAP = {
    "בית קפה": "cafe",
    "מסעדה בשרית": "restaurant",
    "מזנון בשרי": "restaurant",
    "בית מלון": "hotel",
    "מסעדת פועלים": "restaurant",
    "פלאפל": "falafel_shawarma",
    "חנות פיצוחים": "nuts_dried_fruit",
    "פיצרייה": "pizzeria",
    "בית חולים": "institutional_kitchen",
    "חומוסיה": "restaurant",
    "מזנון חלבי": "restaurant",
    "מפעל": "factory",
    "קונדטוריה": "bakery_patisserie",  # sic - site's own spelling
    "מסעדה אסיתית": "sushi_asian",  # sic - site's own spelling
    # "רשתות שיווק" ("marketing chains"?) - unclear what kind of business
    # this actually is. Left unmapped on purpose.
    "קייטרינג": "catering",
    "מזנון בשרי/חלבי": "restaurant",
    "איטליז": "butcher",
    "חנות פירות וירקות": "greengrocer",
    "מזנון": "restaurant",
    "מסעדה חלבית": "restaurant",
    "אולם אירועים": "event_hall",
    "חנות מיצים": "cafe",
    "חנות ביצים": "grocery_supermarket",
    "מעדניה+בית קפה": "delicatessen",
    "מזנון פרווה": "restaurant",
    "מאפיה": "bakery_patisserie",
    "מעדניה": "delicatessen",
    # "בית ספר לבישול" (cooking school) isn't a place the public eats at -
    # left unmapped rather than forced into the eating-out taxonomy.
    "גלידרייה": "ice_cream",
    "בית אבות": "institutional_kitchen",
    "מסעדה צמחונית": "restaurant",
    "מסעדת דגים": "restaurant",
    "בר חלבי": "cafe",
    "מפעל בשר": "factory",
    "קצביה": "butcher",
    "חנות תבלינים": "grocery_supermarket",
    # "מטבח" (just "kitchen") is too generic to place confidently.
    "מרכול": "grocery_supermarket",
    "בית קפה/מאפיה": "cafe",
    "בית קפה חלבי": "cafe",
    "חנות דגים": "fishmonger",
    "מסעדה בשרי/חלבי": "restaurant",
    "מסעדה": "restaurant",
    # "בר אקטיבי" ("active bar"?) - same genuinely ambiguous label as the
    # one Netanya collector leaves unmapped. Left unmapped here too.
    "מפעל קוקטיילים": "factory",
    "חנות מזון": "grocery_supermarket",
    "עגלת קפה": "cafe",
    "מזנון פירות וירקות": "greengrocer",
    "אטליז ודגים": "butcher",
    "מעדניה בשרית": "delicatessen",
    "טחנת קמח": "factory",
    "מזנון קפה": "cafe",
    "טבעוני": "restaurant",
    "חנות שוקולדים": "bakery_patisserie",
    "מסעדה מעורבת": "restaurant",
    "סושי": "sushi_asian",
    "שיווק ירקות": "greengrocer",
    "מפעל דגים": "factory",
    # "דוכן תירס" (corn stand) is too niche/ambiguous to place confidently.
    # "חנות" (just "store") is too generic to place confidently.
    "מפעל שוקולד": "factory",
    "קונדיטוריה": "bakery_patisserie",
    "אכסניית נוער": "hotel",
    "מסעדה ובר בשרי": "restaurant",
    "מפעל גלידות": "factory",
    "גלידריה": "ice_cream",
    "מסעדה פרווה": "restaurant",
}

# Meat/dairy/parve is not published as its own field for this source - it
# is sometimes implicit in the business_type label. Per spec section 6.2,
# derive it only where the label itself contains an explicit meat/dairy/
# parve word; leave it empty everywhere else rather than guessing (e.g. a
# butcher or a pizzeria could plausibly be either, so they're left out on
# purpose even though a human might guess confidently).
KOSHER_TYPE_FROM_BUSINESS_TYPE = {
    "מסעדה בשרית": ["meat"],
    "מזנון בשרי": ["meat"],
    "מזנון חלבי": ["dairy"],
    "מסעדה חלבית": ["dairy"],
    "מזנון בשרי/חלבי": ["meat", "dairy"],
    "מסעדה בשרי/חלבי": ["meat", "dairy"],
    "מזנון פרווה": ["parve"],
    "בית קפה חלבי": ["dairy"],
    "מעדניה בשרית": ["meat"],
    "מסעדה פרווה": ["parve"],
    "בר חלבי": ["dairy"],
}
