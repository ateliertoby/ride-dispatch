import re
from datetime import datetime, timedelta

import httpx

HKIA_URL = "https://www.hongkongairport.com/flightinfo-rest/rest/flights"

TRACK_BUFFER_HOURS = 3
WATCHDOG_INTERVAL = 600
FALLBACK_INTERVAL = 1800

_TIME_RE = re.compile(r"(\d{2}:\d{2})")


def normalize_flight_no(s: str) -> str:
    return s.replace(" ", "").upper()


def parse_status(status: str) -> dict:
    if status.startswith("Est at "):
        m = _TIME_RE.search(status)
        return {"eta": m.group(1) if m else None, "gate": None, "status": "est"}
    if status.startswith("Landed "):
        m = _TIME_RE.search(status)
        return {"eta": m.group(1) if m else None, "gate": None, "status": "landed"}
    if status.startswith("At gate "):
        m = _TIME_RE.search(status)
        return {"eta": None, "gate": m.group(1) if m else None, "status": "gate"}
    return {"eta": None, "gate": None, "status": None}


def fetch_arrivals(date_str: str) -> list[dict]:
    resp = httpx.get(
        HKIA_URL,
        params={"date": date_str, "arrival": "true", "cargo": "false", "lang": "en", "span": "1"},
        timeout=15,
    )
    resp.raise_for_status()
    flights = []
    for day in resp.json():
        flights.extend(day.get("list", []))
    return flights


def match_flights(orders: list[dict], arrivals: list[dict]) -> dict[str, dict]:
    lookup = {}
    for flight in arrivals:
        scheduled = flight.get("time", "")
        parsed = parse_status(flight.get("status", ""))
        info = {"scheduled": scheduled, "hall": flight.get("hall", ""), "baggage": flight.get("baggage", ""), **parsed}
        for f in flight.get("flight", []):
            key = normalize_flight_no(f.get("no", ""))
            if key:
                lookup[key] = info

    result = {}
    for order in orders:
        key = normalize_flight_no(order["flight_number"])
        if key in lookup:
            result[order["order_id"]] = lookup[key]
    return result


def _attach_date(hhmm: str, base: datetime) -> datetime:
    # HKIA times are bare HH:MM; anchor to the order's date, allowing the
    # flight to land up to 12h either side of the pickup time (red-eyes).
    dt = base.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:5]), second=0, microsecond=0)
    if dt > base + timedelta(hours=12):
        dt -= timedelta(days=1)
    elif dt < base - timedelta(hours=12):
        dt += timedelta(days=1)
    return dt


def _window_end(order: dict, sched: datetime) -> datetime:
    end = sched
    arrival = order.get("flight_eta") or order.get("flight_scheduled")
    if arrival:
        end = max(end, _attach_date(arrival, sched))
    return end + timedelta(hours=TRACK_BUFFER_HOURS)


def calc_next_interval(orders: list[dict], now: datetime | None = None) -> int | None:
    # Termination is time-based: an order stops being tracked only when its
    # window (max(pickup time, latest ETA) + buffer) expires. Status only
    # picks the tier — even a wrong 'gate' just slows polling to watchdog
    # pace, and the next poll self-corrects it.
    now = now or datetime.now()
    tracking = []
    for o in orders:
        if o.get("service_type") != "接机" or not o.get("flight_number"):
            continue
        sched = datetime.strptime(o["scheduled_time"], "%Y-%m-%d %H:%M:%S")
        if _window_end(o, sched) < now:
            continue
        tracking.append(o)

    if not tracking:
        return None
    if any(o.get("flight_status") == "landed" for o in tracking):
        return 60
    if all(o.get("flight_status") == "gate" for o in tracking):
        return WATCHDOG_INTERVAL

    min_seconds = float("inf")
    for o in tracking:
        if o.get("flight_status") == "gate":
            continue
        arrival = o.get("flight_eta") or o.get("flight_scheduled")
        if not arrival:
            continue
        sched = datetime.strptime(o["scheduled_time"], "%Y-%m-%d %H:%M:%S")
        target = _attach_date(arrival, sched)
        min_seconds = min(min_seconds, (target - now).total_seconds())

    if min_seconds == float("inf"):
        return FALLBACK_INTERVAL
    return max(60, int(min_seconds / 2))
