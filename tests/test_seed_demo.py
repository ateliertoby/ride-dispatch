"""The synthetic database the screenshot and end-to-end scripts run on.

It has to hold each form the settle page can take, from its own rows and at
any date, and every month of it has to add up.
"""
import os
import sys
from datetime import date, datetime, time, timedelta

import pytest

from ride_dispatch import db
from ride_dispatch.service import PLATFORMS

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import seed_demo_db  # noqa: E402

# Either side of a month's first and last day and of a year's end, a leap
# day, and a day in the middle of a month.
TODAYS = ["2026-10-02", "2026-10-08", "2026-10-15", "2026-10-31", "2026-11-01", "2026-12-31",
          "2027-01-01", "2027-03-01", "2028-02-29", "2028-03-31"]
PARTS = ("received", "awaiting", "unsettled", "short")


class Seeded:
    def __init__(self, path: str, today: date):
        self.path = path
        self.today = today
        self.now = datetime.combine(today, time(seed_demo_db.DEMO_HOUR, 0))
        self.t = seed_demo_db.seed(path, today)

    def month(self, months_back: int, platform: str = "ride") -> dict:
        key = seed_demo_db.month_start(self.today, months_back).isoformat()[:7]
        return db.get_settle_month(self.path, key, platform, now=self.now)

    def months(self, platform: str = "ride") -> list:
        """Every month the seed reaches and the one after, oldest first."""
        return [self.month(n, platform) for n in (3, 2, 1, 0, -1)]

    def batches(self, platform: str = "ride") -> dict:
        return {b["id"]: b for m in self.months(platform) for b in m["settlements"]}


@pytest.fixture(params=TODAYS)
def seeded(request, tmp_path):
    return Seeded(str(tmp_path / "demo.db"), date.fromisoformat(request.param))


def test_every_month_of_every_platform_adds_up(seeded):
    for platform in PLATFORMS:
        for month in seeded.months(platform):
            totals = month["month_totals"]
            assert totals is not None and month["earlier"] is not None
            assert round(totals["fare"] * 100) == sum(round(totals[p] * 100) for p in PARTS)
            assert all(totals[p] >= 0 for p in PARTS)


def test_the_months_between_them_count_every_fare_already_driven(seeded):
    from ride_dispatch.service import expected_of, platform_of
    for platform in PLATFORMS:
        fares = sum(m["month_totals"]["fare"] for m in seeded.months(platform))
        driven = 0.0
        for back in range(-2, 125):
            for o in db.get_orders_by_date(seeded.path, seed_demo_db.day(seeded.today, back)):
                if (o["status"] or "active") == "active" and platform_of(o["service_type"]) == platform \
                        and o["scheduled_time"] < db._now_str(seeded.now):
                    driven += expected_of(o)
        assert round(fares * 100) == round(driven * 100)


def test_unsettled_days_in_a_row_and_a_mixed_day(seeded):
    orders = [o for m in seeded.months() for o in m["orders"]]

    def on(back):
        return [o for o in orders if o["scheduled_time"].startswith(seed_demo_db.day(seeded.today, back))]

    for back in (3, 2, 1):
        assert on(back) and all(o["settlement_id"] is None for o in on(back))
    mixed = [o for o in orders if o["scheduled_time"].startswith(seeded.t["mixed_day"])]
    assert {o["settlement_id"] for o in mixed} == {None, seed_demo_db.BATCH["ahead"]}


def test_a_statement_paid_short_names_its_unpaid_leg(seeded):
    short = seeded.batches()[seed_demo_db.BATCH["short"]]
    assert (short["state"], short["outstanding"]) == ("partial", 380.0)
    assert [o["order_id"] for o in short["orders"] if o["unpaid"]] == [seed_demo_db.ORDER["short_unpaid_leg"]]
    assert sum(m["month_totals"]["short"] for m in seeded.months()) == 380.0


def test_a_statement_straddles_two_months(seeded):
    straddle = seeded.batches()[seed_demo_db.BATCH["straddle"]]
    days = sorted({o["scheduled_time"][:10] for o in straddle["orders"]})
    assert days == [d.isoformat() for d in seed_demo_db.straddle_days(seeded.today)]
    assert len({d[:7] for d in days}) == 2 and days[2][:7] == seeded.t["straddle_month"]
    assert straddle["state"] == "paid"
    # Each month counts its own two legs and no more.
    assert seeded.month(3)["month_totals"] == {
        "fare": 870.0, "received": 870.0, "awaiting": 0.0, "unsettled": 0.0, "short": 0.0}


def test_one_month_holds_two_statements_collected_in_full(seeded):
    pair = seeded.t["collected_pair"]
    month = db.get_settle_month(seeded.path, pair["month"], "ride", now=seeded.now)
    full = [b for b in month["settlements"]
            if b["state"] == "paid" and any(o["scheduled_time"][:7] == pair["month"] for o in b["orders"])]
    # Listed as the target names them, the later confirmed first. A statement
    # placed by offset can fall in the month as well.
    newest_first = [b["id"] for b in sorted(full, key=lambda b: b["settled_on"], reverse=True)]
    assert [i for i in newest_first if i in pair["batches"]] == pair["batches"]


def test_a_statement_carries_a_line_that_is_no_orders_fare(seeded):
    from ride_dispatch.service import owed_of
    straddle = seeded.batches()[seed_demo_db.BATCH["straddle"]]
    assert straddle["adjustments"] == [
        {"order_ref": seed_demo_db.ORDER["unknown_fined"], "date": straddle["adjustments"][0]["date"], "amount": -30.0}]
    assert db.get_order_by_id(seeded.path, seed_demo_db.ORDER["unknown_fined"]) is None
    # The line is the whole of the difference between its figure and its fares.
    assert round(straddle["confirmed_amount"] - sum(owed_of(o) for o in straddle["orders"]), 2) == -30.0
    assert straddle["confirmed_amount"] == straddle["expected_amount"] == 1680.0


def test_a_statement_with_days_apart_and_one_that_differs_from_its_fares(seeded):
    from ride_dispatch.service import owed_of
    batches = seeded.batches()
    held = sorted({date.fromisoformat(o["scheduled_time"][:10])
                   for o in batches[seed_demo_db.BATCH["held_back"]]["orders"]})
    assert any(b - a > timedelta(days=1) for a, b in zip(held, held[1:]))
    waiting = batches[seed_demo_db.BATCH["awaiting"]]
    assert waiting["state"] == "awaiting"
    assert round(waiting["confirmed_amount"] - sum(owed_of(o) for o in waiting["orders"]), 2) == -20.0


def test_bank_credits_unmatched_and_put_away(seeded):
    states = [c["state"] for c in db.list_credits(seeded.path, "ride")]
    assert "open" in states and "partial" in states and "archived" in states


def test_the_backlog_adds_old_credits_nothing_accounts_for(seeded, tmp_path):
    from ride_dispatch import credits
    path = str(tmp_path / "backlog.db")
    seed_demo_db.seed(path, seeded.today, backlog=True)
    def ledger_of(db_path):
        # But for when the row was written, which is the clock's.
        return [{k: v for k, v in c.items() if k != "imported_at"} for c in db.list_credits(db_path, "ride")]

    plain = {c["id"]: c for c in ledger_of(seeded.path)}
    ledger = ledger_of(path)
    old = [c for c in ledger if c["id"] not in plain]
    # Every row of the plain seed is the same row with the backlog behind it.
    assert [c for c in ledger if c["id"] in plain] == list(plain.values())
    assert len(old) == seed_demo_db.BACKLOG and all(c["state"] == "open" for c in old)
    assert [(c["value_date"], c["amount"]) for c in old] == seed_demo_db.backlog_credits(seeded.today)
    # Oldest first, they fill the queue ahead of every credit that has an answer.
    waiting = [c for c in ledger if c["state"] in ("open", "partial")]
    assert waiting[:len(old)] == old and len(waiting) > len(old)
    for c in old:
        m = credits.propose_credit(path, c["id"])
        assert credits.offer(m, db.open_batches(path, "ride")) == []
    # What can be acted on is still there: one credit agreeing with the
    # statement that waits for its transfer.
    answer = credits.propose_credit(path, seed_demo_db.CREDIT["exact"])
    assert (answer.reason, answer.exact) == ("exact", [seed_demo_db.BATCH["awaiting"]])


def test_the_current_month_has_open_money_before_it(seeded):
    earlier = seeded.month(0)["earlier"]
    assert earlier["open"] > 0 and earlier["month"] < seeded.today.isoformat()[:7]


def test_one_month_has_nothing_left_to_do(seeded):
    clean = seeded.t["clean"]
    month = db.get_settle_month(seeded.path, clean["month"], clean["platform"], now=seeded.now)
    assert month["month_totals"] == {
        "fare": 617.5, "received": 617.5, "awaiting": 0.0, "unsettled": 0.0, "short": 0.0}
    assert month["earlier"] == {"open": 0.0, "month": None}
    assert not [c for c in db.list_credits(seeded.path, clean["platform"]) if c["state"] in ("open", "partial")]
    assert len(month["orders"]) == 3 and len(month["settlements"]) == 1


def test_every_statement_prints_due_dates_and_no_two_print_the_same(seeded):
    for platform in PLATFORMS:
        batches = seeded.batches(platform)
        due = [tuple(b["due_dates"]) for b in batches.values()]
        assert all(due) and len(set(due)) == len(due)
        for b in batches.values():
            # One value per service day, and a figure the statement adds up to.
            for day in b["statement"]["days"]:
                assert len({r["settle_date"] for r in day["rows"]}) == 1
            assert b["statement"]["total"] == b["confirmed_amount"]
    # Two statements confirmed on one day are still told apart.
    batches = seeded.batches()
    ahead, group = batches[seed_demo_db.BATCH["ahead"]], batches[seed_demo_db.BATCH["group"]]
    assert ahead["settled_on"] == group["settled_on"] and ahead["due_dates"] != group["due_dates"]


def test_a_statement_due_on_two_days_running_and_one_due_on_two_days_apart(seeded):
    batches = seeded.batches()

    def gaps(key):
        days = [date.fromisoformat(d) for d in batches[seed_demo_db.BATCH[key]]["due_dates"]]
        return [(b - a).days for a, b in zip(days, days[1:])]

    assert gaps("short") == [1]
    assert gaps("held_back") == [3]
