"""The settle page's statement intake: upload, read, confirm.

The reader itself is monkeypatched throughout — what is under test is the
endpoint's handling of the file, the pending token, and the refusals; OCR is
covered by test_statement_reader.py.
"""
import io
import os
import tempfile
from datetime import datetime, timedelta

import pytest

import ride_dispatch.web as web
from ride_dispatch import statement
from ride_dispatch.db import (
    init_db, save_order, update_price, insert_credit, open_batches, get_credit,
    create_settlement, statements_dir, statement_image_path,
)
from ride_dispatch.parser import Order
from ride_dispatch.statement import Statement, StatementDay, StatementRow

NOW = datetime.now()
TODAY = NOW.strftime("%Y-%m-%d")
YESTERDAY = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
TWO_DAYS = (NOW - timedelta(days=2)).strftime("%Y-%m-%d")


@pytest.fixture
def client(monkeypatch):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "orders.db")
    init_db(path)
    monkeypatch.setattr(web, "DB_PATH", path)
    web._pending_statements.clear()
    web.app.config["TESTING"] = True
    with web.app.test_client() as c:
        yield c
    web._pending_statements.clear()


def make_order(order_id, scheduled):
    return Order(order_id=order_id, service_type="送机", vehicle_type="经济5座",
                 passenger_name="TEST/USER", scheduled_time=scheduled,
                 passenger_phone="86 13800000000", overseas_phone="", flight_number="",
                 pickup="尖沙咀", dropoff="香港国际机场 T1", distance_km=30, notes="",
                 driver_notes="", additional_services="", passenger_exit_minutes=None,
                 third_party_contact="", more_contacts="", raw_message="raw")


def seed(oid, scheduled, price=210.0):
    save_order(web.DB_PATH, make_order(oid, scheduled), telegram_msg_id=1, parking=0.0,
               source="携程")
    update_price(web.DB_PATH, oid, price)


def stmt_for(rows_by_date, total):
    days = []
    for date, rows in rows_by_date.items():
        days.append(StatementDay(date=date, count=len(rows), sum=round(sum(a for _, a in rows), 2),
                                 rows=[StatementRow(date=date, order_id=oid, amount=a, time="09:00",
                                                    settle_date=date) for oid, a in rows]))
    return Statement(days=days, account="YY0000", total=total, reader="test")


def use_statement(monkeypatch, stmt):
    monkeypatch.setattr(statement, "ocr_available", lambda: True)
    monkeypatch.setattr(statement, "read_image", lambda data: stmt)


def upload(client, data=b"\xff\xd8img", name="statement.jpg"):
    return client.post("/api/statements/read",
                       data={"file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


# ---- read ----

def test_read_returns_the_report_the_bot_card_shows(client, monkeypatch):
    seed("A1", f"{TWO_DAYS} 09:00:00", 280.0)
    seed("B1", f"{YESTERDAY} 10:00:00", 210.0)
    use_statement(monkeypatch,
                  stmt_for({TWO_DAYS: [("A1", 280.0)], YESTERDAY: [("B1", 210.0)]}, 490.0))
    res = upload(client)
    assert res.status_code == 200
    body = res.get_json()
    assert body["report"].startswith("結算單 YY0000 · 2 日 2 行 · 平台 $490")
    assert "差額 $0" in body["report"]
    assert body["credit_line"] == "未收到呢筆數"
    assert body["confirm_label"] == "確認結算 · 2 程 · $490"
    assert body["can_settle"] is True
    assert body["no_orders_offer"] is None
    assert body["token"] in web._pending_statements
    # Reading writes nothing: the batch only exists after the confirm.
    assert open_batches(web.DB_PATH, "ride") == []


def test_read_names_the_credit_the_confirm_would_also_take(client, monkeypatch):
    seed("A1", f"{YESTERDAY} 09:00:00", 2540.0)
    insert_credit(web.DB_PATH, {"ref": "R1", "platform": "ride", "amount": 2540.0,
                                "currency": "HKD", "value_date": TODAY, "payer": None,
                                "memo": None, "email_id": None, "received_at": None,
                                "recorded_at": None})
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 2540.0)]}, 2540.0))
    body = upload(client).get_json()
    assert body["credit_line"].startswith("對到入數")
    assert body["confirm_label"] == "確認結算 + 對入數 · 1 程 · $2,540"


def test_read_offers_no_token_when_nothing_can_settle(client, monkeypatch):
    """A statement whose legs never reached the system has no batch to write,
    so there is no confirm to promise — only the credit to take out of the
    queue."""
    cid = insert_credit(web.DB_PATH, {"ref": "R1", "platform": "ride", "amount": 1000.0,
                                      "currency": "HKD", "value_date": TODAY, "payer": None,
                                      "memo": None, "email_id": None, "received_at": None,
                                      "recorded_at": None})
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("Z1", 600.0), ("Z2", 400.0)]}, 1000.0))
    body = upload(client).get_json()
    assert body["can_settle"] is False
    assert body["token"] is None and body["confirm_label"] == ""
    assert body["no_orders_offer"] == {"credit_id": cid, "label": "收埋入數（單未入系統）"}
    assert web._pending_statements == {}


def test_read_rejects_an_empty_upload(client, monkeypatch):
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 210.0)]}, 210.0))
    assert client.post("/api/statements/read", data={},
                       content_type="multipart/form-data").status_code == 400
    assert upload(client, data=b"").status_code == 400


def test_read_rejects_an_oversize_upload(client, monkeypatch):
    """The cap is enforced on what is read, not on a declared length."""
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 210.0)]}, 210.0))
    res = upload(client, data=b"\xff\xd8" + b"x" * web.MAX_STATEMENT_BYTES)
    assert res.status_code == 413
    assert res.get_json()["error"] == "張圖大過 10 MB"
    assert web._pending_statements == {}


def test_read_reports_an_unreadable_image_and_keeps_it(client, monkeypatch, caplog):
    """The upload is the only copy of the bytes a reader bug can be reproduced
    from, and it is gone as soon as the operator closes the sheet."""
    use_statement(monkeypatch, Statement(days=[], warnings=["image could not be decoded"]))
    with caplog.at_level("WARNING", logger="web"):
        res = upload(client, data=b"\x89PNGrest", name="IMG_0001.PNG")
    assert res.status_code == 400
    assert res.get_json()["error"] == "讀唔到張圖（image could not be decoded）— 再上載一次"
    kept = os.listdir(os.path.join(statements_dir(web.DB_PATH), "failed"))
    assert len(kept) == 1 and kept[0].endswith("-IMG_0001_PNG.png")
    assert "statement unreadable" in caplog.text and "name=IMG_0001.PNG" in caplog.text


def test_read_says_so_when_ocr_is_missing(client, monkeypatch):
    monkeypatch.setattr(statement, "ocr_available", lambda: False)
    res = upload(client)
    assert res.status_code == 503 and res.get_json()["error"] == "OCR 未裝，讀唔到張圖"


# ---- confirm ----

def test_confirm_writes_the_batch_and_stores_the_image(client, monkeypatch):
    seed("A1", f"{TWO_DAYS} 09:00:00", 280.0)
    seed("B1", f"{YESTERDAY} 10:00:00", 210.0)
    use_statement(monkeypatch,
                  stmt_for({TWO_DAYS: [("A1", 280.0)], YESTERDAY: [("B1", 210.0)]}, 490.0))
    token = upload(client).get_json()["token"]

    res = client.post("/api/statements/confirm", json={"token": token})
    assert res.status_code == 200
    body = res.get_json()
    b = open_batches(web.DB_PATH, "ride")[0]
    assert body["settlement_id"] == b["id"]
    assert body["text"].startswith(f"已結算 批次 #{b['id']}")
    assert "確認無誤" in body["text"]
    assert b["confirmed_amount"] == 490.0
    assert b["statement"]["reader"] == "test"
    assert os.path.exists(statement_image_path(web.DB_PATH, b["id"], "jpg"))
    assert web._pending_statements == {}


def test_confirm_takes_the_credit_the_card_named(client, monkeypatch):
    seed("A1", f"{YESTERDAY} 09:00:00", 2540.0)
    cid = insert_credit(web.DB_PATH, {"ref": "R1", "platform": "ride", "amount": 2540.0,
                                      "currency": "HKD", "value_date": TODAY, "payer": None,
                                      "memo": None, "email_id": None, "received_at": None,
                                      "recorded_at": None})
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 2540.0)]}, 2540.0))
    token = upload(client).get_json()["token"]
    body = client.post("/api/statements/confirm", json={"token": token}).get_json()
    credit = get_credit(web.DB_PATH, cid)
    assert [a["settlement_id"] for a in credit["allocations"]] == [body["settlement_id"]]
    assert body["text"].endswith(f"批次 #{body['settlement_id']} 收齊 · 已到帳 {TODAY[5:]}")


def test_an_unknown_token_is_expired(client):
    res = client.post("/api/statements/confirm", json={"token": "nope"})
    assert res.status_code == 410 and res.get_json()["error"] == "已過期，再上載一次"
    assert client.post("/api/statements/confirm", json={}).status_code == 410


def test_a_second_confirm_finds_the_token_spent(client, monkeypatch):
    seed("A1", f"{YESTERDAY} 09:00:00", 280.0)
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 280.0)]}, 280.0))
    token = upload(client).get_json()["token"]
    assert client.post("/api/statements/confirm", json={"token": token}).status_code == 200
    second = client.post("/api/statements/confirm", json={"token": token})
    assert second.status_code == 410
    assert len(open_batches(web.DB_PATH, "ride")) == 1


def test_confirm_reports_a_leg_that_was_settled_since_the_read(client, monkeypatch):
    """create_settlement revalidates everything, so a stale token cannot write
    a wrong batch — it says which leg is in the way."""
    seed("A1", f"{YESTERDAY} 09:00:00", 280.0)
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 280.0)]}, 280.0))
    token = upload(client).get_json()["token"]
    create_settlement(web.DB_PATH, "ride", ["A1"], 280.0, YESTERDAY)

    res = client.post("/api/statements/confirm", json={"token": token})
    assert res.status_code == 409
    assert res.get_json()["error"] == "結算唔到：A1: 已經結算咗"
    assert len(open_batches(web.DB_PATH, "ride")) == 1


def test_a_pending_read_expires(client, monkeypatch):
    seed("A1", f"{YESTERDAY} 09:00:00", 280.0)
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 280.0)]}, 280.0))
    token = upload(client).get_json()["token"]
    read_at, prepared, image = web._pending_statements[token]
    web._pending_statements[token] = (read_at - web.PENDING_TTL - 1, prepared, image)

    res = client.post("/api/statements/confirm", json={"token": token})
    assert res.status_code == 410
    assert open_batches(web.DB_PATH, "ride") == []


def test_a_new_read_sweeps_what_has_expired(client, monkeypatch):
    """The sweep runs on access rather than on a timer: an abandoned upload
    holds its screenshot in memory only until the next one arrives."""
    seed("A1", f"{YESTERDAY} 09:00:00", 280.0)
    seed("A2", f"{TWO_DAYS} 09:00:00", 280.0)
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 280.0)]}, 280.0))
    stale = upload(client).get_json()["token"]
    read_at, prepared, image = web._pending_statements[stale]
    web._pending_statements[stale] = (read_at - web.PENDING_TTL - 1, prepared, image)

    use_statement(monkeypatch, stmt_for({TWO_DAYS: [("A2", 280.0)]}, 280.0))
    fresh = upload(client).get_json()["token"]
    assert list(web._pending_statements) == [fresh]
