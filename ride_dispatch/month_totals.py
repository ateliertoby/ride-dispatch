"""Where one month's fares have got to.

Pure: no database and no clock.  The settle page's header and foot are built
on these figures, and they are computed order by order so that

    fare == received + awaiting + unsettled + short

holds by construction rather than by two queries happening to agree.
"""

from .service import expected_of, owed_of

CENT = 0.005


def _state_bucket(state: str) -> str:
    # What arrived on a statement paid short is received; the legs it did not
    # pay are named one by one and handled by the caller.
    return "awaiting" if state == "awaiting" else "received"


def _remainder(batch: dict) -> float:
    """What a statement paid short is still owed beyond its ticked legs.

    Zero when the ticks account for the shortfall.  Positive when no leg, or
    not enough of them, was ticked; negative when the ticked legs are worth
    more to the system than the platform left unpaid.
    """
    ticked = sum(owed_of(o) for o in batch["orders"] if o.get("unpaid"))
    return batch["outstanding"] - ticked


def split_month(orders: list[dict], batches: dict[int, dict], now: str) -> dict:
    """Split one month's orders into fare, received, awaiting, unsettled, short.

    `orders` are the active orders of one platform scheduled in one month, as
    the settle columns select them.  `batches` maps a batch id to the derived
    batch (state, outstanding, and all of its member orders whichever month
    they fall in) for every batch an order here is on or was paid ahead by.
    `now` is "YYYY-MM-DD HH:MM:SS"; an order not yet driven is counted nowhere,
    because it may still be cancelled.

    An order is worth expected_of.  The part other batches paid ahead of its
    trip takes the state of the batch that carried that line, and the rest
    takes the state of the order's own batch: none is unsettled, awaiting is
    awaiting, paid is received, and on a statement paid short a leg ticked as
    unpaid is short and every other leg is received.

    A statement paid short has to be short by exactly what it is still owed,
    over all the months it covers.  Its ticked legs need not add up to that:
    nothing may be ticked yet, and the ticks are accepted in the platform's
    figures, which can differ from the system's.  The difference is moved
    between received and short in the one month that holds the statement's
    latest order, so a statement straddling two months is never corrected
    twice.

    Raises ValueError when that move would leave the month's received or short
    below zero.  The statement's figure and its orders' fares then disagree by
    more than the month can absorb, and clamping or spreading the difference
    would show a made-up figure as an exact one.
    """
    sums = {"fare": 0.0, "received": 0.0, "awaiting": 0.0, "unsettled": 0.0, "short": 0.0}
    counted = set()
    for order in orders:
        if (order.get("scheduled_time") or "") >= now:
            continue
        counted.add(order["order_id"])
        sums["fare"] += expected_of(order)

        ahead = order.get("paid_ahead") or 0
        if ahead:
            carrier = batches.get(order.get("ahead_batch"))
            if carrier is None:
                raise ValueError(f"{order['order_id']}: the batch that paid ahead of it was not given")
            # A line paid ahead is not a leg of the batch that carried it, so
            # it can never be one of that batch's ticked legs.
            sums[_state_bucket(carrier["state"])] += ahead

        own = owed_of(order)
        batch_id = order.get("settlement_id")
        if batch_id is None:
            sums["unsettled"] += own
            continue
        batch = batches.get(batch_id)
        if batch is None:
            raise ValueError(f"{order['order_id']}: batch {batch_id} was not given")
        if batch["state"] == "partial" and order.get("unpaid"):
            sums["short"] += own
        else:
            sums[_state_bucket(batch["state"])] += own

    moved = []
    for batch_id in sorted(batches):
        batch = batches[batch_id]
        if batch["state"] != "partial" or not batch["orders"]:
            continue
        latest = max(batch["orders"], key=lambda o: o.get("scheduled_time") or "")
        if latest["order_id"] not in counted:
            continue
        move = _remainder(batch)
        if abs(move) > CENT:
            sums["received"] -= move
            sums["short"] += move
            moved.append(batch_id)
    if moved and (sums["received"] < -CENT or sums["short"] < -CENT):
        raise ValueError(
            f"batch {', '.join(map(str, moved))}: the shortfall cannot be split over "
            "the orders' fares without a figure below zero")

    # Rounded once, here: every part is a whole number of cents, so each sum is
    # within float noise of one and the identity survives the rounding.  Adding
    # 0.0 turns a negative zero into a plain one.
    return {key: round(value, 2) + 0.0 for key, value in sums.items()}
