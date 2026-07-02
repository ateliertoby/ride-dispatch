import os
import tempfile
from datetime import datetime, timedelta

import pytest

from ride_dispatch.parser import Order
from ride_dispatch.db import (
    init_db,
    save_order,
    update_flight_info,
    cancel_order,
    get_tracking_dates,
)
from ride_dispatch.flight import calc_next_interval, WATCHDOG_INTERVAL


NOW = datetime(2026, 7, 2, 12, 0, 0)


def make_order(**overrides) -> Order:
    defaults = dict(
        order_id="TEST001",
        service_type="接机",
        vehicle_type="经济5座",
        passenger_name="TEST/USER",
        scheduled_time="2026-07-02 12:01:00",
        passenger_phone="86 13800000000",
        overseas_phone="",
        flight_number="MU5017",
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


# --- get_tracking_dates: time-gated, flight_status has no say ---


def test_tracking_dates_ignores_gate_status(db_path):
    # Regression: stale 'gate' status must not stop polling (MU5017 2026-07-02)
    save_order(db_path, make_order(order_id="A"), telegram_msg_id=1)
    update_flight_info(db_path, "A", "11:50", None, "11:45", "gate")
    assert get_tracking_dates(db_path, now=NOW) == ["2026-07-02"]


def test_tracking_dates_excludes_stale_orders(db_path):
    old = (NOW - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    save_order(db_path, make_order(order_id="A", scheduled_time=old), telegram_msg_id=1)
    assert get_tracking_dates(db_path, now=NOW) == []


def test_tracking_dates_includes_future_dates(db_path):
    save_order(db_path, make_order(order_id="A", scheduled_time="2026-07-03 09:00:00"), telegram_msg_id=1)
    assert get_tracking_dates(db_path, now=NOW) == ["2026-07-03"]


def test_tracking_dates_excludes_cancelled(db_path):
    save_order(db_path, make_order(order_id="A"), telegram_msg_id=1)
    cancel_order(db_path, "A")
    assert get_tracking_dates(db_path, now=NOW) == []


def test_tracking_dates_excludes_non_pickup_and_no_flight(db_path):
    save_order(db_path, make_order(order_id="A", service_type="送机"), telegram_msg_id=1)
    save_order(db_path, make_order(order_id="B", flight_number=""), telegram_msg_id=2)
    assert get_tracking_dates(db_path, now=NOW) == []


# --- calc_next_interval: 3-tier, window-terminated ---


def order_dict(scheduled_time="2026-07-02 12:01:00", flight_status=None, flight_eta=None,
               flight_scheduled=None, service_type="接机", flight_number="MU5017"):
    return {
        "service_type": service_type,
        "flight_number": flight_number,
        "scheduled_time": scheduled_time,
        "flight_status": flight_status,
        "flight_eta": flight_eta,
        "flight_scheduled": flight_scheduled,
    }


def test_no_orders_returns_none():
    assert calc_next_interval([], now=NOW) is None


def test_gate_within_window_returns_watchdog():
    # Regression core: gate slows polling to watchdog, does not kill it
    orders = [order_dict(scheduled_time="2026-07-02 11:00:00", flight_status="gate", flight_eta="11:45")]
    assert calc_next_interval(orders, now=NOW) == WATCHDOG_INTERVAL


def test_gate_window_expired_returns_none():
    orders = [order_dict(scheduled_time="2026-07-02 07:00:00", flight_status="gate", flight_eta="07:30")]
    assert calc_next_interval(orders, now=NOW) is None


def test_any_landed_returns_60():
    orders = [
        order_dict(flight_status="gate", flight_eta="11:30"),
        order_dict(flight_status="landed", flight_eta="11:55"),
    ]
    assert calc_next_interval(orders, now=NOW) == 60


def test_est_halves_time_to_eta():
    orders = [order_dict(scheduled_time="2026-07-02 14:30:00", flight_status="est", flight_eta="14:00")]
    assert calc_next_interval(orders, now=NOW) == 3600


def test_halving_uses_flight_scheduled_when_no_eta():
    orders = [order_dict(scheduled_time="2026-07-02 14:30:00", flight_status=None, flight_scheduled="14:00")]
    assert calc_next_interval(orders, now=NOW) == 3600


def test_no_flight_data_returns_fallback():
    orders = [order_dict(scheduled_time="2026-07-02 13:00:00")]
    assert calc_next_interval(orders, now=NOW) == 1800


def test_delayed_eta_extends_window():
    # scheduled_time long past, but HKIA says flight still inbound — keep tracking
    orders = [order_dict(scheduled_time="2026-07-02 08:00:00", flight_status="est", flight_eta="12:30")]
    assert calc_next_interval(orders, now=NOW) == 900


def test_non_pickup_orders_ignored():
    orders = [
        order_dict(service_type="滴滴", flight_number=""),
        order_dict(service_type="送机"),
    ]
    assert calc_next_interval(orders, now=NOW) is None


def test_interval_floor_60():
    orders = [order_dict(scheduled_time="2026-07-02 12:30:00", flight_status="est", flight_eta="12:01")]
    assert calc_next_interval(orders, now=NOW) == 60


def test_eta_before_midnight_for_after_midnight_pickup():
    # 00:30 pickup, flight lands 23:50 previous evening
    orders = [order_dict(scheduled_time="2026-07-03 00:30:00", flight_status="est", flight_eta="23:50")]
    late_evening = datetime(2026, 7, 2, 23, 0, 0)
    assert calc_next_interval(orders, now=late_evening) == 1500
