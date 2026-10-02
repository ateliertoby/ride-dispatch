"""split_month is pure, so every case here is a handful of dict literals."""
import pytest

from ride_dispatch.month_totals import split_month

NOW = "2026-10-22 12:00:00"
BUCKETS = ("received", "awaiting", "unsettled", "short")


def order(order_id, scheduled, price, settlement_id=None, unpaid=0, banner=0.0,
          paid_ahead=0.0, ahead_batch=None, service_type="接机"):
    return {"order_id": order_id, "scheduled_time": scheduled + ":00", "service_type": service_type,
            "price": price, "banner_fee": banner, "tunnel_fee": 0.0, "penalty_fee": None,
            "settlement_id": settlement_id, "unpaid": unpaid,
            "paid_ahead": paid_ahead, "ahead_batch": ahead_batch}


def batch(batch_id, members, confirmed, received):
    """A batch as the database derives it: state and outstanding from the money."""
    outstanding = round(confirmed - received, 2)
    state = "paid" if outstanding <= 0.005 else "partial" if received > 0.005 else "awaiting"
    return {"id": batch_id, "confirmed_amount": confirmed, "received": received,
            "outstanding": outstanding, "state": state, "orders": members}


def totals(fare=0.0, received=0.0, awaiting=0.0, unsettled=0.0, short=0.0):
    return {"fare": fare, "received": received, "awaiting": awaiting,
            "unsettled": unsettled, "short": short}


def in_month(orders, month):
    return [o for o in orders if o["scheduled_time"].startswith(month)]


def unsettled_only():
    return [order("A1", "2026-10-05 09:00", 500, banner=40), order("A2", "2026-10-06 09:00", 300.5)], {}


def awaiting_only():
    orders = [order("A1", "2026-10-05 09:00", 500, settlement_id=3),
              order("A2", "2026-10-06 09:00", 300.5, settlement_id=3)]
    return orders, {3: batch(3, orders, confirmed=800.5, received=0.0)}


def paid():
    orders = [order("A1", "2026-10-05 09:00", 500, settlement_id=3),
              order("A2", "2026-10-06 09:00", 300.5, settlement_id=3)]
    return orders, {3: batch(3, orders, confirmed=800.5, received=800.5)}


def partial_ticked():
    orders = [order("A1", "2026-10-05 09:00", 1000, settlement_id=7),
              order("A2", "2026-10-06 09:00", 80, settlement_id=7, unpaid=1),
              order("A3", "2026-10-07 09:00", 1594.5, settlement_id=7)]
    return orders, {7: batch(7, orders, confirmed=2674.5, received=2594.5)}


def partial_unticked():
    orders = [order("A1", "2026-10-05 09:00", 1000, settlement_id=7),
              order("A2", "2026-10-06 09:00", 1674.5, settlement_id=7)]
    return orders, {7: batch(7, orders, confirmed=2674.5, received=2594.5)}


def partial_ticks_fall_short_of_the_shortfall():
    """The platform priced the ticked leg at 100 and the system at 80, so the
    tick accounts for 80 of a shortfall of 100."""
    orders = [order("A1", "2026-10-05 09:00", 1000, settlement_id=7),
              order("A2", "2026-10-06 09:00", 80, settlement_id=7, unpaid=1)]
    return orders, {7: batch(7, orders, confirmed=1100, received=1000)}


def partial_ticks_exceed_the_shortfall():
    orders = [order("A1", "2026-10-05 09:00", 1000, settlement_id=7),
              order("A2", "2026-10-06 09:00", 80, settlement_id=7, unpaid=1)]
    return orders, {7: batch(7, orders, confirmed=1060, received=1000)}


def straddling_paid():
    orders = [order("S1", "2026-09-29 09:00", 400, settlement_id=4),
              order("S2", "2026-09-30 21:00", 250.25, settlement_id=4),
              order("O1", "2026-10-01 09:00", 600, settlement_id=4)]
    return orders, {4: batch(4, orders, confirmed=1250.25, received=1250.25)}


def straddling_short_ticked_in_the_earlier_month():
    orders = [order("S1", "2026-09-29 09:00", 400, settlement_id=4),
              order("S2", "2026-09-30 21:00", 250.25, settlement_id=4, unpaid=1),
              order("O1", "2026-10-01 09:00", 600, settlement_id=4)]
    return orders, {4: batch(4, orders, confirmed=1250.25, received=1000)}


def straddling_short_unticked():
    orders = [order("S1", "2026-09-29 09:00", 400, settlement_id=4),
              order("S2", "2026-09-30 21:00", 250.25, settlement_id=4),
              order("O1", "2026-10-01 09:00", 600, settlement_id=4)]
    return orders, {4: batch(4, orders, confirmed=1250.25, received=1000)}


def with_a_future_order():
    return [order("A1", "2026-10-05 09:00", 500),
            order("F1", "2026-10-22 12:00", 900),
            order("F2", "2026-10-28 09:00", 700)], {}


def paid_ahead_on_an_awaiting_batch():
    """B1's 舉牌 rode on batch 5, which is still waiting; its trip is on batch 6,
    which is paid."""
    carrier = order("A1", "2026-10-02 09:00", 210, settlement_id=5)
    held = order("B1", "2026-10-03 22:00", 300, settlement_id=6, banner=40,
                 paid_ahead=40.0, ahead_batch=5)
    return [carrier, held], {5: batch(5, [carrier], confirmed=250, received=0.0),
                             6: batch(6, [held], confirmed=300, received=300)}


def paid_ahead_trip_still_unsettled():
    carrier = order("A1", "2026-10-02 09:00", 210, settlement_id=5)
    held = order("B1", "2026-10-03 22:00", 300, banner=40, paid_ahead=40.0, ahead_batch=5)
    return [carrier, held], {5: batch(5, [carrier], confirmed=250, received=250)}


def paid_ahead_on_a_short_batch():
    """The line paid ahead is not a leg of the batch that carried it, so it
    cannot be one of that batch's ticked legs: what arrived covers it."""
    carrier = order("A1", "2026-10-02 09:00", 210, settlement_id=5)
    other = order("A2", "2026-10-02 12:00", 100, settlement_id=5, unpaid=1)
    held = order("B1", "2026-10-03 22:00", 300, banner=40, paid_ahead=40.0, ahead_batch=5)
    return [carrier, other, held], {5: batch(5, [carrier, other], confirmed=350, received=250)}


def test_unsettled_only():
    assert split_month(*unsettled_only(), now=NOW) == totals(fare=840.5, unsettled=840.5)


def test_awaiting_only():
    assert split_month(*awaiting_only(), now=NOW) == totals(fare=800.5, awaiting=800.5)


def test_paid():
    assert split_month(*paid(), now=NOW) == totals(fare=800.5, received=800.5)


def test_partial_with_a_ticked_leg_is_short_by_that_leg():
    assert split_month(*partial_ticked(), now=NOW) == totals(fare=2674.5, received=2594.5, short=80.0)


def test_partial_without_ticks_moves_the_shortfall_out_of_received():
    assert split_month(*partial_unticked(), now=NOW) == totals(fare=2674.5, received=2594.5, short=80.0)


def test_ticks_that_account_for_less_than_the_shortfall_leave_the_rest_short_too():
    t = split_month(*partial_ticks_fall_short_of_the_shortfall(), now=NOW)
    assert t == totals(fare=1080.0, received=980.0, short=100.0)


def test_ticks_that_account_for_more_than_the_shortfall_give_the_excess_back():
    t = split_month(*partial_ticks_exceed_the_shortfall(), now=NOW)
    assert t == totals(fare=1080.0, received=1020.0, short=60.0)


def test_a_straddling_batch_puts_in_each_month_only_its_own_orders():
    orders, batches = straddling_paid()
    assert split_month(in_month(orders, "2026-09"), batches, now=NOW) == totals(fare=650.25, received=650.25)
    assert split_month(in_month(orders, "2026-10"), batches, now=NOW) == totals(fare=600.0, received=600.0)


def test_a_straddling_short_batch_is_short_in_the_month_of_its_ticked_leg():
    orders, batches = straddling_short_ticked_in_the_earlier_month()
    sep = split_month(in_month(orders, "2026-09"), batches, now=NOW)
    octo = split_month(in_month(orders, "2026-10"), batches, now=NOW)
    assert sep == totals(fare=650.25, received=400.0, short=250.25)
    assert octo == totals(fare=600.0, received=600.0)


def test_a_straddling_short_batch_without_ticks_is_short_in_its_last_month():
    orders, batches = straddling_short_unticked()
    sep = split_month(in_month(orders, "2026-09"), batches, now=NOW)
    octo = split_month(in_month(orders, "2026-10"), batches, now=NOW)
    assert sep == totals(fare=650.25, received=650.25)
    assert octo == totals(fare=600.0, received=349.75, short=250.25)
    assert round(sep["short"] + octo["short"], 2) == batches[4]["outstanding"]


def test_a_future_order_is_counted_nowhere():
    """An order due exactly now has not been driven either."""
    assert split_month(*with_a_future_order(), now=NOW) == totals(fare=500.0, unsettled=500.0)


def test_the_part_paid_ahead_follows_the_batch_that_carried_it():
    t = split_month(*paid_ahead_on_an_awaiting_batch(), now=NOW)
    assert t == totals(fare=550.0, received=300.0, awaiting=250.0)


def test_a_trip_on_no_batch_is_unsettled_net_of_what_was_paid_ahead():
    t = split_month(*paid_ahead_trip_still_unsettled(), now=NOW)
    assert t == totals(fare=550.0, received=250.0, unsettled=300.0)


def test_the_part_paid_ahead_on_a_short_batch_counts_as_arrived():
    t = split_month(*paid_ahead_on_a_short_batch(), now=NOW)
    assert t == totals(fare=650.0, received=250.0, unsettled=300.0, short=100.0)


def test_other_platforms_reimburse_their_own_fee():
    """The value of an order is expected_of, which differs by platform."""
    didi = order("D1", "2026-10-05 09:00", 200, service_type="滴滴")
    didi["tunnel_fee"] = 50.0
    assert split_month([didi], {}, now=NOW) == totals(fare=250.0, unsettled=250.0)


def test_a_penalty_comes_off_the_fare():
    fined = order("A1", "2026-10-05 09:00", 280)
    fined["penalty_fee"] = 97.38
    assert split_month([fined], {}, now=NOW) == totals(fare=182.62, unsettled=182.62)


def test_an_empty_month_is_all_zero():
    assert split_month([], {}, now=NOW) == totals()


CASES = [unsettled_only, awaiting_only, paid, partial_ticked, partial_unticked,
         partial_ticks_fall_short_of_the_shortfall, partial_ticks_exceed_the_shortfall,
         straddling_paid, straddling_short_ticked_in_the_earlier_month, straddling_short_unticked,
         with_a_future_order, paid_ahead_on_an_awaiting_batch, paid_ahead_trip_still_unsettled,
         paid_ahead_on_a_short_batch]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__)
def test_the_fare_is_the_sum_of_the_four_buckets_and_none_is_negative(case):
    orders, batches = case()
    whole = {key: 0.0 for key in BUCKETS}
    for month in sorted({o["scheduled_time"][:7] for o in orders}):
        t = split_month(in_month(orders, month), batches, now=NOW)
        assert round(t["fare"] * 100) == sum(round(t[key] * 100) for key in BUCKETS), month
        assert all(t[key] >= 0 for key in t), month
        for key in BUCKETS:
            whole[key] += t[key]
    # Over all its months a short batch is short by exactly what it is owed.
    owed = sum(b["outstanding"] for b in batches.values() if b["state"] == "partial")
    assert round(whole["short"], 2) == round(owed, 2)


def test_a_shortfall_larger_than_the_month_received_is_refused():
    """The batch carries a line worth more than its legs and was paid less than
    that line: no split of the legs' fares can show the shortfall, and a figure
    below zero would be a wrong answer shown as a right one."""
    orders = [order("A1", "2026-10-05 09:00", 100, settlement_id=7)]
    batches = {7: batch(7, orders, confirmed=1100, received=50)}
    with pytest.raises(ValueError, match="7"):
        split_month(orders, batches, now=NOW)


def test_an_excess_larger_than_the_month_short_is_refused():
    """The ticked leg is in the earlier month and worth more than the shortfall:
    the excess would be taken from a month that is not short at all."""
    orders = [order("S1", "2026-09-30 09:00", 300, settlement_id=4, unpaid=1),
              order("O1", "2026-10-01 09:00", 600, settlement_id=4)]
    batches = {4: batch(4, orders, confirmed=900, received=700)}
    assert split_month(in_month(orders, "2026-09"), batches, now=NOW) == totals(
        fare=300.0, short=300.0)
    with pytest.raises(ValueError, match="4"):
        split_month(in_month(orders, "2026-10"), batches, now=NOW)


def test_a_part_paid_ahead_whose_batch_is_missing_is_refused():
    held = order("B1", "2026-10-03 22:00", 300, banner=40, paid_ahead=40.0, ahead_batch=5)
    with pytest.raises(ValueError, match="B1"):
        split_month([held], {}, now=NOW)
