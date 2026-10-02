"""Build a synthetic database that puts every visual state of the two views on screen.

    python scripts/seed_demo_db.py PATH [--today YYYY-MM-DD]

Every value is invented. Rows are written through ride_dispatch.db's own
functions, the ones the bot and the web app use, so the database is one the
application could have produced. Dates are offsets from `today`, or days of
the months before today's, and nothing else varies, so the same `today` always
gives the same database: screenshots taken from two builds can be compared
pixel for pixel.

Most rows are placed by offset, so which month a statement falls in depends
on the date. What has to be true of a month whatever the date is placed by
the calendar instead (_months_back): a statement whose legs lie either side of
a month's first day, a second statement collected in full in the month it
reaches into, and a month of one platform with nothing left to do.

PATH must not exist yet. The script never opens a database it did not create.
"""
import argparse
import os
import struct
import sys
import zlib
from datetime import date, datetime, time, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ride_dispatch import db  # noqa: E402
from ride_dispatch.ingest import parking_fee  # noqa: E402
from ride_dispatch.parser import Order  # noqa: E402
from ride_dispatch.service import expected_of  # noqa: E402

# The moment the demo is looking at the day from. Orders before it are done,
# the ones after it are still to come; screenshots pin both clocks to it.
DEMO_HOUR = 14

AIRPORT = "香港国际机场1号航站楼(香港国际机场1号航站楼)"
# Invented addresses. The district names are real because the price suggestion
# reads the district out of the address.
TST = "尖沙咀示範酒店(九龍尖沙咀示範道8號)"
MONGKOK = "旺角樣本賓館(九龍旺角樣本街21號)"
WANCHAI = "灣仔例子酒店(香港灣仔例子道12號)"
SHATIN = "沙田範例廣場(新界沙田範例路3號)"
STATION = "香港西九龍站(香港西九龍站)"

# Order numbers: sixteen digits like a platform's, told apart by their last four.
def _oid(n: int) -> str:
    return f"88000000000{n:05d}"


# What the screenshot and end-to-end scripts aim at. Ids are AUTOINCREMENT on a
# fresh database, so creation order fixes them; seed() checks that it did.
BATCH = {"paid": 1, "short": 2, "awaiting": 3, "held_back": 4, "ahead": 5, "group": 6,
         "straddle": 7, "clean": 8, "pair": 9}
CREDIT = {"paid": 1, "short": 2, "exact": 3, "partial": 4, "archived": 5, "group": 6,
          "straddle": 7, "clean": 8, "pair": 9}
# The 應結算日期 each statement placed by offset prints, in days before today:
# one for the whole statement, or one per service day, keyed by that day's own
# days before today. Between them the names take every form a name has: one
# date, two days running, two days apart. No two statements print the same
# set, as no two of the platform's do, although two were confirmed on one day.
DUE = {"paid": 32, "short": {27: 25, 26: 25, 25: 24}, "awaiting": 17,
       "held_back": {15: 13, 12: 10, 11: 10}, "ahead": 1, "group": 3}
# The platform each of them is on, where it is not the ride platform.
PLATFORM = {"clean": "uber"}
ORDER = {
    "done_pickup": _oid(1),
    "landed_banner": _oid(2),
    "upcoming_hotel": _oid(3),
    "dropoff": _oid(4),
    "unpriced": _oid(5),
    "cancelled": _oid(6),
    "short_unpaid_leg": _oid(204),
    "held_trip": _oid(601),
    # A number a statement prints that no order carries.
    "unknown_fined": _oid(990),
}


def day(today: date, back: int) -> str:
    """The date `back` days before today, as the database writes dates."""
    return (today - timedelta(days=back)).isoformat()


def month_start(today: date, back: int) -> date:
    """The first day of the month `back` months before today's."""
    m = today.year * 12 + today.month - 1 - back
    return date(m // 12, m % 12 + 1, 1)


# How many months before today's the calendar-placed rows lie. The rows placed
# by offset reach 38 days back, which is never earlier than the 22nd of the
# month two before today's, so the first three weeks of that month and all of
# the month before it are free of them.
STRADDLE_MONTHS_BACK = 2
CLEAN_MONTHS_BACK = 2


def straddle_days(today: date) -> list:
    """The four service days of the statement that straddles two months: the
    last two days of one month and the first two of the next."""
    first = month_start(today, STRADDLE_MONTHS_BACK)
    return [first + timedelta(days=n) for n in (-2, -1, 0, 1)]


def targets(today: date) -> dict:
    """Names for the rows a script has to find, without opening the database."""
    return {
        "today": today.isoformat(),
        "batch": dict(BATCH),
        "credit": dict(CREDIT),
        "order": dict(ORDER),
        # A day of the short-paid batch: its sheet lists batched legs.
        "batched_day": day(today, 26),
        # A day whose legs no batch has claimed.
        "loose_day": day(today, 1),
        # A day with one leg on a statement and one on none.
        "mixed_day": day(today, 5),
        # The month a statement reaches into from the month before it.
        "straddle_month": month_start(today, STRADDLE_MONTHS_BACK).isoformat()[:7],
        # A month holding at least two statements collected in full, and two
        # that are always among them, the later confirmed first.
        "collected_pair": {"month": month_start(today, STRADDLE_MONTHS_BACK).isoformat()[:7],
                           "batches": [BATCH["pair"], BATCH["straddle"]]},
        # A month of a platform with every fare collected, nothing unmatched
        # and nothing open before it.
        "clean": {"platform": PLATFORM["clean"],
                  "month": month_start(today, CLEAN_MONTHS_BACK).isoformat()[:7]},
        # The due dates each of those statements prints, which name it.
        "due": {key: sorted({day(today, n) for n in (due.values() if isinstance(due, dict) else [due])})
                for key, due in DUE.items()},
        # A day each batch covers: where its sheet is reached from.
        "batch_day": {"paid": day(today, 34), "short": day(today, 26), "awaiting": day(today, 19),
                      "held_back": day(today, 12), "ahead": day(today, 6), "group": day(today, 8)},
    }


def _png() -> bytes:
    """A valid 1x1 PNG, standing in for a statement screenshot."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff"))
            + chunk(b"IEND", b""))


class _Seeder:
    def __init__(self, path: str, today: date):
        self.path = path
        self.today = today
        self.now = datetime.combine(today, time(DEMO_HOUR, 0))

    def ride(self, n: int, service: str, back: int, hhmm: str, price: float | None, *,
             source: str = "携程", banner: bool = False, flight: str = "",
             place: str = TST, exit_minutes: int | None = None, name: str = "",
             phone: str = "", overseas: str = "", third_party: str = "",
             vehicle: str = "经济5座", notes: str = "") -> str:
        """One platform order, entered the way a pasted message is."""
        order_id = _oid(n)
        if service == "接机":
            pickup, dropoff = AIRPORT, place
        elif service == "送机":
            pickup, dropoff = place, AIRPORT
        elif service == "接站":
            pickup, dropoff = STATION, place
        else:
            pickup, dropoff = place, MONGKOK
        order = Order(
            order_id=order_id, service_type=service, vehicle_type=vehicle,
            passenger_name=name or f"DEMO/PASSENGER{n}",
            scheduled_time=f"{day(self.today, back)} {hhmm}:00",
            passenger_phone=phone, overseas_phone=overseas, flight_number=flight,
            pickup=pickup, dropoff=dropoff, distance_km=36, notes="", driver_notes=notes,
            additional_services="举牌接机" if banner else "",
            passenger_exit_minutes=exit_minutes, third_party_contact=third_party,
            more_contacts="", raw_message="",
        )
        db.save_or_revive_order(self.path, order, None,
                                parking=parking_fee(order, source), source=source)
        if price is not None:
            db.update_price(self.path, order_id, price)
        return order_id

    def back(self, d: date) -> int:
        """A date as the number of days before today, which is how every row
        is placed."""
        return (self.today - d).days

    def quick(self, kind: str, back: int, hhmm: str, price: float, toll: float = 0.0) -> str:
        service = {"didi": "滴滴", "uber": "Uber", "foodpanda": "foodpanda"}[kind]
        order_id = f"{kind}_{day(self.today, back).replace('-', '')}{hhmm.replace(':', '')}_demo"
        db.save_quick_order(self.path, order_id, service, f"{day(self.today, back)} {hhmm}:00",
                            price, toll, source=service)
        return order_id

    def statement(self, order_ids: list[str], due, *, priced: dict | None = None,
                  lines: tuple = ()) -> dict:
        """What the platform's statement said of those legs, as a batch stores
        it: one row a leg under its service day, at what the book holds for
        the leg unless `priced` gives the platform's own figure for it, then
        `lines`, the rows that are not a leg's fare, each (order id, days
        before today of the service day it is printed under, amount).

        `due` is the 應結算日期 the rows print, in days before today: one
        number for the whole statement, or one per service day, keyed by that
        day's own days before today. The platform prints one value per
        service day, so a row takes its day's."""
        priced = priced or {}
        by_day: dict[int, list] = {}
        for order_id in order_ids:
            order = db.get_order_by_id(self.path, order_id)
            back = self.back(date.fromisoformat(order["scheduled_time"][:10]))
            amount = priced.get(order_id, expected_of(order))
            by_day.setdefault(back, []).append((order_id, amount, order["scheduled_time"][11:16]))
        for order_id, back, amount in lines:
            by_day.setdefault(back, []).append((order_id, amount, None))
        days = []
        for back in sorted(by_day, reverse=True):
            due_on = day(self.today, due[back] if isinstance(due, dict) else due)
            rows = [{"date": day(self.today, back), "order_id": order_id, "amount": float(amount),
                     "time": hhmm, "settle_date": due_on, "truncated": False}
                    for order_id, amount, hhmm in by_day[back]]
            days.append({"date": day(self.today, back), "count": len(rows),
                         "sum": round(sum(r["amount"] for r in rows), 2), "rows": rows})
        return {"account": "DEMO00", "total": round(sum(d["sum"] for d in days), 2),
                "reader": "demo", "days": days}

    def batch(self, key: str, order_ids: list[str], confirmed: float, settled_back: int, **kw) -> int:
        sid = db.create_settlement(self.path, PLATFORM.get(key, "ride"), order_ids, confirmed,
                                   day(self.today, settled_back), now=self.now, **kw)
        if sid != BATCH[key]:
            raise RuntimeError(f"batch {key} got id {sid}, expected {BATCH[key]}")
        return sid

    def credit(self, key: str, amount: float, back: int) -> int:
        cid = db.insert_credit(self.path, {
            "ref": f"DEMO-REF-{CREDIT[key]:04d}", "platform": PLATFORM.get(key, "ride"), "amount": amount,
            "currency": "HKD", "value_date": day(self.today, back),
            "payer": "DEMO PLATFORM LTD", "memo": "SUPPLIERPAY",
            "email_id": None, "received_at": None, "recorded_at": None,
        })
        if cid != CREDIT[key]:
            raise RuntimeError(f"credit {key} got id {cid}, expected {CREDIT[key]}")
        return cid


def _today(s: _Seeder) -> None:
    """The day view: one row of every kind, on both sides of the demo hour."""
    # A pickup that is over: landed in the morning, passenger long gone.
    done = s.ride(1, "接机", 0, "09:40", 480, flight="CX0488", place=TST, exit_minutes=30,
                  name="DEMO/ALPHA", phone="86 13800000101")
    db.update_flight_info(s.path, done, "09:10", "09:05", "09:12", "gate", "A")
    # Landed, not yet met: 舉牌, so it parks at P4. The row the day opens on.
    landed = s.ride(2, "接机", 0, "14:20", 520, banner=True, flight="UO623", place=WANCHAI,
                    exit_minutes=45, name="DEMO/BRAVO(重要贵宾)", phone="86 13800000102",
                    overseas="886 900000102", third_party="【WhatsApp】 886900000102",
                    notes="請司機到達後聯絡乘客")
    db.update_flight_info(s.path, landed, "13:35", "13:42", None, "landed", "B")
    # Still in the air, short exit time; not a 携程 order, so it meets at 富豪.
    upcoming = s.ride(3, "接机", 0, "17:15", 450, source="同程", flight="HX237", place=SHATIN,
                      exit_minutes=20, name="DEMO/CHARLIE", phone="852 60000103")
    db.update_flight_info(s.path, upcoming, "16:50", "16:55", None, "est", None)
    s.ride(4, "送机", 0, "11:00", 400, flight="CX0731", place=MONGKOK, name="DEMO/DELTA",
           phone="86 13800000104")
    s.ride(5, "单程接送", 0, "20:30", None, place=TST, name="DEMO/ECHO")
    cancelled = s.ride(6, "送机", 0, "18:00", 410, place=WANCHAI, name="DEMO/FOXTROT")
    db.update_order_fields(s.path, cancelled, {"status": "cancelled"})
    s.quick("didi", 0, "12:10", 128, 25)
    s.quick("uber", 0, "15:05", 96)
    s.quick("foodpanda", 0, "19:20", 55.5)


def _neighbours(s: _Seeder) -> None:
    """Yesterday and tomorrow, two orders each."""
    s.ride(11, "接机", 1, "10:15", 500, banner=True, flight="CX0488", place=TST, exit_minutes=40)
    s.ride(12, "送机", 1, "16:30", 400, flight="UO622", place=WANCHAI)
    s.ride(21, "接机", -1, "11:45", 490, flight="HX237", place=MONGKOK, exit_minutes=35)
    s.ride(22, "送机", -1, "19:00", 410, flight="CX0731", place=TST)


def _months_back(s: _Seeder) -> dict:
    """The orders placed by the calendar, in months before today's; returns
    the legs each of their statements will hold. The statements themselves
    are written after the ledger's, so the ids scripts aim at stay as they
    are."""
    # Four legs either side of a month's first day, settled as one statement.
    a, b, c, d = (s.back(x) for x in straddle_days(s.today))
    straddle = [s.ride(901, "接机", a, "09:10", 470, flight="CX0488", place=TST),
                s.ride(902, "送机", b, "16:20", 400, place=WANCHAI),
                s.ride(903, "接机", c, "10:30", 480, flight="UO623", place=MONGKOK),
                s.ride(904, "接站", d, "14:00", 360, place=TST)]
    # Two more legs in the month that statement reaches into, on a statement
    # of their own: whatever the date, one month holds two collected in full.
    first = month_start(s.today, STRADDLE_MONTHS_BACK)
    pair = [s.ride(911, "接机", s.back(first + timedelta(days=8)), "11:15", 450, flight="HX237", place=SHATIN),
            s.ride(912, "送机", s.back(first + timedelta(days=9)), "07:50", 400, place=TST)]
    # One month of another platform, every trip of it on one statement.
    first = month_start(s.today, CLEAN_MONTHS_BACK)
    clean = [s.quick("uber", s.back(first + timedelta(days=5)), "09:30", 180, 20),
             s.quick("uber", s.back(first + timedelta(days=12)), "21:10", 152.5),
             s.quick("uber", s.back(first + timedelta(days=19)), "13:40", 240, 25)]
    return {"straddle": straddle, "clean": clean, "pair": pair}


def _months_back_settled(s: _Seeder, legs: dict) -> None:
    """The statements over those orders, each collected in full."""
    first = month_start(s.today, STRADDLE_MONTHS_BACK)
    # The statement also carries a 判罰 under a number the book never had: a
    # line of its own, which is no order's fare, so the transfer is smaller
    # than the fares the month's totals count for it.
    fined_day = s.back(first - timedelta(days=1))
    s.batch("straddle", legs["straddle"], 1680, s.back(first + timedelta(days=4)),
            statement=s.statement(legs["straddle"], s.back(first + timedelta(days=3)),
                                  lines=((ORDER["unknown_fined"], fined_day, -30.0),)),
            adjustments=[{"order_ref": ORDER["unknown_fined"], "date": day(s.today, fined_day), "amount": -30.0}])
    db.allocate(s.path, s.credit("straddle", 1680, s.back(first + timedelta(days=6))), BATCH["straddle"])
    # Nothing of this platform is open before this month and no credit of it
    # is unmatched, so the month has nothing left to do.
    first = month_start(s.today, CLEAN_MONTHS_BACK)
    s.batch("clean", legs["clean"], 617.5, s.back(first + timedelta(days=23)),
            statement=s.statement(legs["clean"], s.back(first + timedelta(days=22))))
    db.allocate(s.path, s.credit("clean", 617.5, s.back(first + timedelta(days=25))), BATCH["clean"])
    first = month_start(s.today, STRADDLE_MONTHS_BACK)
    s.batch("pair", legs["pair"], 850, s.back(first + timedelta(days=11)),
            statement=s.statement(legs["pair"], s.back(first + timedelta(days=10))))
    db.allocate(s.path, s.credit("pair", 850, s.back(first + timedelta(days=12))), BATCH["pair"])


def _ledger(s: _Seeder) -> None:
    """Five weeks of settlement: each state a batch and a credit can be in."""
    # An old day no statement ever covered.
    s.ride(101, "送机", 38, "08:30", 450, place=WANCHAI)

    # Paid in full by one credit. One leg was fined, so the figures carry cents,
    # and the batch has its statement and screenshot.
    fined = s.ride(111, "接机", 34, "09:20", 500, banner=True, flight="CX0488", place=TST)
    paid = [fined,
            s.ride(112, "送机", 34, "15:10", 420, place=WANCHAI),
            s.ride(113, "接机", 33, "13:05", 480, flight="UO623", place=MONGKOK)]
    statement = s.statement(paid, DUE["paid"], lines=((fined, 34, -63.45),))
    s.batch("paid", paid, 1376.55, 31, penalties={fined: 63.45}, statement=statement, image=_png())
    db.allocate(s.path, s.credit("paid", 1376.55, 29), BATCH["paid"])

    # Paid short: the platform left one leg out, and the operator has named it.
    short = [s.ride(201, "接机", 27, "08:50", 520, banner=True, flight="HX237", place=TST),
             s.ride(202, "送机", 27, "17:40", 400, place=SHATIN),
             s.ride(203, "接机", 26, "11:30", 460, flight="CX0488", place=WANCHAI),
             s.ride(204, "单程接送", 26, "19:15", 380, place=TST),
             s.ride(205, "接机", 25, "14:00", 510, flight="UO623", place=MONGKOK)]
    # Its legs were due on two days running, so its name is a run of two.
    s.batch("short", short, 2310, 23, statement=s.statement(short, DUE["short"]))
    db.allocate(s.path, s.credit("short", 1930, 21), BATCH["short"])
    db.mark_unpaid(s.path, BATCH["short"], [ORDER["short_unpaid_leg"]])

    # Confirmed for less than the system expected, and still waiting for money.
    awaiting = [s.ride(301, "接机", 19, "10:40", 470, banner=True, flight="CX0488", place=TST),
                s.ride(302, "送机", 19, "18:20", 390, place=WANCHAI),
                s.ride(303, "接机", 18, "12:10", 390, flight="HX237", place=SHATIN)]
    s.batch("awaiting", awaiting, 1270, 16,
            statement=s.statement(awaiting, DUE["awaiting"], priced={awaiting[1]: 370.0}))
    # A credit nobody has matched yet, agreeing with that batch to the cent.
    s.credit("exact", 1270, 14)

    # A leg the platform held back and settled with a later statement, so the
    # batch's dates are not consecutive.
    held_back = [s.ride(401, "接机", 15, "09:00", 440, flight="UO623", place=MONGKOK),
                 s.ride(402, "接机", 12, "13:30", 530, banner=True, flight="CX0488", place=TST),
                 s.ride(403, "送机", 11, "07:45", 410, place=WANCHAI),
                 s.ride(404, "接站", 11, "16:00", 360, place=TST)]
    # The held-back leg was due days before the rest, so the statement's name
    # is two dates apart.
    s.batch("held_back", held_back, 1780, 9, statement=s.statement(held_back, DUE["held_back"]))
    # Paid by a credit bigger than the batch: the rest stays on the credit.
    db.allocate(s.path, s.credit("partial", 2080, 7), BATCH["held_back"])

    # A credit with nothing to match, taken out of the queue.
    db.archive_credit(s.path, s.credit("archived", 215.5, 36), "no-orders", day(s.today, 35))

    # A 舉牌 paid on a statement that held its trip back: the batch carries the
    # line, the trip stays unsettled on its own day.
    s.ride(601, "接机", 3, "10:10", 525, banner=True, flight="HX237", place=TST)
    s.ride(602, "送机", 3, "15:45", 415, place=MONGKOK)
    ahead = [s.ride(501, "接机", 6, "09:30", 515, flight="CX0488", place=WANCHAI),
             s.ride(502, "送机", 6, "17:00", 395, place=TST),
             s.ride(503, "接机", 5, "12:20", 475, flight="UO623", place=SHATIN)]
    # A leg of the same day the statement left out, which no later one has
    # taken: the day has money on a statement and money on none.
    s.ride(504, "送机", 5, "18:40", 430, place=WANCHAI)
    s.batch("ahead", ahead, 1425, 2,
            statement=s.statement(ahead, DUE["ahead"], lines=((ORDER["held_trip"], 3, 40.0),)),
            adjustments=[
                {"order_ref": ORDER["held_trip"], "date": day(s.today, 3), "amount": 40.0, "ahead": True}])

    # Confirmed the same day as the batch above; one transfer pays both. The
    # platform's own due dates still tell the two statements apart.
    group = [s.ride(701, "接机", 9, "08:15", 495, flight="UO623", place=TST),
             s.ride(702, "送机", 8, "10:30", 405, place=WANCHAI),
             s.ride(703, "接机", 8, "20:10", 505, banner=True, flight="CX0488", place=MONGKOK)]
    s.batch("group", group, 1445, 2, statement=s.statement(group, DUE["group"]))
    s.credit("group", 2870, 0)

    s.ride(801, "接机", 2, "11:20", 485, flight="HX237", place=WANCHAI)

    # The other platforms, so their chips carry counts.
    s.quick("didi", 20, "13:00", 150, 30)
    s.quick("didi", 10, "21:15", 88)
    s.quick("uber", 13, "09:45", 210, 20)
    s.quick("foodpanda", 4, "12:30", 62)


def seed(path: str, today: date) -> dict:
    """Create the database at `path`; returns targets(today)."""
    if os.path.exists(path):
        raise FileExistsError(f"{path} exists; the demo data goes into a new file only")
    db.init_db(path)
    s = _Seeder(path, today)
    # Oldest first, so the price suggestion has history by the time today's
    # orders exist and ids follow the calendar.
    legs = _months_back(s)
    _ledger(s)
    _months_back_settled(s, legs)
    _neighbours(s)
    _today(s)
    return targets(today)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", help="database file to create; must not exist")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today(),
                    help="the day the data is built around (default: today)")
    args = ap.parse_args()
    seed(args.path, args.today)
    print(f"seeded {args.path} around {args.today.isoformat()}")


if __name__ == "__main__":
    main()
