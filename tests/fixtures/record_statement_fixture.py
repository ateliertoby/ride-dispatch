"""Record RapidOCR output for a statement screenshot as a test fixture.

Usage: STATEMENT_FIXTURE_SALT=<private> [STATEMENT_FIXTURE_NAMES=<frag>,<frag>]
       python tests/fixtures/record_statement_fixture.py <image> <out.json>

Order numbers and the account name are real data and must not enter the
repository.  Every token the reader would treat as an id keeps its first four
digits (the platform prefix shape) and has its tail replaced by digits of
sha256(salt + id); the account label, name included, is replaced wholesale.
Amounts, dates and layout are kept — they are what the reader is tested on, and
so are the bracket tokens that hold no account code (see _ACCOUNT_BRACKET_RE).

The salt is read from the environment and must never be committed.  A fixed
permutation was tried first and is unusable: the key sits in this file, so
anyone holding the repository can invert it and recover every real order
number.  A keyed hash is one-way without the salt.

The account holder's name also prints in the 收款人 column of every data row,
outside any bracket, and the platform stamps his account number across the
table as a watermark; OCR renders both differently in each cell, so no pattern
can find them: STATEMENT_FIXTURE_NAMES carries the fragments that survive the
mis-reads (a surname character, or the watermark's digits) and any box holding
one is replaced wholesale.  A box that is nothing but digits keeps that shape
and becomes zeros, because the reader is tested on what stray digits beside a
column do.  Like the salt these fragments are supplied per run and never
committed.  Geometry is untouched, so scrubbed cells still group into their
rows.

Ids are stable for whoever holds the salt, so re-recording the same screenshot
reproduces the same fixture.  Recording with a different salt changes every id,
and the ground truth in tests/test_statement_reader.py must be updated to match.
"""
import hashlib
import json
import os
import pathlib
import re
import sys

# Run as a script, so the repo root is not on sys.path.  The id pattern has to
# come from the package the fixture will be replayed against: a token the reader
# would read as an id but this script did not anonymise is a real-data leak.
# The engine and the padding come from there for the same reason the pattern
# does: boxes recorded through a different pipeline than the reader runs are a
# fixture of nothing (past the detector's width/height threshold an unpadded
# wide screenshot yields no boxes at all).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ride_dispatch.statement import (  # noqa: E402
    _ID_RE, _decode, _engine, _normalise_id, _pad_for_detection)


def _salt() -> str:
    salt = os.environ.get("STATEMENT_FIXTURE_SALT")
    if not salt:
        raise SystemExit("set STATEMENT_FIXTURE_SALT to a private value; it is never committed")
    return salt


def anonymise_id(digits: str) -> str:
    tail = len(digits) - 4
    h = int(hashlib.sha256((_salt() + digits).encode()).hexdigest(), 16)
    return digits[:4] + str(h)[:tail].zfill(tail)


def _anonymise_match(m: re.Match) -> str:
    # Normalise first so a letter-for-digit misread ("112815O4…") is anonymised
    # as the id it denotes, instead of slipping through as a different string.
    token = _normalise_id(m.group(0))
    if token.startswith("SPACE"):
        return "SPACE" + anonymise_id(token[5:])
    return anonymise_id(token)


# An account code is alphanumeric, and the name printed before it belongs to the
# same person, so both go.  A bracket holding only CJK is not an account label
# but one of the platform's category chips, which OCR renders with 【】 often
# enough that the reader is tested against them: those must survive verbatim.
_ACCOUNT_BRACKET_RE = re.compile(r"[^【]*【[^】]*[0-9A-Za-z][^】]*】")

_PLACEHOLDER_NAME = "測試人"


def _name_fragments() -> list[str]:
    raw = os.environ.get("STATEMENT_FIXTURE_NAMES", "")
    return [f for f in (part.strip() for part in raw.split(",")) if f]


def anonymise_text(text: str) -> str:
    text = _ID_RE.sub(_anonymise_match, text)
    text = _ACCOUNT_BRACKET_RE.sub(f"{_PLACEHOLDER_NAME}【YY0000】", text)
    if any(fragment in text for fragment in _name_fragments()):
        return "0" * len(text) if text.isdigit() else _PLACEHOLDER_NAME
    return text


def main(src: str, dst: str) -> None:
    img = _pad_for_detection(_decode(pathlib.Path(src).read_bytes()))
    result, _ = _engine()(img)
    boxes = [[[[float(x), float(y)] for x, y in quad], anonymise_text(str(text)), float(score)]
             for quad, text, score in (result or [])]
    with open(dst, "w", encoding="utf-8") as f:
        json.dump({"width": img.shape[1], "boxes": boxes}, f, ensure_ascii=False, indent=0)
    print(f"{len(boxes)} boxes → {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
