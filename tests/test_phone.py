import pytest
from ride_dispatch.phone import format_phone_e164


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
    ("65 91111111", "+6591111111"),
    ("65 92222222", "+6592222222"),
    ("66 911111111", "+66911111111"),
    ("7 9111111111", "+79111111111"),
    ("971 501111111", "+971501111111"),
    # Already has +
    ("+852 5111 1111", "+85251111111"),
    ("+86-138-0000-0000", "+8613800000000"),
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
