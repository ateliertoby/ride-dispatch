"""HKIA car park visits: API client and the decisions made from them.

Everything here is driven by HKIA's undocumented online-payment endpoints
(the ones its own payment page calls). They are public and unauthenticated
but can change without notice; callers treat a ParkingError as "no parking
information right now", never as a reason to stop the flight poller.
"""
import base64
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

from .flight import landing_datetime
from .service import is_flight_pickup

# The config below is read at import time and this module is imported before
# the bot calls load_dotenv(), so .env has to be loaded here as well.
# load_dotenv is idempotent and never overrides a real environment variable.
load_dotenv()

FREE_MINUTES = 30          # leave within this and the visit is free (once per 24h)
FREE_WINDOW_HOURS = 24     # rolling, from the free visit's entry time
HOUR_MINUTES = 60
# Published hourly tariff. Used only to preview a cost before any link is
# generated; every amount actually sent to the gateway comes from the API.
HOURLY_FEE = 32
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


BASE_URL = "https://parking.hongkongairport.com"
QUERY_PATH = "/api/booking/getOnlinePayInfo"
STORE_PATH = "/api/booking/storeOnlinePayment"
GATEWAY_PATH = "/api/booking/payDollarParametersForIntegration"
# Values the HKIA page sends as-is: 3 = identify the car by plate (ANPR),
# channel "1" = the public web channel.
ENTRY_METHOD = 3
CHANNEL = "1"

CAR_PLATE = os.environ.get("CAR_PLATE", "").strip().upper()
# Optional: HKIA's pay path never validates it, and an empty address is
# accepted. It only labels their own confirmation screen and receipt mail.
PARKING_EMAIL = os.environ.get("PARKING_EMAIL", "").strip()


def is_configured() -> bool:
    return bool(CAR_PLATE)


class ParkingClient:
    def __init__(self, plate: str, email: str, transport=None, timeout: float = 15):
        self.plate = plate.strip().upper()
        self.email = email
        self._transport = transport
        self._timeout = timeout

    async def _post(self, path: str, body: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                resp = await client.post(BASE_URL + path, json=body)
        except httpx.HTTPError as e:
            raise ParkingError(f"{path}: {e}") from e
        # A 412 carries the legitimate "not inside" body; the JSON decides.
        try:
            return resp.json()
        except ValueError as e:
            raise ParkingError(f"{path}: non-JSON reply (HTTP {resp.status_code})") from e

    async def _query(self, schedule_exit=None, location=None, pv_nr=None) -> ParkingStatus:
        body = {
            "entryMethod": ENTRY_METHOD,
            "cardNumber": self.plate,
            "parkingLocation": location,
            "PvNr": pv_nr,
            "scheduleExit": schedule_exit,
            "channel": CHANNEL,
            "carPlateNo": self.plate,
        }
        return parse_status(await self._post(QUERY_PATH, body))

    async def query(self) -> ParkingStatus:
        return await self._query()

    async def fee_for_exit(self, status: ParkingStatus, scheduled_exit: datetime) -> ParkingStatus:
        return await self._query(api_time(scheduled_exit), status.location, status.pv_nr)

    async def create_payment(self, status: ParkingStatus, scheduled_exit: datetime,
                             amount: float, now: datetime) -> dict:
        body = {
            "entryMethod": ENTRY_METHOD,
            "cardNo": self.plate,
            "carPlateNo": self.plate,
            "parkingLocation": status.location,
            "entryDateTime": api_time(from_db_time(status.entry_time)),
            "exitDateTime": api_time(scheduled_exit),
            "paymentAmt": amount,
            "paymentCurrency": "HKD",
            "emailAddress": self.email,
            "channel": int(CHANNEL),
            "timeStamp": db_time(now),
            "PvNr": status.pv_nr,
        }
        reply = await self._post(STORE_PATH, body)
        if reply.get("resultCode") != 200 or not reply.get("refNoForPay"):
            raise ParkingError(f"storeOnlinePayment: {reply!r}")
        return {
            "payment_ref": reply["paymentRefNo"],
            "order_ref": reply["refNoForPay"],
            "secure_hash": reply["secureHash"],
            "process_time": now.strftime("%Y%m%d%H%M%S"),
        }

    async def gateway_params(self) -> dict:
        reply = await self._post(GATEWAY_PATH, {"channel": int(CHANNEL), "function": "onlinePayment"})
        if not reply.get("paymentGatwayUrl") or not reply.get("merchantId"):
            raise ParkingError(f"gateway params: {reply!r}")
        return reply

    async def pay_link(self, status: ParkingStatus, scheduled_exit: datetime,
                       amount: float, now: datetime) -> tuple[str, dict]:
        payment = await self.create_payment(status, scheduled_exit, amount, now)
        gateway = await self.gateway_params()
        return build_pay_url(gateway, payment, amount, self.email), payment


def callback_token(action: str, status_word: str, email: str, payment_ref: str, process_time: str) -> str:
    # HKIA's page encodes its own post-payment routing into the callback
    # URLs; the gateway bounces the browser back to that page with the token
    # and the page reads it. Reproduced byte for byte.
    raw = (f"action={action}&email={email}&paymentNo={payment_ref}"
           f"&processTime={process_time}&function=onlinepayment&status={status_word}")
    return base64.b64encode(raw.encode()).decode()


def build_pay_url(gateway: dict, payment: dict, amount: float, email: str, lang: str = "C") -> str:
    # PayDollar's form endpoint accepts the same fields as a GET, which is
    # what lets a Telegram button open the payment page directly.
    def cb(key: str, action: str, status_word: str) -> str:
        base = gateway[key].replace("/en/", "/tc/")
        tok = callback_token(action, status_word, email, payment["payment_ref"], payment["process_time"])
        return f"{base}?token={tok}&lang=tc"

    amount_str = f"{amount:g}"
    params = [
        ("merchantId", gateway["merchantId"]),
        ("orderRef", payment["order_ref"]),
        ("amount", amount_str),
        ("currCode", gateway.get("currCode", "344")),
        ("payType", gateway.get("payType", "N")),
        ("payMethod", gateway.get("payMethod", "ALL")),
        ("mpsMode", ""),
        ("lang", lang),
        ("secureHash", payment["secure_hash"]),
        ("successUrl", cb("successUrl", "CONFIRMED", "pay-success")),
        ("failUrl", cb("failUrl", "FAILED", "fail")),
        ("cancelUrl", cb("cancelUrl", "CANCELLED", "cancel")),
    ]
    return gateway["paymentGatwayUrl"] + "?" + urlencode(params)
