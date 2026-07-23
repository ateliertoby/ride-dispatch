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
DRIVE_MINUTES = 40
EXIT_URGENT_MAX = 20
EXIT_TIGHT_MAX = 30

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


def exit_urgency(minutes: int | None) -> str | None:
    # 0 means "missing" throughout this codebase (see svc_reminder_due), not a band.
    if not minutes or minutes < 0:
        return None
    if minutes <= EXIT_URGENT_MAX:
        return "urgent"
    if minutes <= EXIT_TIGHT_MAX:
        return "tight"
    return None


def predicted_landing_hhmm(order: dict) -> str | None:
    for key in ("flight_eta", "flight_scheduled"):
        v = order.get(key)
        if v and _TIME_RE.fullmatch(v):
            return v
    # No flight data: the platform books pickup at scheduled landing + exit
    # buffer, so walking the exit minutes back recovers a landing estimate.
    exit_min = order.get("passenger_exit_minutes")
    if not exit_min:
        return None
    try:
        sched = datetime.strptime(order.get("scheduled_time") or "", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    total = sched.hour * 60 + sched.minute - exit_min
    return f"{total // 60 % 24:02d}:{total % 60:02d}"


def depart_hhmm(order: dict) -> str | None:
    """When the driver should leave for the airport: landing + exit - drive."""
    exit_min = order.get("passenger_exit_minutes")
    if not exit_min:
        return None
    return svc_time(predicted_landing_hhmm(order), exit_min - DRIVE_MINUTES)


def effective_service_time(order: dict) -> str:
    """Sort key: predicted passenger walk-out time for 接机, else booked time.

    Returns a "%Y-%m-%d %H:%M:%S" string so lexicographic sort equals
    chronological sort, and 接机 orders with flight data mix correctly
    with orders that only have a booked scheduled_time.
    """
    sched_str = order.get("scheduled_time") or ""
    if order.get("service_type") != "接机":
        return sched_str

    landing_hhmm = None
    for key in ("flight_eta", "flight_scheduled"):
        v = order.get(key)
        if v and _TIME_RE.fullmatch(v):
            landing_hhmm = v
            break
    if not landing_hhmm:
        return sched_str

    try:
        sched_dt = datetime.strptime(sched_str, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return sched_str

    landing_dt = sched_dt.replace(
        hour=int(landing_hhmm[:2]),
        minute=int(landing_hhmm[3:5]),
        second=0,
    )
    # Midnight crossing: HKIA ETAs are bare HH:MM. Normally the ETA is
    # close to or earlier than the booked time (booked = landing + exit
    # buffer). A gap > 12h means the flight lands just after midnight on
    # the next calendar day (e.g. booked 23:50, ETA 00:10). Only the
    # forward shift applies — no reverse (mirrors dashboard doneAt()).
    if (sched_dt - landing_dt).total_seconds() > 12 * 3600:
        landing_dt += timedelta(days=1)

    exit_min = order.get("passenger_exit_minutes") or 0
    service_dt = landing_dt + timedelta(minutes=exit_min)
    return service_dt.strftime("%Y-%m-%d %H:%M:%S")


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
    if status.startswith("Cancelled"):
        return {"eta": None, "gate": None, "status": "cancelled"}
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
            "raw_status": flight.get("status", ""),
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
    if all(o.get("flight_status") in ("gate", "cancelled") for o in tracking):
        return WATCHDOG_INTERVAL

    min_seconds = float("inf")
    urgent_soon = False
    for o in tracking:
        if o.get("flight_status") in ("gate", "cancelled"):
            continue
        arrival = o.get("flight_eta") or o.get("flight_scheduled")
        if not arrival:
            continue
        sched = datetime.strptime(o["scheduled_time"], "%Y-%m-%d %H:%M:%S")
        target = _attach_date(arrival, sched)
        seconds = (target - now).total_seconds()
        min_seconds = min(min_seconds, seconds)
        # Reminders fire before the fetch inside a tick, so a depart push uses
        # the previous tick's ETA — near landing that staleness must stay
        # small for urgent-exit orders.
        exit_min = o.get("passenger_exit_minutes")
        if exit_min and exit_min <= EXIT_URGENT_MAX and seconds <= 3600:
            urgent_soon = True

    if min_seconds == float("inf"):
        interval = FALLBACK_INTERVAL
    else:
        interval = max(60, int(min_seconds / 2))
    if urgent_soon:
        interval = min(interval, 300)
    return interval


# ---- Reminder pure logic (no IO) ----

_DEPARTURE_TYPES = ('送机', '单程接送')


def svc_reminder_due(order: dict, now: datetime) -> str | None:
    """Return svc HH:MM if the 用車時間 reminder should fire, else None."""
    if order.get('service_type') != '接机':
        return None
    if order.get('flight_status') not in ('landed', 'gate'):
        return None
    sent = set(filter(None, (order.get('reminders_sent') or '').split(',')))
    if 'svc' in sent:
        return None
    eta = order.get('flight_eta')
    exit_min = order.get('passenger_exit_minutes')
    if not eta or not exit_min:
        return None
    svc_hhmm = svc_time(eta, exit_min)
    if not svc_hhmm:
        return None
    sched = datetime.strptime(order['scheduled_time'], '%Y-%m-%d %H:%M:%S')
    svc_dt = _attach_date(svc_hhmm, sched)
    if now < svc_dt:
        return None
    if (now - svc_dt).total_seconds() >= 7200:
        return None  # staleness guard
    return svc_hhmm


def depart_reminder_due(order: dict, now: datetime) -> str | None:
    """Return depart HH:MM if the departure reminder should fire, else None.

    Unlike svc_reminder_due this must fire pre-landing, so any non-cancelled
    flight status (including no flight data at all) qualifies.
    """
    if order.get('service_type') != '接机':
        return None
    if order.get('flight_status') == 'cancelled':
        return None
    sent = set(filter(None, (order.get('reminders_sent') or '').split(',')))
    if 'depart' in sent:
        return None
    hhmm = depart_hhmm(order)
    if not hhmm:
        return None
    sched = datetime.strptime(order['scheduled_time'], '%Y-%m-%d %H:%M:%S')
    due = _attach_date(hhmm, sched)
    if now < due:
        return None
    if (now - due).total_seconds() >= 7200:
        return None  # staleness guard
    return hhmm


def departure_milestones_due(order: dict, now: datetime) -> list[str]:
    """Return list of milestone tags (dep30, dep10) that should fire now."""
    if order.get('service_type') not in _DEPARTURE_TYPES:
        return []
    sched = datetime.strptime(order['scheduled_time'], '%Y-%m-%d %H:%M:%S')
    sent = set(filter(None, (order.get('reminders_sent') or '').split(',')))
    result = []
    for tag, minutes in [('dep30', 30), ('dep10', 10)]:
        if tag in sent:
            continue
        if now >= sched - timedelta(minutes=minutes) and now < sched:
            result.append(tag)
    return result


def pending_reminder_times(orders: list[dict], now: datetime) -> list[datetime]:
    """Return future datetimes when pending reminders will come due."""
    times: list[datetime] = []
    for o in orders:
        sent = set(filter(None, (o.get('reminders_sent') or '').split(',')))
        # svc reminder
        if o.get('service_type') == '接机' and o.get('flight_status') in ('landed', 'gate'):
            if 'svc' not in sent:
                eta = o.get('flight_eta')
                exit_min = o.get('passenger_exit_minutes')
                if eta and exit_min:
                    svc_hhmm = svc_time(eta, exit_min)
                    if svc_hhmm:
                        sched = datetime.strptime(o['scheduled_time'], '%Y-%m-%d %H:%M:%S')
                        svc_dt = _attach_date(svc_hhmm, sched)
                        if svc_dt > now:
                            times.append(svc_dt)
        # depart reminder (接机; any non-cancelled status, pre-landing included)
        if o.get('service_type') == '接机' and o.get('flight_status') != 'cancelled':
            if 'depart' not in sent:
                hhmm = depart_hhmm(o)
                if hhmm:
                    sched = datetime.strptime(o['scheduled_time'], '%Y-%m-%d %H:%M:%S')
                    due = _attach_date(hhmm, sched)
                    if due > now:
                        times.append(due)
        # departure milestones
        if o.get('service_type') in _DEPARTURE_TYPES:
            sched = datetime.strptime(o['scheduled_time'], '%Y-%m-%d %H:%M:%S')
            for tag, minutes in [('dep30', 30), ('dep10', 10)]:
                if tag not in sent:
                    due = sched - timedelta(minutes=minutes)
                    if due > now:
                        times.append(due)
    return times


def clamp_interval(interval: int, pending_times: list[datetime], now: datetime) -> int:
    """Clamp poll interval so the next tick fires before the earliest pending reminder."""
    if not pending_times:
        return interval
    earliest = min(pending_times)
    seconds_until = (earliest - now).total_seconds()
    if seconds_until <= 0:
        return interval  # due now, fires this tick
    return max(30, min(interval, int(seconds_until)))
