"""Record RapidOCR output for a statement screenshot as a test fixture.

Usage: STATEMENT_FIXTURE_SALT=<private> python tests/fixtures/record_statement_fixture.py <image> <out.json>

Order numbers and the account name are real data and must not enter the
repository.  Every token the reader would treat as an id keeps its first four
digits (the platform prefix shape) and has its tail replaced by digits of
sha256(salt + id); the account label is replaced wholesale.  Amounts, dates and
layout are kept — they are what the reader is tested on.

The salt is read from the environment and must never be committed.  A fixed
permutation was tried first and is unusable: the key sits in this file, so
anyone holding the repository can invert it and recover every real order
number.  A keyed hash is one-way without the salt.

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

from rapidocr_onnxruntime import RapidOCR

# Run as a script, so the repo root is not on sys.path.  The id pattern has to
# come from the package the fixture will be replayed against: a token the reader
# would read as an id but this script did not anonymise is a real-data leak.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ride_dispatch.statement import _ID_RE, _normalise_id  # noqa: E402


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


def anonymise_text(text: str) -> str:
    text = _ID_RE.sub(_anonymise_match, text)
    return re.sub(r"[^【]*【[^】]*】", "測試人【YY0000】", text)


def main(src: str, dst: str) -> None:
    result, _ = RapidOCR()(src)
    boxes = [[[[float(x), float(y)] for x, y in quad], anonymise_text(text), float(score)]
             for quad, text, score in (result or [])]
    from PIL import Image
    width = Image.open(src).size[0]
    with open(dst, "w", encoding="utf-8") as f:
        json.dump({"width": width, "boxes": boxes}, f, ensure_ascii=False, indent=0)
    print(f"{len(boxes)} boxes → {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
