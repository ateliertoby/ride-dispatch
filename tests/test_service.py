from ride_dispatch.service import (
    FLIGHT_PICKUP, FLIGHT_TYPES, STATION_TYPES, QUICK_TYPES,
    is_flight_pickup, is_station, needs_departure_reminder,
    anchor_end, label,
)


# ---- is_flight_pickup ----


def test_flight_pickup_jieji():
    assert is_flight_pickup('接机') is True


def test_flight_pickup_songji():
    assert is_flight_pickup('送机') is False


def test_flight_pickup_jiezhan():
    assert is_flight_pickup('接站') is False


def test_flight_pickup_dancheng():
    assert is_flight_pickup('单程接送') is False


def test_flight_pickup_empty():
    assert is_flight_pickup('') is False


# ---- is_station ----


def test_station_jiezhan():
    assert is_station('接站') is True


def test_station_unreceived_type_not_classified():
    """Only service types actually received from a platform are classified.

    A station-sounding type that has never arrived stays unclassified so it
    surfaces as an unhandled order rather than being routed on a guess.
    """
    assert is_station('送站') is False
    assert is_station('高铁送站') is False


def test_station_jieji_not_station():
    assert is_station('接机') is False


def test_station_songji_not_station():
    assert is_station('送机') is False


def test_station_dancheng_not_station():
    assert is_station('单程接送') is False


def test_station_quick_types_never_station():
    for qt in QUICK_TYPES:
        assert is_station(qt) is False


def test_station_empty():
    assert is_station('') is False


# ---- needs_departure_reminder ----


def test_departure_reminder_songji():
    assert needs_departure_reminder('送机') is True


def test_departure_reminder_dancheng():
    assert needs_departure_reminder('单程接送') is True


def test_departure_reminder_jiezhan():
    assert needs_departure_reminder('接站') is True


def test_departure_reminder_unreceived_type():
    assert needs_departure_reminder('送站') is False
    assert needs_departure_reminder('高铁送站') is False


def test_departure_reminder_jieji():
    assert needs_departure_reminder('接机') is False


def test_departure_reminder_quick_types():
    for qt in QUICK_TYPES:
        assert needs_departure_reminder(qt) is False


def test_departure_reminder_empty():
    assert needs_departure_reminder('') is False


# ---- anchor_end ----


def test_anchor_end_jieji():
    assert anchor_end('接机') == 'dropoff'


def test_anchor_end_jiezhan():
    assert anchor_end('接站') == 'dropoff'


def test_anchor_end_songji():
    assert anchor_end('送机') == 'pickup'


def test_anchor_end_unreceived_type():
    assert anchor_end('送站') == ''


def test_anchor_end_dancheng():
    assert anchor_end('单程接送') == ''


def test_anchor_end_empty():
    assert anchor_end('') == ''


def test_anchor_end_quick():
    for qt in QUICK_TYPES:
        assert anchor_end(qt) == ''


# ---- label ----


def test_label_jieji():
    assert label('接机') == '接機'


def test_label_songji():
    assert label('送机') == '送機'


def test_label_jiezhan():
    assert label('接站') == '接站'


def test_label_unreceived_type():
    assert label('送站') == '單程'


def test_label_quick_passthrough():
    for qt in QUICK_TYPES:
        assert label(qt) == qt


def test_label_dancheng():
    assert label('单程接送') == '單程'


def test_label_unknown():
    assert label('未知类型') == '單程'


def test_label_unreceived_station_variant():
    assert label('高铁送站') == '單程'


# orders.service_type is a nullable TEXT column, so every predicate has to
# take None the same way it takes an empty string.
def test_predicates_accept_none():
    assert is_flight_pickup(None) is False
    assert is_station(None) is False
    assert needs_departure_reminder(None) is False
    assert anchor_end(None) == ''
    assert label(None) == '單程'
