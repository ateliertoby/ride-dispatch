import json
from datetime import datetime

import pytest

from ride_dispatch import credits
from ride_dispatch.credits import (
    Match, anchor, in_window, match_credit, match_batch, ingest_feed, feed_changed, offer,
    propose_credit, propose_batch, propose_statement,
)
from ride_dispatch.db import (
    init_db, save_order, update_price, create_settlement, get_settlement, get_credit,
    insert_credit, allocate, unallocated_credits, open_batches,
)
from ride_dispatch.parser import Order
from ride_dispatch.service import expected_of

NOW = datetime(2026, 8, 27, 12, 0)


def make_order(order_id, scheduled, service_type="送机"):
    return Order(
        order_id=order_id, service_type=service_type, vehicle_type="经济5座", passenger_name="TEST/USER",
        scheduled_time=scheduled, passenger_phone="86 13800000000", overseas_phone="", flight_number="",
        pickup="尖沙咀", dropoff="香港国际机场 T1", distance_km=30, notes="", driver_notes="",
        additional_services="", passenger_exit_minutes=None, third_party_contact="",
        more_contacts="", raw_message="raw",
    )


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "orders.db")
    init_db(path)
    return path


def seed(db_path, order_id, scheduled, price=210.0, **kw):
    save_order(db_path, make_order(order_id, scheduled, **kw), telegram_msg_id=1, parking=0.0, source="携程")
    update_price(db_path, order_id, price)


def line(ref, amount, value_date, **over):
    d = {"v": 1, "feed": "ride-dispatch", "ref": ref, "platform": "ride", "amount": amount,
         "currency": "HKD", "value_date": value_date, "payer": "A B**** C***** L",
         "memo": "SUPPLIERPAY", "email_id": "m", "received_at": "2026-08-27T00:00:54Z",
         "recorded_at": "2026-08-27T00:15:03Z"}
    d.update(over)
    return json.dumps(d)


def write_feed(tmp_path, *lines, tail=""):
    p = tmp_path / "ride-dispatch.jsonl"
    p.write_text("".join(l + "\n" for l in lines) + tail, encoding="utf-8")
    return str(p)


# ---- feed ingestion ----

def test_ingest_inserts_valid_lines_once_and_skips_junk(db_path, tmp_path, caplog):
    path = write_feed(tmp_path, line("R1", 2540.0, "2026-08-26"), "not json",
                      line("R2", 1.0, "2026-08-26", v=2),
                      line("R3", 1.0, "2026-08-26", platform="mars"),
                      json.dumps({"v": 1, "ref": "R4"}),
                      line("R5", 930.0, "2026-08-24"),
                      tail=line("R6", 1.0, "2026-08-26"))
    with caplog.at_level("WARNING", logger="credits"):
        new = ingest_feed(db_path, path)
    assert [c["ref"] for c in new] == ["R1", "R5"]
    assert ingest_feed(db_path, path) == []
    assert get_credit(db_path, 1)["remaining"] == 2540.0
    assert "mars" in caplog.text


def test_ingest_picks_up_a_fragment_once_it_is_complete(db_path, tmp_path):
    path = write_feed(tmp_path, line("R1", 100.0, "2026-08-26"), tail=line("R2", 200.0, "2026-08-26"))
    assert [c["ref"] for c in ingest_feed(db_path, path)] == ["R1"]
    write_feed(tmp_path, line("R1", 100.0, "2026-08-26"), line("R2", 200.0, "2026-08-26"))
    assert [c["ref"] for c in ingest_feed(db_path, path)] == ["R2"]


def test_ingest_keeps_the_feed_fields(db_path, tmp_path):
    path = write_feed(tmp_path, line("R1", 2540.0, "2026-08-26"))
    c = ingest_feed(db_path, path)[0]
    assert c["payer"] == "A B**** C***** L" and c["memo"] == "SUPPLIERPAY"
    assert c["currency"] == "HKD" and c["value_date"] == "2026-08-26"
    assert c["received_at"] == "2026-08-27T00:00:54Z"
    assert c["remaining"] == 2540.0


def test_feed_changed_tracks_mtime_and_size(tmp_path):
    p = tmp_path / "f.jsonl"
    s = str(p)
    credits._feed_seen.clear()
    credits._feed_missing_logged.clear()
    assert feed_changed(s) is False
    p.write_text("a\n", encoding="utf-8")
    assert feed_changed(s) is True and feed_changed(s) is False
    with open(s, "a", encoding="utf-8") as f:
        f.write("b\n")
    assert feed_changed(s) is True


def test_feed_missing_is_logged_once_per_disappearance(tmp_path, caplog):
    s = str(tmp_path / "gone.jsonl")
    credits._feed_seen.clear()
    credits._feed_missing_logged.clear()
    with caplog.at_level("WARNING", logger="credits"):
        assert feed_changed(s) is False
        assert feed_changed(s) is False
    assert caplog.text.count("not readable") == 1


# ---- the matcher ----

def B(id, amount, dates, settle_dates=None, platform="ride", received=0.0):
    stmt = {"days": [{"date": d, "rows": [{"order_id": "x", "amount": amount, "settle_date": sd}]}
                     for d, sd in zip(dates, settle_dates)]} if settle_dates else None
    return {"id": id, "platform": platform, "confirmed_amount": amount, "statement": stmt,
            "received": received, "outstanding": round(amount - received, 2),
            "orders": [{"scheduled_time": f"{d} 09:00:00"} for d in dates]}


def C(id, amount, value_date, remaining=None, platform="ride"):
    remaining = amount if remaining is None else remaining
    return {"id": id, "platform": platform, "amount": amount, "value_date": value_date,
            "allocated": round(amount - remaining, 2), "remaining": remaining}


def test_anchor_prefers_statement_settle_date():
    assert anchor(B(1, 100, ["2026-08-01"], ["2026-08-07"])) == "2026-08-07"
    assert anchor(B(1, 100, ["2026-08-01", "2026-08-03"])) == "2026-08-03"
    assert in_window("2026-08-19", "2026-08-26") and in_window("2026-08-26", "2026-08-26")
    assert not in_window("2026-08-18", "2026-08-26") and not in_window("2026-08-27", "2026-08-26")


def test_match_credit_exact_single():
    m = match_credit(C(1, 2540.0, "2026-08-26"),
                     [B(4, 2540.0, ["2026-08-23", "2026-08-24"]), B(3, 1450.0, ["2026-08-20"])])
    assert m.reason == "exact" and m.exact == [4]
    # The proposal leads, the alternatives stay reachable: the operator taps.
    assert [b["id"] for b in offer(m, [B(4, 2540.0, ["2026-08-23"]), B(3, 1450.0, ["2026-08-20"])])] == [4, 3]


def test_match_credit_exact_several_prefers_window_then_ambiguous():
    old = B(1, 930.0, ["2026-07-01"])
    new = B(2, 930.0, ["2026-08-22"])
    assert match_credit(C(1, 930.0, "2026-08-24"), [old, new]).exact == [2]
    m = match_credit(C(1, 930.0, "2026-08-24"), [new, B(3, 930.0, ["2026-08-21"])])
    assert m.reason == "ambiguous" and set(m.exact) == {2, 3}


def test_match_credit_subset_unique_and_ambiguous_and_none():
    bs = [B(1, 930.0, ["2026-08-22"]), B(2, 1080.0, ["2026-08-21"]), B(3, 1450.0, ["2026-08-20"])]
    sub_m = match_credit(C(1, 2530.0, "2026-08-24"), bs)
    assert sub_m.reason == "subset" and sub_m.exact == [2, 3]
    assert match_credit(C(1, 2010.0, "2026-08-24"), bs).exact == [1, 2]
    amb = match_credit(C(1, 2000.0, "2026-08-24"),
                       [B(1, 1000.0, ["2026-08-22"]), B(2, 1000.0, ["2026-08-21"]), B(3, 1000.0, ["2026-08-20"])])
    assert amb.reason == "ambiguous" and amb.exact == [] and len(amb.candidates) == 3
    none = match_credit(C(1, 2950.0, "2026-08-24"), bs)
    assert none.reason == "none" and [b["id"] for b in none.candidates] == [1, 2, 3]   # newest anchor first
    far = match_credit(C(1, 2530.0, "2026-09-30"), bs)                                 # outside window: no subset
    assert far.reason == "none"


def test_match_credit_will_not_link_an_exact_amount_outside_the_window():
    """A lone amount agreeing to the cent is not enough on its own. Amounts here
    are round hundreds and the ledger holds months of them, so the dates have to
    agree too; the stale batch still leads the card as the likeliest tap."""
    stale = B(1, 1080.0, ["2026-08-01"])                 # anchor 20 days before the credit
    m = match_credit(C(1, 1080.0, "2026-08-21"), [stale])
    assert m.reason == "none" and m.exact == []
    assert [b["id"] for b in m.candidates] == [1]
    fresh = B(2, 1080.0, ["2026-08-21"])
    assert match_credit(C(1, 1080.0, "2026-08-24"), [fresh]).exact == [2]


def test_match_batch_will_not_link_an_exact_amount_outside_the_window():
    stale = C(1, 1450.0, "2026-09-09")                   # value date 20 days after the batch
    m = match_batch(B(3, 1450.0, ["2026-08-20"]), [stale])
    assert m.reason == "none" and m.exact == []
    assert [c["id"] for c in m.candidates] == [1]
    fresh = C(2, 1450.0, "2026-08-24")
    assert match_batch(B(3, 1450.0, ["2026-08-20"]), [fresh]).exact == [2]


def test_a_round_amount_months_apart_is_a_coincidence_not_a_payment():
    """The first backfill put 48 credits beside 4 batches and linked two of them
    to payouts that predate the service: $1,080 paid 07-30 against legs of
    08-21, $1,450 paid 08-11 against legs of 08-20. Neither direction may."""
    credits_ = [C(1, 1080.0, "2026-07-30"), C(2, 1450.0, "2026-08-11")]
    batches = [B(2, 1080.0, ["2026-08-21"]), B(3, 1450.0, ["2026-08-20"])]
    for c in credits_:
        assert match_credit(c, batches).exact == []
    for b in batches:
        assert match_batch(b, credits_).exact == []


def test_match_credit_never_links_a_near_miss():
    """A thirty dollar gap is a question for the operator, not a rounding error."""
    bs = [B(1, 1450.0, ["2026-08-22"])]
    m = match_credit(C(1, 1480.0, "2026-08-24"), bs)
    assert m.reason == "none" and m.exact == [] and [b["id"] for b in m.candidates] == [1]


def test_match_credit_uses_remaining_filters_platform_and_caps_candidates():
    bs = [B(i, 100.0, ["2026-08-2%d" % (i % 10)]) for i in range(1, 12)] + \
         [B(99, 100.0, ["2026-08-22"], platform="uber")]
    m = match_credit(C(1, 5000.0, "2026-08-24", remaining=100.0), bs)
    assert m.reason == "ambiguous" and len(m.candidates) <= 8 and all(b["platform"] == "ride" for b in m.candidates)
    # Money is allocated in amounts, so a credit smaller than every batch pays
    # part of one instead of nothing: those are the short proposals.
    m2 = match_credit(C(1, 5000.0, "2026-08-24", remaining=35.0), bs)
    assert m2.reason == "none" and len(m2.short) == credits.MAX_SHORT
    assert all(b["platform"] == "ride" for b in m2.short)
    assert all(b["id"] not in {x["id"] for x in m2.short} for b in m2.candidates)


def test_match_batch_exact_and_candidates():
    cs = [C(1, 2540.0, "2026-08-26"), C(2, 2950.0, "2026-08-24", remaining=1500.0), C(3, 100.0, "2026-08-20")]
    assert match_batch(B(4, 2540.0, ["2026-08-23", "2026-08-24"]), cs).exact == [1]
    m = match_batch(B(5, 1450.0, ["2026-08-20"]), cs)
    assert m.reason == "none" and [c["id"] for c in m.candidates] == [1, 2]   # remaining >= amount, newest first
    assert [c["id"] for c in m.short] == [3]                                  # would leave the batch partial
    assert match_batch(B(6, 1500.0, ["2026-08-21"]), cs).exact == [2]


def test_match_batch_filters_platform():
    cs = [C(1, 2540.0, "2026-08-26", platform="uber")]
    m = match_batch(B(4, 2540.0, ["2026-08-23"]), cs)
    assert m.exact == [] and m.candidates == []


# ---- money cannot arrive before the work it pays for ----

def stand_in(amount, dates):
    """A statement standing in for the batch it is about to become, the way
    propose_statement builds it: day headers and a total, no orders yet."""
    return {"id": 0, "platform": "ride", "confirmed_amount": amount, "outstanding": amount,
            "statement": {"days": [{"date": d, "rows": [{"order_id": "x", "amount": amount,
                                                         "settle_date": d}]} for d in dates]},
            "orders": []}


def old_pool():
    """Two unmatched credits from months back and one that arrived after the
    service.  The old two carry the outstanding amount exactly: agreeing to the
    cent is what must not rescue them."""
    return [C(1, 1450.0, "2026-06-27"), C(2, 1450.0, "2026-07-21"), C(3, 2950.0, "2026-08-30")]


def test_match_batch_will_not_offer_credits_older_than_the_service_days():
    """A statement forwarded on 08-28 for legs driven 08-25/26: no money has
    arrived for it yet, and June's and July's leftovers never can be it."""
    m = match_batch(B(5, 1450.0, ["2026-08-25", "2026-08-26"]), old_pool())
    assert m.exact == [] and m.short == []
    assert [c["id"] for c in m.candidates] == [3]


def test_match_batch_reads_the_service_days_off_a_statement_with_no_orders():
    """The same bound before the batch exists: the statement's day headers say
    when the work was done."""
    m = match_batch(stand_in(1450.0, ["2026-08-25", "2026-08-26"]), old_pool())
    assert m.exact == [] and m.short == []
    assert [c["id"] for c in m.candidates] == [3]


def test_match_credit_will_not_offer_a_batch_worked_after_the_money_arrived():
    """The mirror: a credit dated 08-24 is not for legs driven on 08-26,
    however exactly the amounts agree."""
    earlier = B(1, 1450.0, ["2026-08-22"])
    later = B(2, 1450.0, ["2026-08-25", "2026-08-26"])
    m = match_credit(C(9, 1450.0, "2026-08-24"), [earlier, later])
    assert m.exact == [1] and m.short == [] and m.candidates == []


def test_a_batch_that_cannot_say_when_it_was_worked_hides_nothing():
    """An unknown service end excludes nothing: the whole pool is still offered
    rather than silently dropped."""
    dateless = {"id": 5, "platform": "ride", "confirmed_amount": 1450.0, "outstanding": 1450.0,
                "statement": None, "orders": []}
    m = match_batch(dateless, old_pool())
    assert m.exact == [] and m.short == []
    assert [c["id"] for c in m.candidates] == [3, 2, 1]


# ---- short payments ----

def test_match_credit_offers_the_batch_it_cannot_cover():
    """The case that broke round 2: $3,460 of statement, $2,950 paid, because
    the platform failed to submit two of its own legs."""
    batch = B(5, 3460.0, ["2026-08-20", "2026-08-21", "2026-08-22"])
    m = match_credit(C(1, 2950.0, "2026-08-24"), [batch])
    assert m.reason == "none" and m.exact == []
    assert [b["id"] for b in m.short] == [5]
    assert m.candidates == []           # the short proposal is not repeated below itself


def test_match_credit_short_is_newest_first_and_capped():
    bs = [B(i, 9999.0, ["2026-08-%02d" % (14 + i)]) for i in range(1, 7)]
    m = match_credit(C(1, 100.0, "2026-08-21"), bs)
    assert [b["id"] for b in m.short] == [6, 5, 4, 3]     # anchors 08-20 down to 08-17
    assert [b["id"] for b in m.candidates] == [2, 1]


def test_match_credit_measures_against_what_a_batch_is_still_owed():
    """A batch already part-paid is offered for its shortfall, not its total:
    a make-up payment agrees with what is left, never with the whole batch."""
    part = B(5, 3460.0, ["2026-08-22"], received=2950.0)
    assert match_credit(C(9, 510.0, "2026-08-28"), [part]).exact == [5]
    assert match_credit(C(9, 3460.0, "2026-08-28"), [part]).exact == []


def test_match_credit_subset_sums_outstanding_not_totals():
    bs = [B(1, 1000.0, ["2026-08-22"], received=600.0),   # owed 400
          B(2, 1080.0, ["2026-08-21"])]
    m = match_credit(C(1, 1480.0, "2026-08-24"), bs)
    assert m.reason == "subset" and m.exact == [1, 2]


def test_match_batch_short_credits_and_offer_order():
    """A card leads with what agrees to the cent, then what would pay part of
    it, then everything else."""
    exact = C(1, 510.0, "2026-08-30")
    short = C(2, 300.0, "2026-08-29")
    big = C(3, 5000.0, "2026-08-28")
    batch = B(5, 510.0, ["2026-08-25"])
    m = match_batch(batch, [big, short, exact])
    assert m.exact == [1] and [c["id"] for c in m.short] == [2]
    assert [c["id"] for c in m.candidates] == [3]
    assert [c["id"] for c in offer(m, [big, short, exact])] == [1, 2, 3]


# ---- the tick arithmetic ----

def leg(oid, hh, price, banner=0.0):
    return {"order_id": oid, "scheduled_time": f"2026-08-22 {hh}:00:00",
            "service_type": "接机", "price": price, "banner_fee": banner,
            "tunnel_fee": 0.0, "unpaid": 0}


def ticked_batch(rows, legs, outstanding):
    return {"id": 5, "outstanding": outstanding, "orders": legs,
            "statement": {"days": [{"date": "2026-08-22", "rows": [
                {"order_id": oid, "amount": a} for oid, a in rows]}]}}


def test_leg_amount_uses_the_platforms_figures():
    """The bank sent the statement's total minus the platform's own amounts for
    the legs it failed to submit, so a leg it priced above the system still has
    to account for the gap."""
    legs = [leg("A1", "09", 2950.0), leg("A2", "10", 210.0), leg("A3", "11", 300.0)]
    b = ticked_batch([("A1", 2950.0), ("A2", 220.0), ("A3", 300.0)], legs, 520.0)
    assert credits.leg_amount(b, legs[1]) == 220.0        # platform's, not the system's 210
    # Priced the system's way the same correct ticks would be refused.
    assert round(sum(expected_of(o) for o in legs[1:]), 2) == 510.0


def test_a_leg_is_worth_its_own_rows_including_the_banner_line():
    """A 舉牌 line sits under its trip's id, so the rows merge exactly as
    reconcile folds them."""
    legs = [leg("A1", "09", 210.0, banner=40.0)]
    b = ticked_batch([("A1", 210.0), ("A1", 40.0)], legs, 250.0)
    assert credits.leg_amount(b, legs[0]) == 250.0


def test_without_a_statement_a_leg_is_worth_what_the_system_says():
    legs = [leg("A1", "09", 210.0, banner=40.0)]
    b = {"id": 5, "outstanding": 250.0, "orders": legs, "statement": None}
    assert credits.leg_amount(b, legs[0]) == 250.0 == expected_of(legs[0])
    # A statement that does not list the leg leaves the system's figure too.
    assert credits.leg_amount(ticked_batch([("Z9", 1.0)], legs, 250.0), legs[0]) == 250.0


def test_tick_vocabulary_is_gone():
    """Round 4: the tick card moved to the dashboard.  The functions that only
    served the bot's tick card are removed so nothing can call them."""
    for name in ("tick_head", "tick_label", "tick_total", "ticks_add_up",
                 "tick_footer", "unpaid_text"):
        assert not hasattr(credits, name), f"credits.{name} should not exist"


# ---- guess_unpaid ----

def partial_batch(rows, legs, outstanding, received=None):
    """A partial batch for guess_unpaid: has state, received, and outstanding."""
    total = sum(a for _, a in rows)
    if received is None:
        received = round(total - outstanding, 2)
    return {"id": 5, "outstanding": outstanding, "received": received,
            "confirmed_amount": total, "state": "partial", "orders": legs,
            "statement": {"days": [{"date": "2026-08-22", "rows": [
                {"order_id": oid, "amount": a} for oid, a in rows]}]}}


def test_guess_unpaid_unique_subset():
    """One subset of legs accounts for the shortfall — the common case."""
    legs = [leg("A1", "09", 2950.0), leg("A2", "10", 210.0), leg("A3", "11", 300.0)]
    b = partial_batch([("A1", 2950.0), ("A2", 210.0), ("A3", 300.0)], legs, 510.0)
    assert credits.guess_unpaid(b) == [["A2", "A3"]]


def test_guess_unpaid_several_subsets_ordered_smallest_first():
    legs = [leg("A1", "09", 300.0), leg("A2", "10", 200.0),
            leg("A3", "11", 100.0), leg("A4", "12", 300.0)]
    b = partial_batch([("A1", 300.0), ("A2", 200.0), ("A3", 100.0), ("A4", 300.0)],
                      legs, 300.0)
    guesses = credits.guess_unpaid(b)
    assert guesses[0] == ["A1"]      # size 1 before size 2
    assert guesses[1] == ["A4"]      # same size, lexicographic
    assert ["A2", "A3"] in guesses   # size 2 subset


def test_guess_unpaid_none():
    legs = [leg("A1", "09", 210.0), leg("A2", "10", 300.0)]
    b = partial_batch([("A1", 210.0), ("A2", 300.0)], legs, 400.0)
    assert credits.guess_unpaid(b) == []


def test_guess_unpaid_cap():
    """More than MAX_GUESSES subsets are truncated."""
    # 10 legs of $100, outstanding $200: C(10,2) = 45 subsets
    legs = [leg(f"Z{i:02d}", f"{9+i}", 100.0) for i in range(10)]
    rows = [(f"Z{i:02d}", 100.0) for i in range(10)]
    b = partial_batch(rows, legs, 200.0)
    guesses = credits.guess_unpaid(b)
    assert len(guesses) == credits.MAX_GUESSES


def test_guess_unpaid_non_partial_returns_empty():
    legs = [leg("A1", "09", 500.0)]
    b = {"id": 5, "outstanding": 0, "received": 500.0,
         "confirmed_amount": 500.0, "state": "paid", "orders": legs,
         "statement": {"days": [{"date": "2026-08-22", "rows": [("A1", 500.0)]}]}}
    assert credits.guess_unpaid(b) == []
    b2 = {"id": 5, "outstanding": 500.0, "received": 0,
          "confirmed_amount": 500.0, "state": "awaiting", "orders": legs,
          "statement": None}
    assert credits.guess_unpaid(b2) == []


def test_guess_unpaid_uses_platform_amounts():
    """The guess is measured in the platform's figures, not the system's,
    because the transfer is the sum of what the platform printed."""
    legs = [leg("A1", "09", 2950.0), leg("A2", "10", 210.0), leg("A3", "11", 300.0)]
    # Platform prices A2 at $220, not system's $210.
    b = partial_batch([("A1", 2950.0), ("A2", 220.0), ("A3", 300.0)], legs, 520.0)
    guesses = credits.guess_unpaid(b)
    assert guesses == [["A2", "A3"]]


# ---- the wrappers that read the DB ----

def test_propose_credit_and_propose_batch_find_the_match_without_writing(db_path):
    """Round 2's rule: the matcher proposes, the operator's tap links.  Nothing
    here may leave a settlement paid."""
    seed(db_path, "A1", "2026-08-23 09:00:00", price=2540.0)
    sid = create_settlement(db_path, "ride", ["A1"], 2540.0, "2026-08-26", now=NOW)
    cid = insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 2540.0, "currency": "HKD",
                                  "value_date": "2026-08-26", "payer": None, "memo": None,
                                  "email_id": None, "received_at": None, "recorded_at": None})
    m = propose_credit(db_path, cid)
    assert m.reason == "exact" and m.exact == [sid]
    assert propose_batch(db_path, sid).exact == [cid]
    batch = get_settlement(db_path, sid)
    assert batch["allocations"] == [] and batch["paid_on"] is None
    assert batch["received"] == 0.0 and batch["state"] == "awaiting"
    assert [c["id"] for c in unallocated_credits(db_path)] == [cid]
    assert [b["id"] for b in open_batches(db_path, "ride")] == [sid]


def test_propose_credit_proposes_a_subset_without_writing(db_path):
    seed(db_path, "A1", "2026-08-20 09:00:00", price=1450.0)
    seed(db_path, "A2", "2026-08-21 09:00:00", price=1080.0)
    s1 = create_settlement(db_path, "ride", ["A1"], 1450.0, "2026-08-22", now=NOW)
    s2 = create_settlement(db_path, "ride", ["A2"], 1080.0, "2026-08-22", now=NOW)
    cid = insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 2530.0, "currency": "HKD",
                                  "value_date": "2026-08-24", "payer": None, "memo": None,
                                  "email_id": None, "received_at": None, "recorded_at": None})
    m = propose_credit(db_path, cid)
    assert m.reason == "subset" and m.exact == [s1, s2]
    assert len(open_batches(db_path, "ride")) == 2
    assert len(unallocated_credits(db_path)) == 1


def test_propose_is_empty_for_an_archived_or_spent_credit_and_a_linked_batch(db_path):
    seed(db_path, "A1", "2026-08-23 09:00:00", price=2540.0)
    sid = create_settlement(db_path, "ride", ["A1"], 2540.0, "2026-08-26", now=NOW)
    cid = insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 2540.0, "currency": "HKD",
                                  "value_date": "2026-08-26", "payer": None, "memo": None,
                                  "email_id": None, "received_at": None, "recorded_at": None})
    allocate(db_path, cid, sid)
    assert propose_credit(db_path, cid) == Match()      # nothing left to allocate
    assert propose_batch(db_path, sid) == Match()       # already linked
    assert propose_credit(db_path, 999) == Match()
    assert propose_batch(db_path, 999) == Match()


# ---- the statement card, before the batch exists ----

def stmt_json(settle_dates, rows=1):
    """A statement as corrected_json stores it: what the card matches on is the
    settle dates and the total, neither of which needs a batch to exist."""
    return {"days": [{"date": d, "rows": [{"order_id": f"X{i}", "amount": 100.0, "settle_date": d}
                                          for i in range(rows)]}
                     for d in settle_dates]}


def a_credit(db_path, ref, amount, value_date):
    return insert_credit(db_path, {"ref": ref, "platform": "ride", "amount": amount,
                                   "currency": "HKD", "value_date": value_date, "payer": None,
                                   "memo": None, "email_id": None, "received_at": None,
                                   "recorded_at": None})


def test_propose_statement_matches_the_total_inside_the_window(db_path):
    cid = a_credit(db_path, "R1", 2540.0, "2026-08-26")
    m = propose_statement(db_path, "ride", 2540.0, stmt_json(["2026-08-24"]))
    assert m.reason == "exact" and m.exact == [cid]


def test_propose_statement_offers_credits_it_cannot_prove(db_path):
    big = a_credit(db_path, "R1", 2950.0, "2026-08-26")
    small = a_credit(db_path, "R2", 100.0, "2026-08-25")
    m = propose_statement(db_path, "ride", 1450.0, stmt_json(["2026-08-24"]))
    assert m.reason == "none" and m.exact == []
    assert [c["id"] for c in m.candidates] == [big]      # could pay the whole statement
    assert [c["id"] for c in m.short] == [small]         # would pay part of it


def test_propose_statement_offers_a_short_payment(db_path):
    """The case round 2 could not record: the platform failed to submit two of
    its own legs, paid the rest, and said so by hand."""
    cid = a_credit(db_path, "R1", 2950.0, "2026-08-24")
    m = propose_statement(db_path, "ride", 3460.0, stmt_json(["2026-08-22"]))
    assert m.exact == [] and [c["id"] for c in m.short] == [cid]
    assert m.short[0]["remaining"] == 2950.0


def test_propose_statement_matches_nothing_when_no_credit_is_in_the_window(db_path):
    a_credit(db_path, "R1", 100.0, "2026-10-05")
    m = propose_statement(db_path, "ride", 1450.0, stmt_json(["2026-08-24"]))
    assert m.exact == [] and m.short == []
    assert [c["ref"] for c in m.candidates] == ["R1"]    # out of window: only a suggestion


def test_propose_statement_will_not_match_a_credit_outside_the_window(db_path):
    cid = a_credit(db_path, "R1", 1000.0, "2026-10-05")
    m = propose_statement(db_path, "ride", 1000.0, stmt_json(["2026-08-24"]))
    assert m.reason == "none" and m.exact == [] and [c["id"] for c in m.candidates] == [cid]


def test_propose_statement_needs_a_total_and_a_date(db_path):
    """An unreadable total is not a match on $0, and a statement with no settle
    date has nothing to put inside the window."""
    a_credit(db_path, "R1", 1000.0, "2026-08-26")
    assert propose_statement(db_path, "ride", 0.0, stmt_json(["2026-08-24"])) == Match()
    undated = {"days": [{"date": "2026-08-24", "rows": [{"order_id": "X", "amount": 1000.0}]}]}
    assert anchor({"statement": undated, "orders": []}) is None
    assert propose_statement(db_path, "ride", 1000.0, undated).exact == []
