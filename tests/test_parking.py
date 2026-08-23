from datetime import datetime, timedelta

import pytest

from ride_dispatch.parking import (
    ParkingError, ParkingStatus, parse_status, api_time, from_api_time,
    db_time, from_db_time, free_available, next_free_at, pay_plan, classify,
    arming_orders, is_armed, pick_order, FREE_MINUTES,
)
from ride_dispatch.flight import landing_datetime

# Real replies captured 2026-08-23.
INSIDE_UNPAID = {
    "requestDateTime": "202608231855", "resultCode": 200, "count": 1, "resultMessage": "",
    "infoList": [{"alreadyPaid": 0, "parkingLocation": "P4O", "parkingName": "Car Park 4",
                  "carPlateNo": "YY3953", "entryTime": "202608231848", "parkTime": 6,
                  "timestamp": "2026-08-23 18:54", "pvNr": 212022,
                  "scheduledExit": "2026-08-23 18:54", "fee": 0, "count": 0,
                  "breakdownList": [], "disable": 0}],
}
INSIDE_PAID = {
    "requestDateTime": "202608231903", "resultCode": 200, "count": 1, "resultMessage": "",
    "infoList": [{"alreadyPaid": 1, "parkingLocation": "P4O", "parkingName": "Car Park 4",
                  "carPlateNo": "YY3953", "entryTime": "202608231848", "parkTime": 15,
                  "timestamp": "2026-08-23 19:03", "pvNr": 212022,
                  "scheduledExit": "2026-08-23 19:03", "fee": 0, "count": 0,
                  "breakdownList": [], "disable": 0}],
}
FEE_FOR_EXIT = {
    "requestDateTime": "202608231859", "resultCode": 200, "count": 1, "resultMessage": "",
    "infoList": [{"alreadyPaid": 0, "parkingLocation": "P4O", "parkingName": "Car Park 4",
                  "carPlateNo": "YY3953", "entryTime": "202608231848", "parkTime": 11,
                  "timestamp": "2026-08-23 18:59", "pvNr": 212022,
                  "scheduledExit": "2026-08-23 19:48", "fee": 32, "count": 1,
                  "breakdownList": [{"unitDescription": "Hourly", "unitPrice": 32,
                                     "quantity": 1, "unitAmount": 32}], "disable": 0}],
}
NOT_INSIDE = {"requestDateTime": "202608231343", "resultCode": 401, "infoList": [],
              "resultMessage": "Record Error Found", "count": 0}

NOW = datetime(2026, 8, 23, 19, 0)


def test_parse_inside_unpaid():
    s = parse_status(INSIDE_UNPAID)
    assert s.inside is True
    assert s.pv_nr == 212022
    assert s.location == "P4O" and s.location_name == "Car Park 4"
    assert s.entry_time == "2026-08-23 18:48"
    assert s.park_minutes == 6
    assert s.paid is False
    assert s.fee == 0


def test_parse_inside_paid_and_fee_reply():
    assert parse_status(INSIDE_PAID).paid is True
    fee = parse_status(FEE_FOR_EXIT)
    assert fee.fee == 32 and fee.scheduled_exit == "2026-08-23 19:48"


def test_parse_not_inside_is_a_status_not_an_error():
    s = parse_status(NOT_INSIDE)
    assert s.inside is False and s.pv_nr is None


@pytest.mark.parametrize("body", [{}, {"resultCode": 500}, {"resultCode": 200, "infoList": []}, "junk"])
def test_parse_unknown_shapes_raise(body):
    with pytest.raises(ParkingError):
        parse_status(body)


def test_time_conversions_round_trip():
    dt = datetime(2026, 8, 23, 18, 48)
    assert api_time(dt) == "202608231848"
    assert from_api_time("202608231848") == dt
    assert db_time(dt) == "2026-08-23 18:48"
    assert from_db_time("2026-08-23 18:48") == dt


def test_free_available_rolling_24h():
    assert free_available([], NOW)
    assert not free_available([NOW - timedelta(hours=23, minutes=59)], NOW)
    assert free_available([NOW - timedelta(hours=24)], NOW)          # exactly 24h: available again
    assert free_available([NOW - timedelta(hours=30)], NOW)


def test_next_free_at_is_latest_free_entry_plus_24h():
    assert next_free_at([]) is None
    a = NOW - timedelta(hours=30)
    b = NOW - timedelta(hours=5)
    assert next_free_at([a, b]) == b + timedelta(hours=24)


@pytest.mark.parametrize("minute,hours", [(10, 1), (50, 1), (60, 1), (61, 2), (70, 2), (125, 3)])
def test_pay_plan_rounds_elapsed_time_up_to_whole_hours(minute, hours):
    entry = datetime(2026, 8, 23, 18, 48)
    h, exit_at = pay_plan(entry, entry + timedelta(minutes=minute))
    assert h == hours
    assert exit_at == entry + timedelta(hours=hours)


def test_classify():
    entry = datetime(2026, 8, 23, 18, 48)
    assert classify(False, entry, entry + timedelta(minutes=FREE_MINUTES)) == "free"
    assert classify(False, entry, entry + timedelta(minutes=FREE_MINUTES + 1)) == "gate"
    assert classify(True, entry, entry + timedelta(minutes=10)) == "paid"
    assert classify(True, entry, entry + timedelta(minutes=90)) == "paid"


def order(**kw):
    base = {"order_id": "O1", "service_type": "接机", "flight_number": "CA727",
            "status": "active", "scheduled_time": "2026-08-23 19:30:00",
            "passenger_exit_minutes": 30, "flight_eta": "19:00", "flight_scheduled": "18:50",
            "flight_status": "est"}
    base.update(kw)
    return base


def test_landing_datetime_prefers_eta_then_scheduled_then_derived():
    assert landing_datetime(order()) == datetime(2026, 8, 23, 19, 0)
    assert landing_datetime(order(flight_eta=None)) == datetime(2026, 8, 23, 18, 50)
    assert landing_datetime(order(flight_eta=None, flight_scheduled=None)) == datetime(2026, 8, 23, 19, 0)
    assert landing_datetime(order(flight_eta=None, flight_scheduled=None, passenger_exit_minutes=None)) is None


def test_is_armed_by_status_or_window():
    landing = datetime(2026, 8, 23, 19, 0)
    assert is_armed([order(flight_status="landed")], landing - timedelta(hours=1))
    assert is_armed([order(flight_status="gate")], landing - timedelta(hours=1))
    assert is_armed([order()], landing - timedelta(minutes=30))
    assert not is_armed([order()], landing - timedelta(minutes=31))
    assert is_armed([order()], landing + timedelta(hours=2))
    assert not is_armed([order()], landing + timedelta(hours=2, minutes=1))
    assert not is_armed([order(flight_status="landed")], landing + timedelta(hours=2, minutes=1))


def test_is_armed_ignores_non_pickups_cancelled_and_no_flight():
    landing = datetime(2026, 8, 23, 19, 0)
    assert not is_armed([order(service_type="送机")], landing)
    assert not is_armed([order(status="cancelled")], landing)
    assert not is_armed([order(flight_number="")], landing)
    assert not is_armed([], landing)


def test_pick_order_closest_landing_to_entry():
    a = order(order_id="A", flight_eta="18:30")
    b = order(order_id="B", flight_eta="19:10")
    assert pick_order([a, b], datetime(2026, 8, 23, 19, 5))["order_id"] == "B"
    assert pick_order([], datetime(2026, 8, 23, 19, 5)) is None
