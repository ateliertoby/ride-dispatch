from datetime import datetime

from ride_dispatch.statement import (
    Statement, StatementDay, StatementRow, reconcile, levenshtein, dates_of, corrected_json,
)

NOW = datetime(2026, 8, 26, 12, 0)


def row(date, oid, amount, **kw):
    return StatementRow(date=date, order_id=oid, amount=amount, **kw)


def order(oid, scheduled, price=210.0, banner=0.0, status="active", settlement_id=None, service_type="送机"):
    return {"order_id": oid, "scheduled_time": scheduled, "service_type": service_type, "flight_number": "",
            "pickup": "", "dropoff": "", "price": price, "banner_fee": banner, "tunnel_fee": 0.0,
            "settlement_id": settlement_id, "status": status}


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
    assert kinds == {"OK": "matched", "DIFF": "amount_diff", "DONE": "already_settled", "GONE": "cancelled",
                     "NEW": "unknown", "LATE": "not_ready", "FREE": "not_ready"}
    diff_entry = next(e for e in r.entries if e.statement_id == "DIFF")
    assert diff_entry.platform_amount == 210.0 and diff_entry.expected == 250.0
    assert next(e for e in r.entries if e.statement_id == "DONE").settlement_id == 3
    assert [o["order_id"] for o in r.missing] == ["HELD"]
    assert sorted(r.settle_ids) == ["DIFF", "OK"]
    assert r.expected == 460.0 and r.confirmed == 1470.0 and r.diff == 1010.0
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
