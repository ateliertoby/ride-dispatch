import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
import pytest
import ride_dispatch.web as web
from ride_dispatch.db import init_db, save_quick_order, get_orders_by_date, get_order_by_id


@pytest.fixture
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    monkeypatch.setattr(web, "DB_PATH", path)
    web.app.config["TESTING"] = True
    with web.app.test_client() as c:
        yield c
    os.unlink(path)


def seed_order(order_id="Q1", scheduled="2026-07-01 14:30:00"):
    save_quick_order(web.DB_PATH, order_id, "滴滴", scheduled, 200.0, 50.0, source="滴滴")


# ---- write ops work without auth ----

def test_create_without_auth_succeeds(client):
    res = client.post(
        "/api/orders",
        json={"type": "didi", "date": "2026-07-01", "time": "14:30", "price": 250, "tunnel_fee": 50},
    )
    assert res.status_code == 201


def test_patch_without_auth_succeeds(client):
    seed_order()
    res = client.patch("/api/orders/Q1", json={"price": 300})
    assert res.status_code == 200


# ---- create quick order ----

def test_create_didi(client):
    res = client.post(
        "/api/orders",
        json={"type": "didi", "date": "2026-07-01", "time": "14:30", "price": 250, "tunnel_fee": 50},
    )
    assert res.status_code == 201
    order_id = res.get_json()["order_id"]
    assert order_id.startswith("didi_202607011430_")
    rows = get_orders_by_date(web.DB_PATH, "2026-07-01")
    assert len(rows) == 1
    assert rows[0]["service_type"] == "滴滴"
    assert rows[0]["source"] == "滴滴"
    assert rows[0]["scheduled_time"] == "2026-07-01 14:30:00"
    assert rows[0]["price"] == 250
    assert rows[0]["tunnel_fee"] == 50


def test_create_uber_and_foodpanda(client):
    client.post("/api/orders", json={"type": "uber", "date": "2026-07-01", "time": "09:05", "price": 180, "tunnel_fee": 30})
    client.post("/api/orders", json={"type": "foodpanda", "date": "2026-07-01", "time": "12:00", "price": 55})
    rows = get_orders_by_date(web.DB_PATH, "2026-07-01")
    by_type = {r["service_type"]: r for r in rows}
    assert by_type["Uber"]["price"] == 180
    assert by_type["foodpanda"]["price"] == 55
    assert by_type["foodpanda"]["tunnel_fee"] == 0
    assert by_type["foodpanda"]["source"] == "foodpanda"


def test_create_rejects_bad_input(client):
    base = {"type": "didi", "date": "2026-07-01", "time": "14:30", "price": 250}
    assert client.post("/api/orders", json={**base, "type": "taxi"}).status_code == 400
    assert client.post("/api/orders", json={**base, "date": "2026-13-01"}).status_code == 400
    assert client.post("/api/orders", json={**base, "time": "25:00"}).status_code == 400
    assert client.post("/api/orders", json={**base, "time": "1430"}).status_code == 400
    assert client.post("/api/orders", json={**base, "price": "abc"}).status_code == 400
    assert client.post("/api/orders", json={**base, "price": -5}).status_code == 400
    assert client.post("/api/orders", json={"type": "didi", "date": "2026-07-01", "time": "14:30"}).status_code == 400


def test_created_order_ids_unique_same_minute(client):
    body = {"type": "didi", "date": "2026-07-01", "time": "14:30", "price": 100}
    ids = {client.post("/api/orders", json=body).get_json()["order_id"] for _ in range(5)}
    assert len(ids) == 5


# ---- patch order ----

def test_patch_price_and_fees(client):
    seed_order()
    res = client.patch(
        "/api/orders/Q1",
        json={"price": 300, "tunnel_fee": 0, "parking_fee": 32, "banner_fee": 40},
    )
    assert res.status_code == 200
    row = get_order_by_id(web.DB_PATH, "Q1")
    assert row["price"] == 300
    assert row["tunnel_fee"] == 0
    assert row["parking_fee"] == 32
    assert row["banner_fee"] == 40


def test_patch_time_keeps_date(client):
    seed_order()
    res = client.patch("/api/orders/Q1", json={"time": "16:45"})
    assert res.status_code == 200
    row = get_order_by_id(web.DB_PATH, "Q1")
    assert row["scheduled_time"] == "2026-07-01 16:45:00"


def test_patch_cancel(client):
    seed_order()
    res = client.patch("/api/orders/Q1", json={"status": "cancelled"})
    assert res.status_code == 200
    assert get_order_by_id(web.DB_PATH, "Q1") is None  # active-only lookup
    assert get_orders_by_date(web.DB_PATH, "2026-07-01") == []


def test_patch_rejects_bad_input(client):
    seed_order()
    assert client.patch("/api/orders/Q1", json={"price": -1}).status_code == 400
    assert client.patch("/api/orders/Q1", json={"time": "9:00"}).status_code == 400
    assert client.patch("/api/orders/Q1", json={"status": "active"}).status_code == 400
    assert client.patch("/api/orders/Q1", json={}).status_code == 400
    assert client.patch("/api/orders/Q1", json={"flight_number": "CX100"}).status_code == 400
    row = get_order_by_id(web.DB_PATH, "Q1")
    assert row["price"] == 200.0 and row["scheduled_time"] == "2026-07-01 14:30:00"


def test_patch_unknown_order_404(client):
    assert client.patch("/api/orders/NOPE", json={"price": 1}).status_code == 404
    assert client.patch("/api/orders/NOPE", json={"time": "10:00"}).status_code == 404


PASTE_MSG = """服务类型: 接机
接单车型: 经济5座
乘客姓名: WONG/SIUMING
用车时间: 2026-07-22 12:35:00
航班号: CX477
上车点: 香港国际机场1号航站楼
下车点: 九龙塘又一城
订单号: 1128000000000099
附加服务: 举牌服务
乘客出场时长: 30
乘客电话: 86 13800000003"""

# The same booking re-sent after the customer gave a full address.
PASTE_CHANGED_MSG = PASTE_MSG.replace(
    "下车点: 九龙塘又一城", "下车点: 新界坑口裕明苑裕昌閣B座\n订单里程: 49.105")

# A re-send does not have to repeat every line the first message carried; the
# contact numbers are the ones it usually drops.
PASTE_NO_PHONE_MSG = "\n".join(
    l for l in PASTE_CHANGED_MSG.splitlines() if not l.startswith("乘客电话"))


# ---- parse preview ----

def test_parse_preview(client):
    res = client.post("/api/orders/parse", json={"text": PASTE_MSG})
    assert res.status_code == 200
    data = res.get_json()
    assert data["source"] == "携程"
    assert data["order"]["order_id"] == "1128000000000099"
    assert data["order"]["service_type"] == "接机"
    assert data["parking_fee"] == 32.0
    assert data["banner_fee"] == 40.0
    assert data["duplicate"] is False


def test_parse_preview_duplicate_flag(client):
    seed_order(order_id="1128000000000099")
    res = client.post("/api/orders/parse", json={"text": PASTE_MSG})
    assert res.get_json()["duplicate"] is True


def test_parse_preview_cancelled_is_not_duplicate(client):
    seed_order(order_id="1128000000000099")
    client.patch("/api/orders/1128000000000099", json={"status": "cancelled"})
    res = client.post("/api/orders/parse", json={"text": PASTE_MSG})
    assert res.get_json()["duplicate"] is False


def test_parse_preview_reports_no_changes_for_a_new_order(client):
    data = client.post("/api/orders/parse", json={"text": PASTE_MSG}).get_json()
    assert data["changes"] == []
    assert data["locked"] is False
    assert data["current_price"] is None


def test_parse_preview_reports_changes_against_a_live_row(client):
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    data = client.post("/api/orders/parse", json={"text": PASTE_CHANGED_MSG}).get_json()
    assert data["duplicate"] is True
    assert data["current_price"] == 500.0
    assert data["changes"] == [
        {"field": "dropoff", "label": "目的地", "old": "九龙塘又一城", "new": "新界坑口裕明苑裕昌閣B座"},
        {"field": "distance_km", "label": "里程", "old": "", "new": "49.105 km"},
    ]


def test_parse_preview_ignores_fields_the_resend_omits(client):
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    data = client.post("/api/orders/parse", json={"text": PASTE_NO_PHONE_MSG}).get_json()
    # the dropped 乘客电话 line is silence, not a request to clear the number
    assert [c["field"] for c in data["changes"]] == ["dropoff", "distance_km"]


def test_parse_preview_identical_resend_has_no_changes(client):
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG})
    data = client.post("/api/orders/parse", json={"text": PASTE_MSG}).get_json()
    assert data["duplicate"] is True
    assert data["changes"] == []


def test_parse_preview_locked_when_settled(client):
    from ride_dispatch.db import create_settlement
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    create_settlement(web.DB_PATH, "ride", ["1128000000000099"], 500.0, "2026-07-23",
                      now=datetime(2026, 7, 23, 9, 0))
    data = client.post("/api/orders/parse", json={"text": PASTE_CHANGED_MSG}).get_json()
    assert data["locked"] is True


def test_parse_preview_rejects_garbage(client):
    assert client.post("/api/orders/parse", json={"text": "唔係單"}).status_code == 400
    assert client.post("/api/orders/parse", json={}).status_code == 400


TC_MSG = """订单号：TC9876543-同程用车
车型：舒适5座
用车时间：2026-07-21 09:00:00
出发地：尖沙咀九龙酒店
目的地：香港国际机场T1
乘客姓名CHAN TAI MAN
乘客手机号852-62222222
航班号：UO123"""

NO_TIME_MSG = """订单号：TC0000001-同程用车
出发地：尖沙咀九龙酒店
目的地：香港国际机场T1
乘客姓名CHAN TAI MAN"""


# ---- paste save ----

def test_paste_save_with_price(client):
    res = client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    assert res.status_code == 201
    data = res.get_json()
    assert data["order_id"] == "1128000000000099"
    assert data["date"] == "2026-07-22"
    row = get_order_by_id(web.DB_PATH, "1128000000000099")
    assert row["source"] == "携程"
    assert row["price"] == 500
    assert row["parking_fee"] == 32.0
    assert row["banner_fee"] == 40.0
    assert row["telegram_msg_id"] is None


def test_paste_save_without_price(client):
    res = client.post("/api/orders", json={"type": "paste", "text": TC_MSG})
    assert res.status_code == 201
    row = get_order_by_id(web.DB_PATH, "TC9876543")
    assert row["price"] is None
    assert row["source"] == "同程"
    assert row["parking_fee"] == 0


def test_paste_identical_resend_changes_nothing(client):
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    res = client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG})
    assert res.status_code == 200
    data = res.get_json()
    assert data["updated"] is False
    assert data["changed"] == []
    assert get_order_by_id(web.DB_PATH, "1128000000000099")["price"] == 500


def test_paste_on_a_live_order_updates_it(client):
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    res = client.post("/api/orders", json={"type": "paste", "text": PASTE_CHANGED_MSG})
    assert res.status_code == 200
    data = res.get_json()
    assert data["order_id"] == "1128000000000099"
    assert data["date"] == "2026-07-22"
    assert data["updated"] is True
    assert data["changed"] == ["目的地", "里程"]
    assert data["price_kept"] == 500.0
    row = get_order_by_id(web.DB_PATH, "1128000000000099")
    assert row["dropoff"] == "新界坑口裕明苑裕昌閣B座"
    assert row["distance_km"] == 49.105
    assert row["price"] == 500.0                    # operator-entered, kept
    assert row["parking_fee"] == 32.0
    # one row, not two
    listed = client.get("/api/orders?date=2026-07-22").get_json()["orders"]
    assert [o["order_id"] for o in listed] == ["1128000000000099"]


def test_paste_update_keeps_the_phone_the_resend_omits(client):
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    res = client.post("/api/orders", json={"type": "paste", "text": PASTE_NO_PHONE_MSG})
    assert res.status_code == 200
    assert res.get_json()["changed"] == ["目的地", "里程"]
    row = get_order_by_id(web.DB_PATH, "1128000000000099")
    assert row["passenger_phone"] == "86 13800000003"
    assert row["dropoff"] == "新界坑口裕明苑裕昌閣B座"


def test_paste_update_with_a_new_price_sets_it(client):
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    res = client.post("/api/orders", json={"type": "paste", "text": PASTE_CHANGED_MSG, "price": 620})
    assert res.status_code == 200
    assert res.get_json()["price_kept"] is None
    assert get_order_by_id(web.DB_PATH, "1128000000000099")["price"] == 620


def test_paste_on_a_settled_order_409(client):
    from ride_dispatch.db import SETTLED_LOCK_MSG, create_settlement
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    create_settlement(web.DB_PATH, "ride", ["1128000000000099"], 500.0, "2026-07-23",
                      now=datetime(2026, 7, 23, 9, 0))
    res = client.post("/api/orders", json={"type": "paste", "text": PASTE_CHANGED_MSG})
    assert res.status_code == 409
    assert res.get_json()["error"] == SETTLED_LOCK_MSG
    assert get_order_by_id(web.DB_PATH, "1128000000000099")["dropoff"] == "九龙塘又一城"


def test_paste_revives_cancelled_order(client):
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    client.patch("/api/orders/1128000000000099", json={"status": "cancelled"})
    assert get_order_by_id(web.DB_PATH, "1128000000000099") is None

    res = client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 620})
    assert res.status_code == 201
    data = res.get_json()
    assert data["order_id"] == "1128000000000099"
    assert data["revived"] is True

    listed = client.get("/api/orders?date=2026-07-22").get_json()["orders"]
    assert [o["order_id"] for o in listed] == ["1128000000000099"]
    assert listed[0]["price"] == 620


def test_paste_new_order_is_not_revived(client):
    res = client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG})
    assert res.get_json()["revived"] is False


def test_paste_rejects_bad_input(client):
    assert client.post("/api/orders", json={"type": "paste", "text": "唔係單"}).status_code == 400
    assert client.post("/api/orders", json={"type": "paste"}).status_code == 400
    assert client.post("/api/orders", json={"type": "paste", "text": NO_TIME_MSG}).status_code == 400
    assert client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": -1}).status_code == 400


def test_kick_bot_noop_when_socket_missing(client):
    web._kick_bot()  # no bot.sock next to the tmp DB; must be a silent no-op


# ---- exit-time enrichment ----


def test_parse_preview_exit_urgency_tight(client):
    res = client.post("/api/orders/parse", json={"text": PASTE_MSG})
    assert res.get_json()["exit_urgency"] == "tight"


def test_parse_preview_exit_urgency_none_without_field(client):
    res = client.post("/api/orders/parse", json={"text": TC_MSG})
    assert res.get_json()["exit_urgency"] is None


def test_orders_enriched_with_depart_time(client):
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG})
    rows = client.get("/api/orders?date=2026-07-22").get_json()["orders"]
    # No flight data yet: depart = booking 12:35 - 40 = 11:55
    assert rows[0]["depart_hhmm"] == "11:55"
    assert rows[0]["exit_urgency"] == "tight"


def test_orders_depart_follows_eta(client):
    from ride_dispatch.db import update_flight_info
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG})
    update_flight_info(web.DB_PATH, "1128000000000099", "12:00", "12:10", None, "est")
    rows = client.get("/api/orders?date=2026-07-22").get_json()["orders"]
    # eta 12:10 + 30 - 40 = 12:00
    assert rows[0]["depart_hhmm"] == "12:00"


def test_orders_enrichment_none_for_quick_orders(client):
    seed_order()
    rows = client.get("/api/orders?date=2026-07-01").get_json()["orders"]
    assert rows[0]["depart_hhmm"] is None
    assert rows[0]["exit_urgency"] is None


# ---- row time sort ----


def test_api_orders_sorted_by_row_time(client):
    """Regression: delayed EK384 (booked 18:15, eta 20:30) must sort after
    UO213 (booked 19:18, eta 18:58)."""
    import sqlite3
    from ride_dispatch.db import update_flight_info

    save_quick_order(web.DB_PATH, "EK384-order", "接机", "2026-07-23 18:15:00", 500, 0)
    update_flight_info(web.DB_PATH, "EK384-order", "18:00", "20:30", None, "est")

    save_quick_order(web.DB_PATH, "UO213-order", "接机", "2026-07-23 19:18:00", 500, 0)
    update_flight_info(web.DB_PATH, "UO213-order", "19:00", "18:58", None, "est")
    conn = sqlite3.connect(web.DB_PATH)
    conn.execute("UPDATE orders SET passenger_exit_minutes = 40 WHERE order_id = 'UO213-order'")
    conn.commit()
    conn.close()

    rows = client.get("/api/orders?date=2026-07-23").get_json()["orders"]
    assert len(rows) == 2
    assert rows[0]["order_id"] == "UO213-order"   # 18:58 < 20:30
    assert rows[1]["order_id"] == "EK384-order"


def test_api_orders_at_gate_pickup_sorts_by_the_time_it_displays(client):
    """Regression: a 接机 showing its at-gate 12:55 must not sort above a
    12:50 送机 on the strength of an ETA the row no longer shows."""
    from ride_dispatch.db import update_flight_info

    save_quick_order(web.DB_PATH, "GJ8007-order", "接机", "2026-08-24 12:55:00", 500, 0)
    update_flight_info(web.DB_PATH, "GJ8007-order", "12:55", "12:40", "12:55", "gate")
    save_quick_order(web.DB_PATH, "dropoff-order", "送机", "2026-08-24 12:50:00", 500, 0)

    rows = client.get("/api/orders?date=2026-08-24").get_json()["orders"]
    assert [r["order_id"] for r in rows] == ["dropoff-order", "GJ8007-order"]
    assert rows[0]["row_time"] == "2026-08-24 12:50:00"
    assert rows[1]["row_time"] == "2026-08-24 12:55:00"


def test_api_orders_carry_row_time(client):
    """The dashboard places its NOW line against this field, so every row
    carries it — quick orders included."""
    seed_order()
    rows = client.get("/api/orders?date=2026-07-01").get_json()["orders"]
    assert rows[0]["row_time"] == "2026-07-01 14:30:00"


# ---- pages ----


def test_settle_page_renders(client):
    res = client.get("/settle")
    assert res.status_code == 200
    assert "埋數" in res.get_data(as_text=True)


def test_dashboard_links_to_settle_page(client):
    res = client.get("/")
    assert res.status_code == 200
    assert 'href="/settle"' in res.get_data(as_text=True)


# ---- settlement ----


def seed_ride(order_id="R1", scheduled="2026-07-01 09:00:00", price=500.0, banner=40.0):
    from ride_dispatch.db import _conn
    save_quick_order(web.DB_PATH, order_id, "接机", scheduled, price, 0.0, source="携程")
    with _conn(web.DB_PATH) as conn:
        conn.execute("UPDATE orders SET banner_fee = ? WHERE order_id = ?", (banner, order_id))
        conn.commit()
    return order_id


def seed_credit(amount=540.0, value_date="2026-07-05", ref="R1", platform="ride"):
    from ride_dispatch.db import insert_credit
    return insert_credit(web.DB_PATH, {"ref": ref, "platform": platform, "amount": amount,
                                       "currency": "HKD", "value_date": value_date,
                                       "payer": "A B**** C***** L", "memo": "SUPPLIERPAY",
                                       "email_id": None, "received_at": None, "recorded_at": None})


def settle(client, month="2026-07", platform="ride"):
    res = client.get(f"/api/settle?month={month}&platform={platform}")
    assert res.status_code == 200
    return res.get_json()


# A batch is born from a statement image in the bot and nowhere else, so these
# tests create one the way that flow does rather than through an endpoint.
def create_batch(order_ids, platform="ride", confirmed=540, settled_on="2026-07-03",
                 statement=None):
    from ride_dispatch.db import create_settlement
    return create_settlement(web.DB_PATH, platform, order_ids, confirmed, settled_on,
                             statement=statement)


def test_settle_shape(client):
    seed_ride("R1")
    seed_order("D1", "2026-07-01 10:00:00")
    data = settle(client)
    assert data["month"] == "2026-07"
    assert data["platform"] == "ride"
    assert len(data["now"]) == 19
    assert [o["order_id"] for o in data["orders"]] == ["R1"]
    assert data["orders"][0]["banner_fee"] == 40.0
    assert data["orders"][0]["settlement_id"] is None
    assert data["settlements"] == []
    assert data["counts"] == {"ride": 1, "didi": 1, "uber": 0, "foodpanda": 0}
    assert data["totals"] == {"unsettled": 540.0, "awaiting": 0}


def test_settle_totals_follow_the_batch(client):
    seed_ride("R1")
    seed_ride("R2", scheduled="2026-07-02 09:00:00", price=300.0, banner=0.0)
    create_batch(["R1"], confirmed=530)
    data = settle(client)
    assert data["counts"]["ride"] == 1
    assert data["totals"] == {"unsettled": 300.0, "awaiting": 530.0}
    assert len(data["settlements"]) == 1
    batch = data["settlements"][0]
    assert batch["expected_amount"] == 540.0
    assert batch["confirmed_amount"] == 530.0
    assert [o["order_id"] for o in batch["orders"]] == ["R1"]


def test_settle_returns_straddling_batch_whole(client):
    seed_ride("JUN30", scheduled="2026-06-30 20:00:00", banner=0.0)
    seed_ride("JUL01", scheduled="2026-07-01 09:00:00", banner=0.0)
    create_batch(["JUN30", "JUL01"], confirmed=1000, settled_on="2026-07-02")
    data = settle(client)
    assert [o["order_id"] for o in data["orders"]] == ["JUL01"]
    assert [o["order_id"] for o in data["settlements"][0]["orders"]] == ["JUN30", "JUL01"]


def test_settle_rejects_bad_query(client):
    assert client.get("/api/settle?month=2026-13&platform=ride").status_code == 400
    assert client.get("/api/settle?month=2026-07-01&platform=ride").status_code == 400
    # unpadded month would LIKE-match nothing and read as an empty month
    assert client.get("/api/settle?month=2026-7&platform=ride").status_code == 400
    assert client.get("/api/settle?month=2026-07&platform=taxi").status_code == 400


def test_settle_defaults_to_this_month_and_ride(client):
    res = client.get("/api/settle")
    assert res.status_code == 200
    assert res.get_json()["platform"] == "ride"


def test_settlements_cannot_be_created_from_the_page(client):
    """A batch exists only because a statement image was read, so it traces
    back to that image and on to the orders the statement lists.  The page
    ticking legs together left no such trail, so it is gone rather than
    hidden."""
    seed_ride("R1")
    res = client.post("/api/settlements", json={
        "platform": "ride", "order_ids": ["R1"], "confirmed_amount": 540,
    })
    assert res.status_code in (404, 405)
    assert settle(client)["settlements"] == []


def test_paid_endpoint_is_gone(client):
    """The page cannot mark a batch paid any more: only a bank credit can."""
    seed_ride("R1")
    settlement_id = create_batch(["R1"])
    assert client.post(f"/api/settlements/{settlement_id}/paid", json={}).status_code in (404, 405)
    assert settle(client)["settlements"][0]["paid_on"] is None


def test_settle_carries_what_a_batch_has_received_and_still_owes(client):
    from ride_dispatch.db import allocate, mark_unpaid
    seed_ride("R1")
    seed_ride("R2", scheduled="2026-07-01 10:00:00", price=200.0, banner=0.0)
    settlement_id = create_batch(["R1", "R2"], confirmed=740)
    cid = seed_credit(amount=540.0)
    data = settle(client)
    assert data["credits"] == {"unallocated": 1, "unallocated_sum": 540.0}
    batch = data["settlements"][0]
    assert batch["state"] == "awaiting" and batch["received"] == 0 and batch["outstanding"] == 740.0
    assert batch["allocations"] == []
    allocate(web.DB_PATH, cid, settlement_id)
    mark_unpaid(web.DB_PATH, settlement_id, ["R2"])
    data = settle(client)
    batch = data["settlements"][0]
    assert batch["state"] == "partial"
    assert batch["received"] == 540.0 and batch["outstanding"] == 200.0
    assert batch["allocations"] == [{"credit_id": cid, "amount": 540.0,
                                     "value_date": "2026-07-05"}]
    assert {o["order_id"]: o["unpaid"] for o in batch["orders"]} == {"R1": 0, "R2": 1}
    # Waiting for money is the shortfall, not the whole batch.
    assert data["totals"]["awaiting"] == 200.0
    assert data["credits"] == {"unallocated": 0, "unallocated_sum": 0.0}


def test_unpaid_endpoint_200_marks_legs_and_returns_batch(client):
    from ride_dispatch.db import allocate
    seed_ride("R1", price=300.0, banner=0.0)
    seed_ride("R2", scheduled="2026-07-01 10:00:00", price=200.0, banner=0.0)
    sid = create_batch(["R1", "R2"], confirmed=500)
    cid = seed_credit(amount=300.0)
    allocate(web.DB_PATH, cid, sid)
    res = client.post(f"/api/settlements/{sid}/unpaid",
                      json={"order_ids": ["R2"]})
    assert res.status_code == 200
    batch = res.get_json()
    assert {o["order_id"]: o["unpaid"] for o in batch["orders"]} == {"R1": 0, "R2": 1}
    # The response carries the derived fields the page needs.
    assert all("platform_amount" in o for o in batch["orders"])
    assert "unpaid_guesses" in batch


def test_unpaid_endpoint_400_on_sum_mismatch(client):
    from ride_dispatch.db import allocate
    seed_ride("R1", price=300.0, banner=0.0)
    seed_ride("R2", scheduled="2026-07-01 10:00:00", price=200.0, banner=0.0)
    sid = create_batch(["R1", "R2"], confirmed=500)
    cid = seed_credit(amount=300.0)
    allocate(web.DB_PATH, cid, sid)
    res = client.post(f"/api/settlements/{sid}/unpaid",
                      json={"order_ids": ["R1"]})
    assert res.status_code == 400
    assert "剔咗" in res.get_json()["error"]


def test_unpaid_endpoint_404_unknown_batch(client):
    res = client.post("/api/settlements/999/unpaid", json={"order_ids": []})
    assert res.status_code == 404


def test_settle_carries_unpaid_guesses_and_platform_amount(client):
    from ride_dispatch.db import allocate
    seed_ride("R1", price=300.0, banner=0.0)
    seed_ride("R2", scheduled="2026-07-01 10:00:00", price=200.0, banner=0.0)
    sid = create_batch(["R1", "R2"], confirmed=500)
    cid = seed_credit(amount=300.0)
    allocate(web.DB_PATH, cid, sid)
    data = settle(client)
    batch = data["settlements"][0]
    assert batch["state"] == "partial"
    # The guess should find R2 ($200 == outstanding $200).
    assert batch["unpaid_guesses"] == [["R2"]]
    # Every order carries its platform_amount.
    amounts = {o["order_id"]: o["platform_amount"] for o in batch["orders"]}
    assert amounts == {"R1": 300.0, "R2": 200.0}


def test_settle_non_partial_batch_has_empty_guesses(client):
    seed_ride("R1")
    sid = create_batch(["R1"], confirmed=540)
    data = settle(client)
    batch = data["settlements"][0]
    assert batch["state"] == "awaiting"
    assert batch["unpaid_guesses"] == []


def test_fingerprint_tracks_the_credit_ledger(client):
    """An open settle page has to repaint when a credit lands, is archived, is
    put against a batch, or a leg is marked unpaid — none of which changes an
    order or a batch row."""
    from ride_dispatch.db import allocate, archive_credit, mark_unpaid
    seed_ride("R1", price=300.0, banner=0.0)
    seed_ride("R2", scheduled="2026-07-01 10:00:00", price=200.0, banner=0.0)
    settlement_id = create_batch(["R1", "R2"], confirmed=500)
    before = web._fingerprint()
    cid = seed_credit(amount=300.0)
    landed = web._fingerprint()
    assert landed != before
    allocate(web.DB_PATH, cid, settlement_id)
    put = web._fingerprint()
    assert put != landed
    mark_unpaid(web.DB_PATH, settlement_id, ["R2"])
    ticked = web._fingerprint()
    assert ticked != put
    archive_credit(web.DB_PATH, cid, "pre-system", "2026-07-06")
    assert web._fingerprint() != ticked


def test_web_does_not_pull_in_telegram(tmp_path):
    """The dashboard process has never needed the bot library and must not
    start needing it: credits.py keeps the text, bot.py owns the buttons.
    A fresh interpreter, because the test run has telegram imported already."""
    env = {**os.environ, "RIDE_DB_PATH": str(tmp_path / "orders.db")}
    done = subprocess.run(
        [sys.executable, "-c", "import ride_dispatch.web, sys; assert 'telegram' not in sys.modules"],
        capture_output=True, text=True, env=env, cwd=os.path.dirname(os.path.dirname(__file__)))
    assert done.returncode == 0, done.stderr


# ---- the credits view ----


def test_credits_endpoint_carries_the_whole_ledger_with_its_states(client):
    from ride_dispatch.db import archive_credit, allocate
    seed_ride("R1", scheduled="2026-07-01 09:00:00", price=500.0, banner=40.0)
    seed_ride("R2", scheduled="2026-07-02 09:00:00", price=300.0, banner=0.0)
    paid = create_batch(["R1"], confirmed=540)
    part = create_batch(["R2"], confirmed=300, settled_on="2026-07-04")
    done = seed_credit(amount=540.0, value_date="2026-07-05", ref="C1")
    partial = seed_credit(amount=1000.0, value_date="2026-07-06", ref="C2")
    seed_credit(amount=1200.0, value_date="2026-06-05", ref="C3")
    gone = seed_credit(amount=99.0, value_date="2026-06-01", ref="C4")
    allocate(web.DB_PATH, done, paid)
    allocate(web.DB_PATH, partial, part)
    archive_credit(web.DB_PATH, gone, "pre-system", "2026-07-06")

    data = client.get("/api/credits?platform=ride").get_json()
    assert data["platform"] == "ride"
    assert data["counts"] == {"open": 1, "partial": 1, "done": 1, "archived": 1}
    # open counts what is still owed, not what landed: a part-linked credit
    # contributes only its remainder.
    assert data["sums"] == {"open": 1900.0, "done": 540.0}
    # Oldest value date first: the page groups them, the API does not.
    assert [c["ref"] for c in data["credits"]] == ["C4", "C3", "C1", "C2"]
    by_ref = {c["ref"]: c for c in data["credits"]}
    assert [by_ref[r]["state"] for r in ("C1", "C2", "C3", "C4")] == [
        "done", "partial", "open", "archived"]
    assert by_ref["C2"]["remaining"] == 700.0 and by_ref["C2"]["allocated"] == 300.0
    assert by_ref["C4"]["archived_reason"] == "pre-system"
    assert by_ref["C1"]["memo"] == "SUPPLIERPAY"
    assert by_ref["C3"]["batches"] == []
    assert by_ref["C1"]["batches"] == [{"id": paid, "confirmed_amount": 540.0,
                                        "amount": 540.0, "state": "paid",
                                        "dates": ["2026-07-01"], "orders": 1,
                                        "has_image": False, "outstanding": 0.0}]


def test_credits_endpoint_carries_payer_and_what_a_batch_is_still_owed(client):
    """The credit sheet names who paid and, for a batch the credit only partly
    covered, what that batch is still owed; neither is re-derived on the page."""
    from ride_dispatch.db import allocate
    seed_ride("R1", scheduled="2026-07-01 09:00:00", price=500.0, banner=40.0)
    seed_ride("R2", scheduled="2026-07-02 09:00:00", price=800.0, banner=0.0)
    whole = create_batch(["R1"], confirmed=540)
    short = create_batch(["R2"], confirmed=800, settled_on="2026-07-04")
    cid = seed_credit(amount=1040.0, value_date="2026-07-05", ref="C1")
    allocate(web.DB_PATH, cid, whole)
    allocate(web.DB_PATH, cid, short, 500.0)

    credit = client.get("/api/credits?platform=ride").get_json()["credits"][0]
    assert credit["payer"] == "A B**** C***** L"
    assert [(b["id"], b["state"], b["outstanding"]) for b in credit["batches"]] == [
        (whole, "paid", 0.0), (short, "partial", 300.0)]


def test_credits_endpoint_reports_the_batch_that_has_a_statement_image(client):
    from ride_dispatch.db import create_settlement, allocate
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    sid = create_settlement(web.DB_PATH, "ride", ["1128000000000099"], 500.0, "2026-07-23",
                            now=datetime(2026, 7, 23, 9, 0), statement=STATEMENT_JSON,
                            image=b"\xff\xd8x")
    cid = seed_credit(amount=500.0, value_date="2026-07-24")
    allocate(web.DB_PATH, cid, sid)
    batch = client.get("/api/credits").get_json()["credits"][0]["batches"][0]
    assert batch["has_image"] is True and batch["orders"] == 1
    assert batch["dates"] == ["2026-07-22"]


def test_credits_endpoint_is_empty_and_platform_scoped(client):
    seed_credit(amount=100.0, ref="U1", platform="uber")
    data = client.get("/api/credits").get_json()
    assert data["credits"] == [] and data["counts"]["open"] == 0
    assert data["sums"] == {"open": 0.0, "done": 0.0}
    assert [c["ref"] for c in client.get("/api/credits?platform=uber").get_json()["credits"]] == ["U1"]
    assert client.get("/api/credits?platform=taxi").status_code == 400


def test_the_writes_a_batch_accepts_are_undo_and_unpaid(client):
    """Batches come from statements and paid means linked to a bank credit, so
    neither creating one nor marking one paid is something the page can do.
    Naming the unpaid legs of a short-paid batch is the only other write."""
    writes = {(r.rule, m) for r in web.app.url_map.iter_rules()
              for m in r.methods - {"GET", "HEAD", "OPTIONS"}}
    assert {r for r, _ in writes if r.startswith("/api/settlements")} == {
        "/api/settlements/<int:settlement_id>",
        "/api/settlements/<int:settlement_id>/unpaid"}


def test_settle_page_exposes_only_the_actions_that_remain(client):
    """These attribute names are the full set of what the page can do, so a
    new action has to be added here deliberately."""
    page = client.get("/settle").get_data(as_text=True)
    assert set(re.findall(r"data-([a-z]+)=", page)) == {
        "back", "bar", "bl", "chip", "close", "copy", "credit", "credits", "d", "f",
        "fold", "upbatch", "upguess", "uptick", "upsave",
        "undo", "undogo"}


def test_allocating_the_whole_batch_pays_it(client):
    from ride_dispatch.db import allocate
    seed_ride("R1")
    settlement_id = create_batch(["R1"], confirmed=530)
    allocate(web.DB_PATH, seed_credit(amount=530.0), settlement_id)
    data = settle(client)
    assert data["settlements"][0]["paid_on"] == "2026-07-05"
    assert data["totals"]["awaiting"] == 0


def test_a_credits_row_carries_what_it_paid_of_each_batch(client):
    """A credit that paid part of a batch is a different row from one that
    paid all of it: the page needs both figures to say which."""
    from ride_dispatch.db import allocate
    seed_ride("R1", price=3000.0, banner=0.0)
    settlement_id = create_batch(["R1"], confirmed=3000)
    part = seed_credit(amount=2540.0, ref="C1")
    allocate(web.DB_PATH, part, settlement_id)
    batches = client.get("/api/credits").get_json()["credits"][0]["batches"]
    assert batches == [{"id": settlement_id, "confirmed_amount": 3000.0, "amount": 2540.0,
                        "state": "partial", "dates": ["2026-07-01"], "orders": 1,
                        "has_image": False, "outstanding": 460.0}]


def test_delete_settlement_endpoint(client):
    seed_ride("R1")
    settlement_id = create_batch(["R1"])
    assert client.delete(f"/api/settlements/{settlement_id}").status_code == 200
    data = settle(client)
    assert data["settlements"] == []
    assert data["orders"][0]["settlement_id"] is None
    assert data["counts"]["ride"] == 1


def test_delete_settlement_unknown_id(client):
    assert client.delete("/api/settlements/999").status_code == 404


def test_patch_batched_order_rejected(client):
    seed_ride("R1")
    create_batch(["R1"])
    res = client.patch("/api/orders/R1", json={"price": 600})
    assert res.status_code == 400
    assert res.get_json()["error"] == "已結算嘅單要先撤銷結算"
    assert client.patch("/api/orders/R1", json={"status": "cancelled"}).status_code == 400
    assert get_order_by_id(web.DB_PATH, "R1")["price"] == 500.0


def test_patch_batched_order_allows_parking(client):
    seed_ride("R1")
    create_batch(["R1"])
    assert client.patch("/api/orders/R1", json={"parking_fee": 32}).status_code == 200
    assert get_order_by_id(web.DB_PATH, "R1")["parking_fee"] == 32


def test_api_orders_carry_settlement_columns(client):
    seed_ride("R1")
    rows = client.get("/api/orders?date=2026-07-01").get_json()["orders"]
    assert rows[0]["settlement_paid_on"] is None
    assert rows[0]["settlement_settled_on"] is None
    settlement_id = create_batch(["R1"])
    from ride_dispatch.db import allocate
    allocate(web.DB_PATH, seed_credit(), settlement_id)
    rows = client.get("/api/orders?date=2026-07-01").get_json()["orders"]
    assert rows[0]["settlement_settled_on"] == "2026-07-03"
    assert rows[0]["settlement_paid_on"] == "2026-07-05"


def test_fingerprint_tracks_settlements(client):
    seed_ride("R1")
    before = web._fingerprint()
    settlement_id = create_batch(["R1"])
    created = web._fingerprint()
    assert created != before
    from ride_dispatch.db import allocate
    allocate(web.DB_PATH, seed_credit(), settlement_id)
    paid = web._fingerprint()
    assert paid != created
    client.delete(f"/api/settlements/{settlement_id}")
    assert web._fingerprint() != paid


def test_fingerprint_distinguishes_a_resettle(client):
    """Undo empties the book back to its pre-settlement fingerprint, so the
    batch id is what keeps a re-settle from looking like no change at all."""
    seed_ride("R1")
    first = create_batch(["R1"])
    created = web._fingerprint()
    client.delete(f"/api/settlements/{first}")
    create_batch(["R1"])
    assert web._fingerprint() != created


# ---- statement on a batch ----

STATEMENT_JSON = {
    "account": "YY0000", "total": 500.0, "reader": "test",
    "days": [{"date": "2026-07-01", "count": 2, "sum": 500.0,
              "rows": [{"order_id": "Q1", "amount": 250.0, "time": "14:30", "settle_date": "2026-07-03",
                        "read_as": "Q7"},
                       {"order_id": "EXTRA", "amount": 250.0, "time": "15:00", "settle_date": "2026-07-03"}]}],
}


def test_settle_batch_carries_statement_and_image(client):
    from ride_dispatch.db import create_settlement, save_quick_order
    # A ride-platform order: quick orders are didi/uber/foodpanda, so paste one instead.
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    sid = create_settlement(web.DB_PATH, "ride", ["1128000000000099"], 500.0, "2026-07-23",
                            now=datetime(2026, 7, 23, 9, 0), statement=STATEMENT_JSON, image=b"\xff\xd8x")
    data = client.get("/api/settle?month=2026-07&platform=ride").get_json()
    batch = next(b for b in data["settlements"] if b["id"] == sid)
    assert batch["statement"]["total"] == 500.0
    assert batch["statement_image"] is True
    res = client.get(f"/api/settlements/{sid}/image")
    assert res.status_code == 200 and res.mimetype == "image/jpeg" and res.data == b"\xff\xd8x"


def test_settle_batch_without_statement(client):
    from ride_dispatch.db import create_settlement
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    sid = create_settlement(web.DB_PATH, "ride", ["1128000000000099"], 500.0, "2026-07-23",
                            now=datetime(2026, 7, 23, 9, 0))
    data = client.get("/api/settle?month=2026-07&platform=ride").get_json()
    batch = next(b for b in data["settlements"] if b["id"] == sid)
    assert batch["statement"] is None and batch["statement_image"] is False
    assert client.get(f"/api/settlements/{sid}/image").status_code == 404
    assert client.get("/api/settlements/9999/image").status_code == 404


def test_settle_batch_image_keeps_its_own_type(client):
    from ride_dispatch.db import create_settlement
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG, "price": 500})
    png = b"\x89PNG\r\n\x1a\n" + b"x"
    sid = create_settlement(web.DB_PATH, "ride", ["1128000000000099"], 500.0, "2026-07-23",
                            now=datetime(2026, 7, 23, 9, 0), statement=STATEMENT_JSON, image=png)
    res = client.get(f"/api/settlements/{sid}/image")
    assert res.status_code == 200 and res.mimetype == "image/png" and res.data == png
