import re

# ITU country codes we're confident about — covers all patterns seen in prod DB.
KNOWN_CC = {
    '1', '7', '44', '61', '62', '63', '65', '66',
    '81', '82', '86', '852', '853', '886', '971',
}

_SEP_RE = re.compile(r'[\s\-]')


def format_phone_e164(raw: str) -> str:
    """Normalize a phone number for tap-to-call display (+CC...).

    Display-time only — never rewrites stored values.  Returns the
    original string unchanged when the input doesn't match a
    recognised pattern (wrong guess = wrong number dialled).
    """
    s = raw.strip()
    if not s:
        return raw

    # Already has +: collapse separators, then drop the trunk zero some
    # channels leave between CC and subscriber (+81 0 80... won't dial IDD).
    # Longest CC match first so 852/853/886 beat the 1-digit codes.
    if s.startswith('+'):
        digits = _SEP_RE.sub('', s[1:])
        for n in (3, 2, 1):
            cc = digits[:n]
            if cc in KNOWN_CC:
                subscriber = digits[n:]
                if subscriber.startswith('0'):
                    subscriber = subscriber[1:]
                return f'+{cc}{subscriber}'
        return '+' + digits

    has_sep = bool(_SEP_RE.search(s))

    if has_sep:
        parts = _SEP_RE.split(s, maxsplit=1)
        if len(parts) == 2:
            cc_candidate = parts[0]
            subscriber = _SEP_RE.sub('', parts[1])
            if cc_candidate in KNOWN_CC:
                # Strip trunk zero (none of the CCs in our set keep it)
                if subscriber.startswith('0'):
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
