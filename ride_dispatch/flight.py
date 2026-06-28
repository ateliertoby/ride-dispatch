import logging
import httpx

logger = logging.getLogger(__name__)

HKIA_URL = "https://www.hongkongairport.com/flightinfo-rest/rest/flights"


def normalize_flight_no(s: str) -> str:
    return s.replace(" ", "").upper()


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


def match_flights(orders: list[dict], arrivals: list[dict]) -> dict[str, str]:
    lookup = {}
    for flight in arrivals:
        status = flight.get("status", "")
        if not status:
            continue
        for f in flight.get("flight", []):
            key = normalize_flight_no(f.get("no", ""))
            if key:
                lookup[key] = status

    result = {}
    for order in orders:
        key = normalize_flight_no(order["flight_number"])
        if key in lookup:
            result[order["order_id"]] = lookup[key]
    return result
