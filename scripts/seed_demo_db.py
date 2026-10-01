"""Build a synthetic database that puts every visual state of the two pages on screen.

    python scripts/seed_demo_db.py PATH [--today YYYY-MM-DD]

Every value is invented. Rows are written through ride_dispatch.db's own
functions, the ones the bot and the web app use, so the database is one the
application could have produced. Dates are offsets from `today`, and nothing
else varies, so the same `today` always gives the same database: screenshots
taken from two builds can be compared pixel for pixel.

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
BATCH = {"paid": 1, "short": 2, "awaiting": 3, "held_back": 4, "ahead": 5, "group": 6}
CREDIT = {"paid": 1, "short": 2, "exact": 3, "partial": 4, "archived": 5, "group": 6}
ORDER = {
    "done_pickup": _oid(1),
    "landed_banner": _oid(2),
    "upcoming_hotel": _oid(3),
    "dropoff": _oid(4),
    "unpriced": _oid(5),
    "cancelled": _oid(6),
    "short_unpaid_leg": _oid(204),
    "held_trip": _oid(601),
}


def day(today: date, back: int) -> str:
    """The date `back` days before today, as the database writes dates."""
    return (today - timedelta(days=back)).isoformat()


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

    def quick(self, kind: str, back: int, hhmm: str, price: float, toll: float = 0.0) -> str:
        service = {"didi": "滴滴", "uber": "Uber", "foodpanda": "foodpanda"}[kind]
        order_id = f"{kind}_{day(self.today, back).replace('-', '')}{hhmm.replace(':', '')}_demo"
        db.save_quick_order(self.path, order_id, service, f"{day(self.today, back)} {hhmm}:00",
                            price, toll, source=service)
        return order_id

    def batch(self, key: str, order_ids: list[str], confirmed: float, settled_back: int, **kw) -> int:
        sid = db.create_settlement(self.path, "ride", order_ids, confirmed,
                                   day(self.today, settled_back), now=self.now, **kw)
        if sid != BATCH[key]:
            raise RuntimeError(f"batch {key} got id {sid}, expected {BATCH[key]}")
        return sid

    def credit(self, key: str, amount: float, back: int) -> int:
        cid = db.insert_credit(self.path, {
            "ref": f"DEMO-REF-{CREDIT[key]:04d}", "platform": "ride", "amount": amount,
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
    statement = {
        "account": "DEMO00", "total": 1376.55, "reader": "demo",
        "days": [
            {"date": day(s.today, 34), "count": 3, "sum": 896.55, "rows": [
                {"order_id": paid[0], "amount": 540.0, "time": "09:20", "settle_date": day(s.today, 31)},
                {"order_id": paid[0], "amount": -63.45, "time": "09:20", "settle_date": day(s.today, 31)},
                {"order_id": paid[1], "amount": 420.0, "time": "15:10", "settle_date": day(s.today, 31)}]},
            {"date": day(s.today, 33), "count": 1, "sum": 480.0, "rows": [
                {"order_id": paid[2], "amount": 480.0, "time": "13:05", "settle_date": day(s.today, 31)}]},
        ],
    }
    s.batch("paid", paid, 1376.55, 31, penalties={fined: 63.45}, statement=statement, image=_png())
    db.allocate(s.path, s.credit("paid", 1376.55, 29), BATCH["paid"])

    # Paid short: the platform left one leg out, and the operator has named it.
    short = [s.ride(201, "接机", 27, "08:50", 520, banner=True, flight="HX237", place=TST),
             s.ride(202, "送机", 27, "17:40", 400, place=SHATIN),
             s.ride(203, "接机", 26, "11:30", 460, flight="CX0488", place=WANCHAI),
             s.ride(204, "单程接送", 26, "19:15", 380, place=TST),
             s.ride(205, "接机", 25, "14:00", 510, flight="UO623", place=MONGKOK)]
    s.batch("short", short, 2310, 23)
    db.allocate(s.path, s.credit("short", 1930, 21), BATCH["short"])
    db.mark_unpaid(s.path, BATCH["short"], [ORDER["short_unpaid_leg"]])

    # Confirmed for less than the system expected, and still waiting for money.
    awaiting = [s.ride(301, "接机", 19, "10:40", 470, banner=True, flight="CX0488", place=TST),
                s.ride(302, "送机", 19, "18:20", 390, place=WANCHAI),
                s.ride(303, "接机", 18, "12:10", 390, flight="HX237", place=SHATIN)]
    s.batch("awaiting", awaiting, 1270, 16)
    # A credit nobody has matched yet, agreeing with that batch to the cent.
    s.credit("exact", 1270, 14)

    # A leg the platform held back and settled with a later statement, so the
    # batch's dates are not consecutive.
    held_back = [s.ride(401, "接机", 15, "09:00", 440, flight="UO623", place=MONGKOK),
                 s.ride(402, "接机", 12, "13:30", 530, banner=True, flight="CX0488", place=TST),
                 s.ride(403, "送机", 11, "07:45", 410, place=WANCHAI),
                 s.ride(404, "接站", 11, "16:00", 360, place=TST)]
    s.batch("held_back", held_back, 1780, 9)
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
    s.batch("ahead", ahead, 1425, 2, adjustments=[
        {"order_ref": ORDER["held_trip"], "date": day(s.today, 3), "amount": 40.0, "ahead": True}])

    # Confirmed the same day as the batch above; one transfer pays both.
    group = [s.ride(701, "接机", 9, "08:15", 495, flight="UO623", place=TST),
             s.ride(702, "送机", 8, "10:30", 405, place=WANCHAI),
             s.ride(703, "接机", 8, "20:10", 505, banner=True, flight="CX0488", place=MONGKOK)]
    s.batch("group", group, 1445, 2)
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
    _ledger(s)
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
