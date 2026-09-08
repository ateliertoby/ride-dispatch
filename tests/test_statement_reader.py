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
# A third recording, of a statement whose category chips OCR drew with 【】 —
# one of them a matched pair, inside the last data row of the last day.
DAY3 = [("11:47", "1578701224818578", 200.0), ("14:00", "1539272630110852", 210.0),
        ("15:25", "1128100382197280", 310.0), ("11:47", "1578701224818578", 40.0),
        ("17:40", "3316196058926202002", 280.0)]
# A fourth recording, of a statement carrying a 判罰賠款 row: a second line under
# the trip's own order id whose 司機應結算金額 is negative.
PENALTY_DAY1 = [("11:17", "1128296513125016", 300.0)]
PENALTY_DAY2 = [("13:00", "1128106371423384", 280.0), ("13:00", "1128106371423384", -97.38)]
# A fifth recording, off the platform's 完單待確認 view: a 結算狀態 column prints
# to the right of 司機應結算金額, so the amount is not the row's last cell.
STATUS_DAY = [("09:20", "1128962105062088", 210.0), ("11:15", "1128795078574671", 300.0),
              ("16:30", "5127288820957047223", 210.0), ("17:10", "5127696895158503886", 300.0)]


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


def test_a_bracketed_chip_costs_neither_a_row_nor_the_totals():
    """A statement whose 【】 category chips OCR happened to close: the chip
    lands in a data row, which must still be read as one, and must not be able
    to restate the account and grand total the top row already carried."""
    stmt = parse_boxes(*load("statement_bracketed_chip.json"))
    assert stmt.account == "YY0000"
    assert stmt.total == 3060.0
    assert [d.date for d in stmt.days] == ["2026-08-27", "2026-08-28", "2026-08-29"]
    assert [d.count for d in stmt.days] == [4, 6, 5]
    assert [d.sum for d in stmt.days] == [940.0, 1080.0, 1040.0]
    assert [len(d.rows) for d in stmt.days] == [4, 6, 5]
    assert [(r.time, r.order_id, r.amount) for r in stmt.days[2].rows] == DAY3


def test_a_penalty_row_is_read_with_its_minus_sign():
    """A 判罰賠款 line repeats its trip's order id and carries a negative
    司機應結算金額.  Dropping the sign turns the day into 280 + 97.38 and fails
    a checksum the statement itself passes."""
    stmt = parse_boxes(*load("statement_penalty.json"))
    assert stmt.account == "YY0000"
    assert stmt.total == 482.62
    assert [d.date for d in stmt.days] == ["2026-08-15", "2026-08-19"]
    assert [d.count for d in stmt.days] == [1, 2]
    assert [d.sum for d in stmt.days] == [300.0, 182.62]
    assert [(r.time, r.order_id, r.amount) for r in stmt.days[0].rows] == PENALTY_DAY1
    assert [(r.time, r.order_id, r.amount) for r in stmt.days[1].rows] == PENALTY_DAY2
    assert stmt.warnings == []


def test_the_penalty_statement_agrees_with_itself():
    """The rows of the penalty day add up to the day's own 求和 only once the
    sign is kept, so the checksum is what proves the sign was read."""
    from ride_dispatch.statement import _checksum
    stmt = parse_boxes(*load("statement_penalty.json"))
    assert _checksum(stmt) == ("ok", [])


def test_a_statement_with_a_settlement_status_column_is_read():
    """A whole screenshot of the 完單待確認 view, where every data row ends with
    its 結算狀態 and the header for 司機應結算金額 came back too mangled to name
    the column, so the figures alone place it.  The account number the platform
    stamps across the table prints a row of its own between two data rows: it
    names nothing, so it costs neither a row nor a column."""
    stmt = parse_boxes(*load("statement_status_column.json"))
    assert stmt.account == "YY0000"
    assert stmt.total == 1020.0
    assert [d.date for d in stmt.days] == ["2026-09-07"]
    assert [d.count for d in stmt.days] == [4]
    assert [d.sum for d in stmt.days] == [1020.0]
    assert [(r.time, r.order_id, r.amount) for r in stmt.days[0].rows] == STATUS_DAY
    assert all(r.settle_date == "2026-09-09" for r in stmt.days[0].rows)
    assert stmt.warnings == []


def test_the_status_column_statement_agrees_with_itself():
    """Rows and 求和 now come out of one column, so the checksum is a check on
    that column rather than on whatever each row happened to end with."""
    from ride_dispatch.statement import _checksum
    stmt = parse_boxes(*load("statement_status_column.json"))
    assert _checksum(stmt) == ("ok", [])


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


def test_money_keeps_a_minus_sign_in_either_glyph():
    """A day whose only line is a 判罰賠款 prints a negative 求和, and the
    platform's minus comes back as a hyphen or as U+2212 depending on the
    render."""
    assert statement._MONEY_RE.findall("求和-97.38") == ["-97.38"]
    assert statement._MONEY_RE.findall("求和−97.38") == ["−97.38"]
    assert statement._money("-97.38") == -97.38
    assert statement._money("−97.38") == -97.38
    assert statement._money("-2.540.00") == -2540.0


def test_a_signed_amount_does_not_let_a_date_or_a_cut_off_box_pass_as_money():
    """The lookarounds have to keep holding with a sign in front: the platform
    truncates its own 求和 cell ("求和-97...."), and a dotted date is the shape
    the sign was most likely to open up."""
    assert statement._MONEY_RE.findall("求和-97....") == []
    assert statement._MONEY_RE.findall("求和-97.") == []
    assert statement._MONEY_RE.findall("2026-08-25") == []
    assert statement._MONEY_RE.findall("2026.08.25") == []
    assert statement._MONEY_RE.findall("-0.00%") == []


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
    """A data line off the wide table, in the view that ends at 司機應結算金額:
    a 司機預估收入 figure and a 派單風險率 percentage print in columns to its
    left, either of which is money-shaped but is not the amount."""
    return [box(39, "2026-08-16", y),
            box(138, "2026-08-16 09:00", y),
            box(300, "1128000000000001", y, w=170),
            box(560, "200.00", y),
            box(900, "0.00%", y),
            box(1100, "2026-08-18", y),
            box(1240, amount_text, y, w=40)]


def status_row(amount_text, status, y):
    """The same line off the 完單待確認 view, which prints 結算狀態 to the right
    of 司機應結算金額, so the amount is not the row's last cell."""
    return wide_row(amount_text, y) + [box(1300, status, y, w=60)]


def wide_day_row(date, count, total, y=10):
    """A day group row of the wide table: 记录数 left of the aggregates, the
    day's 求和 in the 司機應結算金額 column, and 記錄數 repeated under 結算狀態."""
    return [box(39, date, y), box(300, f"记录数{count}", y),
            box(1240, f"{total:.2f}", y, w=40), box(1300, f"记录数{count}", y, w=60)]


def test_a_percentage_is_not_money():
    assert statement._MONEY_RE.findall("0.00%") == []
    assert statement._MONEY_RE.findall("210.00 合格 0.90%") == ["210.00"]


def test_amount_is_read_off_the_end_of_its_cell():
    """Junk OCR merges into the amount cell is ignored; a decimal point drawn
    at ~7 px comes back as ":"; one decimal digit is not an amount."""
    f = statement._amount_in_cell
    assert f("正常 210.00") == 210.0
    assert f("#fo 830.00") == 830.0
    assert f("1,234.00") == 1234.0
    assert f("2.540.00") == 2540.0
    assert f("200:00") == 200.0
    assert f("0.00%") is None
    assert f("HKD") is None
    assert f("210.0") is None


def test_a_negative_amount_in_its_cell_keeps_its_sign():
    """The sign is the whole signature of a 判罰賠款 row — the category chip is
    unreadable — so it must survive the amount cell in either glyph."""
    f = statement._amount_in_cell
    assert f("-97.38") == -97.38
    assert f("−97.38") == -97.38
    assert f("不适用 -97.38") == -97.38
    assert f("-1,234.00") == -1234.0
    assert f("-97:38") == -97.38
    # A hyphen belonging to a date must not be read as the amount's sign.
    assert f("2026-08-19 13:00") == 13.0


def test_amount_comes_from_the_amount_column_not_the_percentage_column():
    boxes = [box(39, "2026-08-16", 10), box(300, "1", 10), box(1240, "200.00", 10, w=40)]
    boxes += wide_row("200:00", y=40)
    stmt = parse_boxes(boxes, 1280)
    assert [r.amount for r in stmt.days[0].rows] == [200.0]
    assert stmt.warnings == []


def test_a_status_column_right_of_the_amount_does_not_hide_it():
    """The 完單待確認 view prints 結算狀態 after 司機應結算金額, so the amount is
    no longer the row's last cell — and OCR mangles the status text, so the
    column it opened cannot be recognised by reading what it says."""
    boxes = wide_day_row("2026-08-16", 2, 400.0)
    boxes += status_row("200.00", "完單待確認", y=40)
    boxes += status_row("200:00", "完草待班部", y=70)
    stmt = parse_boxes(boxes, 1400)
    assert [d.date for d in stmt.days] == ["2026-08-16"]
    assert [r.amount for r in stmt.days[0].rows] == [200.0, 200.0]
    assert [d.sum for d in stmt.days] == [400.0]
    assert [d.count for d in stmt.days] == [2]
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


def named_amount_row(amount_text, y, trailing_money=None):
    """A data line whose 司機應結算金額 column the header names, optionally with
    a second money column printed to the right of it."""
    row = [box(39, "2026-08-16", y),
           box(138, "2026-08-16 09:00", y),
           box(300, "1128000000000001", y, w=170),
           box(1100, "2026-08-18", y),
           box(1200, amount_text, y, w=80)]
    if trailing_money is not None:
        row.append(box(1320, trailing_money, y, w=80))
    return row


def named_amount_header():
    return [box(138, "用車時間", 10, w=80),
            box(1100, "應結算日期", 10, w=80),
            box(1200, "司機應結算金額", 10, w=80)]


def test_a_named_amount_column_and_the_rightmost_money_column_agreeing_are_read():
    """The control for the refusal below: the same statement without the extra
    money column, where the header and the figures name one column."""
    boxes = named_amount_header()
    boxes += [box(39, "2026-08-16", 40), box(300, "1", 40), box(1200, "200.00", 40, w=80)]
    boxes += named_amount_row("200.00", y=70)
    stmt = parse_boxes(boxes, 1500)
    assert [d.sum for d in stmt.days] == [200.0]
    assert [r.amount for r in stmt.days[0].rows] == [200.0]
    assert stmt.warnings == []


def test_a_money_column_right_of_the_named_amount_column_is_refused():
    """Two readings of which column holds 司機應結算金額 — the one the header
    names and the rightmost one holding money — that disagree leave no way to
    tell which of them the platform changed.  Choosing either is invisible
    afterwards: the day's 求和 would be read off the same column as its rows and
    agree with them, so the statement would pass its own checksum on a figure
    that is not the money.  Refusing is what the operator can see."""
    boxes = named_amount_header()
    boxes += [box(39, "2026-08-16", 40), box(300, "1", 40),
              box(1200, "200.00", 40, w=80), box(1320, "1,234.00", 40, w=80)]
    boxes += named_amount_row("200.00", y=70, trailing_money="1,234.00")
    stmt = parse_boxes(boxes, 1500)
    assert stmt.days[0].rows == []
    assert stmt.days[0].sum is None
    assert stmt.total is None
    assert any("amount column unclear" in w for w in stmt.warnings)
    assert any("row without amount" in w for w in stmt.warnings)


# ---- naming the columns ----

def column_named(cell):
    m = statement._column_match(cell)
    return None if m is None else statement._COLUMNS[m[0]][1][0]


def test_a_garbled_header_still_names_its_column():
    """OCR rewrites the header's characters and hands them back simplified, so
    a column is recognised by distance from its printed name in either
    script — every character differs between the two."""
    assert column_named("结真状能") == "結算狀態"
    assert column_named("结真日期") == "應結算日期"
    assert column_named("用率特间") == "用車時間"
    assert column_named("鹿结算日期") == "應結算日期"
    assert column_named("[司模库结算金镇") == "司機應結算金額"


def test_a_header_cell_too_far_gone_names_nothing():
    """Four wrong characters out of six is not a reading of 出賬單日期, and an
    unnamed column costs nothing here — the reader falls back to shape — while
    a wrongly named one is acted on."""
    assert column_named("国出联禁日期") is None
    assert column_named("收款神的干重") is None
    assert column_named("合格") is None


def test_the_estimate_column_can_never_pass_as_the_amount_column():
    """司機預估收入 prints beside 司機應結算金額 and holds a different figure, so
    the tolerance has to stay under the distance between the two names: reading
    a day off the estimate would settle a wrong sum while every subtotal on the
    image still agreed with itself."""
    for cell in ("司機預估收入", "司机预估收入", "团司概预估收入"):
        m = statement._column_match(cell)
        assert m is not None, cell
        assert statement._COLUMNS[m[0]][0] is None, cell
    assert statement._COLUMNS[statement._column_match("司機應結算金額")[0]][0] == "amount"


# ---- bracketed cells ----

def test_the_top_row_carries_the_account_and_grand_total():
    boxes = [box(39, "測試人【YY0000】", 10), box(1240, "3060.00", 10, w=40)]
    stmt = parse_boxes(boxes, 1280)
    assert stmt.account == "YY0000"
    assert stmt.total == 3060.0


def test_a_row_holding_an_order_id_is_data_however_it_is_bracketed():
    """OCR renders the platform's category chips with 【】, and a matched pair
    lands in a data row, where the account pattern must not claim it."""
    boxes = [box(39, "測試人【YY0000】", 10), box(1240, "3060.00", 10, w=40)]
    boxes += [box(39, "2026-08-29", 40), box(300, "1", 40), box(1240, "280.00", 40, w=40)]
    boxes += [box(39, "2026-08-29", 70), box(138, "2026-08-29 17:40", 70),
              box(298, "【送机】", 70, w=50), box(357, "3316000000000000001", 70, w=170),
              box(1100, "2026-08-31", 70), box(1240, "280.00", 70, w=40)]
    stmt = parse_boxes(boxes, 1280)
    assert stmt.account == "YY0000"
    assert stmt.total == 3060.0
    assert [(r.time, r.order_id, r.amount) for r in stmt.days[0].rows] == [
        ("17:40", "3316000000000000001", 280.0)]


def test_a_bracket_below_a_day_header_cannot_restate_the_totals():
    """The account and grand total sit above every day header, so a bracket
    met after a day has opened is chip noise even when nothing else in the row
    says what it is."""
    boxes = [box(39, "測試人【YY0000】", 10), box(1240, "3060.00", 10, w=40)]
    boxes += [box(39, "2026-08-29", 40), box(300, "1", 40), box(1240, "280.00", 40, w=40)]
    boxes += [box(298, "【合格】", 70, w=50), box(1240, "40.00", 70, w=40)]
    stmt = parse_boxes(boxes, 1280)
    assert stmt.account == "YY0000"
    assert stmt.total == 3060.0
    assert stmt.days[0].rows == []


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
