"""HKIA car park visits: API client and the decisions made from them.

Everything here is driven by HKIA's undocumented online-payment endpoints
(the ones its own payment page calls). They are public and unauthenticated
but can change without notice; callers treat a ParkingError as "no parking
information right now", never as a reason to stop the flight poller.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .flight import landing_datetime
from .service import is_flight_pickup

FREE_MINUTES = 30          # leave within this and the visit is free (once per 24h)
FREE_WINDOW_HOURS = 24     # rolling, from the free visit's entry time
HOUR_MINUTES = 60
GRACE_MINUTES = 30         # granted after the paid-until time
AUTO_LINK_MINUTE = 50      # unpaid this long inside -> send a link unprompted
ARM_BEFORE_MINUTES = 30    # poll from this long before predicted landing
ARM_AFTER_HOURS = 2        # ...until this long after; no entry by then = not coming

API_TIME = "%Y%m%d%H%M"
DB_TIME = "%Y-%m-%d %H:%M"


class ParkingError(Exception):
    pass


@dataclass
class ParkingStatus:
    inside: bool
    pv_nr: int | None = None
    location: str | None = None
    location_name: str | None = None
    entry_time: str | None = None      # DB_TIME
    park_minutes: int | None = None
    paid: bool = False
    fee: float | None = None
    scheduled_exit: str | None = None  # DB_TIME, only meaningful on a fee query


def api_time(dt: datetime) -> str:
    return dt.strftime(API_TIME)


def from_api_time(s: str) -> datetime:
    return datetime.strptime(s, API_TIME)


def db_time(dt: datetime) -> str:
    return dt.strftime(DB_TIME)


def from_db_time(s: str) -> datetime:
    return datetime.strptime(s, DB_TIME)


def parse_status(body) -> ParkingStatus:
    # "Not inside" arrives as HTTP 412 with resultCode 401 and an empty list;
    # it is a normal answer, not a failure. Anything else unfamiliar is.
    if not isinstance(body, dict):
        raise ParkingError(f"non-object reply: {body!r}")
    code = body.get("resultCode")
    infos = body.get("infoList") or []
    if code == 401 and not infos:
        return ParkingStatus(inside=False)
    if code == 200 and infos:
        # The plate can only be in one car park; if the API ever lists more,
        # the latest entry is the live one.
        info = max(infos, key=lambda i: i.get("entryTime") or "")
        entry = info.get("entryTime")
        sched = info.get("scheduledExit")
        return ParkingStatus(
            inside=True,
            pv_nr=info.get("pvNr"),
            location=info.get("parkingLocation"),
            location_name=info.get("parkingName"),
            entry_time=db_time(from_api_time(entry)) if entry else None,
            park_minutes=info.get("parkTime"),
            paid=bool(info.get("alreadyPaid")),
            fee=info.get("fee"),
            scheduled_exit=sched[:16] if sched else None,
        )
    raise ParkingError(f"unexpected reply: {body!r}")


def free_available(free_entry_times: list[datetime], now: datetime) -> bool:
    cutoff = now - timedelta(hours=FREE_WINDOW_HOURS)
    return all(t <= cutoff for t in free_entry_times)


def next_free_at(free_entry_times: list[datetime]) -> datetime | None:
    if not free_entry_times:
        return None
    return max(free_entry_times) + timedelta(hours=FREE_WINDOW_HOURS)


def pay_plan(entry: datetime, now: datetime) -> tuple[int, datetime]:
    # Whole hours elapsed so far, rounded up, minimum one: the scheduled exit
    # must never already be in the past when the link is generated.
    elapsed = max(0, int((now - entry).total_seconds() // 60))
    hours = max(1, math.ceil(elapsed / HOUR_MINUTES))
    return hours, entry + timedelta(hours=hours)


def classify(paid: bool, entry: datetime, exit: datetime) -> str:
    if paid:
        return "paid"
    stayed = (exit - entry).total_seconds() / 60
    return "free" if stayed <= FREE_MINUTES else "gate"


def _trackable(o: dict) -> bool:
    return (
        is_flight_pickup(o.get("service_type") or "")
        and bool(o.get("flight_number"))
        and (o.get("status") or "active") == "active"
    )


def arming_orders(orders: list[dict], now: datetime) -> list[dict]:
    armed = []
    for o in orders:
        if not _trackable(o):
            continue
        landing = landing_datetime(o)
        if landing is None:
            continue
        if now > landing + timedelta(hours=ARM_AFTER_HOURS):
            continue
        by_status = o.get("flight_status") in ("landed", "gate")
        by_time = now >= landing - timedelta(minutes=ARM_BEFORE_MINUTES)
        if by_status or by_time:
            armed.append(o)
    return armed


def is_armed(orders: list[dict], now: datetime) -> bool:
    return bool(arming_orders(orders, now))


def pick_order(orders: list[dict], entry: datetime) -> dict | None:
    best, best_gap = None, None
    for o in orders:
        landing = landing_datetime(o)
        if landing is None:
            continue
        gap = abs((landing - entry).total_seconds())
        if best is None or gap < best_gap:
            best, best_gap = o, gap
    return best
