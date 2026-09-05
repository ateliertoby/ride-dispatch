import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import ride_dispatch.bot as bot
from ride_dispatch.parser import Order
from ride_dispatch.db import (
    init_db, save_order, mark_reminder_sent, get_departure_reminders, get_orders_by_date,
    update_flight_info,
)
from ride_dispatch.flight import (
    svc_reminder_due,
    departure_milestones_due,
    pending_reminder_times,
    clamp_interval,
    depart_reminder_due,
    eta_passed_advisory_due,
    ETA_PASSED_GRACE,
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


# ---- depart_reminder_due ----


def test_depart_due_est_status():
    order = {
        "service_type": "接机",
        "flight_status": "est",
        "flight_eta": "12:10",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:40:00",
        "reminders_sent": "",
    }
    # depart = 12:10 + 30 - 40 = 12:00 → fires pre-landing
    assert depart_reminder_due(order, NOW) == "12:00"


def test_depart_due_no_status_uses_booking_fallback():
    order = {
        "service_type": "接机",
        "flight_status": None,
        "flight_eta": None,
        "flight_scheduled": None,
        "passenger_exit_minutes": 20,
        "scheduled_time": "2026-07-13 12:30:00",
        "reminders_sent": "",
    }
    # No flight data: depart = booking 12:30 - 40 = 11:50
    assert depart_reminder_due(order, NOW) == "11:50"


def test_depart_not_due_future():
    order = {
        "service_type": "接机",
        "flight_status": "est",
        "flight_eta": "13:00",
        "passenger_exit_minutes": 20,
        "scheduled_time": "2026-07-13 13:20:00",
        "reminders_sent": "",
    }
    # depart = 12:40, now 12:00 → not yet
    assert depart_reminder_due(order, NOW) is None


def test_depart_postponed_when_eta_slips():
    base = {
        "service_type": "接机",
        "flight_status": "est",
        "passenger_exit_minutes": 20,
        "scheduled_time": "2026-07-13 12:40:00",
        "reminders_sent": "",
    }
    assert depart_reminder_due({**base, "flight_eta": "12:15"}, NOW) == "11:55"
    # Same order after HKIA slips the ETA to 12:50 → no longer due
    assert depart_reminder_due({**base, "flight_eta": "12:50"}, NOW) is None


def test_depart_not_due_already_sent():
    order = {
        "service_type": "接机",
        "flight_status": "est",
        "flight_eta": "12:10",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:40:00",
        "reminders_sent": "depart",
    }
    assert depart_reminder_due(order, NOW) is None


def test_depart_not_due_staleness_guard():
    order = {
        "service_type": "接机",
        "flight_status": "landed",
        "flight_eta": "09:00",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 09:30:00",
        "reminders_sent": "",
    }
    # depart = 08:50, now 12:00 → >2h past
    assert depart_reminder_due(order, NOW) is None


def test_depart_not_due_cancelled_flight():
    order = {
        "service_type": "接机",
        "flight_status": "cancelled",
        "flight_eta": "12:10",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:40:00",
        "reminders_sent": "",
    }
    assert depart_reminder_due(order, NOW) is None


def test_depart_not_due_songji():
    order = {
        "service_type": "送机",
        "flight_status": "est",
        "flight_eta": "12:10",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:40:00",
        "reminders_sent": "",
    }
    assert depart_reminder_due(order, NOW) is None


def test_depart_not_due_missing_exit_minutes():
    order = {
        "service_type": "接机",
        "flight_status": "est",
        "flight_eta": "12:10",
        "passenger_exit_minutes": None,
        "scheduled_time": "2026-07-13 12:40:00",
        "reminders_sent": "",
    }
    assert depart_reminder_due(order, NOW) is None


def test_depart_due_landed_catchup():
    # Late entry / early flight: already landed but never reminded → catch up
    order = {
        "service_type": "接机",
        "flight_status": "landed",
        "flight_eta": "11:50",
        "passenger_exit_minutes": 20,
        "scheduled_time": "2026-07-13 12:10:00",
        "reminders_sent": "",
    }
    # depart = 11:30, 30 min past, within the 2h guard
    assert depart_reminder_due(order, NOW) == "11:30"


# ---- pending_reminder_times: depart branch ----


def test_pending_depart_future_est():
    order = {
        "service_type": "接机",
        "flight_status": "est",
        "flight_eta": "13:00",
        "passenger_exit_minutes": 20,
        "scheduled_time": "2026-07-13 13:20:00",
        "reminders_sent": "",
    }
    # depart = 12:40 — pre-landing, must be a wake-up target
    assert datetime(2026, 7, 13, 12, 40, 0) in pending_reminder_times([order], NOW)


def test_pending_depart_and_svc_together():
    order = {
        "service_type": "接机",
        "flight_status": "landed",
        "flight_eta": "12:20",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:50:00",
        "reminders_sent": "",
    }
    times = pending_reminder_times([order], NOW)
    # depart = 12:10, svc = 12:50
    assert datetime(2026, 7, 13, 12, 10, 0) in times
    assert datetime(2026, 7, 13, 12, 50, 0) in times


def test_pending_depart_sent_not_included():
    order = {
        "service_type": "接机",
        "flight_status": "est",
        "flight_eta": "13:00",
        "passenger_exit_minutes": 20,
        "scheduled_time": "2026-07-13 13:20:00",
        "reminders_sent": "depart",
    }
    # depart = 12:50; the est order's own eta-passed wake-up at 13:05 stays
    assert datetime(2026, 7, 13, 12, 50, 0) not in pending_reminder_times([order], NOW)


def test_pending_depart_cancelled_not_included():
    order = {
        "service_type": "接机",
        "flight_status": "cancelled",
        "flight_eta": "13:00",
        "passenger_exit_minutes": 20,
        "scheduled_time": "2026-07-13 13:20:00",
        "reminders_sent": "",
    }
    assert pending_reminder_times([order], NOW) == []


def test_pending_depart_no_flight_data_uses_booking():
    order = {
        "service_type": "接机",
        "flight_status": None,
        "flight_eta": None,
        "flight_scheduled": None,
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 13:30:00",
        "reminders_sent": "",
    }
    # depart = 13:30 - 40 = 12:50; svc branch needs landed/gate so this is the only entry
    assert pending_reminder_times([order], NOW) == [datetime(2026, 7, 13, 12, 50, 0)]


# ---- eta_passed_advisory_due ----


def est_order(**overrides) -> dict:
    order = {
        "service_type": "接机",
        "flight_status": "est",
        "flight_eta": "11:50",
        "passenger_exit_minutes": 30,
        "scheduled_time": "2026-07-13 12:20:00",
        "reminders_sent": "",
    }
    order.update(overrides)
    return order


def test_etapass_due_when_eta_and_grace_have_passed():
    # eta 11:50 + 5 min grace = 11:55, now 12:00
    assert eta_passed_advisory_due(est_order(), NOW) == "11:50"


def test_etapass_not_due_inside_grace():
    # eta 11:58 + 5 min grace = 12:03, now 12:00
    assert eta_passed_advisory_due(est_order(flight_eta="11:58"), NOW) is None


def test_etapass_grace_boundary_is_inclusive():
    order = est_order(flight_eta="11:55")
    assert eta_passed_advisory_due(order, NOW) == "11:55"
    assert eta_passed_advisory_due(order, NOW - timedelta(seconds=1)) is None


def test_etapass_grace_constant_is_five_minutes():
    assert ETA_PASSED_GRACE == 300


def test_etapass_not_due_already_sent():
    assert eta_passed_advisory_due(est_order(reminders_sent="etapass"), NOW) is None


def test_etapass_dedupe_is_independent_of_other_tags():
    assert eta_passed_advisory_due(est_order(reminders_sent="svc,depart"), NOW) == "11:50"


def test_etapass_not_due_staleness_guard():
    # due = 09:05, now 12:00 → nearly 3h old, a restart must not back-fill it
    order = est_order(flight_eta="09:00", scheduled_time="2026-07-13 09:30:00")
    assert eta_passed_advisory_due(order, NOW) is None


def test_etapass_due_just_inside_staleness_guard():
    order = est_order(flight_eta="09:56", scheduled_time="2026-07-13 10:30:00")
    # due = 10:01, now 12:00 → 1h59m old
    assert eta_passed_advisory_due(order, NOW) == "09:56"


@pytest.mark.parametrize("status", ["landed", "gate", "cancelled", None])
def test_etapass_never_fires_outside_est(status):
    # Every other state either has its own push or is terminal.
    assert eta_passed_advisory_due(est_order(flight_status=status), NOW) is None


def test_etapass_not_due_songji():
    assert eta_passed_advisory_due(est_order(service_type="送机"), NOW) is None


@pytest.mark.parametrize("eta", [None, "", "?", "Est at 11:50"])
def test_etapass_not_due_unusable_eta(eta):
    assert eta_passed_advisory_due(est_order(flight_eta=eta), NOW) is None


def test_etapass_does_not_need_exit_minutes():
    # The advisory only says the plane is probably down; 用車 timing is not its job.
    assert eta_passed_advisory_due(est_order(passenger_exit_minutes=None), NOW) == "11:50"


def test_etapass_red_eye_eta_after_midnight():
    order = est_order(flight_eta="00:10", scheduled_time="2026-07-14 00:40:00")
    just_after = datetime(2026, 7, 14, 0, 16, 0)
    assert eta_passed_advisory_due(order, just_after) == "00:10"


# ---- pending_reminder_times: eta-passed advisory branch ----


def test_pending_etapass_future():
    order = est_order(flight_eta="12:10", scheduled_time="2026-07-13 12:30:00")
    # etapass due 12:15; svc needs landed/gate, depart (12:00) already past
    assert pending_reminder_times([order], NOW) == [datetime(2026, 7, 13, 12, 15, 0)]


def test_pending_etapass_past_not_included():
    order = est_order(flight_eta="11:50", scheduled_time="2026-07-13 12:20:00")
    assert pending_reminder_times([order], NOW) == []


def test_pending_etapass_sent_not_included():
    order = est_order(flight_eta="12:10", scheduled_time="2026-07-13 12:30:00",
                      reminders_sent="etapass")
    assert pending_reminder_times([order], NOW) == []


def test_pending_etapass_not_added_once_landed():
    order = est_order(flight_status="landed", flight_eta="12:10",
                      scheduled_time="2026-07-13 12:30:00")
    times = pending_reminder_times([order], NOW)
    assert datetime(2026, 7, 13, 12, 15, 0) not in times
    assert datetime(2026, 7, 13, 12, 40, 0) in times   # svc still wakes the poller


# ---- 接站 departure milestones ----


def test_jiezhan_dep30_fires(db_path):
    """Station pickup fires dep30 via get_departure_reminders + milestones."""
    save_order(db_path, make_order(
        order_id="Z1", service_type="接站",
        scheduled_time="2026-07-13 12:30:00",
        flight_number="",
        pickup="香港西九龙站(香港西九龙站)",
        dropoff="香港紫珀酒店(尖沙咀诺士佛台6号)",
    ), telegram_msg_id=1)
    orders = get_departure_reminders(db_path, NOW)
    assert len(orders) == 1
    assert orders[0]["order_id"] == "Z1"
    tags = departure_milestones_due(orders[0], NOW)
    assert "dep30" in tags


def test_jiezhan_dep10_fires():
    order = {
        "service_type": "接站",
        "scheduled_time": "2026-07-13 12:10:00",
        "reminders_sent": "",
    }
    assert "dep10" in departure_milestones_due(order, NOW)


def test_jiezhan_pending_dep_future():
    order = {
        "service_type": "接站",
        "scheduled_time": "2026-07-13 12:40:00",
        "reminders_sent": "",
    }
    times = pending_reminder_times([order], NOW)
    assert datetime(2026, 7, 13, 12, 10, 0) in times
    assert datetime(2026, 7, 13, 12, 30, 0) in times


# ---- eta-passed advisory push ----


@pytest.fixture
def bot_db(db_path, monkeypatch):
    monkeypatch.setattr(bot, "DB_PATH", db_path)
    monkeypatch.setattr(bot, "_parking_client", None)
    monkeypatch.setattr(bot, "_parking_client_built", True)
    return db_path


@pytest.fixture
def tg():
    b = MagicMock()
    b.send_message = AsyncMock()
    return b


def est_pickup(db_path, eta="11:50", order_id="TEST001"):
    save_order(db_path, make_order(order_id=order_id), telegram_msg_id=1)
    update_flight_info(db_path, order_id, "11:45", eta, None, "est")


def sent_text(tg) -> str:
    return tg.send_message.call_args.kwargs["text"]


def test_advisory_push_names_the_eta_and_flags_it_unconfirmed(bot_db, tg):
    est_pickup(bot_db)
    asyncio.run(bot._check_eta_passed_advisories(tg, 123, NOW))
    text = sent_text(tg)
    assert text.startswith("預計已落地 11:50（HKIA 未確認）")
    assert "航班: CX100" in text
    assert "用車: 12:20" in text
    assert get_orders_by_date(bot_db, "2026-07-13")[0]["reminders_sent"] == "etapass"


def test_advisory_push_sent_once(bot_db, tg):
    est_pickup(bot_db)
    asyncio.run(bot._check_eta_passed_advisories(tg, 123, NOW))
    asyncio.run(bot._check_eta_passed_advisories(tg, 123, NOW + timedelta(minutes=1)))
    assert tg.send_message.call_count == 1


def test_advisory_push_skipped_while_the_eta_is_still_ahead(bot_db, tg):
    est_pickup(bot_db, eta="11:58")
    asyncio.run(bot._check_eta_passed_advisories(tg, 123, NOW))
    tg.send_message.assert_not_called()
    assert get_orders_by_date(bot_db, "2026-07-13")[0]["reminders_sent"] == ""


def test_advisory_push_carries_allowance_line(bot_db, tg, monkeypatch):
    est_pickup(bot_db)
    monkeypatch.setattr(bot, "_parking_client", MagicMock())
    asyncio.run(bot._check_eta_passed_advisories(tg, 123, NOW))
    assert "停車場 免費可用" in sent_text(tg)


def test_advisory_push_has_no_allowance_line_when_parking_is_off(bot_db, tg):
    est_pickup(bot_db)
    asyncio.run(bot._check_eta_passed_advisories(tg, 123, NOW))
    assert "停車場" not in sent_text(tg)


def test_advisory_leaves_flight_status_untouched(bot_db, tg):
    est_pickup(bot_db)
    asyncio.run(bot._check_eta_passed_advisories(tg, 123, NOW))
    assert get_orders_by_date(bot_db, "2026-07-13")[0]["flight_status"] == "est"


def test_advisory_does_not_consume_the_svc_or_depart_tags(bot_db, tg):
    est_pickup(bot_db)
    asyncio.run(bot._check_eta_passed_advisories(tg, 123, NOW))
    row = get_orders_by_date(bot_db, "2026-07-13")[0]
    assert set(row["reminders_sent"].split(",")) == {"etapass"}


def test_landed_push_still_fires_after_the_advisory(bot_db, tg):
    est_pickup(bot_db)
    asyncio.run(bot._check_eta_passed_advisories(tg, 123, NOW))
    info = {"scheduled": "11:45", "eta": "11:50", "gate": None, "status": "landed", "hall": "A"}
    asyncio.run(bot._notify_status_change(tg, 123, "TEST001", info, "est", "landed"))
    texts = [c.kwargs["text"] for c in tg.send_message.call_args_list]
    assert len(texts) == 2
    assert texts[1].startswith("已降落 11:50")


def test_poll_and_notify_runs_the_advisory_check(bot_db, tg, monkeypatch):
    monkeypatch.setenv("NOTIFY_CHAT_ID", "123")
    spy = AsyncMock()
    monkeypatch.setattr(bot, "_check_eta_passed_advisories", spy)
    ctx = MagicMock()
    ctx.application.bot = tg
    asyncio.run(bot._poll_and_notify(ctx))
    spy.assert_awaited_once()
