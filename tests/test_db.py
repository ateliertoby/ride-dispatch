import os
import tempfile
from datetime import datetime
import pytest
from ride_dispatch.parser import Order
from ride_dispatch.db import (
    init_db,
    resolve_db_path,
    save_order,
    update_price,
    get_orders_by_date,
    get_order_by_telegram_msg_id,
    get_pickup_flights,
    update_flight_info,
)


def make_order(**overrides) -> Order:
    defaults = dict(
        order_id="TEST001",
        service_type="接机",
        vehicle_type="经济5座",
        passenger_name="TEST/USER",
        scheduled_time="2026-06-27 11:00:00",
        passenger_phone="86 13800000000",
        overseas_phone="",
        flight_number="CX100",
        pickup="香港国际机场 T1",
        dropoff="尖沙咀",
        distance_km=30,
        notes="",
        driver_notes="",
        additional_services="",
        passenger_exit_minutes=30,
        third_party_contact="",
        more_contacts="",
        raw_message="raw text here",
    )
    defaults.update(overrides)
    return Order(**defaults)


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    os.unlink(path)


def test_init_creates_table(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='orders'"
    ).fetchall()
    conn.close()
    assert len(tables) == 1


def test_save_and_retrieve(db_path):
    order = make_order()
    save_order(db_path, order, telegram_msg_id=12345)
    rows = get_orders_by_date(db_path, "2026-06-27")
    assert len(rows) == 1
    assert rows[0]["order_id"] == "TEST001"
    assert rows[0]["passenger_name"] == "TEST/USER"
    assert rows[0]["telegram_msg_id"] == 12345


def test_update_price(db_path):
    order = make_order()
    save_order(db_path, order, telegram_msg_id=100)
    update_price(db_path, "TEST001", 350.0)
    rows = get_orders_by_date(db_path, "2026-06-27")
    assert rows[0]["price"] == 350.0


def test_get_by_telegram_msg_id(db_path):
    order = make_order()
    save_order(db_path, order, telegram_msg_id=99999)
    result = get_order_by_telegram_msg_id(db_path, 99999)
    assert result is not None
    assert result["order_id"] == "TEST001"
    assert get_order_by_telegram_msg_id(db_path, 11111) is None


def test_orders_sorted_by_time(db_path):
    save_order(db_path, make_order(order_id="LATE", scheduled_time="2026-06-27 15:00:00"), 1)
    save_order(db_path, make_order(order_id="EARLY", scheduled_time="2026-06-27 08:00:00"), 2)
    save_order(db_path, make_order(order_id="MID", scheduled_time="2026-06-27 11:00:00"), 3)
    rows = get_orders_by_date(db_path, "2026-06-27")
    assert [r["order_id"] for r in rows] == ["EARLY", "MID", "LATE"]


def test_duplicate_order_id_raises(db_path):
    import sqlite3
    order = make_order()
    save_order(db_path, order, telegram_msg_id=1)
    with pytest.raises(sqlite3.IntegrityError):
        save_order(db_path, order, telegram_msg_id=2)


def test_get_pickup_flights(db_path):
    save_order(db_path, make_order(order_id="P1", service_type="接机", flight_number="CX100", scheduled_time="2026-06-27 11:00:00"), 1)
    save_order(db_path, make_order(order_id="P2", service_type="送机", flight_number="QW916", scheduled_time="2026-06-27 12:00:00"), 2)
    save_order(db_path, make_order(order_id="P3", service_type="接机", flight_number="", scheduled_time="2026-06-27 13:00:00"), 3)
    rows = get_pickup_flights(db_path, "2026-06-27")
    assert len(rows) == 1
    assert rows[0]["order_id"] == "P1"


def test_count_active_orders(db_path):
    from ride_dispatch.db import count_active_orders, cancel_order
    save_order(db_path, make_order(order_id="C1"), 1)
    save_order(db_path, make_order(order_id="C2"), 2)
    cancel_order(db_path, "C2")
    assert count_active_orders(db_path) == 1


def test_get_pickup_flights_includes_scheduled_time(db_path):
    # match_flights needs pickup time to pick the right day's leg
    save_order(db_path, make_order(order_id="P1", scheduled_time="2026-06-27 11:00:00"), 1)
    rows = get_pickup_flights(db_path, "2026-06-27")
    assert rows[0]["scheduled_time"] == "2026-06-27 11:00:00"


def test_update_flight_info_est(db_path):
    save_order(db_path, make_order(order_id="F1"), 1)
    update_flight_info(db_path, "F1", scheduled="14:40", eta="14:26", gate=None, status="est")
    rows = get_orders_by_date(db_path, "2026-06-27")
    assert rows[0]["flight_scheduled"] == "14:40"
    assert rows[0]["flight_eta"] == "14:26"
    assert rows[0]["flight_gate"] is None
    assert rows[0]["flight_status"] == "est"


def test_update_flight_info_gate_preserves_eta(db_path):
    save_order(db_path, make_order(order_id="F2"), 1)
    update_flight_info(db_path, "F2", scheduled="14:40", eta="14:30", gate=None, status="landed")
    update_flight_info(db_path, "F2", scheduled="14:40", eta=None, gate="14:35", status="gate")
    rows = get_orders_by_date(db_path, "2026-06-27")
    assert rows[0]["flight_eta"] == "14:30"
    assert rows[0]["flight_gate"] == "14:35"
    assert rows[0]["flight_status"] == "gate"


def test_update_flight_info_scheduled_only(db_path):
    save_order(db_path, make_order(order_id="F3"), 1)
    update_flight_info(db_path, "F3", scheduled="16:00", eta=None, gate=None, status=None)
    rows = get_orders_by_date(db_path, "2026-06-27")
    assert rows[0]["flight_scheduled"] == "16:00"
    assert rows[0]["flight_eta"] is None
    assert rows[0]["flight_gate"] is None
    assert rows[0]["flight_status"] is None


def test_update_order_fields(db_path):
    from ride_dispatch.db import update_order_fields
    save_order(db_path, make_order(), 1)
    assert update_order_fields(db_path, "TEST001", {"price": 300.0, "scheduled_time": "2026-06-27 12:30:00"}) is True
    rows = get_orders_by_date(db_path, "2026-06-27")
    assert rows[0]["price"] == 300.0
    assert rows[0]["scheduled_time"] == "2026-06-27 12:30:00"


def test_update_order_fields_rejects_non_whitelisted(db_path):
    from ride_dispatch.db import update_order_fields
    save_order(db_path, make_order(), 1)
    with pytest.raises(ValueError):
        update_order_fields(db_path, "TEST001", {"order_id": "HAX"})


def test_update_order_fields_unknown_order(db_path):
    from ride_dispatch.db import update_order_fields
    assert update_order_fields(db_path, "NOPE", {"price": 1.0}) is False


def test_flight_columns_null_by_default(db_path):
    save_order(db_path, make_order(order_id="F4"), 1)
    rows = get_orders_by_date(db_path, "2026-06-27")
    assert rows[0]["flight_scheduled"] is None
    assert rows[0]["flight_eta"] is None
    assert rows[0]["flight_gate"] is None
    assert rows[0]["flight_status"] is None


def test_order_id_exists(tmp_path):
    from ride_dispatch.db import init_db, save_quick_order, order_id_exists, update_order_fields
    path = str(tmp_path / "t.db")
    init_db(path)
    assert order_id_exists(path, "Q9") is False
    save_quick_order(path, "Q9", "滴滴", "2026-07-01 10:00:00", 100.0, 0.0)
    assert order_id_exists(path, "Q9") is True
    update_order_fields(path, "Q9", {"status": "cancelled"})
    assert order_id_exists(path, "Q9") is True  # cancelled orders still hold the order_id (UNIQUE column)


def test_resolve_db_path_expands_tilde(monkeypatch):
    monkeypatch.setenv("RIDE_DB_PATH", "~/.ride-dispatch/orders.db")
    resolved = resolve_db_path()
    assert resolved == os.path.join(os.path.expanduser("~"), ".ride-dispatch", "orders.db")
    assert "~" not in resolved


def test_resolve_db_path_keeps_absolute_path(monkeypatch):
    monkeypatch.setenv("RIDE_DB_PATH", "/srv/ride-dispatch/orders.db")
    assert resolve_db_path() == "/srv/ride-dispatch/orders.db"


def test_resolve_db_path_keeps_relative_path(monkeypatch):
    monkeypatch.setenv("RIDE_DB_PATH", "data/orders.db")
    assert resolve_db_path() == "data/orders.db"


def test_resolve_db_path_defaults_to_cwd_file(monkeypatch):
    monkeypatch.delenv("RIDE_DB_PATH", raising=False)
    assert resolve_db_path() == "orders.db"


# ---- settlement ----

NOW = datetime(2026, 8, 20, 12, 0, 0)


def seed_leg(db_path, order_id, service_type="接机", scheduled="2026-08-17 09:00:00",
             price=500.0, tunnel=0.0, banner=0.0):
    from ride_dispatch.db import save_quick_order, _conn
    save_quick_order(db_path, order_id, service_type, scheduled, price, tunnel)
    with _conn(db_path) as conn:
        conn.execute("UPDATE orders SET banner_fee = ? WHERE order_id = ?", (banner, order_id))
        conn.commit()
    return order_id


def settlement_row(db_path, settlement_id):
    from ride_dispatch.db import _conn
    with _conn(db_path) as conn:
        row = conn.execute("SELECT * FROM settlements WHERE id = ?", (settlement_id,)).fetchone()
        return dict(row) if row else None


def settlement_id_of(db_path, order_id):
    from ride_dispatch.db import _conn
    with _conn(db_path) as conn:
        return conn.execute(
            "SELECT settlement_id FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()[0]


def count_settlements(db_path):
    from ride_dispatch.db import _conn
    with _conn(db_path) as conn:
        return conn.execute("SELECT count(*) FROM settlements").fetchone()[0]


def test_create_settlement_computes_expected_from_rows(db_path):
    from ride_dispatch.db import create_settlement
    seed_leg(db_path, "A1", price=500.0, banner=40.0)
    seed_leg(db_path, "A2", price=300.0, banner=0.0)
    sid = create_settlement(db_path, "ride", ["A1", "A2"], 830.0, "2026-08-20", now=NOW)
    row = settlement_row(db_path, sid)
    assert row["expected_amount"] == 840.0   # client-sent 830 is the confirmed figure only
    assert row["confirmed_amount"] == 830.0
    assert row["platform"] == "ride"
    assert row["settled_on"] == "2026-08-20"
    assert row["paid_on"] is None
    assert settlement_id_of(db_path, "A1") == sid
    assert settlement_id_of(db_path, "A2") == sid


def test_create_settlement_rejects_unpriced(db_path):
    from ride_dispatch.db import create_settlement
    seed_leg(db_path, "A1")
    seed_leg(db_path, "NOPRICE", price=0.0)
    with pytest.raises(ValueError, match="NOPRICE"):
        create_settlement(db_path, "ride", ["A1", "NOPRICE"], 500.0, "2026-08-20", now=NOW)
    assert count_settlements(db_path) == 0
    assert settlement_id_of(db_path, "A1") is None


def test_create_settlement_rejects_future_leg(db_path):
    from ride_dispatch.db import create_settlement
    seed_leg(db_path, "LATER", scheduled="2026-08-20 23:00:00")
    with pytest.raises(ValueError, match="LATER"):
        create_settlement(db_path, "ride", ["LATER"], 500.0, "2026-08-20", now=NOW)
    assert count_settlements(db_path) == 0


def test_create_settlement_rejects_cancelled(db_path):
    from ride_dispatch.db import create_settlement, cancel_order
    seed_leg(db_path, "GONE")
    cancel_order(db_path, "GONE")
    with pytest.raises(ValueError, match="GONE"):
        create_settlement(db_path, "ride", ["GONE"], 500.0, "2026-08-20", now=NOW)
    assert count_settlements(db_path) == 0


def test_create_settlement_rejects_missing_order(db_path):
    from ride_dispatch.db import create_settlement
    with pytest.raises(ValueError, match="NOPE"):
        create_settlement(db_path, "ride", ["NOPE"], 500.0, "2026-08-20", now=NOW)
    assert count_settlements(db_path) == 0


def test_create_settlement_rejects_already_batched(db_path):
    from ride_dispatch.db import create_settlement
    seed_leg(db_path, "A1")
    seed_leg(db_path, "A2")
    create_settlement(db_path, "ride", ["A1"], 500.0, "2026-08-20", now=NOW)
    with pytest.raises(ValueError, match="A1"):
        create_settlement(db_path, "ride", ["A2", "A1"], 1000.0, "2026-08-20", now=NOW)
    assert count_settlements(db_path) == 1
    assert settlement_id_of(db_path, "A2") is None


def test_create_settlement_rejects_cross_platform(db_path):
    from ride_dispatch.db import create_settlement
    seed_leg(db_path, "A1")
    seed_leg(db_path, "D1", service_type="滴滴", tunnel=30.0)
    with pytest.raises(ValueError, match="D1"):
        create_settlement(db_path, "ride", ["A1", "D1"], 1000.0, "2026-08-20", now=NOW)
    assert count_settlements(db_path) == 0
    assert settlement_id_of(db_path, "A1") is None


def test_create_settlement_rejects_duplicate_ids(db_path):
    from ride_dispatch.db import create_settlement
    seed_leg(db_path, "A1")
    with pytest.raises(ValueError, match="A1"):
        create_settlement(db_path, "ride", ["A1", "A1"], 1000.0, "2026-08-20", now=NOW)
    assert count_settlements(db_path) == 0


def test_create_settlement_rejects_unknown_platform_and_empty_list(db_path):
    from ride_dispatch.db import create_settlement
    seed_leg(db_path, "A1")
    with pytest.raises(ValueError):
        create_settlement(db_path, "taxi", ["A1"], 500.0, "2026-08-20", now=NOW)
    with pytest.raises(ValueError):
        create_settlement(db_path, "ride", [], 0.0, "2026-08-20", now=NOW)
    assert count_settlements(db_path) == 0


def test_create_settlement_didi_expects_tunnel(db_path):
    from ride_dispatch.db import create_settlement
    seed_leg(db_path, "D1", service_type="滴滴", price=200.0, tunnel=30.0)
    sid = create_settlement(db_path, "didi", ["D1"], 230.0, "2026-08-20", now=NOW)
    assert settlement_row(db_path, sid)["expected_amount"] == 230.0


def test_mark_settlement_paid(db_path):
    from ride_dispatch.db import create_settlement, mark_settlement_paid
    seed_leg(db_path, "A1")
    sid = create_settlement(db_path, "ride", ["A1"], 500.0, "2026-08-20", now=NOW)
    assert mark_settlement_paid(db_path, sid, "2026-08-22") is True
    assert settlement_row(db_path, sid)["paid_on"] == "2026-08-22"


def test_mark_settlement_paid_again_keeps_first_date(db_path):
    from ride_dispatch.db import create_settlement, mark_settlement_paid
    seed_leg(db_path, "A1")
    sid = create_settlement(db_path, "ride", ["A1"], 500.0, "2026-08-20", now=NOW)
    mark_settlement_paid(db_path, sid, "2026-08-22")
    assert mark_settlement_paid(db_path, sid, "2026-08-25") is True
    assert settlement_row(db_path, sid)["paid_on"] == "2026-08-22"


def test_mark_settlement_paid_unknown_id(db_path):
    from ride_dispatch.db import mark_settlement_paid
    assert mark_settlement_paid(db_path, 999, "2026-08-22") is False


def test_delete_settlement_unlinks_orders(db_path):
    from ride_dispatch.db import create_settlement, delete_settlement
    seed_leg(db_path, "A1")
    seed_leg(db_path, "A2")
    sid = create_settlement(db_path, "ride", ["A1", "A2"], 1000.0, "2026-08-20", now=NOW)
    assert delete_settlement(db_path, sid) is True
    assert count_settlements(db_path) == 0
    assert settlement_id_of(db_path, "A1") is None
    assert settlement_id_of(db_path, "A2") is None


def test_delete_settlement_unknown_id(db_path):
    from ride_dispatch.db import delete_settlement
    assert delete_settlement(db_path, 999) is False


def test_delete_settlement_frees_orders_for_a_new_batch(db_path):
    from ride_dispatch.db import create_settlement, delete_settlement
    seed_leg(db_path, "A1")
    sid = create_settlement(db_path, "ride", ["A1"], 500.0, "2026-08-20", now=NOW)
    delete_settlement(db_path, sid)
    again = create_settlement(db_path, "ride", ["A1"], 500.0, "2026-08-21", now=NOW)
    assert again != sid


def test_batched_order_cannot_be_cancelled(db_path):
    from ride_dispatch.db import create_settlement, cancel_order, get_order_by_id
    seed_leg(db_path, "A1")
    create_settlement(db_path, "ride", ["A1"], 500.0, "2026-08-20", now=NOW)
    with pytest.raises(ValueError):
        cancel_order(db_path, "A1")
    assert get_order_by_id(db_path, "A1") is not None


def test_batched_order_amount_fields_locked(db_path):
    from ride_dispatch.db import create_settlement, update_order_fields, get_order_by_id
    seed_leg(db_path, "A1", price=500.0, banner=40.0)
    create_settlement(db_path, "ride", ["A1"], 540.0, "2026-08-20", now=NOW)
    for field, value in [("price", 600.0), ("banner_fee", 0.0),
                         ("tunnel_fee", 50.0), ("status", "cancelled")]:
        with pytest.raises(ValueError):
            update_order_fields(db_path, "A1", {field: value})
    row = get_order_by_id(db_path, "A1")
    assert row["price"] == 500.0 and row["banner_fee"] == 40.0


def test_batched_order_parking_and_time_still_editable(db_path):
    from ride_dispatch.db import create_settlement, update_order_fields, get_order_by_id
    seed_leg(db_path, "A1")
    create_settlement(db_path, "ride", ["A1"], 500.0, "2026-08-20", now=NOW)
    assert update_order_fields(db_path, "A1", {"parking_fee": 32.0}) is True
    assert update_order_fields(db_path, "A1", {"scheduled_time": "2026-08-17 10:00:00"}) is True
    row = get_order_by_id(db_path, "A1")
    assert row["parking_fee"] == 32.0
    assert row["scheduled_time"] == "2026-08-17 10:00:00"


def test_unbatched_order_still_editable_and_cancellable(db_path):
    from ride_dispatch.db import update_order_fields, cancel_order, get_order_by_id
    seed_leg(db_path, "A1")
    assert update_order_fields(db_path, "A1", {"price": 600.0}) is True
    cancel_order(db_path, "A1")
    assert get_order_by_id(db_path, "A1") is None


def test_get_settlement_carries_orders(db_path):
    from ride_dispatch.db import create_settlement, get_settlement
    seed_leg(db_path, "A2", scheduled="2026-08-17 15:00:00")
    seed_leg(db_path, "A1", scheduled="2026-08-17 09:00:00")
    sid = create_settlement(db_path, "ride", ["A1", "A2"], 1000.0, "2026-08-20", now=NOW)
    batch = get_settlement(db_path, sid)
    assert [o["order_id"] for o in batch["orders"]] == ["A1", "A2"]
    assert batch["expected_amount"] == 1000.0


def test_get_settlement_unknown_id(db_path):
    from ride_dispatch.db import get_settlement
    assert get_settlement(db_path, 999) is None


def test_get_orders_by_date_carries_settlement_dates(db_path):
    from ride_dispatch.db import create_settlement, mark_settlement_paid
    seed_leg(db_path, "A1")
    rows = get_orders_by_date(db_path, "2026-08-17")
    assert rows[0]["settlement_id"] is None
    assert rows[0]["settlement_settled_on"] is None
    assert rows[0]["settlement_paid_on"] is None
    sid = create_settlement(db_path, "ride", ["A1"], 500.0, "2026-08-20", now=NOW)
    rows = get_orders_by_date(db_path, "2026-08-17")
    assert rows[0]["settlement_settled_on"] == "2026-08-20"
    assert rows[0]["settlement_paid_on"] is None
    mark_settlement_paid(db_path, sid, "2026-08-22")
    assert get_orders_by_date(db_path, "2026-08-17")[0]["settlement_paid_on"] == "2026-08-22"


def test_get_settle_month_counts_all_platforms_all_time(db_path):
    from ride_dispatch.db import get_settle_month, create_settlement
    seed_leg(db_path, "OLD", scheduled="2026-07-02 09:00:00")
    seed_leg(db_path, "A1")
    seed_leg(db_path, "D1", service_type="滴滴", price=200.0, tunnel=30.0)
    seed_leg(db_path, "U1", service_type="Uber", price=100.0)
    seed_leg(db_path, "F1", service_type="foodpanda", price=55.0)
    seed_leg(db_path, "FUTURE", scheduled="2026-08-30 09:00:00")
    seed_leg(db_path, "NOPRICE", price=0.0)
    data = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert data["counts"] == {"ride": 2, "didi": 1, "uber": 1, "foodpanda": 1}
    assert data["totals"]["unsettled"] == 1000.0   # OLD + A1, both past and priced
    create_settlement(db_path, "ride", ["OLD"], 480.0, "2026-08-20", now=NOW)
    data = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert data["counts"]["ride"] == 1
    assert data["totals"]["unsettled"] == 500.0
    assert data["totals"]["awaiting"] == 480.0


def test_get_settle_month_awaiting_drops_once_paid(db_path):
    from ride_dispatch.db import get_settle_month, create_settlement, mark_settlement_paid
    seed_leg(db_path, "A1")
    sid = create_settlement(db_path, "ride", ["A1"], 500.0, "2026-08-20", now=NOW)
    mark_settlement_paid(db_path, sid, "2026-08-22")
    assert get_settle_month(db_path, "2026-08", "ride", now=NOW)["totals"]["awaiting"] == 0


def test_get_settle_month_filters_by_platform_and_month(db_path):
    from ride_dispatch.db import get_settle_month
    seed_leg(db_path, "A1")
    seed_leg(db_path, "JULY", scheduled="2026-07-02 09:00:00")
    seed_leg(db_path, "D1", service_type="滴滴", price=200.0)
    data = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert [o["order_id"] for o in data["orders"]] == ["A1"]
    assert data["now"] == "2026-08-20 12:00:00"


def test_get_settle_month_returns_straddling_batch_whole(db_path):
    from ride_dispatch.db import get_settle_month, create_settlement
    seed_leg(db_path, "JUL31", scheduled="2026-07-31 20:00:00")
    seed_leg(db_path, "AUG01", scheduled="2026-08-01 09:00:00")
    sid = create_settlement(db_path, "ride", ["JUL31", "AUG01"], 1000.0, "2026-08-02", now=NOW)
    data = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert [o["order_id"] for o in data["orders"]] == ["AUG01"]
    assert len(data["settlements"]) == 1
    batch = data["settlements"][0]
    assert batch["id"] == sid
    assert [o["order_id"] for o in batch["orders"]] == ["JUL31", "AUG01"]


def test_get_settle_month_empty(db_path):
    from ride_dispatch.db import get_settle_month
    data = get_settle_month(db_path, "2026-08", "ride", now=NOW)
    assert data["orders"] == []
    assert data["settlements"] == []
    assert data["counts"] == {"ride": 0, "didi": 0, "uber": 0, "foodpanda": 0}
    assert data["totals"] == {"unsettled": 0, "awaiting": 0}
