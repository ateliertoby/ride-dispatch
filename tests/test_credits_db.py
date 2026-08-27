from datetime import datetime

import pytest

from ride_dispatch.db import (
    init_db, save_order, save_quick_order, update_price, create_settlement, delete_settlement,
    get_settlement, get_settle_month, insert_credit, get_credit, unallocated_credits,
    awaiting_batches, link_credit, unlink_credit, archive_credit, unarchive_credit,
    archive_credits_before,
)
from ride_dispatch.parser import Order

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
    assert c["amount"] == 2540.0 and c["remaining"] == 2540.0 and c["linked"] == 0.0 and c["settlement_ids"] == []
    assert c["payer"] == "A B**** C***** L" and c["memo"] == "SUPPLIERPAY"
    assert get_credit(db_path, 999) is None


def test_link_sets_paid_on_to_value_date_and_derives_remaining(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", price=1000.0)
    seed(db_path, "A2", "2026-08-24 09:00:00", price=1540.0)
    sid = batch(db_path, "A1", "A2", confirmed=2540.0)
    cid = insert_credit(db_path, credit())
    out = link_credit(db_path, cid, sid)
    assert out["remaining"] == 0.0 and out["settlement_ids"] == [sid]
    s = get_settlement(db_path, sid)
    assert s["bank_credit_id"] == cid and s["paid_on"] == "2026-08-26"
    assert awaiting_batches(db_path, "ride") == []
    assert unallocated_credits(db_path) == []


def test_link_refuses_overshoot_double_link_other_platform_and_archived(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", price=3000.0)
    sid = batch(db_path, "A1", confirmed=3000.0)
    cid = insert_credit(db_path, credit(amount=2540.0))
    with pytest.raises(ValueError, match="超過"):
        link_credit(db_path, cid, sid)
    save_quick_order(db_path, "U1", "Uber", "2026-08-23 10:00:00", 100.0, 0.0, source="Uber")
    usid = create_settlement(db_path, "uber", ["U1"], 100.0, "2026-08-26", now=NOW)
    with pytest.raises(ValueError, match="平台"):
        link_credit(db_path, cid, usid)
    seed(db_path, "A2", "2026-08-23 09:00:00", price=2540.0)
    sid2 = batch(db_path, "A2", confirmed=2540.0)
    link_credit(db_path, cid, sid2)
    with pytest.raises(ValueError, match="已經對"):
        link_credit(db_path, cid, sid2)
    cid2 = insert_credit(db_path, credit(ref="R2"))
    archive_credit(db_path, cid2, "manual: test", "2026-08-27")
    seed(db_path, "A3", "2026-08-23 09:00:00", price=10.0)
    sid3 = batch(db_path, "A3", confirmed=10.0)
    with pytest.raises(ValueError, match="收埋"):
        link_credit(db_path, cid2, sid3)
    with pytest.raises(ValueError, match="搵唔到入數"):
        link_credit(db_path, 999, sid3)
    with pytest.raises(ValueError, match="搵唔到批次"):
        link_credit(db_path, cid, 999)


def test_partial_links_accumulate_on_one_credit(db_path):
    seed(db_path, "A1", "2026-08-20 09:00:00", price=1450.0)
    seed(db_path, "A2", "2026-08-21 09:00:00", price=1080.0)
    s1 = batch(db_path, "A1", confirmed=1450.0)
    s2 = batch(db_path, "A2", confirmed=1080.0)
    cid = insert_credit(db_path, credit(amount=2950.0, value_date="2026-08-24"))
    assert link_credit(db_path, cid, s1)["remaining"] == 1500.0
    assert link_credit(db_path, cid, s2)["remaining"] == 420.0
    c = unallocated_credits(db_path, "ride")
    assert [x["id"] for x in c] == [cid] and c[0]["settlement_ids"] == [s1, s2]


def test_unlink_restores_and_undo_leaves_the_credit(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", price=2540.0)
    sid = batch(db_path, "A1", confirmed=2540.0)
    cid = insert_credit(db_path, credit())
    link_credit(db_path, cid, sid)
    assert unlink_credit(db_path, sid) is True
    assert unlink_credit(db_path, sid) is False
    s = get_settlement(db_path, sid)
    assert s["bank_credit_id"] is None and s["paid_on"] is None
    assert get_credit(db_path, cid)["remaining"] == 2540.0
    link_credit(db_path, cid, sid)
    assert delete_settlement(db_path, sid) is True
    assert get_credit(db_path, cid)["remaining"] == 2540.0


def test_awaiting_batches_carries_orders_and_statement(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", price=2540.0)
    stmt = {"account": "YY0000", "total": 2540.0, "reader": "test",
            "days": [{"date": "2026-08-23", "count": 1, "sum": 2540.0,
                      "rows": [{"order_id": "A1", "amount": 2540.0, "time": "09:00",
                                "settle_date": "2026-08-25"}]}]}
    sid = create_settlement(db_path, "ride", ["A1"], 2540.0, "2026-08-26", now=NOW, statement=stmt)
    save_quick_order(db_path, "U1", "Uber", "2026-08-23 10:00:00", 100.0, 0.0, source="Uber")
    create_settlement(db_path, "uber", ["U1"], 100.0, "2026-08-26", now=NOW)
    rows = awaiting_batches(db_path, "ride")
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


def test_settle_month_carries_credit_summary_and_link(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", price=2540.0)
    sid = batch(db_path, "A1", confirmed=2540.0)
    insert_credit(db_path, credit(ref="R1"))
    insert_credit(db_path, credit(ref="R2", amount=930.0, value_date="2026-08-24"))
    m = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert m["credits"] == {"unallocated": 2, "unallocated_sum": 3470.0}
    assert m["settlements"][0]["bank_credit"] is None
    link_credit(db_path, 1, sid)
    m = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert m["credits"] == {"unallocated": 1, "unallocated_sum": 930.0}
    assert m["settlements"][0]["bank_credit"] == {"id": 1, "amount": 2540.0, "value_date": "2026-08-26"}
    assert m["totals"]["awaiting"] == 0.0


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
