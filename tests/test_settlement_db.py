import os
import sqlite3
from datetime import datetime

import pytest

from ride_dispatch import db as db_module
from ride_dispatch.db import (
    init_db, save_order, update_price, cancel_order, create_settlement, delete_settlement,
    get_settlement, get_settle_month, open_batches, insert_credit, allocate, mark_unpaid,
    settlement_candidates, statement_image_path, image_extension,
)
from ride_dispatch.parser import Order

NOW = datetime(2026, 8, 26, 12, 0)


def make_order(order_id, scheduled, service_type="送机", additional_services=""):
    return Order(
        order_id=order_id, service_type=service_type, vehicle_type="经济5座", passenger_name="TEST/USER",
        scheduled_time=scheduled, passenger_phone="86 13800000000", overseas_phone="", flight_number="",
        pickup="尖沙咀", dropoff="香港国际机场 T1", distance_km=30, notes="", driver_notes="",
        additional_services=additional_services, passenger_exit_minutes=None, third_party_contact="",
        more_contacts="", raw_message="raw",
    )


def _boom(*args, **kwargs):
    raise OSError("no space left on device")


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "orders.db")
    init_db(path)
    return path


def seed(db_path, order_id, scheduled, price=210.0, **kw):
    save_order(db_path, make_order(order_id, scheduled, **kw), telegram_msg_id=1, parking=0.0, source="携程")
    update_price(db_path, order_id, price)


STATEMENT = {
    "account": "YY0000", "total": 490.0, "reader": "test",
    "days": [{"date": "2026-08-23", "count": 2, "sum": 490.0,
              "rows": [{"order_id": "A1", "amount": 280.0, "time": "09:00", "settle_date": "2026-08-25"},
                       {"order_id": "A2", "amount": 210.0, "time": "12:30", "settle_date": "2026-08-25"}]}],
}


def test_create_stores_statement_json_and_image(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    seed(db_path, "A2", "2026-08-23 12:30:00", 210.0)
    sid = create_settlement(db_path, "ride", ["A1", "A2"], 490.0, "2026-08-26", now=NOW,
                            statement=STATEMENT, image=b"\xff\xd8jpegbytes")
    batch = get_settlement(db_path, sid)
    assert batch["statement"]["total"] == 490.0
    assert batch["statement"]["days"][0]["rows"][1]["order_id"] == "A2"
    assert batch["statement_image"] == f"{sid}.jpg"
    path = statement_image_path(db_path, sid)
    assert os.path.dirname(path) == os.path.join(os.path.dirname(db_path), "statements")
    with open(path, "rb") as f:
        assert f.read() == b"\xff\xd8jpegbytes"


def test_create_without_statement_leaves_columns_null(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    sid = create_settlement(db_path, "ride", ["A1"], 280.0, "2026-08-26", now=NOW)
    batch = get_settlement(db_path, sid)
    assert batch["statement"] is None
    assert batch["statement_image"] is None
    assert not os.path.exists(statement_image_path(db_path, sid))


def test_settle_month_carries_statement(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    sid = create_settlement(db_path, "ride", ["A1"], 280.0, "2026-08-26", now=NOW,
                            statement=STATEMENT, image=b"\xff\xd8x")
    data = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    batch = data["settlements"][0]
    assert batch["id"] == sid
    assert batch["statement"]["account"] == "YY0000"
    assert batch["statement_image"] == f"{sid}.jpg"


def test_delete_removes_image_file(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    sid = create_settlement(db_path, "ride", ["A1"], 280.0, "2026-08-26", now=NOW,
                            statement=STATEMENT, image=b"\xff\xd8x")
    path = statement_image_path(db_path, sid)
    assert os.path.exists(path)
    assert delete_settlement(db_path, sid) is True
    assert not os.path.exists(path)


def test_open_batches_drops_a_batch_once_a_credit_is_linked(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    seed(db_path, "A2", "2026-08-24 09:00:00", 210.0)
    seed(db_path, "A3", "2026-08-25 09:00:00", 280.0)
    s1 = create_settlement(db_path, "ride", ["A1"], 280.0, "2026-08-26", now=NOW)
    s2 = create_settlement(db_path, "ride", ["A2"], 210.0, "2026-08-26", now=NOW)
    s3 = create_settlement(db_path, "ride", ["A3"], 280.0, "2026-08-26", now=NOW)
    cid = insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 280.0,
                                  "currency": "HKD", "value_date": "2026-08-26",
                                  "payer": "A B**** C***** L", "memo": "SUPPLIERPAY",
                                  "email_id": None, "received_at": None, "recorded_at": None})
    allocate(db_path, cid, s3)
    awaiting = open_batches(db_path, "ride")
    assert [b["id"] for b in awaiting] == [s1, s2]
    assert awaiting[0]["orders"][0]["order_id"] == "A1"


def test_create_refuses_anything_that_cannot_enter_a_batch(db_path):
    """All-or-nothing, and the refusal names the leg.  The bot's statement flow
    is the only caller left, so this is where the guard is proved."""
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    seed(db_path, "NOPRICE", "2026-08-23 10:00:00", 0.0)
    seed(db_path, "FUTURE", "2026-08-27 09:00:00", 210.0)
    save_order(db_path, make_order("GONE", "2026-08-23 11:00:00"), telegram_msg_id=2,
               parking=0.0, source="携程")
    update_price(db_path, "GONE", 210.0)
    cancel_order(db_path, "GONE")
    for order_ids, says in (
        (["A1", "MISSING"], "搵唔到單"),
        (["A1", "NOPRICE"], "未入價"),
        (["A1", "FUTURE"], "未完成"),
        (["A1", "GONE"], "已取消"),
        (["A1", "A1"], "重複"),
    ):
        with pytest.raises(ValueError, match=says):
            create_settlement(db_path, "ride", order_ids, 490.0, "2026-08-26", now=NOW)
    with pytest.raises(ValueError, match="unknown platform"):
        create_settlement(db_path, "taxi", ["A1"], 280.0, "2026-08-26", now=NOW)
    with pytest.raises(ValueError, match="order_ids required"):
        create_settlement(db_path, "ride", [], 0.0, "2026-08-26", now=NOW)
    sid = create_settlement(db_path, "ride", ["A1"], 280.0, "2026-08-26", now=NOW)
    with pytest.raises(ValueError, match="已經結算咗"):
        create_settlement(db_path, "ride", ["A1"], 280.0, "2026-08-26", now=NOW)
    assert [b["id"] for b in open_batches(db_path, "ride")] == [sid]


# ---- 判罰賠款 ----

def test_a_penalty_is_recorded_and_the_frozen_figure_is_net(db_path):
    """The fine is a cost of its order, and expected_amount is frozen at
    creation, so it has to be written before the sum is taken."""
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    seed(db_path, "A2", "2026-08-23 12:30:00", 210.0)
    sid = create_settlement(db_path, "ride", ["A1", "A2"], 392.62, "2026-08-26", now=NOW,
                            penalties={"A1": 97.38})
    batch = get_settlement(db_path, sid)
    assert batch["expected_amount"] == 392.62
    legs = {o["order_id"]: o for o in batch["orders"]}
    assert legs["A1"]["penalty_fee"] == 97.38
    assert legs["A2"]["penalty_fee"] is None
    # The fare itself is untouched: gross and net are both readable afterwards.
    assert legs["A1"]["price"] == 280.0


def test_penalties_on_the_same_order_accumulate(db_path):
    """A second statement can fine the same trip again, and the column holds
    what the platform has taken in total."""
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    seed(db_path, "A2", "2026-08-23 12:30:00", 210.0)
    create_settlement(db_path, "ride", ["A1"], 182.62, "2026-08-26", now=NOW,
                      penalties={"A1": 97.38})
    delete_settlement(db_path, 1)
    sid = create_settlement(db_path, "ride", ["A1", "A2"], 342.62, "2026-08-27", now=NOW,
                            penalties={"A1": 50.0})
    legs = {o["order_id"]: o for o in get_settlement(db_path, sid)["orders"]}
    assert legs["A1"]["penalty_fee"] == 147.38
    assert get_settlement(db_path, sid)["expected_amount"] == 342.62


def test_a_penalty_outside_the_batch_is_refused(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    seed(db_path, "A2", "2026-08-23 12:30:00", 210.0)
    with pytest.raises(ValueError, match="判罰唔喺呢個 batch 入面"):
        create_settlement(db_path, "ride", ["A1"], 280.0, "2026-08-26", now=NOW,
                          penalties={"A2": 97.38})
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT penalty_fee FROM orders WHERE order_id = 'A2'").fetchone()[0] is None


def test_a_refused_batch_records_no_penalty(db_path):
    """Atomicity: a fine must never outlive the batch it was confirmed with,
    or the order silently loses money no batch accounts for."""
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    seed(db_path, "FUTURE", "2026-08-27 09:00:00", 210.0)
    with pytest.raises(ValueError, match="未完成"):
        create_settlement(db_path, "ride", ["A1", "FUTURE"], 490.0, "2026-08-26", now=NOW,
                          penalties={"A1": 97.38})
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT penalty_fee FROM orders WHERE order_id = 'A1'").fetchone()[0] is None
    assert open_batches(db_path, "ride") == []


# ---- 帳項 (the batch's own statement lines) ----

WAIVED_PAIR = [{"order_ref": "X9", "date": "2026-08-23", "amount": -30.0},
               {"order_ref": "X9", "date": "2026-08-23", "amount": 30.0}]


def test_adjustments_are_stored_as_printed_and_join_the_frozen_figure(db_path):
    """The pair is two lines, not a netted one, and a lone fine has to move the
    figure the batch is owed or the transfer is short with nothing to show."""
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    paired = create_settlement(db_path, "ride", ["A1"], 280.0, "2026-08-26", now=NOW,
                               adjustments=WAIVED_PAIR)
    batch = get_settlement(db_path, paired)
    assert batch["adjustments"] == WAIVED_PAIR
    assert batch["expected_amount"] == 280.0

    seed(db_path, "A2", "2026-08-24 09:00:00", 280.0)
    lone = create_settlement(db_path, "ride", ["A2"], 250.0, "2026-08-27", now=NOW,
                             adjustments=[{"order_ref": "X8", "date": "2026-08-24", "amount": -30.0}])
    assert get_settlement(db_path, lone)["expected_amount"] == 250.0


def test_settle_month_carries_adjustments(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    create_settlement(db_path, "ride", ["A1"], 280.0, "2026-08-26", now=NOW,
                      statement=STATEMENT, adjustments=WAIVED_PAIR)
    batch = get_settle_month(db_path, "2026-08", "ride", now=NOW)["settlements"][0]
    assert batch["adjustments"] == WAIVED_PAIR
    assert open_batches(db_path, "ride")[0]["adjustments"] == WAIVED_PAIR


def test_deleting_a_batch_takes_its_adjustments_with_it(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    sid = create_settlement(db_path, "ride", ["A1"], 280.0, "2026-08-26", now=NOW,
                            adjustments=WAIVED_PAIR)
    assert delete_settlement(db_path, sid) is True
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM settlement_adjustments").fetchone()[0] == 0


def test_a_refused_batch_records_no_adjustment(db_path):
    """Same atomicity as a fine: money taken off a batch that was never created
    is money taken off nothing."""
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    seed(db_path, "FUTURE", "2026-08-27 09:00:00", 210.0)
    with pytest.raises(ValueError, match="未完成"):
        create_settlement(db_path, "ride", ["A1", "FUTURE"], 490.0, "2026-08-26", now=NOW,
                          adjustments=WAIVED_PAIR)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM settlement_adjustments").fetchone()[0] == 0


def test_a_batch_without_adjustments_carries_an_empty_list(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    sid = create_settlement(db_path, "ride", ["A1"], 280.0, "2026-08-26", now=NOW)
    assert get_settlement(db_path, sid)["adjustments"] == []


def test_init_db_adds_the_adjustments_table_to_an_old_database(tmp_path):
    path = str(tmp_path / "orders.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, order_id TEXT UNIQUE, price REAL)")
    conn.commit()
    conn.close()

    init_db(path)
    init_db(path)  # the CREATE must stay a no-op on an already migrated database

    conn = sqlite3.connect(path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(settlement_adjustments)")]
    conn.close()
    assert cols == ["id", "settlement_id", "order_ref", "date", "amount", "ahead"]


def test_init_db_adds_the_ahead_flag_to_existing_adjustments(tmp_path):
    """Every line recorded before the flag existed was a line of its own
    transfer, never a part of a trip paid ahead of it."""
    path = str(tmp_path / "orders.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settlement_adjustments (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "settlement_id INTEGER NOT NULL, order_ref TEXT, date TEXT, amount REAL NOT NULL)")
    conn.execute("INSERT INTO settlement_adjustments (settlement_id, order_ref, date, amount) "
                 "VALUES (1, 'X9', '2026-08-23', -30.0)")
    conn.commit()
    conn.close()

    init_db(path)
    init_db(path)  # the ALTER must stay a no-op on an already migrated database

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT ahead FROM settlement_adjustments").fetchall() == [(0,)]
    conn.close()


# ---- 舉牌先結 (a 舉牌 line paid ahead of its trip) ----

AHEAD = [{"order_ref": "B1", "date": "2026-08-23", "amount": 40.0, "ahead": True}]


def seed_held(db_path):
    """A 接機 with a 舉牌 whose trip the platform held back, and a leg of its own."""
    seed(db_path, "B1", "2026-08-23 22:00:00", 300.0, service_type="接机", additional_services="举牌")
    seed(db_path, "A1", "2026-08-25 10:00:00", 210.0)


def held_row(db_path):
    return next(o for o in settlement_candidates(db_path, ["2026-08-23"], now=NOW)
                if o["order_id"] == "B1")


def test_a_banner_paid_ahead_is_recorded_and_its_trip_stays_owed(db_path):
    seed_held(db_path)
    sid = create_settlement(db_path, "ride", ["A1"], 250.0, "2026-08-26", now=NOW, adjustments=AHEAD)
    batch = get_settlement(db_path, sid)
    assert batch["expected_amount"] == 250.0
    assert batch["adjustments"] == AHEAD
    held = held_row(db_path)
    assert held["settlement_id"] is None
    assert held["paid_ahead"] == 40.0 and held["ahead_batch"] == sid
    # What is left to chase is the trip alone.
    assert get_settle_month(db_path, "2026-08", "ride", now=NOW)["totals"]["unsettled"] == 300.0


def test_an_order_nothing_was_paid_ahead_of_carries_zero(db_path):
    seed_held(db_path)
    held = held_row(db_path)
    assert held["paid_ahead"] == 0 and held["ahead_batch"] is None


def test_the_batch_that_takes_the_trip_is_owed_only_what_was_not_paid_ahead(db_path):
    seed_held(db_path)
    create_settlement(db_path, "ride", ["A1"], 250.0, "2026-08-26", now=NOW, adjustments=AHEAD)
    later = create_settlement(db_path, "ride", ["B1"], 300.0, "2026-08-28", now=NOW)
    assert get_settlement(db_path, later)["expected_amount"] == 300.0


@pytest.mark.parametrize("prepare, legs, message", [
    (lambda p: None, ["A1", "B1"], "B1: 張單喺呢個 batch 入面"),
    (lambda p: create_settlement(p, "ride", ["B1"], 340.0, "2026-08-24", now=NOW), ["A1"],
     "B1: 已經結算咗"),
    (lambda p: cancel_order(p, "B1"), ["A1"], "B1: 已取消"),
    (lambda p: create_settlement(p, "ride", ["C1"], 250.0, "2026-08-24", now=NOW,
                                 adjustments=AHEAD), ["A1"], "B1: 舉牌已經先結咗"),
])
def test_a_banner_is_only_paid_ahead_of_a_trip_still_waiting_for_its_batch(db_path, prepare, legs, message):
    """Any other order would never net it off: the line would be counted twice
    or against nothing, so the batch is refused whole, like a bad leg."""
    seed_held(db_path)
    seed(db_path, "C1", "2026-08-24 10:00:00", 210.0)
    prepare(db_path)
    with sqlite3.connect(db_path) as conn:
        before = conn.execute("SELECT count(*) FROM settlements").fetchone()[0]
    with pytest.raises(ValueError, match=message):
        create_settlement(db_path, "ride", legs, 250.0, "2026-08-26", now=NOW, adjustments=AHEAD)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM settlements").fetchone()[0] == before


def test_a_banner_cannot_be_paid_ahead_of_an_order_the_book_does_not_have(db_path):
    seed_held(db_path)
    with pytest.raises(ValueError, match="ZZ: 搵唔到單"):
        create_settlement(db_path, "ride", ["A1"], 250.0, "2026-08-26", now=NOW,
                          adjustments=[{"order_ref": "ZZ", "date": "2026-08-23", "amount": 40.0,
                                        "ahead": True}])


def test_undoing_the_batch_that_paid_ahead_gives_the_trip_its_banner_back(db_path):
    seed_held(db_path)
    sid = create_settlement(db_path, "ride", ["A1"], 250.0, "2026-08-26", now=NOW, adjustments=AHEAD)
    assert delete_settlement(db_path, sid) is True
    held = held_row(db_path)
    assert held["paid_ahead"] == 0 and held["ahead_batch"] is None


def test_the_batch_that_paid_ahead_cannot_be_undone_under_the_trips_batch(db_path):
    """The trip's batch froze its figure net of the 舉牌, so taking the 舉牌 away
    under it would leave that figure wrong — the reason a batched leg's fees
    are locked.  Undoing the trip's batch first releases it."""
    seed_held(db_path)
    ahead = create_settlement(db_path, "ride", ["A1"], 250.0, "2026-08-26", now=NOW, adjustments=AHEAD)
    later = create_settlement(db_path, "ride", ["B1"], 300.0, "2026-08-28", now=NOW)
    with pytest.raises(ValueError, match=f"#{later}"):
        delete_settlement(db_path, ahead)
    assert get_settlement(db_path, ahead)["adjustments"] == AHEAD
    assert delete_settlement(db_path, later) is True
    assert delete_settlement(db_path, ahead) is True


def test_init_db_adds_the_penalty_column_to_an_old_database(tmp_path):
    path = str(tmp_path / "orders.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, order_id TEXT UNIQUE, price REAL)")
    conn.commit()
    conn.close()

    init_db(path)
    init_db(path)  # the ALTER must stay a no-op on an already migrated database

    conn = sqlite3.connect(path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)")]
    conn.close()
    assert "penalty_fee" in cols


def test_no_db_function_marks_a_batch_paid_by_hand():
    """paid_on is the bank's value date and allocate is the only writer.

    Asserted over the module surface rather than over the names that used to
    exist, so a second hand-written mark under any name fails here too.
    mark_unpaid is the opposite function: it records which legs the platform
    has NOT paid for, and never touches paid_on.
    """
    assert [n for n in dir(db_module) if "paid" in n] == ["mark_unpaid"]
    assert [n for n in dir(db_module) if "awaiting" in n] == []
    # Round 2's one-credit-pays-one-batch functions are gone, not deprecated:
    # money is allocated in amounts now, and a caller of either would be
    # writing a model the ledger no longer has.
    assert not hasattr(db_module, "link_credit")
    assert not hasattr(db_module, "unlink_credit")


def test_candidates_window_and_settleable_tail(db_path):
    seed(db_path, "IN", "2026-08-23 09:00:00")            # in the window
    seed(db_path, "EDGE", "2026-08-22 23:30:00")          # ±1 day: in
    seed(db_path, "OLD", "2026-08-01 09:00:00")           # outside, but settleable → in (tail)
    seed(db_path, "OLDDONE", "2026-08-02 09:00:00")       # outside and batched → out
    seed(db_path, "CANCELLED", "2026-08-23 10:00:00")     # in window, cancelled → in (with status)
    seed(db_path, "FUTURE", "2026-08-23 11:00:00")        # in window, future → in (reconcile decides)
    seed(db_path, "DIDI", "2026-08-23 12:00:00", service_type="滴滴")  # other platform → out
    create_settlement(db_path, "ride", ["OLDDONE"], 210.0, "2026-08-20", now=NOW)
    cancel_order(db_path, "CANCELLED")
    rows = settlement_candidates(db_path, ["2026-08-23"], now=datetime(2026, 8, 23, 10, 30))
    ids = {r["order_id"]: r for r in rows}
    assert set(ids) == {"IN", "EDGE", "OLD", "CANCELLED", "FUTURE"}
    assert ids["CANCELLED"]["status"] == "cancelled"
    assert ids["IN"]["settlement_id"] is None
    assert "price" in ids["IN"] and "banner_fee" in ids["IN"]


def test_init_db_adds_statement_columns_to_an_old_database(tmp_path):
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
    conn.close()
    assert "statement" in cols and "statement_image" in cols


def test_create_survives_an_unwritable_image(db_path, monkeypatch, caplog):
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    monkeypatch.setattr(os, "makedirs", _boom)
    with caplog.at_level("ERROR", logger="db"):
        sid = create_settlement(db_path, "ride", ["A1"], 280.0, "2026-08-26", now=NOW,
                                statement=STATEMENT, image=b"x")
    batch = get_settlement(db_path, sid)
    assert batch["statement"]["total"] == 490.0
    assert [o["order_id"] for o in batch["orders"]] == ["A1"]
    assert batch["statement_image"] is None
    assert "statement image not stored" in caplog.text


def test_candidates_skip_a_date_that_cannot_be_parsed(db_path):
    """The reader's date pattern matches on shape, so an OCR slip can produce a
    well-formed but impossible date: it must cost that one window, not the run."""
    seed(db_path, "IN", "2026-08-23 11:00:00")   # in the window, not yet settleable
    seed(db_path, "OLD", "2026-08-01 09:00:00")  # settleable tail
    now = datetime(2026, 8, 23, 10, 30)
    both = settlement_candidates(db_path, ["2026-88-23", "2026-08-23"], now=now)
    assert {r["order_id"] for r in both} == {"IN", "OLD"}
    only_bad = settlement_candidates(db_path, ["2026-88-23"], now=now)
    assert {r["order_id"] for r in only_bad} == {"OLD"}


def test_image_extension_reads_the_magic_bytes():
    assert image_extension(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "jpg"
    assert image_extension(b"\x89PNG\r\n\x1a\n") == "png"
    assert image_extension(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00") == "heic"
    assert image_extension(b"\x00\x00\x00\x18ftypheix\x00\x00\x00\x00") == "heic"
    assert image_extension(b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00") == "heic"
    assert image_extension(b"\x00\x00\x00\x18ftypqt  \x00\x00\x00\x00") == "bin"
    assert image_extension(b"not an image") == "bin"
    assert image_extension(b"") == "bin"


def test_create_stores_a_png_under_its_own_extension(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", 280.0)
    png = b"\x89PNG\r\n\x1a\n" + b"x"
    sid = create_settlement(db_path, "ride", ["A1"], 280.0, "2026-08-26", now=NOW,
                            statement=STATEMENT, image=png)
    assert get_settlement(db_path, sid)["statement_image"] == f"{sid}.png"
    path = statement_image_path(db_path, sid, "png")
    with open(path, "rb") as f:
        assert f.read() == png
    assert not os.path.exists(statement_image_path(db_path, sid, "jpg"))
    assert delete_settlement(db_path, sid) is True
    assert not os.path.exists(path)


# ---- month totals and what earlier months still hold ----

def credit(db_path, ref, amount, value_date="2026-08-26"):
    return insert_credit(db_path, {"ref": ref, "platform": "ride", "amount": amount,
                                   "currency": "HKD", "value_date": value_date,
                                   "payer": "A B**** C***** L", "memo": "SUPPLIERPAY",
                                   "email_id": None, "received_at": None, "recorded_at": None})


def identity_holds(t):
    return round(t["fare"] * 100) == sum(
        round(t[k] * 100) for k in ("received", "awaiting", "unsettled", "short"))


def seed_every_state(db_path):
    """One August with a leg in each state, and a booking not yet driven."""
    seed(db_path, "LOOSE", "2026-08-20 09:00:00", 210.0)
    seed(db_path, "WAIT1", "2026-08-21 09:00:00", 280.0)
    seed(db_path, "WAIT2", "2026-08-21 12:00:00", 300.5)
    seed(db_path, "PAID", "2026-08-22 09:00:00", 250.0)
    seed(db_path, "SHORT1", "2026-08-23 09:00:00", 400.0)
    seed(db_path, "SHORT2", "2026-08-23 12:00:00", 80.0)
    seed(db_path, "FUTURE", "2026-08-27 09:00:00", 999.0)
    create_settlement(db_path, "ride", ["WAIT1", "WAIT2"], 580.5, "2026-08-24", now=NOW)
    paid = create_settlement(db_path, "ride", ["PAID"], 250.0, "2026-08-24", now=NOW)
    short = create_settlement(db_path, "ride", ["SHORT1", "SHORT2"], 480.0, "2026-08-25", now=NOW)
    allocate(db_path, credit(db_path, "R-PAID", 250.0), paid)
    allocate(db_path, credit(db_path, "R-SHORT", 400.0), short)
    return short


def test_settle_month_splits_the_month_by_where_its_money_is(db_path):
    short = seed_every_state(db_path)
    mark_unpaid(db_path, short, ["SHORT2"])
    data = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert data["month_totals"] == {"fare": 1520.5, "received": 650.0, "awaiting": 580.5,
                                    "unsettled": 210.0, "short": 80.0}
    assert identity_holds(data["month_totals"])
    assert data["earlier"] == {"open": 0.0, "month": None}


def test_a_short_statement_with_no_leg_ticked_is_still_short_by_what_it_is_owed(db_path):
    seed_every_state(db_path)
    totals = get_settle_month(db_path, "2026-08", "ride", now=NOW)["month_totals"]
    assert totals == {"fare": 1520.5, "received": 650.0, "awaiting": 580.5,
                      "unsettled": 210.0, "short": 80.0}


def test_month_totals_leave_the_all_time_totals_as_they_were(db_path):
    """Other readers use them, and they answer a different question: what a
    batch is still owed, not what its orders are worth."""
    seed_every_state(db_path)
    data = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert data["totals"] == {"unsettled": 210.0, "awaiting": 660.5}
    assert data["counts"] == {"ride": 1, "didi": 0, "uber": 0, "foodpanda": 0}


def test_month_totals_count_one_platform(db_path):
    seed(db_path, "A1", "2026-08-20 09:00:00", 210.0)
    seed(db_path, "D1", "2026-08-20 10:00:00", 150.0, service_type="滴滴")
    assert get_settle_month(db_path, "2026-08", "ride", now=NOW)["month_totals"]["fare"] == 210.0
    assert get_settle_month(db_path, "2026-08", "didi", now=NOW)["month_totals"]["fare"] == 150.0


def test_an_empty_month_has_zero_totals(db_path):
    data = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert data["month_totals"] == {"fare": 0.0, "received": 0.0, "awaiting": 0.0,
                                    "unsettled": 0.0, "short": 0.0}
    assert data["earlier"] == {"open": 0.0, "month": None}


def test_earlier_is_an_older_unsettled_order(db_path):
    seed(db_path, "OLD", "2026-07-15 09:00:00", 300.0)
    seed(db_path, "A1", "2026-08-20 09:00:00", 210.0)
    data = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert data["earlier"] == {"open": 300.0, "month": "2026-07"}
    assert data["month_totals"]["unsettled"] == 210.0
    # The month itself and anything after it are not earlier.
    assert get_settle_month(db_path, "2026-07", "ride", now=NOW)["earlier"] == {"open": 0.0, "month": None}
    assert get_settle_month(db_path, "2026-09", "ride", now=NOW)["earlier"] == {"open": 510.0, "month": "2026-07"}


def test_earlier_adds_up_what_every_earlier_month_would_show(db_path):
    """Unsettled, awaiting and short, over every month before the one asked
    for, named by the earliest month that still holds any of it."""
    seed(db_path, "MAY", "2026-05-10 09:00:00", 120.0)
    create_settlement(db_path, "ride", ["MAY"], 120.0, "2026-05-12", now=NOW)
    seed(db_path, "JUN1", "2026-06-10 09:00:00", 400.0)
    seed(db_path, "JUN2", "2026-06-11 09:00:00", 80.0)
    short = create_settlement(db_path, "ride", ["JUN1", "JUN2"], 480.0, "2026-06-12", now=NOW)
    allocate(db_path, credit(db_path, "R-JUN", 400.0), short)
    seed(db_path, "JUL", "2026-07-15 09:00:00", 300.0)
    seed(db_path, "DIDI", "2026-07-16 09:00:00", 150.0, service_type="滴滴")
    seed(db_path, "AUG", "2026-08-20 09:00:00", 210.0)
    data = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert data["earlier"] == {"open": 500.0, "month": "2026-05"}
    by_month = sum(
        t["unsettled"] + t["awaiting"] + t["short"]
        for t in (get_settle_month(db_path, m, "ride", now=NOW)["month_totals"]
                  for m in ("2026-05", "2026-06", "2026-07")))
    assert data["earlier"]["open"] == round(by_month, 2)


def test_earlier_skips_months_that_are_fully_received(db_path):
    seed(db_path, "JUN", "2026-06-10 09:00:00", 400.0)
    paid = create_settlement(db_path, "ride", ["JUN"], 400.0, "2026-06-12", now=NOW)
    allocate(db_path, credit(db_path, "R-JUN", 400.0), paid)
    seed(db_path, "JUL", "2026-07-15 09:00:00", 300.0)
    assert get_settle_month(db_path, "2026-08", "ride", now=NOW)["earlier"] == {
        "open": 300.0, "month": "2026-07"}


def seed_straddling_short(db_path):
    seed(db_path, "JUL31", "2026-07-31 20:00:00", 250.25)
    seed(db_path, "AUG01", "2026-08-01 09:00:00", 600.0)
    short = create_settlement(db_path, "ride", ["JUL31", "AUG01"], 850.25, "2026-08-03", now=NOW)
    allocate(db_path, credit(db_path, "R-STRADDLE", 600.0), short)
    return short


def test_a_straddling_short_statement_is_short_in_the_month_of_its_unpaid_leg(db_path):
    short = seed_straddling_short(db_path)
    mark_unpaid(db_path, short, ["JUL31"])
    august = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert august["month_totals"] == {"fare": 600.0, "received": 600.0, "awaiting": 0.0,
                                      "unsettled": 0.0, "short": 0.0}
    assert august["earlier"] == {"open": 250.25, "month": "2026-07"}
    july = get_settle_month(db_path, "2026-07", "ride", now=NOW)
    assert july["month_totals"] == {"fare": 250.25, "received": 0.0, "awaiting": 0.0,
                                    "unsettled": 0.0, "short": 250.25}
    assert july["earlier"] == {"open": 0.0, "month": None}


def test_a_straddling_short_statement_with_no_tick_is_short_in_its_last_month(db_path):
    """Until a leg is named the shortfall sits with the statement's latest
    order, so the earlier month does not report money the later one shows."""
    seed_straddling_short(db_path)
    august = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert august["month_totals"] == {"fare": 600.0, "received": 349.75, "awaiting": 0.0,
                                      "unsettled": 0.0, "short": 250.25}
    assert august["earlier"] == {"open": 0.0, "month": None}
    july = get_settle_month(db_path, "2026-07", "ride", now=NOW)
    assert july["month_totals"] == {"fare": 250.25, "received": 250.25, "awaiting": 0.0,
                                    "unsettled": 0.0, "short": 0.0}
    assert get_settle_month(db_path, "2026-09", "ride", now=NOW)["earlier"] == {
        "open": 250.25, "month": "2026-08"}


def test_the_part_paid_ahead_follows_its_own_batch_across_months(db_path):
    """The 舉牌 rode on an August statement that is still waiting, while its
    July trip sits on no statement: August loads a batch none of its own
    orders is on, and July's money is open twice over."""
    seed(db_path, "B1", "2026-07-30 22:00:00", 300.0, service_type="接机", additional_services="举牌")
    seed(db_path, "A1", "2026-08-02 10:00:00", 210.0)
    held = next(o for o in settlement_candidates(db_path, ["2026-07-30"], now=NOW)
                if o["order_id"] == "B1")
    banner = held["banner_fee"]
    assert banner > 0
    create_settlement(db_path, "ride", ["A1"], 210.0 + banner, "2026-08-05", now=NOW,
                      adjustments=[{"order_ref": "B1", "date": "2026-07-30", "amount": banner,
                                    "ahead": True}])
    july = get_settle_month(db_path, "2026-07", "ride", now=NOW)["month_totals"]
    assert july == {"fare": 300.0 + banner, "received": 0.0, "awaiting": banner,
                    "unsettled": 300.0, "short": 0.0}
    august = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert august["earlier"] == {"open": 300.0 + banner, "month": "2026-07"}
    # Once the trip is on a paid statement, only the line paid ahead is open.
    trip = create_settlement(db_path, "ride", ["B1"], 300.0, "2026-08-10", now=NOW)
    allocate(db_path, credit(db_path, "R-TRIP", 300.0), trip)
    assert get_settle_month(db_path, "2026-08", "ride", now=NOW)["earlier"] == {
        "open": banner, "month": "2026-07"}
