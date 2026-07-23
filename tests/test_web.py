import os
import tempfile
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


def test_paste_duplicate_409(client):
    client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG})
    res = client.post("/api/orders", json={"type": "paste", "text": PASTE_MSG})
    assert res.status_code == 409


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


# ---- effective service time sort ----


def test_api_orders_sorted_by_effective_service_time(client):
    """Regression: delayed EK384 (booked 18:15, eta 20:30) must sort after
    UO213 (booked 19:18, eta 18:58+40min exit = svc 19:38)."""
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
    assert rows[0]["order_id"] == "UO213-order"   # svc 19:38 < 20:30
    assert rows[1]["order_id"] == "EK384-order"
