import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import ride_dispatch.bot as bot
from ride_dispatch.db import (
    init_db, save_order, update_flight_info, get_open_parking_session, get_parking_session,
    recent_parking_sessions, get_order_by_id,
)
from ride_dispatch.parking import ParkingStatus, ParkingError
from ride_dispatch.parser import Order

ENTRY = datetime(2026, 8, 23, 18, 48)


def make_order(**overrides) -> Order:
    defaults = dict(
        order_id="O1", service_type="接机", vehicle_type="经济5座", passenger_name="WANG/XIAOMING",
        scheduled_time="2026-08-23 19:30:00", passenger_phone="86 13800000000", overseas_phone="",
        flight_number="CA727", pickup="香港国际机场 T1", dropoff="尖沙咀", distance_km=30, notes="",
        driver_notes="", additional_services="", passenger_exit_minutes=30, third_party_contact="",
        more_contacts="", raw_message="raw",
    )
    defaults.update(overrides)
    return Order(**defaults)


@pytest.fixture
def db_path(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    monkeypatch.setattr(bot, "DB_PATH", path)
    monkeypatch.setattr(bot, "_parking_miss_at", None)
    yield path
    os.unlink(path)


def inside(minutes: int, paid: bool = False, pv_nr: int = 212022) -> ParkingStatus:
    return ParkingStatus(inside=True, pv_nr=pv_nr, location="P4O", location_name="Car Park 4",
                         entry_time=ENTRY.strftime("%Y-%m-%d %H:%M"), park_minutes=minutes, paid=paid, fee=0)


OUT = ParkingStatus(inside=False)


class FakeClient:
    plate = "YY3953"

    def __init__(self, replies):
        self.replies = list(replies)
        self.links = 0

    async def query(self):
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def fee_for_exit(self, status, scheduled_exit):
        return ParkingStatus(inside=True, pv_nr=status.pv_nr, location="P4O", location_name="Car Park 4",
                             entry_time=status.entry_time, park_minutes=status.park_minutes,
                             paid=False, fee=32, scheduled_exit=scheduled_exit.strftime("%Y-%m-%d %H:%M"))

    async def pay_link(self, status, scheduled_exit, amount, now):
        self.links += 1
        return "https://www.paydollar.com/pay?x=1", {"payment_ref": f"REF{self.links}", "order_ref": "O",
                                                     "secure_hash": "h", "process_time": "t"}


@pytest.fixture
def tg():
    b = MagicMock()
    b.send_message = AsyncMock()
    return b


def texts(tg):
    return [c.kwargs.get("text") or c.args[1] for c in tg.send_message.call_args_list]


def landed_order(db_path, **kw):
    save_order(db_path, make_order(**kw), telegram_msg_id=1, parking=32.0, source="携程")
    update_flight_info(db_path, kw.get("order_id", "O1"), "18:50", "18:48", None, "landed")


def run(tg, now):
    asyncio.run(bot._check_parking(tg, 123, now))


def test_not_armed_means_no_query(db_path, tg, monkeypatch):
    client = FakeClient([])           # any query would IndexError
    monkeypatch.setattr(bot, "_parking_client", client)
    run(tg, ENTRY)                    # no orders at all
    tg.send_message.assert_not_called()


def test_entry_opens_session_links_order_and_pushes(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(1)]))
    run(tg, ENTRY + timedelta(minutes=1))
    s = get_open_parking_session(db_path)
    assert s and s["pv_nr"] == 212022 and s["entry_time"] == "2026-08-23 18:48" and s["order_id"] == "O1"
    t = texts(tg)
    assert len(t) == 1 and "已入 Car Park 4 18:48" in t[0] and "免費可用" in t[0] and "19:18" in t[0]
    assert "WANG/XIAOMING" in t[0]
    kb = tg.send_message.call_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == f"park:pay:{s['id']}"


def test_entry_when_allowance_used_says_so(db_path, tg, monkeypatch):
    landed_order(db_path)
    from ride_dispatch.db import open_parking_session, close_parking_session
    sid = open_parking_session(db_path, pv_nr=1, plate="YY3953", location="P4O", location_name="Car Park 4",
                               entry_time="2026-08-23 13:36", order_id=None)
    close_parking_session(db_path, sid, exit_time="2026-08-23 13:50", free=1)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(1)]))
    run(tg, ENTRY + timedelta(minutes=1))
    assert "免費已用" in texts(tg)[0] and "$32" in texts(tg)[0] and "19:48" in texts(tg)[0]


def test_one_miss_keeps_session_two_misses_close_at_first_miss(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(1), OUT, OUT]))
    run(tg, ENTRY + timedelta(minutes=1))
    run(tg, ENTRY + timedelta(minutes=20))
    assert get_open_parking_session(db_path) is not None
    run(tg, ENTRY + timedelta(minutes=21))
    assert get_open_parking_session(db_path) is None
    row = recent_parking_sessions(db_path, 1)[0]
    assert row["exit_time"] == "2026-08-23 19:08" and row["free"] == 1
    assert "已出閘 19:08" in texts(tg)[-1] and "免費" in texts(tg)[-1]
    assert get_order_by_id(db_path, "O1")["parking_fee"] == 0


def test_error_reply_changes_nothing(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(1), ParkingError("x"), OUT, OUT]))
    run(tg, ENTRY + timedelta(minutes=1))
    run(tg, ENTRY + timedelta(minutes=2))
    run(tg, ENTRY + timedelta(minutes=3))
    assert get_open_parking_session(db_path) is not None
    run(tg, ENTRY + timedelta(minutes=4))
    assert get_open_parking_session(db_path) is None


def test_open_session_polls_even_when_window_closed(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(1), inside(200), OUT, OUT]))
    run(tg, ENTRY + timedelta(minutes=1))
    late = ENTRY + timedelta(hours=5)   # far past landing + 2h
    run(tg, late)
    run(tg, late + timedelta(minutes=1))
    run(tg, late + timedelta(minutes=2))
    assert get_open_parking_session(db_path) is None
    assert recent_parking_sessions(db_path, 1)[0]["free"] == 0   # 5h unpaid -> gate


def test_restart_resumes_by_pv_nr_without_second_push(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(1), inside(5)]))
    run(tg, ENTRY + timedelta(minutes=1))
    monkeypatch.setattr(bot, "_parking_miss_at", None)   # simulate process restart
    run(tg, ENTRY + timedelta(minutes=5))
    assert tg.send_message.call_count == 1
    assert len(recent_parking_sessions(db_path, 5)) == 1


def test_new_pv_nr_closes_old_and_opens_new(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(1), inside(1, pv_nr=999)]))
    run(tg, ENTRY + timedelta(minutes=1))
    run(tg, ENTRY + timedelta(minutes=2))
    rows = recent_parking_sessions(db_path, 5)
    assert len(rows) == 2
    assert get_open_parking_session(db_path)["pv_nr"] == 999
    assert [r["exit_time"] is None for r in rows].count(True) == 1


def test_paid_transition_pushes_once(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(1), inside(12, paid=True), inside(13, paid=True)]))
    run(tg, ENTRY + timedelta(minutes=1))
    run(tg, ENTRY + timedelta(minutes=12))
    run(tg, ENTRY + timedelta(minutes=13))
    t = texts(tg)
    assert sum("已收到付款" in x for x in t) == 1
    assert get_open_parking_session(db_path)["paid"] == 1


def test_auto_link_at_50_minutes_once_and_not_when_paid(db_path, tg, monkeypatch):
    landed_order(db_path)
    client = FakeClient([inside(1), inside(49), inside(50), inside(51)])
    monkeypatch.setattr(bot, "_parking_client", client)
    for m in (1, 49, 50, 51):
        run(tg, ENTRY + timedelta(minutes=m))
    assert client.links == 1
    t = texts(tg)
    assert any("泊咗 50 分鐘未俾錢" in x for x in t)
    link_call = [c for c in tg.send_message.call_args_list if "泊咗 50 分鐘" in (c.kwargs.get("text") or "")][0]
    assert link_call.kwargs["reply_markup"].inline_keyboard[0][0].url.startswith("https://www.paydollar.com/")
    s = get_open_parking_session(db_path)
    assert s["auto_link_sent"] == 1 and s["payment_ref"] == "REF1" and s["scheduled_exit"] == "2026-08-23 19:48"

    # A paid visit never gets the auto link.
    tg.send_message.reset_mock()
    monkeypatch.setattr(bot, "_parking_miss_at", None)
    from ride_dispatch.db import close_parking_session
    close_parking_session(db_path, s["id"], "2026-08-23 19:40", 0)
    client2 = FakeClient([inside(1, pv_nr=2), inside(55, paid=True, pv_nr=2)])
    monkeypatch.setattr(bot, "_parking_client", client2)
    run(tg, ENTRY + timedelta(minutes=1))
    run(tg, ENTRY + timedelta(minutes=55))
    assert client2.links == 0


def test_paid_exit_writes_amount_to_order(db_path, tg, monkeypatch):
    landed_order(db_path)
    client = FakeClient([inside(1), inside(50), inside(55, paid=True), OUT, OUT])
    monkeypatch.setattr(bot, "_parking_client", client)
    for m in (1, 50, 55, 70, 71):
        run(tg, ENTRY + timedelta(minutes=m))
    row = recent_parking_sessions(db_path, 1)[0]
    assert row["free"] == 0 and row["paid"] == 1 and row["paid_amount"] == 32
    assert get_order_by_id(db_path, "O1")["parking_fee"] == 32
    assert "已付 $32" in texts(tg)[-1]


def test_allowance_line(db_path, monkeypatch):
    now = datetime(2026, 8, 23, 19, 0)
    assert bot._allowance_line(now) == "停車場 免費可用"
    from ride_dispatch.db import open_parking_session, close_parking_session
    sid = open_parking_session(db_path, pv_nr=1, plate="YY3953", location="P4O", location_name="Car Park 4",
                               entry_time="2026-08-23 13:40", order_id=None)
    close_parking_session(db_path, sid, exit_time="2026-08-23 13:55", free=1)
    assert bot._allowance_line(now) == "停車場 免費已用 13:40，明日 13:40 後先有"
    assert bot._allowance_line(datetime(2026, 8, 24, 10, 0)) == "停車場 免費已用 昨日 13:40，今日 13:40 後先有"
