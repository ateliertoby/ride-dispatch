import os
import tempfile
import pytest
import ride_dispatch.web as web
from ride_dispatch.db import init_db, save_quick_order, get_orders_by_date, get_order_by_id

PIN = "4321"


@pytest.fixture
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    monkeypatch.setattr(web, "DB_PATH", path)
    monkeypatch.setattr(web, "WEB_PIN", PIN)
    web._auth_fails.clear()
    web.app.config["TESTING"] = True
    with web.app.test_client() as c:
        yield c
    os.unlink(path)


def auth_headers(client) -> dict:
    token = client.post("/api/auth", json={"pin": PIN}).get_json()["token"]
    return {"Authorization": f"Bearer {token}"}


def seed_order(order_id="Q1", scheduled="2026-07-01 14:30:00"):
    save_quick_order(web.DB_PATH, order_id, "滴滴", scheduled, 200.0, 50.0, source="滴滴")


# ---- auth ----

def test_auth_right_pin_returns_token(client):
    res = client.post("/api/auth", json={"pin": PIN})
    assert res.status_code == 200
    assert len(res.get_json()["token"]) == 64


def test_auth_wrong_pin_401(client):
    assert client.post("/api/auth", json={"pin": "0000"}).status_code == 401


def test_auth_rate_limited_after_failures(client):
    for _ in range(5):
        client.post("/api/auth", json={"pin": "0000"})
    res = client.post("/api/auth", json={"pin": PIN})
    assert res.status_code == 429


def test_auth_unconfigured_pin_403(client, monkeypatch):
    monkeypatch.setattr(web, "WEB_PIN", "")
    assert client.post("/api/auth", json={"pin": ""}).status_code == 403
    assert client.post("/api/orders", json={}).status_code == 403


def test_write_without_token_401(client):
    assert client.post("/api/orders", json={}).status_code == 401
    assert client.patch("/api/orders/X", json={"price": 1}).status_code == 401


def test_write_with_bad_token_401(client):
    headers = {"Authorization": "Bearer deadbeef"}
    assert client.post("/api/orders", json={}, headers=headers).status_code == 401


# ---- create quick order ----

def test_create_didi(client):
    res = client.post(
        "/api/orders",
        json={"type": "didi", "date": "2026-07-01", "time": "14:30", "price": 250, "tunnel_fee": 50},
        headers=auth_headers(client),
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
    headers = auth_headers(client)
    client.post("/api/orders", json={"type": "uber", "date": "2026-07-01", "time": "09:05", "price": 180, "tunnel_fee": 30}, headers=headers)
    client.post("/api/orders", json={"type": "foodpanda", "date": "2026-07-01", "time": "12:00", "price": 55}, headers=headers)
    rows = get_orders_by_date(web.DB_PATH, "2026-07-01")
    by_type = {r["service_type"]: r for r in rows}
    assert by_type["Uber"]["price"] == 180
    assert by_type["foodpanda"]["price"] == 55
    assert by_type["foodpanda"]["tunnel_fee"] == 0
    assert by_type["foodpanda"]["source"] == "foodpanda"


def test_create_rejects_bad_input(client):
    headers = auth_headers(client)
    base = {"type": "didi", "date": "2026-07-01", "time": "14:30", "price": 250}
    assert client.post("/api/orders", json={**base, "type": "taxi"}, headers=headers).status_code == 400
    assert client.post("/api/orders", json={**base, "date": "2026-13-01"}, headers=headers).status_code == 400
    assert client.post("/api/orders", json={**base, "time": "25:00"}, headers=headers).status_code == 400
    assert client.post("/api/orders", json={**base, "time": "1430"}, headers=headers).status_code == 400
    assert client.post("/api/orders", json={**base, "price": "abc"}, headers=headers).status_code == 400
    assert client.post("/api/orders", json={**base, "price": -5}, headers=headers).status_code == 400
    assert client.post("/api/orders", json={"type": "didi", "date": "2026-07-01", "time": "14:30"}, headers=headers).status_code == 400


def test_created_order_ids_unique_same_minute(client):
    headers = auth_headers(client)
    body = {"type": "didi", "date": "2026-07-01", "time": "14:30", "price": 100}
    ids = {client.post("/api/orders", json=body, headers=headers).get_json()["order_id"] for _ in range(5)}
    assert len(ids) == 5


# ---- patch order ----

def test_patch_price_and_fees(client):
    seed_order()
    res = client.patch(
        "/api/orders/Q1",
        json={"price": 300, "tunnel_fee": 0, "parking_fee": 32, "banner_fee": 40},
        headers=auth_headers(client),
    )
    assert res.status_code == 200
    row = get_order_by_id(web.DB_PATH, "Q1")
    assert row["price"] == 300
    assert row["tunnel_fee"] == 0
    assert row["parking_fee"] == 32
    assert row["banner_fee"] == 40


def test_patch_time_keeps_date(client):
    seed_order()
    res = client.patch("/api/orders/Q1", json={"time": "16:45"}, headers=auth_headers(client))
    assert res.status_code == 200
    row = get_order_by_id(web.DB_PATH, "Q1")
    assert row["scheduled_time"] == "2026-07-01 16:45:00"


def test_patch_cancel(client):
    seed_order()
    res = client.patch("/api/orders/Q1", json={"status": "cancelled"}, headers=auth_headers(client))
    assert res.status_code == 200
    assert get_order_by_id(web.DB_PATH, "Q1") is None  # active-only lookup
    assert get_orders_by_date(web.DB_PATH, "2026-07-01") == []


def test_patch_rejects_bad_input(client):
    seed_order()
    headers = auth_headers(client)
    assert client.patch("/api/orders/Q1", json={"price": -1}, headers=headers).status_code == 400
    assert client.patch("/api/orders/Q1", json={"time": "9:00"}, headers=headers).status_code == 400
    assert client.patch("/api/orders/Q1", json={"status": "active"}, headers=headers).status_code == 400
    assert client.patch("/api/orders/Q1", json={}, headers=headers).status_code == 400
    assert client.patch("/api/orders/Q1", json={"flight_number": "CX100"}, headers=headers).status_code == 400
    row = get_order_by_id(web.DB_PATH, "Q1")
    assert row["price"] == 200.0 and row["scheduled_time"] == "2026-07-01 14:30:00"


def test_patch_unknown_order_404(client):
    headers = auth_headers(client)
    assert client.patch("/api/orders/NOPE", json={"price": 1}, headers=headers).status_code == 404
    assert client.patch("/api/orders/NOPE", json={"time": "10:00"}, headers=headers).status_code == 404
