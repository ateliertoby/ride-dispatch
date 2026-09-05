from ride_dispatch.service import (
    FLIGHT_PICKUP, FLIGHT_TYPES, STATION_TYPES, QUICK_TYPES, PLATFORMS,
    is_flight_pickup, is_station, needs_departure_reminder,
    anchor_end, label, platform_of, expected_of,
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


# ---- platform_of ----


def test_platform_didi():
    assert platform_of('滴滴') == 'didi'


def test_platform_uber():
    assert platform_of('Uber') == 'uber'


def test_platform_foodpanda():
    assert platform_of('foodpanda') == 'foodpanda'


def test_platform_ride_types():
    for st in ('接机', '送机', '接站', '单程接送', '未知类型', ''):
        assert platform_of(st) == 'ride'


def test_platform_values_are_declared():
    for st in ('滴滴', 'Uber', 'foodpanda', '接机', ''):
        assert platform_of(st) in PLATFORMS


# ---- expected_of ----


def order(**overrides) -> dict:
    base = dict(service_type='接机', price=500.0, banner_fee=40.0, tunnel_fee=30.0)
    base.update(overrides)
    return base


def test_expected_ride_adds_banner():
    assert expected_of(order()) == 540.0


def test_expected_ride_ignores_tunnel():
    """The 接送 platform pays the toll itself; only 舉牌 is claimed back."""
    assert expected_of(order(tunnel_fee=100.0)) == 540.0


def test_expected_didi_adds_tunnel():
    assert expected_of(order(service_type='滴滴')) == 530.0


def test_expected_uber_adds_tunnel():
    assert expected_of(order(service_type='Uber')) == 530.0


def test_expected_foodpanda_is_price_only():
    assert expected_of(order(service_type='foodpanda')) == 500.0


def test_expected_null_fees_count_as_zero():
    assert expected_of(order(banner_fee=None)) == 500.0
    assert expected_of(order(service_type='滴滴', tunnel_fee=None)) == 500.0


def test_expected_null_price_counts_as_zero():
    assert expected_of(order(price=None)) == 40.0


def test_expected_missing_columns():
    assert expected_of({'service_type': '接机'}) == 0.0


def test_expected_nets_a_penalty_on_every_platform():
    """A 判罰賠款 is money the platform takes back out of whatever it pays, so
    it comes off the fare and the reimbursed fee alike."""
    assert expected_of(order(penalty_fee=97.38)) == 442.62
    assert expected_of(order(service_type='滴滴', penalty_fee=30.0)) == 500.0
    assert expected_of(order(service_type='Uber', penalty_fee=30.0)) == 500.0
    assert expected_of(order(service_type='foodpanda', penalty_fee=100.0)) == 400.0


def test_expected_null_penalty_counts_as_zero():
    assert expected_of(order(penalty_fee=None)) == 540.0
    assert expected_of(order()) == 540.0


def test_expected_can_go_negative_when_the_penalty_exceeds_the_fare():
    """Nothing caps the fine at the fare; an order that cost more than it
    earned has to say so rather than clamp to zero."""
    assert expected_of(order(price=100.0, banner_fee=0.0, penalty_fee=250.0)) == -150.0


# orders.service_type is a nullable TEXT column, so every predicate has to
# take None the same way it takes an empty string.
def test_predicates_accept_none():
    assert is_flight_pickup(None) is False
    assert is_station(None) is False
    assert needs_departure_reminder(None) is False
    assert anchor_end(None) == ''
    assert label(None) == '單程'
