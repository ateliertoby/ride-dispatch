import os
import sqlite3
from datetime import datetime

import pytest

from ride_dispatch import db as db_module
from ride_dispatch.db import (
    init_db, save_order, update_price, cancel_order, create_settlement, delete_settlement,
    get_settlement, get_settle_month, open_batches, insert_credit, allocate,
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
