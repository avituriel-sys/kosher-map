"""Coverage check for the TLV category map against real site data.

Guards against typos in mappings.py: every business_type value actually
seen in the live fixtures must be either mapped or explicitly on the
deliberately-unmapped list, so a silent transcription error doesn't
quietly turn into an "unmapped, defaults to other" warning nobody
notices.
"""

from pathlib import Path

from bs4 import BeautifulSoup

from collectors.tlv import collector
from collectors.tlv.mappings import CATEGORY_MAP

FIXTURES = Path(__file__).parent / "fixtures" / "tlv"

# Categories deliberately left out of CATEGORY_MAP - see mappings.py
# comments for why each one is genuinely ambiguous rather than missing
# by mistake.
DELIBERATELY_UNMAPPED = {
    "רשתות שיווק",
    "בית ספר לבישול",
    "מטבח",
    "בר אקטיבי",
    "דוכן תירס",
    "חנות",
    "",  # at least one live listing publishes no business type at all
}


def test_every_real_business_type_is_accounted_for():
    seen = set()
    for n in range(1, 11):
        soup = BeautifulSoup(
            (FIXTURES / f"all_p{n}.html").read_text(encoding="utf-8"), "lxml"
        )
        seen.update(c.category_raw for c in collector._parse_cards(soup))

    unaccounted = seen - set(CATEGORY_MAP) - DELIBERATELY_UNMAPPED
    assert not unaccounted, f"business_type value(s) with no mapping decision: {unaccounted}"
