from ride_dispatch.bot import format_card, _order_lines, collect_contact_lines
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


# ---- collect_contact_lines / _order_lines contact rendering ----


def _contact_dict(**overrides):
    base = {
        "passenger_phone": "",
        "overseas_phone": "",
        "third_party_contact": "",
        "more_contacts": "",
    }
    base.update(overrides)
    return base


def test_third_party_whatsapp_label_and_e164():
    d = _contact_dict(third_party_contact="【WhatsApp】+39 3330000111")
    lines = collect_contact_lines(d)
    assert len(lines) == 1
    assert lines[0] == ("WhatsApp", "+393330000111")


def test_more_contacts_label():
    d = _contact_dict(more_contacts="65 90000001")
    lines = collect_contact_lines(d)
    assert len(lines) == 1
    assert lines[0] == ("更多", "+6590000001")


def test_dedupe_overseas_and_third_party_same_number():
    d = _contact_dict(
        overseas_phone="86 13800001111",
        third_party_contact="【WhatsApp】+86 13800001111",
    )
    lines = collect_contact_lines(d)
    assert len(lines) == 1
    assert lines[0][0] == "境外"


def test_third_party_no_bracket_renders_raw():
    d = _contact_dict(third_party_contact="some-contact-info 12345")
    lines = collect_contact_lines(d)
    assert len(lines) == 1
    assert lines[0] == ("聯絡", "some-contact-info 12345")


def test_all_four_distinct_shows_four_lines_in_order():
    d = _contact_dict(
        passenger_phone="86 13800001111",
        overseas_phone="+39 3330000111",
        third_party_contact="【WhatsApp】+65 90000001",
        more_contacts="852 91000002",
    )
    lines = collect_contact_lines(d)
    assert len(lines) == 4
    assert lines[0][0] == "電話"
    assert lines[1][0] == "境外"
    assert lines[2][0] == "WhatsApp"
    assert lines[3][0] == "更多"


def test_order_lines_renders_all_contacts():
    d = {
        "service_type": "接机",
        "flight_number": "CX100",
        "passenger_name": "TEST/USER",
        "passenger_phone": "86 13800001111",
        "overseas_phone": "+39 3330000111",
        "third_party_contact": "【WhatsApp】+65 90000001",
        "more_contacts": "852 91000002",
        "passenger_exit_minutes": None,
        "additional_services": "",
        "dropoff": "尖沙咀",
    }
    text = _order_lines(d)
    assert "\n電話: +8613800001111" in text
    assert "\n境外: +393330000111" in text
    assert "\nWhatsApp: +6590000001" in text
    assert "\n更多: +85291000002" in text
