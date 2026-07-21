from ride_dispatch.bot import format_card, _order_lines
from ride_dispatch.parser import Order


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


# ---- format_card exit line ----


def test_card_urgent_exit_warns_pre_landing_departure():
    card = format_card(make_order(passenger_exit_minutes=20))
    assert "出場: 20分鐘 — 降落前要出發" in card


def test_card_tight_exit_warns_immediate_departure():
    card = format_card(make_order(passenger_exit_minutes=30))
    assert "出場: 30分鐘 — 降落即刻出發" in card


def test_card_long_exit_plain():
    card = format_card(make_order(passenger_exit_minutes=60))
    assert "出場: 60分鐘" in card
    assert "出發" not in card


def test_card_no_exit_line_when_missing():
    assert "出場" not in format_card(make_order(passenger_exit_minutes=None))


def test_card_no_exit_line_for_songji():
    assert "出場" not in format_card(make_order(service_type="送机", passenger_exit_minutes=30))


def test_card_exit_line_sits_after_time_line():
    lines = format_card(make_order(passenger_exit_minutes=20)).split("\n")
    assert lines[2].startswith("時間:")
    assert lines[3].startswith("出場:")
    assert lines[4].startswith("上車:")


# ---- _order_lines exit line ----


def _pickup_dict(exit_minutes):
    return {
        "service_type": "接机",
        "flight_number": "CX100",
        "passenger_name": "TEST/USER",
        "passenger_phone": "",
        "overseas_phone": "",
        "passenger_exit_minutes": exit_minutes,
        "additional_services": "",
        "dropoff": "尖沙咀",
    }


def test_order_lines_include_exit_minutes():
    lines = _order_lines(_pickup_dict(30), "12:00")
    assert "\n用車: 12:30" in lines
    assert "\n出場: 30分鐘" in lines


def test_order_lines_no_exit_when_missing():
    assert "出場" not in _order_lines(_pickup_dict(None), "12:00")
