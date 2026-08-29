import json
import os

import pytest

from ride_dispatch import statement
from ride_dispatch.statement import parse_boxes, Statement

FIX = os.path.join(os.path.dirname(__file__), "fixtures")

# Ids are the anonymised values record_statement_fixture.py writes; amounts, times and
# dates are as printed on the statement.  These literals are a real check rather than a
# circular one because the two fixtures were recorded independently — from the original
# PNG and from the Telegram-compressed JPEG — and must agree on all twelve rows.
DAY1 = [("09:00", "1128441073982467", 280.0), ("12:30", "1128654265046668", 210.0),
        ("13:54", "1128106331222667", 210.0), ("17:25", "1128525041306800", 170.0),
        ("19:04", "1128113563272980", 300.0), ("13:54", "1128106331222667", 40.0)]
DAY2 = [("10:00", "1128777147862551", 210.0), ("17:15", "1128184195410622", 210.0),
        ("18:13", "1385660587571823", 210.0), ("12:50", "5127189103823435851", 210.0),
        ("12:55", "3316573881430636069", 280.0), ("10:40", "SPACE202640159248", 210.0)]


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        d = json.load(f)
    return d["boxes"], d["width"]


@pytest.mark.parametrize("name", ["statement_1280.json", "statement_orig.json"])
def test_rows_ids_and_amounts(name):
    stmt = parse_boxes(*load(name))
    assert [d.date for d in stmt.days] == ["2026-08-23", "2026-08-24"]
    got1 = [(r.time, r.order_id, r.amount) for r in stmt.days[0].rows]
    got2 = [(r.time, r.order_id, r.amount) for r in stmt.days[1].rows]
    assert got1 == DAY1
    assert got2 == DAY2
    assert all(r.date == "2026-08-23" for r in stmt.days[0].rows)
    assert all(r.settle_date == "2026-08-25" for r in stmt.days[0].rows)
    assert all(r.settle_date == "2026-08-26" for r in stmt.days[1].rows)


@pytest.mark.parametrize("name", ["statement_1280.json", "statement_orig.json"])
def test_totals_survive_garbled_labels(name):
    stmt = parse_boxes(*load(name))
    assert stmt.days[0].sum == 1210.0
    assert stmt.days[1].sum == 1330.0
    assert stmt.total == 2540.0
    assert stmt.account == "YY0000"


def test_counts_read_from_original_and_tolerated_on_compressed():
    orig = parse_boxes(*load("statement_orig.json"))
    assert [d.count for d in orig.days] == [6, 6]
    small = parse_boxes(*load("statement_1280.json"))
    assert all(c in (6, None) for c in (d.count for d in small.days))


def test_data_row_before_any_header_is_dropped_with_warning():
    boxes = [[[[10, 10], [200, 10], [200, 20], [10, 20]], "99", 0.9],
             [[[50, 10], [180, 10], [180, 20], [50, 20]], "2026-08-23 09:00", 0.9],
             [[[250, 10], [400, 10], [400, 20], [250, 20]], "1128000000000001", 0.9],
             [[[900, 10], [980, 10], [980, 20], [900, 20]], "280.00", 0.9]]
    stmt = parse_boxes(boxes, 1000)
    assert stmt.days == []
    assert stmt.warnings


def test_money_normalisation():
    assert statement._money("2.540.00") == 2540.0
    assert statement._money("1,330.00") == 1330.0
    assert statement._money("0.00") == 0.0
    assert statement._MONEY_RE.findall("#sc2.540.00 x 2026-08-25 1170.00") == ["2.540.00", "1170.00"]


def test_id_needs_digits_not_just_lookalike_letters():
    assert statement._ID_RE.findall("SSSSSSSSSSSS") == []
    # The digit count is taken inside the token, so digits elsewhere in the box
    # cannot vouch for a run of look-alike letters.
    assert statement._ID_RE.findall("SSSSSSSSSSSS 12345678") == []
    assert statement._ID_RE.findall("901234S678901234") == ["901234S678901234"]


def row_boxes(date, order_id, amount, y, settle_date="2026-01-03"):
    """One statement line as OCR hands it over: date, id, settle date, amount."""
    return [[[[10, y], [120, y], [120, y + 12], [10, y + 12]], date + " 09:00", 0.9],
            [[[200, y], [430, y], [430, y + 12], [200, y + 12]], order_id, 0.9],
            [[[600, y], [700, y], [700, y + 12], [600, y + 12]], settle_date, 0.9],
            [[[900, y], [980, y], [980, y + 12], [900, y + 12]], f"{amount:.2f}", 0.9]]


def day_header(date, count, total):
    return [[[[10, 10], [120, 10], [120, 22], [10, 22]], date, 0.9],
            [[[300, 10], [340, 10], [340, 22], [300, 22]], str(count), 0.9],
            [[[900, 10], [980, 10], [980, 22], [900, 22]], f"{total:.2f}", 0.9]]


def test_short_space_ids_and_alphanumeric_ids_are_read():
    """Two shapes the reader used to miss, which failed the checksum of a
    perfectly readable image: SPACE with a short digit run, and 同程's
    alphanumeric code, which the platform's own UI truncates."""
    boxes = day_header("2026-01-01", 2, 420.0)
    boxes += row_boxes("2026-01-01", "SPACE20260101001", 210.0, y=40)
    boxes += row_boxes("2026-01-01", "VBK6A85D6FB8089...", 210.0, y=80)
    stmt = parse_boxes(boxes, 1000)
    rows = stmt.days[0].rows
    assert [r.order_id for r in rows] == ["SPACE20260101001", "VBK6A85D6FB8089"]
    assert [r.truncated for r in rows] == [False, True]
    assert [r.amount for r in rows] == [210.0, 210.0]


def test_an_alphanumeric_id_keeps_its_letters():
    """B is an 8 inside a run of digits and a B inside a code, so the digit
    fixes must know which it is looking at."""
    assert statement._normalise_id("VBK6A85D6FB8089ABCD") == "VBK6A85D6FB8089ABCD"
    assert statement._normalise_id("11281B0000000001") == "1128180000000001"


def test_ids_do_not_swallow_amounts_dates_or_times():
    assert statement._ID_RE.findall("210.00") == []
    assert statement._ID_RE.findall("2026-08-22") == []
    assert statement._ID_RE.findall("09:30") == []
    assert statement._ID_RE.findall("2026-08-22 09:30 1,330.00") == []


# ---- the amount column ----

def box(x, text, y, w=90):
    return [[[x, y], [x + w, y], [x + w, y + 12], [x, y + 12]], text, 0.9]


def wide_row(amount_text, y):
    """A data line off the wide table: the 司機應結算金額 cell is the rightmost
    box, with a 司機預估收入 figure and a 派單風險率 percentage in columns to its
    left, either of which is money-shaped but is not the amount."""
    return [box(39, "2026-08-16", y),
            box(138, "2026-08-16 09:00", y),
            box(300, "1128000000000001", y, w=170),
            box(560, "200.00", y),
            box(900, "0.00%", y),
            box(1100, "2026-08-18", y),
            box(1240, amount_text, y, w=40)]


def test_a_percentage_is_not_money():
    assert statement._MONEY_RE.findall("0.00%") == []
    assert statement._MONEY_RE.findall("210.00 合格 0.90%") == ["210.00"]


def test_amount_is_read_off_the_end_of_the_rightmost_box():
    """Junk OCR merges into the amount cell is ignored; a decimal point drawn
    at ~7 px comes back as ":"; one decimal digit is not an amount."""
    f = statement._amount_at_row_end
    assert f("正常 210.00") == 210.0
    assert f("#fo 830.00") == 830.0
    assert f("1,234.00") == 1234.0
    assert f("2.540.00") == 2540.0
    assert f("200:00") == 200.0
    assert f("0.00%") is None
    assert f("HKD") is None
    assert f("210.0") is None


def test_amount_comes_from_the_rightmost_cell_not_the_percentage_column():
    boxes = [box(39, "2026-08-16", 10), box(300, "1", 10), box(1240, "200.00", 10, w=40)]
    boxes += wide_row("200:00", y=40)
    stmt = parse_boxes(boxes, 1280)
    assert [r.amount for r in stmt.days[0].rows] == [200.0]
    assert stmt.warnings == []


def test_an_unreadable_amount_cell_drops_the_row_rather_than_guessing():
    """Reading the amount off whichever other column happens to be
    money-shaped would settle a wrong sum with nothing to show for it."""
    boxes = [box(39, "2026-08-16", 10), box(300, "1", 10), box(1240, "200.00", 10, w=40)]
    boxes += wide_row("HKD", y=40)
    stmt = parse_boxes(boxes, 1280)
    assert stmt.days[0].rows == []
    assert any("row without amount" in w for w in stmt.warnings)


def test_a_stray_box_left_of_the_date_still_opens_the_day():
    """The ▾ expand caret is recognised as a box of its own, so the header's
    date is no longer its first box — and the 记录数 scan must start after the
    date rather than read the date's own trailing digits as a count."""
    boxes = [box(27, "4", 10, w=14), box(53, "2026-08-16", 10), box(300, "2", 10),
             box(1240, "400.00", 10, w=40)]
    boxes += wide_row("200:00", y=40)
    boxes += wide_row("200.00", y=70)
    stmt = parse_boxes(boxes, 1280)
    assert [d.date for d in stmt.days] == ["2026-08-16"]
    assert [r.amount for r in stmt.days[0].rows] == [200.0, 200.0]
    assert stmt.days[0].count == 2
    assert stmt.warnings == []


def test_undecodable_input_is_reported_without_starting_the_engine(monkeypatch):
    def boom():
        raise AssertionError("OCR engine must not be built for undecodable input")
    monkeypatch.setattr(statement, "_engine", boom)
    for data in (b"", b"not an image"):
        stmt = statement.read_image(data)
        assert stmt.days == []
        assert stmt.warnings
        assert stmt.reader


def test_ocr_available_false_without_package(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("rapidocr_onnxruntime"):
            raise ImportError(name)
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    monkeypatch.setattr(statement, "_ocr", None)
    assert statement.ocr_available() is False


def test_ocr_available_false_when_the_package_is_installed_but_broken(monkeypatch, caplog):
    """A missing onnxruntime shared library raises OSError, not ImportError:
    the bot must fall back to the no-OCR report instead of crashing."""
    import builtins
    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("rapidocr_onnxruntime"):
            raise OSError("libonnxruntime.so: cannot open shared object file")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    monkeypatch.setattr(statement, "_ocr", None)
    monkeypatch.setattr(statement, "_ocr_broken_logged", False)
    with caplog.at_level("WARNING", logger="statement"):
        assert statement.ocr_available() is False
        assert len(caplog.records) == 1
        assert statement.ocr_available() is False
        assert len(caplog.records) == 1  # once, not once per forwarded screenshot


# ---- detector aspect ratio ----

def test_pad_widens_a_short_wide_frame_downwards_only():
    import numpy as np
    img = np.full((221, 1842, 3), 17, dtype=np.uint8)
    out = statement._pad_for_detection(img)
    assert out.shape[1] == 1842
    assert out.shape[1] / out.shape[0] <= statement._MAX_ASPECT
    assert (out[:221] == 17).all()
    assert (out[221:] == 255).all()


def test_pad_leaves_a_frame_under_the_cap_untouched():
    import numpy as np
    img = np.full((220, 1280, 3), 17, dtype=np.uint8)
    assert statement._pad_for_detection(img).shape == img.shape


def test_engine_turns_off_the_detector_aspect_threshold(monkeypatch):
    """Past the engine's own width/height threshold no detection runs at all
    and the whole frame comes back as one recognised line."""
    import sys
    import types
    built = []

    class FakeRapidOCR:
        def __init__(self, **kwargs):
            built.append(kwargs)

    module = types.ModuleType("rapidocr_onnxruntime")
    module.RapidOCR = FakeRapidOCR
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", module)
    monkeypatch.setattr(statement, "_ocr", None)
    engine = statement._engine()
    assert built == [{"width_height_ratio": -1}]
    assert statement._engine() is engine


def test_a_short_wide_statement_is_read():
    """The regression: a statement day with few rows makes a frame wide enough
    that the detector was skipped, so the reader saw no boxes to parse."""
    pytest.importorskip("rapidocr_onnxruntime")
    import cv2
    import numpy as np
    f = cv2.FONT_HERSHEY_SIMPLEX
    img = np.full((221, 1842, 3), 255, dtype=np.uint8)
    cv2.putText(img, "2026-01-01", (20, 70), f, 1.2, (0, 0, 0), 3)
    cv2.putText(img, "1", (700, 70), f, 1.2, (0, 0, 0), 3)
    cv2.putText(img, "210.00", (1560, 70), f, 1.2, (0, 0, 0), 3)
    cv2.putText(img, "2026-01-01 09:00", (20, 170), f, 1.2, (0, 0, 0), 3)
    cv2.putText(img, "1128000000000001", (600, 170), f, 1.2, (0, 0, 0), 3)
    cv2.putText(img, "2026-01-03", (1150, 170), f, 1.2, (0, 0, 0), 3)
    cv2.putText(img, "210.00", (1560, 170), f, 1.2, (0, 0, 0), 3)
    assert img.shape[1] / img.shape[0] > 8
    stmt = statement.read_image(bytes(cv2.imencode(".png", img)[1]))
    assert [d.date for d in stmt.days] == ["2026-01-01"]
    rows = stmt.days[0].rows
    assert [(r.time, r.order_id, r.amount) for r in rows] == [("09:00", "1128000000000001", 210.0)]
