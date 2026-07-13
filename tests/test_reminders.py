import os
import tempfile
from datetime import datetime, timedelta

import pytest

from ride_dispatch.parser import Order
from ride_dispatch.db import init_db, save_order, mark_reminder_sent, get_departure_reminders, get_orders_by_date
from ride_dispatch.flight import (
    svc_reminder_due,
    departure_milestones_due,
    pending_reminder_times,
    clamp_interval,
    WATCHDOG_INTERVAL,
)


NOW = datetime(2026, 7, 13, 12, 0, 0)


def make_order(**overrides) -> Order:
    defaults = dict(
        order_id="TEST001",
        service_type="接机",
        vehicle_type="经济5座",
        passenger_name="TEST/USER",
        scheduled_time="2026-07-13 12:00:00",
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


# ---- reminders_sent migration ----


def test_init_db_adds_reminders_sent_column(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # The column exists and is accessible
    row = conn.execute("SELECT reminders_sent FROM orders LIMIT 0").description
    conn.close()
    assert row is not None


def test_reminders_sent_default_empty(db_path):
    save_order(db_path, make_order(), telegram_msg_id=1)
    rows = get_orders_by_date(db_path, "2026-07-13")
    assert rows[0]["reminders_sent"] == ""


# ---- mark_reminder_sent ----


def test_mark_reminder_sent_single(db_path):
    save_order(db_path, make_order(), telegram_msg_id=1)
    mark_reminder_sent(db_path, "TEST001", "svc")
    rows = get_orders_by_date(db_path, "2026-07-13")
    assert rows[0]["reminders_sent"] == "svc"


def test_mark_reminder_sent_multiple(db_path):
    save_order(db_path, make_order(), telegram_msg_id=1)
    mark_reminder_sent(db_path, "TEST001", "dep30")
    mark_reminder_sent(db_path, "TEST001", "dep10")
    rows = get_orders_by_date(db_path, "2026-07-13")
    assert set(rows[0]["reminders_sent"].split(",")) == {"dep30", "dep10"}


def test_mark_reminder_sent_idempotent(db_path):
    save_order(db_path, make_order(), telegram_msg_id=1)
    mark_reminder_sent(db_path, "TEST001", "svc")
    mark_reminder_sent(db_path, "TEST001", "svc")
    rows = get_orders_by_date(db_path, "2026-07-13")
    assert rows[0]["reminders_sent"] == "svc"


def test_mark_reminder_sent_unknown_order(db_path):
    # Should not raise
    mark_reminder_sent(db_path, "NOPE", "svc")


# ---- get_departure_reminders ----


def test_departure_reminders_returns_songji(db_path):
    save_order(db_path, make_order(
        order_id="S1", service_type="送机",
        scheduled_time="2026-07-13 12:30:00",
    ), telegram_msg_id=1)
    orders = get_departure_reminders(db_path, NOW)
    assert len(orders) == 1
    assert orders[0]["order_id"] == "S1"


def test_departure_reminders_returns_dancheng(db_path):
    save_order(db_path, make_order(
        order_id="D1", service_type="单程接送",
        scheduled_time="2026-07-13 12:30:00",
    ), telegram_msg_id=1)
    orders = get_departure_reminders(db_path, NOW)
    assert len(orders) == 1
    assert orders[0]["order_id"] == "D1"


def test_departure_reminders_excludes_jieji(db_path):
    save_order(db_path, make_order(
        order_id="J1", service_type="接机",
        scheduled_time="2026-07-13 12:30:00",
    ), telegram_msg_id=1)
    assert get_departure_reminders(db_path, NOW) == []


def test_departure_reminders_excludes_past(db_path):
    save_order(db_path, make_order(
        order_id="S1", service_type="送机",
        scheduled_time="2026-07-13 11:59:59",
    ), telegram_msg_id=1)
    assert get_departure_reminders(db_path, NOW) == []


def test_departure_reminders_excludes_far_future(db_path):
    save_order(db_path, make_order(
        order_id="S1", service_type="送机",
        scheduled_time="2026-07-13 14:00:00",
    ), telegram_msg_id=1)
    assert get_departure_reminders(db_path, NOW) == []


def test_departure_reminders_excludes_cancelled(db_path):
    from ride_dispatch.db import cancel_order
    save_order(db_path, make_order(
        order_id="S1", service_type="送机",
        scheduled_time="2026-07-13 12:30:00",
    ), telegram_msg_id=1)
    cancel_order(db_path, "S1")
    assert get_departure_reminders(db_path, NOW) == []


# ---- svc_reminder_due ----


def test_svc_due_landed_within_window():
    order = {
        "service_type": "接机",
        "flight_status": "landed",
        "flight_eta": "11:30",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:00:00",
        "reminders_sent": "",
    }
    assert svc_reminder_due(order, NOW) == "12:00"


def test_svc_due_gate_status():
    order = {
        "service_type": "接机",
        "flight_status": "gate",
        "flight_eta": "11:20",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:00:00",
        "reminders_sent": "",
    }
    assert svc_reminder_due(order, NOW) == "11:50"


def test_svc_not_due_future():
    order = {
        "service_type": "接机",
        "flight_status": "landed",
        "flight_eta": "11:50",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:00:00",
        "reminders_sent": "",
    }
    # svc_time = 12:20, now = 12:00 → not yet
    assert svc_reminder_due(order, NOW) is None


def test_svc_not_due_already_sent():
    order = {
        "service_type": "接机",
        "flight_status": "landed",
        "flight_eta": "11:30",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:00:00",
        "reminders_sent": "svc",
    }
    assert svc_reminder_due(order, NOW) is None


def test_svc_not_due_staleness_guard():
    order = {
        "service_type": "接机",
        "flight_status": "landed",
        "flight_eta": "08:00",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 08:30:00",
        "reminders_sent": "",
    }
    # svc_time = 08:30, now = 12:00 → 3.5h old > 2h guard
    assert svc_reminder_due(order, NOW) is None


def test_svc_not_due_est_status():
    order = {
        "service_type": "接机",
        "flight_status": "est",
        "flight_eta": "11:30",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:00:00",
        "reminders_sent": "",
    }
    assert svc_reminder_due(order, NOW) is None


def test_svc_not_due_songji():
    order = {
        "service_type": "送机",
        "flight_status": "landed",
        "flight_eta": "11:30",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:00:00",
        "reminders_sent": "",
    }
    assert svc_reminder_due(order, NOW) is None


def test_svc_not_due_missing_eta():
    order = {
        "service_type": "接机",
        "flight_status": "landed",
        "flight_eta": None,
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:00:00",
        "reminders_sent": "",
    }
    assert svc_reminder_due(order, NOW) is None


def test_svc_not_due_missing_exit_minutes():
    order = {
        "service_type": "接机",
        "flight_status": "landed",
        "flight_eta": "11:30",
        "passenger_exit_minutes": None,
        "scheduled_time": "2026-07-13 12:00:00",
        "reminders_sent": "",
    }
    assert svc_reminder_due(order, NOW) is None


# ---- departure_milestones_due ----


def test_dep30_fires_at_t_minus_30():
    order = {
        "service_type": "送机",
        "scheduled_time": "2026-07-13 12:30:00",
        "reminders_sent": "",
    }
    assert departure_milestones_due(order, NOW) == ["dep30"]


def test_dep10_fires_at_t_minus_10():
    order = {
        "service_type": "送机",
        "scheduled_time": "2026-07-13 12:10:00",
        "reminders_sent": "",
    }
    assert "dep10" in departure_milestones_due(order, NOW)


def test_both_fire_when_catchup():
    # Order entered 5 min before pickup → both milestones fire
    order = {
        "service_type": "送机",
        "scheduled_time": "2026-07-13 12:05:00",
        "reminders_sent": "",
    }
    tags = departure_milestones_due(order, NOW)
    assert "dep30" in tags
    assert "dep10" in tags


def test_dep30_skipped_when_sent():
    order = {
        "service_type": "送机",
        "scheduled_time": "2026-07-13 12:30:00",
        "reminders_sent": "dep30",
    }
    assert departure_milestones_due(order, NOW) == []


def test_dep10_still_fires_when_dep30_sent():
    order = {
        "service_type": "送机",
        "scheduled_time": "2026-07-13 12:05:00",
        "reminders_sent": "dep30",
    }
    assert departure_milestones_due(order, NOW) == ["dep10"]


def test_no_fire_after_sched():
    order = {
        "service_type": "送机",
        "scheduled_time": "2026-07-13 11:59:00",
        "reminders_sent": "",
    }
    assert departure_milestones_due(order, NOW) == []


def test_no_fire_too_early():
    order = {
        "service_type": "送机",
        "scheduled_time": "2026-07-13 13:00:00",
        "reminders_sent": "",
    }
    # dep30 fires at 12:30, now is 12:00 → not yet
    assert departure_milestones_due(order, NOW) == []


def test_dancheng_fires():
    order = {
        "service_type": "单程接送",
        "scheduled_time": "2026-07-13 12:30:00",
        "reminders_sent": "",
    }
    assert departure_milestones_due(order, NOW) == ["dep30"]


def test_jieji_never_fires():
    order = {
        "service_type": "接机",
        "scheduled_time": "2026-07-13 12:30:00",
        "reminders_sent": "",
    }
    assert departure_milestones_due(order, NOW) == []


# ---- clamp_interval ----


def test_clamp_no_pending():
    assert clamp_interval(600, [], NOW) == 600


def test_clamp_reduces_interval():
    pending = [NOW + timedelta(seconds=180)]
    assert clamp_interval(600, pending, NOW) == 180


def test_clamp_floor_30():
    pending = [NOW + timedelta(seconds=10)]
    assert clamp_interval(600, pending, NOW) == 30


def test_clamp_keeps_shorter_interval():
    pending = [NOW + timedelta(seconds=300)]
    assert clamp_interval(60, pending, NOW) == 60


def test_clamp_past_pending():
    pending = [NOW - timedelta(seconds=10)]
    assert clamp_interval(600, pending, NOW) == 600


def test_clamp_picks_earliest():
    pending = [NOW + timedelta(seconds=300), NOW + timedelta(seconds=100)]
    assert clamp_interval(600, pending, NOW) == 100


# ---- pending_reminder_times ----


def test_pending_svc_future():
    order = {
        "service_type": "接机",
        "flight_status": "landed",
        "flight_eta": "12:10",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:30:00",
        "reminders_sent": "",
    }
    # svc_time = 12:40, which is in the future relative to NOW (12:00)
    times = pending_reminder_times([order], NOW)
    assert len(times) == 1
    assert times[0] == datetime(2026, 7, 13, 12, 40, 0)


def test_pending_svc_past_not_included():
    order = {
        "service_type": "接机",
        "flight_status": "landed",
        "flight_eta": "11:20",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:00:00",
        "reminders_sent": "",
    }
    # svc_time = 11:50, already past NOW (12:00) → fires this tick, not pending
    assert pending_reminder_times([order], NOW) == []


def test_pending_svc_sent_not_included():
    order = {
        "service_type": "接机",
        "flight_status": "landed",
        "flight_eta": "12:10",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:30:00",
        "reminders_sent": "svc",
    }
    assert pending_reminder_times([order], NOW) == []


def test_pending_dep_future():
    order = {
        "service_type": "送机",
        "scheduled_time": "2026-07-13 12:40:00",
        "reminders_sent": "",
    }
    # dep30 at 12:10, dep10 at 12:30 — both future relative to NOW (12:00)
    times = pending_reminder_times([order], NOW)
    assert datetime(2026, 7, 13, 12, 10, 0) in times
    assert datetime(2026, 7, 13, 12, 30, 0) in times


def test_pending_dep_past_not_included():
    order = {
        "service_type": "送机",
        "scheduled_time": "2026-07-13 12:05:00",
        "reminders_sent": "",
    }
    # dep30 at 11:35 (past), dep10 at 11:55 (past)
    assert pending_reminder_times([order], NOW) == []


def test_pending_mixed_orders():
    svc_order = {
        "service_type": "接机",
        "flight_status": "gate",
        "flight_eta": "12:10",
        "passenger_exit_minutes": 20,
        "scheduled_time": "2026-07-13 12:30:00",
        "reminders_sent": "",
    }
    dep_order = {
        "service_type": "单程接送",
        "scheduled_time": "2026-07-13 12:50:00",
        "reminders_sent": "",
    }
    times = pending_reminder_times([svc_order, dep_order], NOW)
    # svc: 12:30, dep30: 12:20, dep10: 12:40
    assert datetime(2026, 7, 13, 12, 30, 0) in times
    assert datetime(2026, 7, 13, 12, 20, 0) in times
    assert datetime(2026, 7, 13, 12, 40, 0) in times
