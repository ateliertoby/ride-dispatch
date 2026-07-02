import re
from datetime import datetime, timedelta

import httpx

HKIA_URL = "https://www.hongkongairport.com/flightinfo-rest/rest/flights"

TRACK_BUFFER_HOURS = 3
WATCHDOG_INTERVAL = 600
FALLBACK_INTERVAL = 1800
# ±12h mirrors _attach_date's red-eye convention: covers late-night pickups
# and delayed flights, rejects the adjacent day's same-numbered leg (~24h off).
MATCH_WINDOW_HOURS = 12

_TIME_RE = re.compile(r"(\d{2}:\d{2})")


def normalize_flight_no(s: str) -> str:
    return s.replace(" ", "").upper()


def svc_time(arrival_hhmm: str | None, exit_minutes: int) -> str | None:
    # Arrival time can legitimately be unknown (est→gate jump carries no eta);
    # returning None beats crashing the whole notify cycle on int("?").
    if not arrival_hhmm or not _TIME_RE.fullmatch(arrival_hhmm):
        return None
    total = int(arrival_hhmm[:2]) * 60 + int(arrival_hhmm[3:5]) + exit_minutes
    return f"{total // 60 % 24:02d}:{total % 60:02d}"


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
    # Even with span=1 the API returns adjacent days (verified 2026-07-02:
    # requesting 07-02 returned 07-01 + 07-02). Tag each flight with its
    # day so match_flights can disambiguate daily-repeating flight numbers.
    flights = []
    for day in resp.json():
        day_date = day.get("date", date_str)
        for f in day.get("list", []):
            flights.append({**f, "date": day_date})
    return flights


def _arrival_dt(info: dict) -> datetime | None:
    try:
        day = datetime.strptime(info.get("date", ""), "%Y-%m-%d")
    except ValueError:
        return None
    m = _TIME_RE.search(info.get("scheduled") or "")
    if m:
        return day.replace(hour=int(m.group(1)[:2]), minute=int(m.group(1)[3:5]))
    return day.replace(hour=12)


def match_flights(orders: list[dict], arrivals: list[dict]) -> dict[str, dict]:
    # Flight numbers repeat every day and the feed spans multiple days, so a
    # bare flight_no lookup can hit the wrong day's leg (MU5017 2026-07-02).
    # Keep every candidate and pick the one closest to the order's pickup
    # time; anything beyond MATCH_WINDOW_HOURS is a different day's flight
    # and matching nothing is safer than matching it.
    lookup: dict[str, list[dict]] = {}
    for flight in arrivals:
        parsed = parse_status(flight.get("status", ""))
        info = {
            "date": flight.get("date", ""),
            "scheduled": flight.get("time", ""),
            "hall": flight.get("hall", ""),
            "baggage": flight.get("baggage", ""),
            **parsed,
        }
        for f in flight.get("flight", []):
            key = normalize_flight_no(f.get("no", ""))
            if key:
                lookup.setdefault(key, []).append(info)

    result = {}
    for order in orders:
        candidates = lookup.get(normalize_flight_no(order["flight_number"]))
        if not candidates:
            continue
        try:
            pickup = datetime.strptime(order["scheduled_time"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError, TypeError):
            continue
        best = None
        best_diff = None
        for c in candidates:
            dt = _arrival_dt(c)
            if dt is None:
                continue
            diff = abs((dt - pickup).total_seconds())
            if best_diff is None or diff < best_diff:
                best, best_diff = c, diff
        if best is not None and best_diff <= MATCH_WINDOW_HOURS * 3600:
            result[order["order_id"]] = best
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
