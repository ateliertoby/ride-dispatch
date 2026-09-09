import os
import re

import pytest
from ride_dispatch.phone import E164_CC, TRUNK_ZERO_CC, format_phone_e164


# Every shape seen in the production DB (synthetic numbers), plus edge cases
@pytest.mark.parametrize("raw,expected", [
    # Mainland with explicit CC
    ("86 13800000001", "+8613800000001"),
    # Bare mainland mobile (11-digit, ^1[3-9])
    ("13900000002", "+8613900000002"),
    # HK local (8-digit, ^[2-9])
    ("41111111", "+85241111111"),
    ("62222222", "+85262222222"),
    # HK with explicit CC
    ("852 51111111", "+85251111111"),
    # Taiwan with trunk zero (strip the 0)
    ("886 0911111111", "+886911111111"),
    # Taiwan without trunk zero (keep as-is)
    ("886 922222222", "+886922222222"),
    # NANP (US/Canada)
    ("1 2015550123", "+12015550123"),
    # Various international
    ("61 411111111", "+61411111111"),
    ("63 9171111111", "+639171111111"),
    ("62 811111111", "+62811111111"),
    ("65 91111111", "+6591111111"),
    ("65 92222222", "+6592222222"),
    ("66 911111111", "+66911111111"),
    ("7 9111111111", "+79111111111"),
    ("971 501111111", "+971501111111"),
    # Already has +
    ("+852 5111 1111", "+85251111111"),
    ("+86-138-0000-0000", "+8613800000000"),
    # + form with trunk zero left after the CC (strip the 0)
    ("+8108012345678", "+818012345678"),
    ("+81 080-1234-5678", "+818012345678"),
    # + form without trunk zero — untouched
    ("+85251111111", "+85251111111"),
    ("+886922222222", "+886922222222"),
    # Italy: 39 is outside TRUNK_ZERO_CC, so the 0 stays part of the number
    ("+390212345678", "+390212345678"),
    ("+39 06 1234567", "+39061234567"),
    ("39 06 1234567", "+39061234567"),
    # Spain: a CC recognised only via the full E.164 table, both forms
    ("34 612 345 678", "+34612345678"),
    ("+34 612 345 678", "+34612345678"),
    ("33 612345678", "+33612345678"),
    # 3-digit CC + exactly 7 digits reads as a NANP area code — leave it alone
    ("212 555 0100", "212 555 0100"),
    ("254 555-0100", "254 555-0100"),
    # Same CC, remainder that cannot be a NANP subscriber number — formatted
    ("212 612345678", "+212612345678"),
    # The guard is 3-digit-CC only: a 2-digit CC with 7 digits still formats
    ("34 6123456", "+346123456"),
    # A + declares an international number, so no NANP guard applies there
    ("+212 555 0100", "+2125550100"),
    # Withdrawn CC (42 Czechoslovakia) — not in the table, passthrough
    ("42 1234567", "42 1234567"),
    # Unrecognised CC after a + still falls back to the digits
    ("+999 12345", "+99912345"),
    # Unknown shape — passthrough unchanged
    ("12345", "12345"),
    ("999 12345", "999 12345"),
    ("", ""),
    ("ABCDE", "ABCDE"),
    # Dash separator
    ("86-13800000001", "+8613800000001"),
    ("852-51111111", "+85251111111"),
])
def test_format_phone_e164(raw, expected):
    assert format_phone_e164(raw) == expected


def test_e164_table_is_prefix_free():
    """The longest-match-first scan is only unambiguous on a prefix-free table."""
    assert not [(a, b) for a in E164_CC for b in E164_CC
                if a != b and b.startswith(a)]


def test_trunk_zero_codes_are_assigned():
    assert TRUNK_ZERO_CC <= E164_CC


# formatPhoneE164() in templates/_shared.js hand-duplicates both tables, and
# nothing else executes that file: there is no JS test rig, so a one-off typo
# in either list would ship silently and un-recognise a country on the web UI
# only.  These two tests are the only thing holding the copies together.
_SHARED_JS = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "templates", "_shared.js")


def _js_const(name):
    """Country codes from a _shared.js Set literal, whitespace-insensitive."""
    src = open(_SHARED_JS, encoding="utf-8").read()
    m = re.search(r'const %s = new Set\((.*?)\);' % name, src, re.S)
    assert m, f"{name} not found in {_SHARED_JS}"
    return set(re.findall(r'\d+', m.group(1)))


def test_js_e164_table_in_sync():
    assert _js_const("_E164_CC") == set(E164_CC)


def test_js_trunk_zero_table_in_sync():
    assert _js_const("_TRUNK_ZERO_CC") == set(TRUNK_ZERO_CC)
