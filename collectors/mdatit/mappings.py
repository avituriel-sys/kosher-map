"""Lookup tables for the national m-datit portal collector.

Per spec section 5.3: category_raw is stored verbatim and these tables
only supply the canonical mapping. Anything unmatched becomes "other"
with a logged warning so a human can extend the tables - never a guess.

The portal's only business-type signal is `SiteDescription`, a free-text
field filled in by each local council in its own words ("מסעדה בשרית,
שווארמה, שניצל", "מפעל מוצרי מאפה", "קונדיטוריות" ...). Coverage differs
a lot by council (near-complete in Givat Shmuel and Kfar Saba, mostly
blank in Petah Tikva, Rishon and Ashdod as of the 2026-09-23 pilot
survey), so matching is keyword-based rather than an exact-value table.
"""

from collectors.common.category_keywords import (  # noqa: F401  (re-exported)
    DESCRIPTION_KEYWORDS,
    NAME_KEYWORDS,
    map_category,
)

# Portal KosherType ids (code table 70): 1=dairy, 2=meat, 3=parve. A
# certificate's KosherType.Name is a comma-joined list of those ids
# ("1,2,3"), so it is decoded id-by-id rather than looked up whole.
KOSHER_TYPE_BY_ID = {"1": "dairy", "2": "meat", "3": "parve"}

# Code table 71. The portal already returns the Hebrew name on each
# certificate. A business with several certificates takes the strictest.
SUPERVISION_BY_LEVEL_NAME = {"רגיל": "regular", "מהדרין": "mehadrin"}
SUPERVISION_RANK = {"unknown": 0, "regular": 1, "mehadrin": 2}
