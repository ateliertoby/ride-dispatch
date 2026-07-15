import os
import tempfile
import pytest
from ride_dispatch.parser import Order
from ride_dispatch.db import (
    init_db,
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
