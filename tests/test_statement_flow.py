"""The statement intake both frontends share, tested without either of them.

Everything here used to live inside the bot's photo handler; these are the
branches that decide what a statement means and what confirming one writes.
"""
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from ride_dispatch import credits, statement, statement_flow
from ride_dispatch.db import (
    init_db, save_order, update_price, insert_credit, get_credit, get_order_by_id,
    get_settlement, open_batches, create_settlement, allocate, statements_dir,
)
from ride_dispatch.parser import Order
from ride_dispatch.statement import Statement, StatementDay, StatementRow

NOW = datetime.now()
TODAY = NOW.strftime("%Y-%m-%d")
YESTERDAY = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
TWO_DAYS = (NOW - timedelta(days=2)).strftime("%Y-%m-%d")


@pytest.fixture
def db_path():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "orders.db")
    init_db(path)
    yield path


def make_order(order_id, scheduled):
    return Order(order_id=order_id, service_type="送机", vehicle_type="经济5座",
                 passenger_name="TEST/USER", scheduled_time=scheduled,
                 passenger_phone="86 13800000000", overseas_phone="", flight_number="",
                 pickup="尖沙咀", dropoff="香港国际机场 T1", distance_km=30, notes="",
                 driver_notes="", additional_services="", passenger_exit_minutes=None,
                 third_party_contact="", more_contacts="", raw_message="raw")


def seed(db_path, oid, scheduled, price=210.0):
    save_order(db_path, make_order(oid, scheduled), telegram_msg_id=1, parking=0.0, source="携程")
    update_price(db_path, oid, price)


def credit_row(db_path, ref, amount, value_date):
    return insert_credit(db_path, {"ref": ref, "platform": "ride", "amount": amount,
                                   "currency": "HKD", "value_date": value_date,
                                   "payer": "A B**** C***** L", "memo": "SUPPLIERPAY",
                                   "email_id": None, "received_at": None, "recorded_at": None})


def stmt_for(rows_by_date, total):
    days = []
    for date, rows in rows_by_date.items():
        days.append(StatementDay(date=date, count=len(rows), sum=round(sum(a for _, a in rows), 2),
                                 rows=[StatementRow(date=date, order_id=oid, amount=a, time="09:00",
                                                    settle_date=date) for oid, a in rows]))
    return Statement(days=days, account="YY0000", total=total, reader="test")


# ---- prepare ----

def test_prepare_reconciles_and_labels_the_confirm(db_path):
    seed(db_path, "A1", f"{TWO_DAYS} 09:00:00", 280.0)
    seed(db_path, "B1", f"{YESTERDAY} 10:00:00", 210.0)
    p = statement_flow.prepare(
        db_path, stmt_for({TWO_DAYS: [("A1", 280.0)], YESTERDAY: [("B1", 210.0)]}, 490.0), NOW)
    assert p.can_settle
    assert p.report.startswith("結算單 YY0000 · 2 日 2 行 · 平台 $490")
    assert "差額 $0" in p.report
    assert p.dates == [TWO_DAYS, YESTERDAY]
    assert p.total == 490.0
    assert p.confirm_label == "確認結算 · 2 程 · $490"
    assert p.credit_id is None and p.short_pair is None
    assert p.credit_line == credits.NO_CREDIT_YET


def test_prepare_stores_corrected_ids(db_path):
    """A near-miss id is bound to the order it names, and the stored JSON keeps
    what was actually read."""
    seed(db_path, "A1", f"{YESTERDAY} 10:00:00", 210.0)
    p = statement_flow.prepare(db_path, stmt_for({YESTERDAY: [("A2", 210.0)]}, 210.0), NOW)
    stored = p.stmt_json["days"][0]["rows"][0]
    assert stored["order_id"] == "A1" and stored["read_as"] == "A2"


def test_prepare_names_the_credit_its_total_agrees_with(db_path):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 2540.0)
    cid = credit_row(db_path, "R1", 2540.0, TODAY)
    p = statement_flow.prepare(db_path, stmt_for({YESTERDAY: [("A1", 2540.0)]}, 2540.0), NOW)
    assert p.credit_id == cid
    assert p.credit_line == f"對到入數 {credits.md(TODAY)} $2,540"
    assert p.confirm_label == "確認結算 + 對入數 · 1 程 · $2,540"
    assert p.short_pair is None


def test_prepare_takes_the_credit_that_pays_it_short(db_path):
    seed(db_path, "A1", f"{TWO_DAYS} 09:00:00", 2950.0)
    seed(db_path, "A2", f"{TWO_DAYS} 10:00:00", 510.0)
    cid = credit_row(db_path, "R1", 2950.0, YESTERDAY)
    p = statement_flow.prepare(
        db_path, stmt_for({TWO_DAYS: [("A1", 2950.0), ("A2", 510.0)]}, 3460.0), NOW)
    assert p.credit_id == cid
    assert p.credit_line == f"對到入數 {credits.md(YESTERDAY)} $2,950，差 $510"
    assert p.short_pair == (2950.0, 510.0)
    assert p.confirm_label == "確認結算 + 對入數 $2,950（差 $510）"


def test_prepare_lists_credits_that_could_contain_it(db_path):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 1450.0)
    credit_row(db_path, "R1", 2950.0, TODAY)
    p = statement_flow.prepare(db_path, stmt_for({YESTERDAY: [("A1", 1450.0)]}, 1450.0), NOW)
    assert p.credit_id is None
    assert p.credit_line.split("\n")[0] == "入數可能係："
    assert p.confirm_label == "確認結算 · 1 程 · $1,450"


def test_prepare_offers_to_archive_a_credit_with_no_orders(db_path):
    """The money is ours but the legs never reached the system: there is no
    batch to create, only a credit to take out of the queue."""
    cid = credit_row(db_path, "R1", 1000.0, TODAY)
    p = statement_flow.prepare(
        db_path, stmt_for({YESTERDAY: [("Z1", 600.0), ("Z2", 400.0)]}, 1000.0), NOW)
    assert not p.can_settle
    assert p.confirm_label == ""
    assert p.no_orders_credit["id"] == cid
    assert p.credit_line == f"對到入數 {credits.md(TODAY)} $1,000，但圖入面 2 張單唔喺系統"


def test_prepare_offers_nothing_when_a_statement_cannot_settle(db_path):
    credit_row(db_path, "R1", 999.0, TODAY)
    p = statement_flow.prepare(db_path, stmt_for({YESTERDAY: [("Z1", 1000.0)]}, 1000.0), NOW)
    assert not p.can_settle
    assert p.no_orders_credit is None and p.credit_line == ""
    assert p.report.endswith("冇單可以入 batch")


def test_prepare_will_not_settle_a_statement_that_does_not_add_up(db_path):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 280.0)
    s = stmt_for({YESTERDAY: [("A1", 280.0)]}, 280.0)
    s.days[0].sum = 290.0
    p = statement_flow.prepare(db_path, s, NOW)
    assert not p.can_settle and p.confirm_label == ""
    assert "讀圖唔一致" in p.report


# ---- confirm ----

def test_confirm_writes_the_batch_and_keeps_the_image(db_path):
    seed(db_path, "A1", f"{TWO_DAYS} 09:00:00", 280.0)
    seed(db_path, "B1", f"{YESTERDAY} 10:00:00", 210.0)
    p = statement_flow.prepare(
        db_path, stmt_for({TWO_DAYS: [("A1", 280.0)], YESTERDAY: [("B1", 210.0)]}, 490.0), NOW)
    done = statement_flow.confirm(db_path, p, b"\xff\xd8img", NOW)
    b = open_batches(db_path, "ride")[0]
    assert done.settlement_id == b["id"]
    assert b["confirmed_amount"] == 490.0 and b["expected_amount"] == 490.0
    assert b["statement"]["reader"] == "test"
    assert get_order_by_id(db_path, "A1")["settlement_id"] == b["id"]
    assert done.text.startswith(f"已結算 批次 #{b['id']}")
    assert f"{statement.date_span_label(p.dates)} 共2程 HKD 490 確認無誤" in done.text
    assert done.batch is None and done.credit_id is None


def test_confirm_records_the_penalties_the_label_promised(db_path):
    seed(db_path, "A1", f"{TWO_DAYS} 09:00:00", 300.0)
    seed(db_path, "B1", f"{YESTERDAY} 13:00:00", 280.0)
    day1 = StatementDay(date=TWO_DAYS, count=1, sum=300.0,
                        rows=[StatementRow(date=TWO_DAYS, order_id="A1", amount=300.0, time="09:00")])
    day2 = StatementDay(date=YESTERDAY, count=2, sum=182.62, rows=[
        StatementRow(date=YESTERDAY, order_id="B1", amount=280.0, time="13:00"),
        StatementRow(date=YESTERDAY, order_id="B1", amount=-97.38, time="13:00"),
    ])
    p = statement_flow.prepare(
        db_path, Statement(days=[day1, day2], account="YY0000", total=482.62, reader="test"), NOW)
    assert p.confirm_label == "確認結算 + 記判罰 · 2 程 · $482.62"
    done = statement_flow.confirm(db_path, p, None, NOW)
    assert get_order_by_id(db_path, "B1")["penalty_fee"] == 97.38
    assert "已記判罰 −$97.38" in done.text


def test_confirm_allocates_the_credit_the_card_named(db_path):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 2540.0)
    cid = credit_row(db_path, "R1", 2540.0, TODAY)
    p = statement_flow.prepare(db_path, stmt_for({YESTERDAY: [("A1", 2540.0)]}, 2540.0), NOW)
    done = statement_flow.confirm(db_path, p, None, NOW)
    credit = get_credit(db_path, cid)
    assert [a["settlement_id"] for a in credit["allocations"]] == [done.settlement_id]
    assert done.credit_id == cid
    assert done.text.endswith(f"批次 #{done.settlement_id} 收齊 · 已到帳 {credits.md(TODAY)}")
    assert done.short_line == ""


def test_confirm_leaves_a_short_paid_batch_owed_the_rest(db_path):
    seed(db_path, "A1", f"{TWO_DAYS} 09:00:00", 2950.0)
    seed(db_path, "A2", f"{TWO_DAYS} 10:00:00", 510.0)
    credit_row(db_path, "R1", 2950.0, YESTERDAY)
    p = statement_flow.prepare(
        db_path, stmt_for({TWO_DAYS: [("A1", 2950.0), ("A2", 510.0)]}, 3460.0), NOW)
    done = statement_flow.confirm(db_path, p, None, NOW)
    assert done.batch["state"] == "partial" and done.batch["outstanding"] == 510.0
    assert done.allocation_line == credits.part_paid_line(done.batch)
    assert done.short_line == credits.short_allocation_line(done.batch)
    assert done.text.endswith(done.short_line)


def test_a_credit_spent_since_the_card_degrades_to_a_note(db_path):
    """The card is a snapshot. A refused link must not cost the batch: it is
    created either way and the credit becomes a line of text."""
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 2540.0)
    seed(db_path, "A2", f"{TWO_DAYS} 09:00:00", 2540.0)
    other = create_settlement(db_path, "ride", ["A2"], 2540.0, TWO_DAYS)
    cid = credit_row(db_path, "R1", 2540.0, TODAY)
    p = statement_flow.prepare(db_path, stmt_for({YESTERDAY: [("A1", 2540.0)]}, 2540.0), NOW)
    assert p.credit_id == cid
    allocate(db_path, cid, other)

    done = statement_flow.confirm(db_path, p, None, NOW)
    assert done.settlement_id != other
    assert done.credit_id is None and done.batch is None
    assert "對唔到入數：" in done.allocation_line
    assert get_settlement(db_path, done.settlement_id)["state"] == "awaiting"


def test_confirm_propagates_the_refusal_that_names_the_order(db_path):
    """create_settlement revalidates every leg, so a stale Prepared cannot
    write a batch over an order that has been settled since."""
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 280.0)
    p = statement_flow.prepare(db_path, stmt_for({YESTERDAY: [("A1", 280.0)]}, 280.0), NOW)
    create_settlement(db_path, "ride", ["A1"], 280.0, YESTERDAY)
    with pytest.raises(ValueError, match="A1"):
        statement_flow.confirm(db_path, p, None, NOW)
    assert len(open_batches(db_path, "ride")) == 1


# ---- unreadable images ----

def test_unreadable_text_names_the_warnings(db_path):
    assert statement_flow.unreadable_text(
        Statement(days=[], warnings=["image could not be decoded"])
    ) == "讀唔到張圖（image could not be decoded）"
    assert statement_flow.unreadable_text(Statement(days=[])) == "讀唔到張圖（冇日期 / 訂單行）"


def test_keep_unread_image_writes_the_exact_bytes(db_path):
    statement_flow.keep_unread_image(db_path, "photo/../1", b"\x89PNGrest")
    kept = os.listdir(os.path.join(statements_dir(db_path), "failed"))
    # The stem is joined onto a path, so nothing that could climb out survives.
    assert len(kept) == 1 and kept[0].endswith("-photo____1.png")
    with open(os.path.join(statements_dir(db_path), "failed", kept[0]), "rb") as f:
        assert f.read() == b"\x89PNGrest"


def test_keep_unread_image_never_raises(db_path):
    """Nothing here may cost the operator their reply, so an unwritable
    statements directory is logged and swallowed."""
    statement_flow.keep_unread_image(os.path.join(db_path, "x.db"), "x", b"\xff\xd8img")
