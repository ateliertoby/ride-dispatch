import re

# Every country code currently assigned in ITU-T E.164, grouped by zone
# (the leading digit).  The assignment is prefix-free — no assigned code is a
# prefix of another — which is what makes the longest-match-first scan below
# unambiguous.  Codes withdrawn from service are deliberately absent (37 East
# Germany, 38 Yugoslavia, 42 Czechoslovakia, 878 UPT, 888 disaster relief,
# 997 Kazakhstan): a number written with one of those cannot be dialled, so
# leaving it unrecognised is the honest result.
E164_CC = frozenset({
    '1',
    '20', '27', '211', '212', '213', '216', '218', '220', '221',
    '222', '223', '224', '225', '226', '227', '228', '229', '230',
    '231', '232', '233', '234', '235', '236', '237', '238', '239',
    '240', '241', '242', '243', '244', '245', '246', '247', '248',
    '249', '250', '251', '252', '253', '254', '255', '256', '257',
    '258', '260', '261', '262', '263', '264', '265', '266', '267',
    '268', '269', '290', '291', '297', '298', '299',
    '30', '31', '32', '33', '34', '36', '39', '350', '351', '352',
    '353', '354', '355', '356', '357', '358', '359', '370', '371',
    '372', '373', '374', '375', '376', '377', '378', '379', '380',
    '381', '382', '383', '385', '386', '387', '389',
    '40', '41', '43', '44', '45', '46', '47', '48', '49', '420',
    '421', '423',
    '51', '52', '53', '54', '55', '56', '57', '58', '500', '501',
    '502', '503', '504', '505', '506', '507', '508', '509', '590',
    '591', '592', '593', '594', '595', '596', '597', '598', '599',
    '60', '61', '62', '63', '64', '65', '66', '670', '672', '673',
    '674', '675', '676', '677', '678', '679', '680', '681', '682',
    '683', '685', '686', '687', '688', '689', '690', '691', '692',
    '7',
    '81', '82', '84', '86', '800', '808', '850', '852', '853',
    '855', '856', '870', '880', '881', '882', '883', '886',
    '90', '91', '92', '93', '94', '95', '98', '960', '961', '962',
    '963', '964', '965', '966', '967', '968', '970', '971', '972',
    '973', '974', '975', '976', '977', '979', '992', '993', '994',
    '995', '996', '998',
})

# The country codes whose subscriber part is written with a national trunk
# zero that has to come off before the number can be dialled internationally
# (+81 0 80... won't dial IDD).  Every other code keeps all of its digits:
# an Italian number (+39) carries the 0 as part of the subscriber number, so
# stripping it there dials someone else.  Only extend this set with a code
# whose trunk prefix is known to be 0.
TRUNK_ZERO_CC = frozenset({
    '1', '7', '44', '61', '62', '63', '65', '66',
    '81', '82', '86', '852', '853', '886', '971',
})

_SEP_RE = re.compile(r'[\s\-]')


def format_phone_e164(raw: str) -> str:
    """Normalize a phone number for tap-to-call display (+CC...).

    Display-time only — never rewrites stored values.  Returns the
    original string unchanged when the input doesn't match a
    recognised pattern (wrong guess = wrong number dialled).

    JS twin: formatPhoneE164() in templates/_shared.js — keep in sync,
    E164_CC and TRUNK_ZERO_CC included.
    """
    s = raw.strip()
    if not s:
        return raw

    # Already has +: the leading + is the caller declaring an international
    # number, so any assigned code can be taken at face value.  Collapse
    # separators, then drop the trunk zero some channels leave between CC and
    # subscriber.  Longest CC match first so 852/853/886 beat the 1-digit codes.
    if s.startswith('+'):
        digits = _SEP_RE.sub('', s[1:])
        for n in (3, 2, 1):
            cc = digits[:n]
            if cc in E164_CC:
                subscriber = digits[n:]
                if cc in TRUNK_ZERO_CC and subscriber.startswith('0'):
                    subscriber = subscriber[1:]
                return f'+{cc}{subscriber}'
        return '+' + digits

    has_sep = bool(_SEP_RE.search(s))

    if has_sep:
        parts = _SEP_RE.split(s, maxsplit=1)
        if len(parts) == 2:
            cc_candidate = parts[0]
            subscriber = _SEP_RE.sub('', parts[1])
            # Without a + there is nothing declaring the number international,
            # and "AAA BBB-BBBB" is how a NANP number is written: a 3-digit
            # area code that may equal a 3-digit country code (212 Morocco,
            # 226 Burkina Faso, 254 Kenya...) followed by exactly 7 digits.
            # That exact shape is unresolvable, and a wrong guess dials a
            # wrong number, so leave it alone.  A 2-digit code carries no such
            # collision.
            ambiguous_nanp = len(cc_candidate) == 3 and len(subscriber) == 7
            if cc_candidate in E164_CC and not ambiguous_nanp:
                if cc_candidate in TRUNK_ZERO_CC and subscriber.startswith('0'):
                    subscriber = subscriber[1:]
                return f'+{cc_candidate}{subscriber}'
        return raw

    # Bare number (no separator)
    digits = _SEP_RE.sub('', s)
    if not digits.isdigit():
        return raw

    # Mainland mobile: 11 digits starting with 1[3-9]
    if len(digits) == 11 and re.match(r'1[3-9]', digits):
        return f'+86{digits}'

    # HK local: 8 digits starting with [2-9]
    if len(digits) == 8 and digits[0] in '23456789':
        return f'+852{digits}'

    return raw
