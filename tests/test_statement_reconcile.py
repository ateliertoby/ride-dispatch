from datetime import datetime

import pytest

from ride_dispatch.statement import (
    Statement, StatementDay, StatementRow, reconcile, levenshtein, dates_of, corrected_json,
    penalties_of, format_report, confirm_label, due_dates,
)

NOW = datetime(2026, 8, 26, 12, 0)


def row(date, oid, amount, **kw):
    return StatementRow(date=date, order_id=oid, amount=amount, **kw)


def order(oid, scheduled, price=210.0, banner=0.0, status="active", settlement_id=None, service_type="送机",
          penalty=None, paid_ahead=0.0, ahead_batch=None):
    return {"order_id": oid, "scheduled_time": scheduled, "service_type": service_type, "flight_number": "",
            "pickup": "", "dropoff": "", "price": price, "banner_fee": banner, "tunnel_fee": 0.0,
            "settlement_id": settlement_id, "status": status, "penalty_fee": penalty,
            "paid_ahead": paid_ahead, "ahead_batch": ahead_batch}


def stmt(days, total=None, account="YY0000"):
    return Statement(days=days, total=total, account=account)


def day(date, rows, count=None, s=None):
    return StatementDay(date=date, rows=rows, count=count, sum=s)


def test_levenshtein():
    assert levenshtein("abc", "abc") == 0
    assert levenshtein("9012345678901234", "9012395678901234") == 1
    assert levenshtein("908764321098765", "9087654321098765") == 1
    assert levenshtein("SPACE1", "SPACE2") == 1


def test_json_round_trip():
    s = stmt([day("2026-08-23", [row("2026-08-23", "A1", 280.0, time="09:00", settle_date="2026-08-25")], 1, 280.0)],
             total=280.0)
    s.reader = "test 1"
    s.warnings.append("w")
    d = s.to_json()
    assert d["days"][0]["rows"][0]["settle_date"] == "2026-08-25"
    assert "warnings" not in d              # transient, not stored
    back = Statement.from_json(d)
    assert back.total == 280.0 and back.days[0].sum == 280.0 and back.days[0].rows[0].time == "09:00"


def test_clean_two_day_statement():
    s = stmt([
        day("2026-08-23", [row("2026-08-23", "A1", 280.0), row("2026-08-23", "A2", 210.0),
                           row("2026-08-23", "A2", 40.0)], 3, 530.0),   # 舉牌 line on A2
        day("2026-08-24", [row("2026-08-24", "B1", 210.0)], 1, 210.0),
    ], total=740.0)
    orders = [order("A1", "2026-08-23 09:00:00", 280.0), order("A2", "2026-08-23 13:45:00", 210.0, banner=40.0),
              order("B1", "2026-08-24 10:00:00", 210.0)]
    r = reconcile(s, orders, NOW)
    assert r.checksum == "ok" and r.checksum_notes == []
    assert [e.kind for e in r.entries] == ["matched", "matched", "matched"]
    assert sorted(r.settle_ids) == ["A1", "A2", "B1"]
    assert r.expected == 740.0 and r.confirmed == 740.0 and r.diff == 0
    assert r.can_settle and r.clean
    assert dates_of(s) == ["2026-08-23", "2026-08-24"]


def test_checksum_fail_blocks_settling():
    s = stmt([day("2026-08-23", [row("2026-08-23", "A1", 280.0)], 1, 290.0)], total=290.0)
    r = reconcile(s, [order("A1", "2026-08-23 09:00:00", 280.0)], NOW)
    assert r.checksum == "fail"
    assert r.checksum_notes == ["8月23日 行加埋 $280，求和 $290"]
    assert not r.can_settle


def test_checksum_count_mismatch_fails():
    s = stmt([day("2026-08-23", [row("2026-08-23", "A1", 280.0)], 2, 280.0)], total=280.0)
    r = reconcile(s, [order("A1", "2026-08-23 09:00:00", 280.0)], NOW)
    assert r.checksum == "fail"
    assert r.checksum_notes == ["8月23日 記錄數 2，讀到 1 行"]


def test_checksum_unverified_when_no_totals():
    s = stmt([day("2026-08-23", [row("2026-08-23", "A1", 280.0)])])
    r = reconcile(s, [order("A1", "2026-08-23 09:00:00", 280.0)], NOW)
    assert r.checksum == "unverified"
    assert not r.can_settle


def test_total_verifies_when_day_sum_unreadable():
    s = stmt([day("2026-08-23", [row("2026-08-23", "A1", 280.0)]),
              day("2026-08-24", [row("2026-08-24", "B1", 210.0)], 1, 210.0)], total=490.0)
    r = reconcile(s, [order("A1", "2026-08-23 09:00:00", 280.0), order("B1", "2026-08-24 10:00:00", 210.0)], NOW)
    assert r.checksum == "ok" and r.confirmed == 490.0


def test_confirmed_falls_back_to_day_sums_then_rows():
    s = stmt([day("2026-08-23", [row("2026-08-23", "A1", 280.0)], 1, 280.0)])
    r = reconcile(s, [order("A1", "2026-08-23 09:00:00", 280.0)], NOW)
    assert r.checksum == "ok" and r.confirmed == 280.0


def test_fuzzy_match_within_two_edits_same_day():
    s = stmt([day("2026-08-23", [row("2026-08-23", "9012395678901234", 280.0)], 1, 280.0)], total=280.0)
    r = reconcile(s, [order("9012345678901234", "2026-08-23 09:00:00", 280.0)], NOW)
    e = r.entries[0]
    assert e.kind == "matched" and e.order_id == "9012345678901234" and e.fuzzy
    assert e.statement_id == "9012395678901234"
    assert r.settle_ids == ["9012345678901234"]


def test_fuzzy_match_ignores_neighbour_on_other_date():
    s = stmt([day("2026-08-23", [row("2026-08-23", "9012395678901234", 280.0)], 1, 280.0)], total=280.0)
    r = reconcile(s, [order("9012345678901234", "2026-08-10 09:00:00", 280.0)], NOW)
    assert r.entries[0].kind == "unknown"


def test_fuzzy_match_ambiguous_is_unknown():
    s = stmt([day("2026-08-23", [row("2026-08-23", "9012345678901230", 280.0)], 1, 280.0)], total=280.0)
    orders = [order("9012345678901231", "2026-08-23 09:00:00", 280.0),
              order("9012345678901232", "2026-08-23 10:00:00", 280.0)]
    r = reconcile(s, orders, NOW)
    assert r.entries[0].kind == "unknown"


def test_truncated_id_binds_to_the_one_order_it_starts():
    """The platform's own table cuts a long code short, so the line names its
    order without spelling it out."""
    s = stmt([day("2026-08-23", [row("2026-08-23", "VBK6A85D6FB8089", 280.0, truncated=True)], 1, 280.0)],
             total=280.0)
    r = reconcile(s, [order("VBK6A85D6FB8089ABCD", "2026-08-23 09:00:00", 280.0)], NOW)
    e = r.entries[0]
    assert e.kind == "matched" and e.order_id == "VBK6A85D6FB8089ABCD"
    assert e.statement_id == "VBK6A85D6FB8089"
    assert corrected_json(s, r)["days"][0]["rows"][0]["read_as"] == "VBK6A85D6FB8089"


def test_a_compressed_image_mangles_a_code_and_it_still_binds():
    """Telegram's photo compression is the operator's normal path, and on it
    RapidOCR read K as X and B as 8 inside an alphanumeric code — letters the
    digit fixes must not touch.  The original image binds by exact prefix; the
    compressed one has to bind on the same opening within two edits."""
    s = stmt([day("2026-08-23", [row("2026-08-23", "VBX6A85D6F8808930A", 280.0, truncated=True)], 1, 280.0)],
             total=280.0)
    r = reconcile(s, [order("VBK6A85D6FB808930ABCD", "2026-08-23 09:00:00", 280.0)], NOW)
    e = r.entries[0]
    assert e.kind == "matched" and e.order_id == "VBK6A85D6FB808930ABCD"
    assert r.settle_ids == ["VBK6A85D6FB808930ABCD"]
    # What was actually read is kept, so batch detail can show it.
    assert corrected_json(s, r)["days"][0]["rows"][0]["read_as"] == "VBX6A85D6F8808930A"


def test_two_codes_within_the_bound_leave_the_line_unknown():
    """A prefix is already partial evidence: two openings that both nearly
    agree is not enough to batch money against, even though one is nearer."""
    s = stmt([day("2026-08-23", [row("2026-08-23", "VBX6A85D6F8808930A", 280.0, truncated=True)], 1, 280.0)],
             total=280.0)
    orders = [order("VBK6A85D6FB808930ABCD", "2026-08-23 09:00:00", 280.0),   # 2 edits
              order("VBX6A85D6F8808931AWXY", "2026-08-23 10:00:00", 280.0)]   # 1 edit
    assert reconcile(s, orders, NOW).entries[0].kind == "unknown"


def test_a_code_three_edits_out_stays_unknown():
    s = stmt([day("2026-08-23", [row("2026-08-23", "VBX6A85D6F8808930A", 280.0, truncated=True)], 1, 280.0)],
             total=280.0)
    r = reconcile(s, [order("VXX6A85D6F0808931ABCD", "2026-08-23 09:00:00", 280.0)], NOW)
    assert r.entries[0].kind == "unknown"


def test_a_mangled_code_only_binds_on_the_statements_dates():
    s = stmt([day("2026-08-23", [row("2026-08-23", "VBX6A85D6F8808930A", 280.0, truncated=True)], 1, 280.0)],
             total=280.0)
    r = reconcile(s, [order("VBK6A85D6FB808930ABCD", "2026-08-10 09:00:00", 280.0)], NOW)
    assert r.entries[0].kind == "unknown"


def test_two_orders_sharing_the_prefix_leave_the_line_unknown():
    s = stmt([day("2026-08-23", [row("2026-08-23", "VBK6A85D6FB8089", 280.0, truncated=True)], 1, 280.0)],
             total=280.0)
    orders = [order("VBK6A85D6FB8089ABCD", "2026-08-23 09:00:00", 280.0),
              order("VBK6A85D6FB8089WXYZ", "2026-08-23 10:00:00", 280.0)]
    assert reconcile(s, orders, NOW).entries[0].kind == "unknown"


def test_a_prefix_only_binds_on_the_statements_dates():
    s = stmt([day("2026-08-23", [row("2026-08-23", "VBK6A85D6FB8089", 280.0, truncated=True)], 1, 280.0)],
             total=280.0)
    r = reconcile(s, [order("VBK6A85D6FB8089ABCD", "2026-08-10 09:00:00", 280.0)], NOW)
    assert r.entries[0].kind == "unknown"


def test_a_long_id_binds_by_prefix_even_without_an_ellipsis():
    """OCR loses the ellipsis when it lands in its own box; ten characters of
    agreement is already more than a coincidence."""
    s = stmt([day("2026-08-23", [row("2026-08-23", "1128150000000001", 280.0)], 1, 280.0)], total=280.0)
    r = reconcile(s, [order("11281500000000019", "2026-08-23 09:00:00", 280.0)], NOW)
    assert r.entries[0].order_id == "11281500000000019"


def test_a_short_id_never_binds_by_prefix():
    s = stmt([day("2026-08-23", [row("2026-08-23", "A1", 280.0)], 1, 280.0)], total=280.0)
    r = reconcile(s, [order("A1234567", "2026-08-23 09:00:00", 280.0)], NOW)
    assert r.entries[0].kind == "unknown"


def test_an_exact_id_beats_a_prefix_of_it():
    """A line naming its order outright must keep it, whatever a shorter line
    on the same statement would have taken."""
    s = stmt([day("2026-08-23", [row("2026-08-23", "VBK6A85D6FB8089ABCD", 280.0),
                                 row("2026-08-23", "VBK6A85D6FB8089", 210.0, truncated=True)], 2, 490.0)],
             total=490.0)
    orders = [order("VBK6A85D6FB8089ABCD", "2026-08-23 09:00:00", 280.0),
              order("VBK6A85D6FB8089QQQQ", "2026-08-23 10:00:00", 210.0)]
    r = reconcile(s, orders, NOW)
    assert [(e.statement_id, e.order_id) for e in r.entries] == [
        ("VBK6A85D6FB8089ABCD", "VBK6A85D6FB8089ABCD"),
        ("VBK6A85D6FB8089", "VBK6A85D6FB8089QQQQ")]


def test_exact_match_beats_fuzzy_neighbour():
    s = stmt([day("2026-08-23", [row("2026-08-23", "A1", 280.0)], 1, 280.0)], total=280.0)
    r = reconcile(s, [order("A1", "2026-08-23 09:00:00", 280.0), order("A2", "2026-08-23 10:00:00", 280.0)], NOW)
    assert r.entries[0].kind == "matched" and r.entries[0].order_id == "A1" and not r.entries[0].fuzzy


def mixed_fixture():
    """One statement covering every entry kind at once, plus an order the
    statement leaves out."""
    s = stmt([day("2026-08-23", [
        row("2026-08-23", "OK", 210.0),
        row("2026-08-23", "DIFF", 210.0),
        row("2026-08-23", "DONE", 210.0),
        row("2026-08-23", "GONE", 210.0),
        row("2026-08-23", "NEW", 210.0),
        row("2026-08-23", "LATE", 210.0),
        row("2026-08-23", "FREE", 210.0),
    ], 7, 1470.0)], total=1470.0)
    orders = [
        order("OK", "2026-08-23 09:00:00", 210.0),
        order("DIFF", "2026-08-23 10:00:00", 250.0),
        order("DONE", "2026-08-23 11:00:00", 210.0, settlement_id=3),
        order("GONE", "2026-08-23 12:00:00", 210.0, status="cancelled"),
        order("LATE", "2026-08-27 09:00:00", 210.0),       # still in the future at NOW
        order("FREE", "2026-08-23 14:00:00", 0.0),          # unpriced
        order("HELD", "2026-08-23 15:00:00", 210.0),        # settleable, not on the statement
    ]
    return s, orders


def test_categories_and_missing():
    s, orders = mixed_fixture()
    r = reconcile(s, orders, NOW)
    kinds = {e.statement_id: e.kind for e in r.entries}
    assert kinds == {"OK": "matched", "DIFF": "amount_diff", "DONE": "already_settled", "GONE": "adjustment",
                     "NEW": "unknown", "LATE": "not_ready", "FREE": "not_ready"}
    diff_entry = next(e for e in r.entries if e.statement_id == "DIFF")
    assert diff_entry.platform_amount == 210.0 and diff_entry.expected == 250.0
    assert next(e for e in r.entries if e.statement_id == "DONE").settlement_id == 3
    assert [o["order_id"] for o in r.missing] == ["HELD"]
    assert sorted(r.settle_ids) == ["DIFF", "OK"]
    # The cancelled trip's money is on the transfer whatever the trip is, so it
    # is recorded on the batch and counted in what the batch is owed.
    assert r.adjustments == [{"order_ref": "GONE", "date": "2026-08-23", "amount": 210.0}]
    assert r.expected == 670.0 and r.confirmed == 1470.0 and r.diff == 800.0
    assert r.can_settle and not r.clean


def test_resend_after_settling_has_nothing_to_settle():
    s = stmt([day("2026-08-23", [row("2026-08-23", "A1", 280.0)], 1, 280.0)], total=280.0)
    r = reconcile(s, [order("A1", "2026-08-23 09:00:00", 280.0, settlement_id=4)], NOW)
    assert r.entries[0].kind == "already_settled"
    assert r.settle_ids == [] and not r.can_settle and r.missing == []


def test_empty_statement():
    r = reconcile(stmt([]), [], NOW)
    assert r.checksum == "unverified" and r.entries == [] and not r.can_settle and r.confirmed is None


def test_fuzzy_cannot_claim_an_order_twice():
    s = stmt([day("2026-08-23", [row("2026-08-23", "SPACE1", 210.0),
                                 row("2026-08-23", "SPACE2", 210.0)], 2, 420.0)], total=420.0)
    r = reconcile(s, [order("SPACE1", "2026-08-23 09:00:00", 210.0)], NOW)
    assert [(e.statement_id, e.kind, e.fuzzy) for e in r.entries] == [
        ("SPACE1", "matched", False), ("SPACE2", "unknown", False)]
    assert r.settle_ids == ["SPACE1"]
    assert r.expected == 210.0
    assert not r.clean


def test_two_fuzzy_rows_contesting_one_order_are_both_unknown():
    s = stmt([day("2026-08-23", [row("2026-08-23", "9012345678901230", 280.0),
                                 row("2026-08-23", "9012345678901231", 280.0)], 2, 560.0)], total=560.0)
    r = reconcile(s, [order("9012345678901239", "2026-08-23 09:00:00", 280.0)], NOW)
    assert [e.kind for e in r.entries] == ["unknown", "unknown"]
    assert r.settle_ids == []


def shape(r):
    return [(e.statement_id, e.kind, e.order_id) for e in r.entries]


def test_result_independent_of_candidate_order():
    s, orders = mixed_fixture()
    a = reconcile(s, orders, NOW)
    b = reconcile(s, list(reversed(orders)), NOW)
    assert shape(a) == shape(b)
    assert sorted(a.settle_ids) == sorted(b.settle_ids)
    assert a.expected == b.expected
    assert [o["order_id"] for o in a.missing] == [o["order_id"] for o in b.missing]


# ---- 判罰賠款 ----

def penalty_stmt(penalty=-97.38, trip=280.0, oid="A1"):
    """The shape the platform prints: the fine repeats its trip's order id."""
    total = round(trip + penalty, 2)
    return stmt([day("2026-08-23", [row("2026-08-23", oid, trip),
                                    row("2026-08-23", oid, penalty)], 2, total)], total=total)


def test_a_penalty_row_explains_the_whole_difference():
    """Nothing is wrong with the pricing: the platform paid the fare and took a
    fine off it, so the batch is settleable and owes exactly what arrived."""
    r = reconcile(penalty_stmt(), [order("A1", "2026-08-23 09:00:00", 280.0)], NOW)
    e = r.entries[0]
    assert e.kind == "penalty" and e.penalty == -97.38
    assert e.platform_amount == 182.62 and e.expected == 280.0
    assert r.settle_ids == ["A1"]
    assert r.expected == 182.62 and r.confirmed == 182.62 and r.diff == 0
    assert r.can_settle and r.clean


def test_resending_a_recorded_penalty_folds_to_matched():
    """The idempotency hinge: expected_of nets the stored fine, so the second
    read of the same image agrees with the platform and settles nothing new."""
    r = reconcile(penalty_stmt(), [order("A1", "2026-08-23 09:00:00", 280.0, penalty=97.38)], NOW)
    assert r.entries[0].kind == "matched"
    assert r.entries[0].expected == 182.62
    assert r.expected == 182.62 and r.diff == 0 and r.clean


def test_a_penalty_on_a_mispriced_order_stays_an_amount_difference():
    """The fine is still recorded — a negative row is reliable — but the fare
    the platform paid disagrees with the system's, which is the operator's to
    settle, so the 差額 measures only that."""
    r = reconcile(penalty_stmt(), [order("A1", "2026-08-23 09:00:00", 250.0)], NOW)
    e = r.entries[0]
    assert e.kind == "amount_diff" and e.penalty == -97.38
    assert r.settle_ids == ["A1"]
    assert r.expected == 152.62 and r.diff == 30.0
    assert not r.clean


def test_a_penalty_against_a_settled_order_is_display_only():
    """A fine arriving after its trip was batched would have to reopen a frozen
    batch, so nothing is written and the shortfall stays visible."""
    r = reconcile(penalty_stmt(), [order("A1", "2026-08-23 09:00:00", 280.0, settlement_id=7)], NOW)
    e = r.entries[0]
    assert e.kind == "already_settled" and e.penalty == -97.38 and e.settlement_id == 7
    assert r.settle_ids == [] and not r.can_settle


def test_a_penalty_for_an_unknown_id_stays_unknown():
    """A fare and a fine under a number the book does not know still nets
    income, and income is leg-shaped: the group is an alarm, not a batch line."""
    r = reconcile(penalty_stmt(oid="NEW"), [], NOW)
    assert r.entries[0].kind == "unknown"
    assert r.settle_ids == [] and r.expected == 0.0 and r.adjustments == []


def test_a_penalty_alongside_a_clean_leg_settles_both():
    s = stmt([day("2026-08-23", [row("2026-08-23", "A1", 300.0),
                                 row("2026-08-23", "A2", 280.0),
                                 row("2026-08-23", "A2", -97.38)], 3, 482.62)], total=482.62)
    orders = [order("A1", "2026-08-23 09:00:00", 300.0), order("A2", "2026-08-23 13:00:00", 280.0)]
    r = reconcile(s, orders, NOW)
    assert [e.kind for e in r.entries] == ["matched", "penalty"]
    assert sorted(r.settle_ids) == ["A1", "A2"]
    assert r.expected == 482.62 and r.diff == 0 and r.clean


def test_a_penalty_bigger_than_its_trip_still_settles_net():
    """Nothing caps a fine at the fare, and the reader keeps the sign, so a day
    can be worth less than nothing."""
    r = reconcile(penalty_stmt(penalty=-330.0), [order("A1", "2026-08-23 09:00:00", 280.0)], NOW)
    assert r.entries[0].kind == "penalty"
    assert r.expected == -50.0 and r.confirmed == -50.0 and r.diff == 0


# ---- 帳項: statement lines the batch records itself ----

def with_leg(rows, leg=280.0):
    """A settleable leg plus whatever else the statement carries that day."""
    all_rows = [row("2026-08-23", "A1", leg)] + rows
    total = round(sum(r.amount for r in all_rows), 2)
    return stmt([day("2026-08-23", all_rows, len(all_rows), total)], total=total)


LEG = [order("A1", "2026-08-23 09:00:00", 280.0)]


def test_a_waived_penalty_pair_is_recorded_as_the_two_lines_it_was():
    """The 改派 case: a fine and the 免責 line that cancels it, under an order
    number that is nobody's leg.  Nothing is owed either way, and the statement
    reads as fully explained rather than as something to look at by hand."""
    r = reconcile(with_leg([row("2026-08-23", "X9", -30.0), row("2026-08-23", "X9", 30.0)]), LEG, NOW)
    assert [e.kind for e in r.entries] == ["matched", "adjustment"]
    assert r.adjustments == [{"order_ref": "X9", "date": "2026-08-23", "amount": -30.0},
                             {"order_ref": "X9", "date": "2026-08-23", "amount": 30.0}]
    assert r.expected == 280.0 and r.confirmed == 280.0 and r.diff == 0
    assert r.settle_ids == ["A1"] and r.clean


def test_a_penalty_with_no_waiver_is_carried_by_the_batch():
    """The case the pair was hiding: without the 免責 line the transfer really
    is short, and the batch has to be owed less or the gap never closes."""
    r = reconcile(with_leg([row("2026-08-23", "X9", -30.0)]), LEG, NOW)
    assert [e.kind for e in r.entries] == ["matched", "adjustment"]
    assert r.adjustments == [{"order_ref": "X9", "date": "2026-08-23", "amount": -30.0}]
    assert r.expected == 250.0 and r.confirmed == 250.0 and r.diff == 0
    assert r.clean


def test_a_fine_bigger_than_what_offsets_it_is_still_the_batchs_to_carry():
    """What decides is the net, not the shape: a group that costs money is a
    line of the transfer even when part of it was given back."""
    r = reconcile(with_leg([row("2026-08-23", "X9", -50.0), row("2026-08-23", "X9", 20.0)]), LEG, NOW)
    assert [e.kind for e in r.entries] == ["matched", "adjustment"]
    assert [a["amount"] for a in r.adjustments] == [-50.0, 20.0]
    assert r.expected == 250.0 and r.confirmed == 250.0 and r.diff == 0 and r.clean


def test_a_lone_positive_under_an_unknown_id_stays_the_alarm_it_is():
    """The safety rule: money coming in under a number the book does not know
    can be a leg whose number was misread, and booking it as a batch line would
    swallow that."""
    r = reconcile(with_leg([row("2026-08-23", "X9", 210.0)]), LEG, NOW)
    assert [e.kind for e in r.entries] == ["matched", "unknown"]
    assert r.adjustments == []
    assert r.expected == 280.0 and r.diff == 210.0 and not r.clean


def test_income_under_an_unknown_id_is_an_alarm_even_with_a_fine_beside_it():
    """A fine attached to it does not make income explainable: a leg the book
    never got looks exactly like this, and booking it would read as clean while
    the money that arrived goes unaccounted for.  When the leg IS in the book
    but its number was misread, it is held back as well — two alarms, one
    statement."""
    orders = LEG + [order("990000000000000001", "2026-08-23 13:00:00", 210.0)]
    r = reconcile(with_leg([row("2026-08-23", "770000000000000009", 210.0),
                            row("2026-08-23", "770000000000000009", -30.0)]), orders, NOW)
    assert [e.kind for e in r.entries] == ["matched", "unknown"]
    assert r.adjustments == []
    assert [o["order_id"] for o in r.missing] == ["990000000000000001"]
    assert not r.clean


def test_a_cancelled_trips_money_is_the_batchs_to_carry():
    """The number is verified real and cannot enter a batch, so whatever the
    platform booked under it is a line of the transfer."""
    orders = LEG + [order("GONE", "2026-08-23 12:00:00", 210.0, status="cancelled")]
    r = reconcile(with_leg([row("2026-08-23", "GONE", -30.0)]), orders, NOW)
    assert [e.kind for e in r.entries] == ["matched", "adjustment"]
    assert r.adjustments == [{"order_ref": "GONE", "date": "2026-08-23", "amount": -30.0}]
    assert r.expected == 250.0 and r.diff == 0 and r.clean


def test_a_late_fine_is_recorded_here_and_the_frozen_batch_left_alone():
    """The fine came off this transfer, so this batch carries it; the trip
    itself stays a read of the batch that already holds the leg."""
    orders = LEG + [order("DONE", "2026-08-23 11:00:00", 210.0, settlement_id=7)]
    r = reconcile(with_leg([row("2026-08-23", "DONE", 210.0),
                            row("2026-08-23", "DONE", -30.0)]), orders, NOW)
    assert [e.kind for e in r.entries] == ["matched", "already_settled"]
    assert r.adjustments == [{"order_ref": "DONE", "date": "2026-08-23", "amount": -30.0}]
    # The trip's own 210 belongs to batch #7 and is not this batch's to expect.
    assert r.expected == 250.0 and r.confirmed == 460.0
    assert not r.clean


def test_a_late_fine_already_taken_off_its_order_is_not_recorded_twice():
    """Idempotency: expected_of nets a stored penalty_fee, so a second read of
    a statement that was confirmed has nothing left to record."""
    orders = LEG + [order("DONE", "2026-08-23 11:00:00", 210.0, settlement_id=7, penalty=30.0)]
    r = reconcile(with_leg([row("2026-08-23", "DONE", 210.0),
                            row("2026-08-23", "DONE", -30.0)]), orders, NOW)
    assert [e.kind for e in r.entries] == ["matched", "already_settled"]
    assert r.adjustments == []
    assert r.expected == 280.0


def test_a_fine_on_a_leg_of_this_batch_stays_on_the_order():
    """The boundary: a cost of a leg the batch holds belongs to that leg, not
    to the batch, so nothing about it becomes a batch line."""
    r = reconcile(penalty_stmt(), [order("A1", "2026-08-23 09:00:00", 280.0)], NOW)
    assert r.entries[0].kind == "penalty" and r.adjustments == []
    assert penalties_of(r) == {"A1": 97.38}


# ---- 舉牌先結: a 舉牌 line paid ahead of its trip ----

# A 接機 with a 舉牌 on the 23rd, and a leg of its own on the 25th.
HELD = order("B1", "2026-08-23 22:00:00", 300.0, banner=40.0, service_type="接机")
LATER_LEG = order("A1", "2026-08-25 10:00:00", 210.0)


def ahead_stmt():
    """The platform held the trip back and paid its 舉牌 line anyway."""
    return stmt([day("2026-08-23", [row("2026-08-23", "B1", 40.0)], 1, 40.0),
                 day("2026-08-25", [row("2026-08-25", "A1", 210.0)], 1, 210.0)], total=250.0)


def test_a_lone_banner_line_is_paid_ahead_and_its_trip_stays_unsettled():
    """The 舉牌 is money on this transfer, so the batch carries it; the trip
    was held back, so the order is not a leg of the batch and stays owed."""
    r = reconcile(ahead_stmt(), [HELD, LATER_LEG], NOW)
    assert [e.kind for e in r.entries] == ["ahead", "matched"]
    assert r.settle_ids == ["A1"]
    assert r.adjustments == [{"order_ref": "B1", "date": "2026-08-23", "amount": 40.0, "ahead": True}]
    assert r.expected == 250.0 and r.confirmed == 250.0 and r.diff == 0
    assert r.missing == []
    # The trip is still to come, which is something to chase: not clean.
    assert r.can_settle and not r.clean


def test_the_card_names_the_banner_paid_ahead_and_the_trip_still_held():
    r = reconcile(ahead_stmt(), [HELD, LATER_LEG], NOW)
    report = format_report(r)
    assert "  舉牌先結  #…B1  $40 · 行程 $300 抽起 — 記入今次" in report
    assert "系統應收 $250 · 差額 $0" in report
    assert confirm_label(r) == "照平台數確認 + 記帳項 · 1 程 · $250（差額 $0）"


def test_the_trip_that_follows_is_owed_only_what_was_not_paid_ahead():
    s = stmt([day("2026-08-23", [row("2026-08-23", "B1", 300.0)], 1, 300.0)], total=300.0)
    held = order("B1", "2026-08-23 22:00:00", 300.0, banner=40.0, service_type="接机",
                 paid_ahead=40.0, ahead_batch=7)
    r = reconcile(s, [held], NOW)
    assert [e.kind for e in r.entries] == ["matched"]
    assert r.settle_ids == ["B1"] and r.adjustments == []
    assert r.expected == 300.0 and r.diff == 0 and r.clean


def test_a_fine_on_the_trip_that_follows_nets_against_what_is_still_owed():
    s = stmt([day("2026-08-23", [row("2026-08-23", "B1", 300.0), row("2026-08-23", "B1", -30.0)],
                  2, 270.0)], total=270.0)
    held = order("B1", "2026-08-23 22:00:00", 300.0, banner=40.0, service_type="接机",
                 paid_ahead=40.0, ahead_batch=7)
    r = reconcile(s, [held], NOW)
    assert [e.kind for e in r.entries] == ["penalty"]
    assert penalties_of(r) == {"B1": 30.0}
    assert r.expected == 270.0 and r.diff == 0


def test_a_second_read_of_a_banner_already_paid_ahead_records_nothing():
    """Re-sending the image after the confirm: the 舉牌 is already on batch #7
    and the trip is still owed, so there is nothing new to write."""
    held = order("B1", "2026-08-23 22:00:00", 300.0, banner=40.0, service_type="接机",
                 paid_ahead=40.0, ahead_batch=7)
    leg = order("A1", "2026-08-25 10:00:00", 210.0, settlement_id=7)
    r = reconcile(ahead_stmt(), [held, leg], NOW)
    assert [e.kind for e in r.entries] == ["already_ahead", "already_settled"]
    assert r.entries[0].settlement_id == 7
    assert r.settle_ids == [] and r.adjustments == [] and not r.can_settle
    assert "  舉牌先結  #…B1  $40 已喺批次 #7 · 行程 $300 抽起" in format_report(r)


def test_a_held_trip_left_off_a_later_statement_is_owed_net_of_its_banner():
    s = stmt([day("2026-08-23", [row("2026-08-23", "A1", 210.0)], 1, 210.0)], total=210.0)
    held = order("B1", "2026-08-23 22:00:00", 300.0, banner=40.0, service_type="接机",
                 paid_ahead=40.0, ahead_batch=7)
    r = reconcile(s, [order("A1", "2026-08-23 10:00:00", 210.0), held], NOW)
    assert [o["order_id"] for o in r.missing] == ["B1"]
    assert "  抽起  #…B1  $300（今次冇計）" in format_report(r)


def test_a_banner_sized_line_on_an_order_without_a_banner_is_an_amount_diff():
    """Only the order's own 舉牌 fee can say what the line is: a trip that
    carries none has no part the platform could have paid ahead."""
    bare = order("B1", "2026-08-23 22:00:00", 300.0, service_type="接机")
    r = reconcile(ahead_stmt(), [bare, LATER_LEG], NOW)
    assert [e.kind for e in r.entries] == ["amount_diff", "matched"]
    assert sorted(r.settle_ids) == ["A1", "B1"] and r.adjustments == []


def test_a_banner_beside_a_trip_line_priced_at_zero_is_not_paid_ahead():
    """The trip line is on the statement, so nothing was held back: the
    platform paid the trip nothing, which is a discrepancy to chase."""
    s = stmt([day("2026-08-23", [row("2026-08-23", "B1", 0.0), row("2026-08-23", "B1", 40.0)],
                  2, 40.0)], total=40.0)
    r = reconcile(s, [HELD], NOW)
    assert [e.kind for e in r.entries] == ["amount_diff"]
    assert r.settle_ids == ["B1"] and r.adjustments == []


def test_a_statement_of_nothing_but_a_banner_paid_ahead_makes_no_batch():
    """The line is recorded on a batch, and with no leg there is no batch to
    record it on: the card says so rather than promising a write."""
    s = stmt([day("2026-08-23", [row("2026-08-23", "B1", 40.0)], 1, 40.0)], total=40.0)
    r = reconcile(s, [HELD], NOW)
    assert [e.kind for e in r.entries] == ["ahead"]
    assert r.settle_ids == [] and r.adjustments == [] and not r.can_settle
    assert "  舉牌先結  #…B1  $40 · 行程 $300 抽起 — 要人手處理" in format_report(r)


def test_corrected_json_rewrites_fuzzy_ids():
    s = stmt([day("2026-08-23", [row("2026-08-23", "9012345678901238", 280.0),
                                 row("2026-08-23", "A1", 210.0)], 2, 490.0)], total=490.0)
    orders = [order("9012345678901234", "2026-08-23 09:00:00", 280.0),
              order("A1", "2026-08-23 13:45:00", 210.0)]
    r = reconcile(s, orders, NOW)
    assert [(e.statement_id, e.order_id, e.fuzzy) for e in r.entries] == [
        ("9012345678901238", "9012345678901234", True), ("A1", "A1", False)]

    d = corrected_json(s, r)
    fuzzy, exact = d["days"][0]["rows"]
    assert fuzzy["order_id"] == "9012345678901234"
    assert fuzzy["read_as"] == "9012345678901238"
    assert exact["order_id"] == "A1" and "read_as" not in exact
    # The stored JSON is read back by Statement.from_json, which must survive
    # the extra key rather than reject the whole statement.
    back = Statement.from_json(d)
    assert [r.order_id for r in back.days[0].rows] == ["9012345678901234", "A1"]


# ---- the due dates a stored statement prints ----

def _stored(*days):
    return {"days": [{"date": d, "rows": [{"order_id": f"X{i}", "amount": 100.0, **row}
                                          for i, row in enumerate(rows)]} for d, rows in days]}


def test_due_dates_are_the_distinct_values_in_order():
    stored = _stored(("2026-09-12", [{"settle_date": "2026-09-15"}, {"settle_date": "2026-09-15"}]),
                     ("2026-09-10", [{"settle_date": "2026-09-13"}]),
                     ("2026-09-30", [{"settle_date": "2026-10-02"}]))
    assert due_dates(stored) == ["2026-09-13", "2026-09-15", "2026-10-02"]


def test_due_dates_of_a_batch_without_a_statement_are_none():
    assert due_dates(None) == []
    assert due_dates({}) == []
    assert due_dates({"days": []}) == []
    assert due_dates({"days": [{"date": "2026-09-12"}]}) == []


def test_due_dates_skip_a_row_that_carries_none():
    stored = _stored(("2026-09-12", [{}, {"settle_date": None}, {"settle_date": ""},
                                     {"settle_date": "2026-09-15"}]))
    assert due_dates(stored) == ["2026-09-15"]
    # Nothing is worked out from the service day of a statement that prints none.
    assert due_dates(_stored(("2026-09-12", [{}, {"settle_date": None}]))) == []


@pytest.mark.parametrize("value", [
    "2026/09/15", "9/15", "2026-9-15", "20260915", "2026-09-15 00:00", " 2026-09-15", "2026-13-01",
    "2026-02-30", "二零二六", 20260915, ["2026-09-15"],
])
def test_due_dates_skip_a_value_that_is_not_a_date(value):
    stored = _stored(("2026-09-12", [{"settle_date": value}, {"settle_date": "2026-09-16"}]))
    assert due_dates(stored) == ["2026-09-16"]
