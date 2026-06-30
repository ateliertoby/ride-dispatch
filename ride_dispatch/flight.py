import re
import httpx

HKIA_URL = "https://www.hongkongairport.com/flightinfo-rest/rest/flights"

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


def build_cache_entries(arrivals: list[dict]) -> list[tuple]:
    entries = []
    for flight in arrivals:
        scheduled = flight.get("time", "")
        parsed = parse_status(flight.get("status", ""))
        for f in flight.get("flight", []):
            key = normalize_flight_no(f.get("no", ""))
            if key:
                entries.append((key, scheduled, parsed["eta"], parsed["gate"], parsed["status"]))
    return entries


def match_order_from_cache(db_path: str, order_id: str, flight_number: str) -> bool:
    from .db import get_cached_arrival, update_flight_info
    cached = get_cached_arrival(db_path, normalize_flight_no(flight_number))
    if cached:
        update_flight_info(db_path, order_id, cached["scheduled"], cached["eta"], cached["gate"], cached["status"])
        return True
    return False


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
