from datetime import datetime

import pytest

from ride_dispatch.db import (
    init_db, save_order, save_quick_order, update_price, create_settlement, delete_settlement,
    get_settlement, get_settle_month, insert_credit, get_credit, unallocated_credits,
    open_batches, allocate, deallocate, mark_unpaid, archive_credit, unarchive_credit,
    archive_credits_before,
)
from ride_dispatch.parser import Order
from ride_dispatch.service import expected_of

NOW = datetime(2026, 8, 27, 12, 0)


def make_order(order_id, scheduled, service_type="送机", additional_services=""):
    return Order(
        order_id=order_id, service_type=service_type, vehicle_type="经济5座", passenger_name="TEST/USER",
        scheduled_time=scheduled, passenger_phone="86 13800000000", overseas_phone="", flight_number="",
        pickup="尖沙咀", dropoff="香港国际机场 T1", distance_km=30, notes="", driver_notes="",
        additional_services=additional_services, passenger_exit_minutes=None, third_party_contact="",
        more_contacts="", raw_message="raw",
    )


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "orders.db")
    init_db(path)
    return path


def seed(db_path, order_id, scheduled, price=210.0, **kw):
    save_order(db_path, make_order(order_id, scheduled, **kw), telegram_msg_id=1, parking=0.0, source="携程")
    update_price(db_path, order_id, price)


def credit(ref="R1", amount=2540.0, value_date="2026-08-26", platform="ride"):
    return {"ref": ref, "platform": platform, "amount": amount, "currency": "HKD", "value_date": value_date,
            "payer": "A B**** C***** L", "memo": "SUPPLIERPAY", "email_id": "m1",
            "received_at": "2026-08-27T00:00:54Z", "recorded_at": "2026-08-27T00:15:03Z"}


def batch(db_path, *order_ids, confirmed, settled_on="2026-08-26"):
    return create_settlement(db_path, "ride", list(order_ids), confirmed, settled_on, now=NOW)


def test_insert_credit_is_idempotent_by_ref(db_path):
    assert insert_credit(db_path, credit()) == 1
    assert insert_credit(db_path, credit(amount=1.0)) is None
    c = get_credit(db_path, 1)
    assert c["amount"] == 2540.0 and c["remaining"] == 2540.0 and c["allocated"] == 0.0
    assert c["allocations"] == []
    assert c["payer"] == "A B**** C***** L" and c["memo"] == "SUPPLIERPAY"
    assert get_credit(db_path, 999) is None


def test_allocate_pays_the_whole_batch_by_default_and_sets_paid_on(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", price=1000.0)
    seed(db_path, "A2", "2026-08-24 09:00:00", price=1540.0)
    sid = batch(db_path, "A1", "A2", confirmed=2540.0)
    cid = insert_credit(db_path, credit())
    out = allocate(db_path, cid, sid)
    assert out["received"] == 2540.0 and out["outstanding"] == 0.0 and out["state"] == "paid"
    assert out["allocations"] == [{"credit_id": cid, "amount": 2540.0, "value_date": "2026-08-26"}]
    s = get_settlement(db_path, sid)
    assert s["paid_on"] == "2026-08-26"
    assert get_credit(db_path, cid)["allocations"] == [{"settlement_id": sid, "amount": 2540.0}]
    assert open_batches(db_path, "ride") == []
    assert unallocated_credits(db_path) == []


def test_allocate_is_capped_by_whichever_side_runs_out_first(db_path):
    """The default is as much of the batch as the credit can still pay: a short
    payment leaves the batch owed the rest, and change stays on the credit."""
    seed(db_path, "A1", "2026-08-22 09:00:00", price=3460.0)
    short_batch = batch(db_path, "A1", confirmed=3460.0)
    cid = insert_credit(db_path, credit(amount=2950.0, value_date="2026-08-24"))
    out = allocate(db_path, cid, short_batch)
    assert out["received"] == 2950.0 and out["outstanding"] == 510.0 and out["state"] == "partial"
    assert get_settlement(db_path, short_batch)["paid_on"] is None
    assert get_credit(db_path, cid)["remaining"] == 0.0

    seed(db_path, "A2", "2026-08-22 10:00:00", price=200.0)
    small = batch(db_path, "A2", confirmed=200.0)
    big = insert_credit(db_path, credit(ref="R2", amount=1000.0, value_date="2026-08-24"))
    out = allocate(db_path, big, small)
    assert out["state"] == "paid" and out["received"] == 200.0
    assert get_credit(db_path, big)["remaining"] == 800.0


def test_allocate_takes_an_explicit_amount_within_both_caps(db_path):
    seed(db_path, "A1", "2026-08-22 09:00:00", price=1000.0)
    sid = batch(db_path, "A1", confirmed=1000.0)
    cid = insert_credit(db_path, credit(amount=1000.0, value_date="2026-08-24"))
    assert allocate(db_path, cid, sid, 400.0)["outstanding"] == 600.0
    with pytest.raises(ValueError, match="已經對過呢筆入數"):
        allocate(db_path, cid, sid, 600.0)
    other = insert_credit(db_path, credit(ref="R2", amount=1000.0, value_date="2026-08-24"))
    with pytest.raises(ValueError, match="批次淨係差"):
        allocate(db_path, other, sid, 700.0)
    assert allocate(db_path, other, sid, 600.0)["state"] == "paid"


def test_the_unpaid_legs_survive_completion_and_deallocation_clears_paid_on(db_path):
    seed(db_path, "A1", "2026-08-22 09:00:00", price=2950.0)
    seed(db_path, "A2", "2026-08-22 10:00:00", price=210.0)
    seed(db_path, "A3", "2026-08-22 11:00:00", price=300.0)
    sid = batch(db_path, "A1", "A2", "A3", confirmed=3460.0)
    first = insert_credit(db_path, credit(amount=2950.0, value_date="2026-08-24"))
    allocate(db_path, first, sid)
    mark_unpaid(db_path, sid, ["A2", "A3"])
    assert [o["order_id"] for o in get_settlement(db_path, sid)["orders"] if o["unpaid"]] == ["A2", "A3"]
    # Taking the money back reopens the batch; the flags are a fact about the
    # statement, not about who paid it, so they are left alone.
    assert deallocate(db_path, sid, first) == 1
    s = get_settlement(db_path, sid)
    assert s["received"] == 0.0 and s["state"] == "awaiting"
    assert [o["order_id"] for o in s["orders"] if o["unpaid"]] == ["A2", "A3"]
    assert deallocate(db_path, sid) == 0

    allocate(db_path, first, sid)
    later = insert_credit(db_path, credit(ref="R2", amount=510.0, value_date="2026-08-30"))
    out = allocate(db_path, later, sid)
    assert out["state"] == "paid" and out["paid_on"] == "2026-08-30"
    # Completion does not erase which legs the platform held back: that is what
    # the day sheet reads to say the make-up payment is what paid for them.
    assert [o["order_id"] for o in out["orders"] if o["unpaid"]] == ["A2", "A3"]
    # The date is the bank's, and it goes when the batch is owed money again.
    assert deallocate(db_path, sid, later) == 1
    s = get_settlement(db_path, sid)
    assert s["paid_on"] is None and s["outstanding"] == 510.0
    assert [o["order_id"] for o in s["orders"] if o["unpaid"]] == ["A2", "A3"]
    assert deallocate(db_path, sid) == 1


def test_deallocate_named_a_credit_removes_that_allocation_only(db_path):
    """A batch paid by two transfers is corrected one line at a time: naming a
    credit takes back its money and leaves the other allocation standing."""
    seed(db_path, "A1", "2026-08-22 09:00:00", price=2950.0)
    seed(db_path, "A2", "2026-08-22 10:00:00", price=510.0)
    sid = batch(db_path, "A1", "A2", confirmed=3460.0)
    first = insert_credit(db_path, credit(amount=2950.0, value_date="2026-08-24"))
    later = insert_credit(db_path, credit(ref="R2", amount=510.0, value_date="2026-08-30"))
    allocate(db_path, first, sid)
    allocate(db_path, later, sid)
    assert deallocate(db_path, sid, later) == 1
    s = get_settlement(db_path, sid)
    assert [a["credit_id"] for a in s["allocations"]] == [first]
    assert s["state"] == "partial" and s["outstanding"] == 510.0 and s["paid_on"] is None
    assert get_credit(db_path, later)["remaining"] == 510.0
    # A credit that is not on the batch takes nothing off it.
    assert deallocate(db_path, sid, later) == 0
    assert get_settlement(db_path, sid)["received"] == 2950.0


def platform_statement(rows, date="2026-08-22", settle_date="2026-08-24"):
    """A statement as corrected_json stores it: ids already the system's."""
    total = round(sum(a for _, a in rows), 2)
    return {"account": "YY0000", "total": total, "reader": "test",
            "days": [{"date": date, "count": len(rows), "sum": total,
                      "rows": [{"order_id": oid, "amount": a, "time": "09:00",
                                "settle_date": settle_date} for oid, a in rows]}]}


def test_ticks_are_priced_in_the_platforms_own_money(db_path):
    """The shortfall is money the platform did not send, so the ticks are
    measured in the amounts the platform printed.  Here it paid $220 for a leg
    the system prices at $210; ticking by the system's figure would tell the
    operator his ticks are wrong over the platform's own discrepancy."""
    seed(db_path, "1128150000000001", "2026-08-22 09:00:00", price=2950.0)
    seed(db_path, "1128150000001704", "2026-08-22 10:00:00", price=210.0)
    seed(db_path, "1128150000003137", "2026-08-22 11:00:00", price=300.0)
    ids = ["1128150000000001", "1128150000001704", "1128150000003137"]
    stmt = platform_statement([(ids[0], 2950.0), (ids[1], 220.0), (ids[2], 300.0)])
    sid = create_settlement(db_path, "ride", ids, 3470.0, "2026-08-23", now=NOW, statement=stmt)
    allocate(db_path, insert_credit(db_path, credit(amount=2950.0, value_date="2026-08-24")), sid)

    batch = get_settlement(db_path, sid)
    assert batch["outstanding"] == 520.0
    # What the old rule would have made of the very same, correct ticks.
    by_id = {o["order_id"]: o for o in batch["orders"]}
    assert round(sum(expected_of(by_id[i]) for i in ids[1:]), 2) == 510.0
    # The refusal quotes the platform's figure too, not the system's $210.
    with pytest.raises(ValueError, match=r"剔咗 \$220，差額係 \$520"):
        mark_unpaid(db_path, sid, [ids[1]])
    out = mark_unpaid(db_path, sid, ids[1:])
    assert [o["order_id"] for o in out["orders"] if o["unpaid"]] == ids[1:]


def test_a_batch_without_a_statement_prices_ticks_from_the_system(db_path):
    """No statement means no platform figure to read, so the system's own is
    the only answer available."""
    seed(db_path, "A1", "2026-08-22 09:00:00", price=2950.0)
    seed(db_path, "A2", "2026-08-22 10:00:00", price=510.0)
    sid = batch(db_path, "A1", "A2", confirmed=3460.0)
    allocate(db_path, insert_credit(db_path, credit(amount=2950.0, value_date="2026-08-24")), sid)
    out = mark_unpaid(db_path, sid, ["A2"])
    assert [o["order_id"] for o in out["orders"] if o["unpaid"]] == ["A2"]


def test_mark_unpaid_needs_a_partial_batch_and_ticks_that_add_up(db_path):
    seed(db_path, "A1", "2026-08-22 09:00:00", price=2950.0)
    seed(db_path, "A2", "2026-08-22 10:00:00", price=210.0)
    seed(db_path, "A3", "2026-08-22 11:00:00", price=300.0)
    sid = batch(db_path, "A1", "A2", "A3", confirmed=3460.0)
    with pytest.raises(ValueError, match="批次未收過錢"):
        mark_unpaid(db_path, sid, ["A2"])
    cid = insert_credit(db_path, credit(amount=2950.0, value_date="2026-08-24"))
    allocate(db_path, cid, sid)
    with pytest.raises(ValueError, match="唔喺呢個批次"):
        mark_unpaid(db_path, sid, ["A2", "NOPE"])
    with pytest.raises(ValueError, match=r"剔咗 \$210，差額係 \$510"):
        mark_unpaid(db_path, sid, ["A2"])
    out = mark_unpaid(db_path, sid, ["A2", "A3"])
    assert [o["order_id"] for o in out["orders"] if o["unpaid"]] == ["A2", "A3"]
    # Replaced, not added to: a mis-tick is corrected by ticking again.
    seed(db_path, "A4", "2026-08-22 12:00:00", price=510.0)
    sid2 = batch(db_path, "A4", confirmed=510.0)
    with pytest.raises(ValueError, match="唔喺呢個批次"):
        mark_unpaid(db_path, sid, ["A4"])
    assert get_settlement(db_path, sid2)["state"] == "awaiting"


def test_allocate_refuses_a_spent_credit_a_paid_batch_and_another_platform(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", price=3000.0)
    sid = batch(db_path, "A1", confirmed=3000.0)
    cid = insert_credit(db_path, credit(amount=2540.0))
    # An overshoot is no longer a refusal: the credit pays what it can and the
    # batch stays owed the difference.
    assert allocate(db_path, cid, sid)["outstanding"] == 460.0
    seed(db_path, "A0", "2026-08-23 08:00:00", price=100.0)
    spent_on = batch(db_path, "A0", confirmed=100.0)
    with pytest.raises(ValueError, match=r"入數剩 \$0 唔夠"):
        allocate(db_path, cid, spent_on)
    save_quick_order(db_path, "U1", "Uber", "2026-08-23 10:00:00", 100.0, 0.0, source="Uber")
    usid = create_settlement(db_path, "uber", ["U1"], 100.0, "2026-08-26", now=NOW)
    with pytest.raises(ValueError, match="平台"):
        allocate(db_path, cid, usid)
    seed(db_path, "A2", "2026-08-23 09:00:00", price=2540.0)
    sid2 = batch(db_path, "A2", confirmed=2540.0)
    cid3 = insert_credit(db_path, credit(ref="R3", amount=2540.0))
    allocate(db_path, cid3, sid2)
    with pytest.raises(ValueError, match="已經對過呢筆入數"):
        allocate(db_path, cid3, sid2)
    cid2 = insert_credit(db_path, credit(ref="R2"))
    archive_credit(db_path, cid2, "manual: test", "2026-08-27")
    seed(db_path, "A3", "2026-08-23 09:00:00", price=10.0)
    sid3 = batch(db_path, "A3", confirmed=10.0)
    with pytest.raises(ValueError, match="收埋"):
        allocate(db_path, cid2, sid3)
    with pytest.raises(ValueError, match="搵唔到入數"):
        allocate(db_path, 999, sid3)
    with pytest.raises(ValueError, match="搵唔到批次"):
        allocate(db_path, cid, 999)
    spare = insert_credit(db_path, credit(ref="R4", amount=10.0))
    allocate(db_path, spare, sid3)
    with pytest.raises(ValueError, match="批次已收齊"):
        allocate(db_path, insert_credit(db_path, credit(ref="R5", amount=10.0)), sid3)


def test_one_credit_spreads_over_several_batches(db_path):
    seed(db_path, "A1", "2026-08-20 09:00:00", price=1450.0)
    seed(db_path, "A2", "2026-08-21 09:00:00", price=1080.0)
    s1 = batch(db_path, "A1", confirmed=1450.0)
    s2 = batch(db_path, "A2", confirmed=1080.0)
    cid = insert_credit(db_path, credit(amount=2950.0, value_date="2026-08-24"))
    allocate(db_path, cid, s1)
    assert get_credit(db_path, cid)["remaining"] == 1500.0
    allocate(db_path, cid, s2)
    c = unallocated_credits(db_path, "ride")
    assert [x["id"] for x in c] == [cid] and c[0]["remaining"] == 420.0
    assert c[0]["allocations"] == [{"settlement_id": s1, "amount": 1450.0},
                                   {"settlement_id": s2, "amount": 1080.0}]


def test_undoing_a_batch_gives_the_money_back_and_forgets_its_unpaid_legs(db_path):
    seed(db_path, "A1", "2026-08-22 09:00:00", price=2950.0)
    seed(db_path, "A2", "2026-08-22 10:00:00", price=510.0)
    sid = batch(db_path, "A1", "A2", confirmed=3460.0)
    cid = insert_credit(db_path, credit(amount=2950.0, value_date="2026-08-24"))
    allocate(db_path, cid, sid)
    mark_unpaid(db_path, sid, ["A2"])
    assert delete_settlement(db_path, sid) is True
    assert get_credit(db_path, cid)["remaining"] == 2950.0
    seed(db_path, "A3", "2026-08-22 11:00:00", price=510.0)
    again = batch(db_path, "A2", "A3", confirmed=1020.0)
    assert all(o["unpaid"] == 0 for o in get_settlement(db_path, again)["orders"])


def test_open_batches_carries_orders_and_statement(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", price=2540.0)
    stmt = {"account": "YY0000", "total": 2540.0, "reader": "test",
            "days": [{"date": "2026-08-23", "count": 1, "sum": 2540.0,
                      "rows": [{"order_id": "A1", "amount": 2540.0, "time": "09:00",
                                "settle_date": "2026-08-25"}]}]}
    sid = create_settlement(db_path, "ride", ["A1"], 2540.0, "2026-08-26", now=NOW, statement=stmt)
    save_quick_order(db_path, "U1", "Uber", "2026-08-23 10:00:00", 100.0, 0.0, source="Uber")
    create_settlement(db_path, "uber", ["U1"], 100.0, "2026-08-26", now=NOW)
    rows = open_batches(db_path, "ride")
    assert [b["id"] for b in rows] == [sid]
    assert rows[0]["statement"]["days"][0]["rows"][0]["settle_date"] == "2026-08-25"
    assert [o["order_id"] for o in rows[0]["orders"]] == ["A1"]


def test_archive_before_and_unarchive(db_path):
    insert_credit(db_path, credit(ref="R1", value_date="2026-06-05"))
    insert_credit(db_path, credit(ref="R2", value_date="2026-06-26"))
    insert_credit(db_path, credit(ref="R3", value_date="2026-06-27"))
    done = archive_credits_before(db_path, "2026-06-27", "pre-system", "2026-08-27")
    assert [d["ref"] for d in done] == ["R1", "R2"]
    assert [c["ref"] for c in unallocated_credits(db_path)] == ["R3"]
    assert get_credit(db_path, 1)["archived_reason"] == "pre-system"
    assert get_credit(db_path, 1)["archived_on"] == "2026-08-27"
    assert unarchive_credit(db_path, 1) is True
    assert unarchive_credit(db_path, 999) is False
    assert archive_credit(db_path, 999, "manual: x", "2026-08-27") is False
    assert [c["ref"] for c in unallocated_credits(db_path)] == ["R1", "R3"]


def test_settle_month_carries_what_each_batch_has_and_still_owes(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", price=2950.0)
    seed(db_path, "A2", "2026-08-23 10:00:00", price=510.0)
    sid = batch(db_path, "A1", "A2", confirmed=3460.0)
    insert_credit(db_path, credit(ref="R1", amount=2950.0, value_date="2026-08-24"))
    insert_credit(db_path, credit(ref="R2", amount=930.0, value_date="2026-08-24"))
    m = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert m["credits"] == {"unallocated": 2, "unallocated_sum": 3880.0}
    assert m["settlements"][0]["state"] == "awaiting"
    assert m["totals"]["awaiting"] == 3460.0
    allocate(db_path, 1, sid)
    mark_unpaid(db_path, sid, ["A2"])
    m = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    b = m["settlements"][0]
    assert b["received"] == 2950.0 and b["outstanding"] == 510.0 and b["state"] == "partial"
    assert b["allocations"] == [{"credit_id": 1, "amount": 2950.0, "value_date": "2026-08-24"}]
    assert [o["order_id"] for o in b["orders"] if o["unpaid"]] == ["A2"]
    # Waiting for money is the shortfall, not the whole batch.
    assert m["totals"]["awaiting"] == 510.0
    assert m["credits"] == {"unallocated": 1, "unallocated_sum": 930.0}


def test_init_db_adds_credit_columns_to_an_old_database(tmp_path):
    import sqlite3
    path = str(tmp_path / "orders.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT,
            expected_amount REAL,
            confirmed_amount REAL,
            settled_on TEXT,
            paid_on TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()

    init_db(path)
    init_db(path)  # the ALTER must stay a no-op on an already migrated database

    conn = sqlite3.connect(path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(settlements)")]
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    conn.close()
    assert "bank_credit_id" in cols
    assert "bank_credits" in tables
    assert "credit_allocations" in tables


def test_round_two_links_become_allocations_once(tmp_path):
    """Round 2 recorded one credit paying one batch in full on
    settlements.bank_credit_id.  init_db runs on every start, so moving those
    links into credit_allocations has to be a no-op the second time."""
    import sqlite3
    path = str(tmp_path / "orders.db")
    init_db(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO bank_credits (id, ref, platform, amount, value_date) "
        "VALUES (7, 'R1', 'ride', 2540.0, '2026-08-26')")
    conn.execute(
        "INSERT INTO settlements (id, platform, expected_amount, confirmed_amount, settled_on, "
        "paid_on, bank_credit_id) VALUES (4, 'ride', 2540.0, 2540.0, '2026-08-25', '2026-08-26', 7)")
    conn.execute(
        "INSERT INTO settlements (id, platform, expected_amount, confirmed_amount, settled_on) "
        "VALUES (5, 'ride', 930.0, 930.0, '2026-08-25')")
    conn.commit()
    conn.close()

    init_db(path)
    init_db(path)

    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT credit_id, settlement_id, amount FROM credit_allocations").fetchall()
    left = conn.execute("SELECT count(bank_credit_id) FROM settlements").fetchone()[0]
    conn.close()
    assert rows == [(7, 4, 2540.0)]
    assert left == 0
    assert get_credit(path, 7)["remaining"] == 0.0
    assert get_settlement(path, 4)["state"] == "paid"
    assert [b["id"] for b in open_batches(path, "ride")] == [5]
