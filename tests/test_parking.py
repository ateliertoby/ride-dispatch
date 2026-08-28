import asyncio
import base64
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs

import httpx
import pytest

from ride_dispatch.parking import (
    ParkingError, ParkingStatus, parse_status, api_time, from_api_time,
    db_time, from_db_time, db_seconds, from_db_seconds,
    free_available, next_free_at, pay_plan, classify,
    arming_orders, is_armed, pick_order, FREE_MINUTES,
    ParkingClient, callback_token, build_pay_url, BASE_URL,
)
from ride_dispatch.flight import landing_datetime

# Real replies captured 2026-08-23.
INSIDE_UNPAID = {
    "requestDateTime": "202608231855", "resultCode": 200, "count": 1, "resultMessage": "",
    "infoList": [{"alreadyPaid": 0, "parkingLocation": "P4O", "parkingName": "Car Park 4",
                  "carPlateNo": "YY3953", "entryTime": "202608231848", "parkTime": 6,
                  "timestamp": "2026-08-23 18:54", "pvNr": 212022,
                  "scheduledExit": "2026-08-23 18:54", "fee": 0, "count": 0,
                  "breakdownList": [], "disable": 0}],
}
INSIDE_PAID = {
    "requestDateTime": "202608231903", "resultCode": 200, "count": 1, "resultMessage": "",
    "infoList": [{"alreadyPaid": 1, "parkingLocation": "P4O", "parkingName": "Car Park 4",
                  "carPlateNo": "YY3953", "entryTime": "202608231848", "parkTime": 15,
                  "timestamp": "2026-08-23 19:03", "pvNr": 212022,
                  "scheduledExit": "2026-08-23 19:03", "fee": 0, "count": 0,
                  "breakdownList": [], "disable": 0}],
}
FEE_FOR_EXIT = {
    "requestDateTime": "202608231859", "resultCode": 200, "count": 1, "resultMessage": "",
    "infoList": [{"alreadyPaid": 0, "parkingLocation": "P4O", "parkingName": "Car Park 4",
                  "carPlateNo": "YY3953", "entryTime": "202608231848", "parkTime": 11,
                  "timestamp": "2026-08-23 18:59", "pvNr": 212022,
                  "scheduledExit": "2026-08-23 19:48", "fee": 32, "count": 1,
                  "breakdownList": [{"unitDescription": "Hourly", "unitPrice": 32,
                                     "quantity": 1, "unitAmount": 32}], "disable": 0}],
}
NOT_INSIDE = {"requestDateTime": "202608231343", "resultCode": 401, "infoList": [],
              "resultMessage": "Record Error Found", "count": 0}

NOW = datetime(2026, 8, 23, 19, 0)


def test_parse_inside_unpaid():
    s = parse_status(INSIDE_UNPAID)
    assert s.inside is True
    assert s.pv_nr == 212022
    assert s.location == "P4O" and s.location_name == "Car Park 4"
    assert s.entry_time == "2026-08-23 18:48"
    assert s.park_minutes == 6
    assert s.paid is False
    assert s.fee == 0


def test_parse_inside_paid_and_fee_reply():
    assert parse_status(INSIDE_PAID).paid is True
    fee = parse_status(FEE_FOR_EXIT)
    assert fee.fee == 32 and fee.scheduled_exit == "2026-08-23 19:48"


def test_parse_missing_fee_is_none_not_zero():
    # A missing fee means "HKIA did not say", which classify has to tell apart
    # from a fee of zero, which means "leaving now is free".
    body = {"resultCode": 200, "count": 1,
            "infoList": [{k: v for k, v in INSIDE_UNPAID["infoList"][0].items() if k != "fee"}]}
    assert parse_status(body).fee is None
    assert parse_status(INSIDE_UNPAID).fee == 0


def test_parse_not_inside_is_a_status_not_an_error():
    s = parse_status(NOT_INSIDE)
    assert s.inside is False and s.pv_nr is None


@pytest.mark.parametrize("body", [{}, {"resultCode": 500}, {"resultCode": 200, "infoList": []}, "junk"])
def test_parse_unknown_shapes_raise(body):
    with pytest.raises(ParkingError):
        parse_status(body)


def test_time_conversions_round_trip():
    dt = datetime(2026, 8, 23, 18, 48)
    assert api_time(dt) == "202608231848"
    assert from_api_time("202608231848") == dt
    assert db_time(dt) == "2026-08-23 18:48"
    assert from_db_time("2026-08-23 18:48") == dt
    seen = datetime(2026, 8, 23, 18, 48, 22)
    assert db_seconds(seen) == "2026-08-23 18:48:22"
    assert from_db_seconds("2026-08-23 18:48:22") == seen


def test_free_available_rolling_24h():
    assert free_available([], NOW)
    assert not free_available([NOW - timedelta(hours=23, minutes=59)], NOW)
    assert free_available([NOW - timedelta(hours=24)], NOW)          # exactly 24h: available again
    assert free_available([NOW - timedelta(hours=30)], NOW)


def test_next_free_at_is_latest_free_entry_plus_24h():
    assert next_free_at([]) is None
    a = NOW - timedelta(hours=30)
    b = NOW - timedelta(hours=5)
    assert next_free_at([a, b]) == b + timedelta(hours=24)


@pytest.mark.parametrize("minute,hours", [(10, 1), (50, 1), (60, 1), (61, 2), (70, 2), (125, 3)])
def test_pay_plan_rounds_elapsed_time_up_to_whole_hours(minute, hours):
    entry = datetime(2026, 8, 23, 18, 48)
    h, exit_at = pay_plan(entry, entry + timedelta(minutes=minute))
    assert h == hours
    assert exit_at == entry + timedelta(hours=hours)


def test_classify_paid_wins_over_every_reading():
    assert classify(True, 10, 0) == "paid"
    assert classify(True, 90, 32) == "paid"
    assert classify(True, 90, None) == "paid"


def test_classify_follows_hkia_fee_not_the_stay_length():
    # A stay well past FREE_MINUTES that HKIA still prices at nothing is free;
    # the 30-minute rule would have called it 閘口找數.
    assert classify(False, 33, 0) == "free"
    assert classify(False, 33, 0.0) == "free"
    assert classify(False, 5, 32) == "gate"
    assert classify(False, 200, 64) == "gate"


def test_classify_falls_back_to_the_minute_rule_without_a_reading():
    assert classify(False, FREE_MINUTES, None) == "free"
    assert classify(False, FREE_MINUTES + 1, None) == "gate"


def order(**kw):
    base = {"order_id": "O1", "service_type": "接机", "flight_number": "CA727",
            "status": "active", "scheduled_time": "2026-08-23 19:30:00",
            "passenger_exit_minutes": 30, "flight_eta": "19:00", "flight_scheduled": "18:50",
            "flight_status": "est"}
    base.update(kw)
    return base


def test_landing_datetime_prefers_eta_then_scheduled_then_derived():
    assert landing_datetime(order()) == datetime(2026, 8, 23, 19, 0)
    assert landing_datetime(order(flight_eta=None)) == datetime(2026, 8, 23, 18, 50)
    assert landing_datetime(order(flight_eta=None, flight_scheduled=None)) == datetime(2026, 8, 23, 19, 0)
    assert landing_datetime(order(flight_eta=None, flight_scheduled=None, passenger_exit_minutes=None)) is None


def test_is_armed_by_status_or_window():
    landing = datetime(2026, 8, 23, 19, 0)
    assert is_armed([order(flight_status="landed")], landing - timedelta(hours=1))
    assert is_armed([order(flight_status="gate")], landing - timedelta(hours=1))
    assert is_armed([order()], landing - timedelta(minutes=30))
    assert not is_armed([order()], landing - timedelta(minutes=31))
    assert is_armed([order()], landing + timedelta(hours=2))
    assert not is_armed([order()], landing + timedelta(hours=2, minutes=1))
    assert not is_armed([order(flight_status="landed")], landing + timedelta(hours=2, minutes=1))


def test_is_armed_ignores_non_pickups_cancelled_and_no_flight():
    landing = datetime(2026, 8, 23, 19, 0)
    assert not is_armed([order(service_type="送机")], landing)
    assert not is_armed([order(status="cancelled")], landing)
    assert not is_armed([order(flight_number="")], landing)
    assert not is_armed([], landing)


def test_pick_order_closest_landing_to_entry():
    a = order(order_id="A", flight_eta="18:30")
    b = order(order_id="B", flight_eta="19:10")
    assert pick_order([a, b], datetime(2026, 8, 23, 19, 5))["order_id"] == "B"
    assert pick_order([], datetime(2026, 8, 23, 19, 5)) is None


STORE_REPLY = {"resultCode": 200, "resultMessage": "", "requestDateTime": "2026-08-23T10:59:46.497Z",
               "paymentRefNo": "PPR9WOO9V", "refNoForPay": "PPR9WOO9V20260823105946409",
               "secureHash": "1d5edb3d09a7e5912f3b2e1c8cf2ec51c8442c0d"}
GATEWAY_REPLY = {"merchantId": "88615152",
                 "paymentGatwayUrl": "https://www.paydollar.com/b2c2/eng/payment/payForm.jsp",
                 "currCode": "344", "payType": "N", "payMethod": "ALL",
                 "cancelUrl": "https://www.hongkongairport.com/en/transport/parking/car-park-booking.page",
                 "failUrl": "https://www.hongkongairport.com/en/transport/parking/car-park-booking.page",
                 "successUrl": "https://www.hongkongairport.com/en/transport/parking/car-park-booking.page",
                 "resultCode": "200"}


def _transport(routes: dict):
    """routes: path -> (status, json) or callable(request) -> httpx.Response."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content or b"{}")))
        r = routes[request.url.path]
        if callable(r):
            return r(request)
        status, body = r
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler), calls


def test_query_sends_plate_and_parses_inside():
    transport, calls = _transport({"/api/booking/getOnlinePayInfo": (200, INSIDE_UNPAID)})
    c = ParkingClient("yy3953", "me@example.com", transport=transport)
    s = asyncio.run(c.query())
    assert s.inside and s.pv_nr == 212022
    path, body = calls[0]
    assert body["carPlateNo"] == "YY3953" and body["cardNumber"] == "YY3953"
    assert body["entryMethod"] == 3 and body["channel"] == "1" and body["scheduleExit"] is None


def test_query_treats_412_not_inside_as_status():
    transport, _ = _transport({"/api/booking/getOnlinePayInfo": (412, NOT_INSIDE)})
    c = ParkingClient("YY3953", "me@example.com", transport=transport)
    assert asyncio.run(c.query()).inside is False


def test_query_raises_on_network_and_garbage():
    def boom(request):
        raise httpx.ConnectError("down")
    transport, _ = _transport({"/api/booking/getOnlinePayInfo": boom})
    with pytest.raises(ParkingError):
        asyncio.run(ParkingClient("YY3953", "x", transport=transport).query())
    transport, _ = _transport({"/api/booking/getOnlinePayInfo": lambda r: httpx.Response(200, text="<html>")})
    with pytest.raises(ParkingError):
        asyncio.run(ParkingClient("YY3953", "x", transport=transport).query())


def test_fee_for_exit_fills_location_pvnr_and_schedule():
    transport, calls = _transport({"/api/booking/getOnlinePayInfo": (200, FEE_FOR_EXIT)})
    c = ParkingClient("YY3953", "x", transport=transport)
    status = parse_status(INSIDE_UNPAID)
    fee = asyncio.run(c.fee_for_exit(status, datetime(2026, 8, 23, 19, 48)))
    assert fee.fee == 32
    _, body = calls[0]
    assert body["scheduleExit"] == "202608231948" and body["PvNr"] == 212022 and body["parkingLocation"] == "P4O"


def test_create_payment_body_and_result():
    transport, calls = _transport({"/api/booking/storeOnlinePayment": (200, STORE_REPLY)})
    c = ParkingClient("YY3953", "me@example.com", transport=transport)
    status = parse_status(INSIDE_UNPAID)
    now = datetime(2026, 8, 23, 18, 59, 46)
    p = asyncio.run(c.create_payment(status, datetime(2026, 8, 23, 19, 48), 32, now))
    _, body = calls[0]
    assert body == {"entryMethod": 3, "cardNo": "YY3953", "carPlateNo": "YY3953", "parkingLocation": "P4O",
                    "entryDateTime": "202608231848", "exitDateTime": "202608231948", "paymentAmt": 32,
                    "paymentCurrency": "HKD", "emailAddress": "me@example.com", "channel": 1,
                    "timeStamp": "2026-08-23 18:59", "PvNr": 212022}
    assert p == {"payment_ref": "PPR9WOO9V", "order_ref": "PPR9WOO9V20260823105946409",
                 "secure_hash": "1d5edb3d09a7e5912f3b2e1c8cf2ec51c8442c0d", "process_time": "20260823185946"}


def test_create_payment_raises_on_bad_result_code():
    transport, _ = _transport({"/api/booking/storeOnlinePayment": (200, {"resultCode": 500})})
    with pytest.raises(ParkingError):
        asyncio.run(ParkingClient("YY3953", "x", transport=transport)
                    .create_payment(parse_status(INSIDE_UNPAID), datetime(2026, 8, 23, 19, 48), 32, NOW))


def test_callback_token_matches_hkia_format():
    tok = callback_token("CONFIRMED", "pay-success", "me@example.com", "PPR9WOO9V", "20260823185946")
    decoded = base64.b64decode(tok).decode()
    assert decoded == ("action=CONFIRMED&email=me@example.com&paymentNo=PPR9WOO9V"
                       "&processTime=20260823185946&function=onlinepayment&status=pay-success")


def test_build_pay_url_is_get_with_every_form_field():
    payment = {"payment_ref": "PPR9WOO9V", "order_ref": "PPR9WOO9V20260823105946409",
               "secure_hash": "abc", "process_time": "20260823185946"}
    url = build_pay_url(GATEWAY_REPLY, payment, 32, "me@example.com")
    parsed = urlparse(url)
    assert parsed.netloc == "www.paydollar.com" and parsed.path == "/b2c2/eng/payment/payForm.jsp"
    q = parse_qs(parsed.query, keep_blank_values=True)
    assert q["merchantId"] == ["88615152"] and q["orderRef"] == ["PPR9WOO9V20260823105946409"]
    assert q["amount"] == ["32"] and q["currCode"] == ["344"] and q["payType"] == ["N"]
    assert q["payMethod"] == ["ALL"] and q["mpsMode"] == [""] and q["lang"] == ["C"]
    assert q["secureHash"] == ["abc"]
    for key, status_word in (("successUrl", "pay-success"), ("failUrl", "fail"), ("cancelUrl", "cancel")):
        cb = urlparse(q[key][0])
        assert cb.path == "/tc/transport/parking/car-park-booking.page"
        cbq = parse_qs(cb.query)
        assert cbq["lang"] == ["tc"]
        assert base64.b64decode(cbq["token"][0]).decode().endswith(f"&status={status_word}")


def test_pay_link_chains_store_and_gateway():
    transport, calls = _transport({"/api/booking/storeOnlinePayment": (200, STORE_REPLY),
                                   "/api/booking/payDollarParametersForIntegration": (200, GATEWAY_REPLY)})
    c = ParkingClient("YY3953", "me@example.com", transport=transport)
    url, payment = asyncio.run(c.pay_link(parse_status(INSIDE_UNPAID), datetime(2026, 8, 23, 19, 48), 32, NOW))
    assert url.startswith("https://www.paydollar.com/") and payment["payment_ref"] == "PPR9WOO9V"
    assert [p for p, _ in calls] == ["/api/booking/storeOnlinePayment", "/api/booking/payDollarParametersForIntegration"]
    assert calls[1][1] == {"channel": 1, "function": "onlinePayment"}
