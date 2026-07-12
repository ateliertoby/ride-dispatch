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
