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
    monkeypatch.setattr(bot, "_parking_pay_busy", set())
    yield path
    os.unlink(path)


def inside(minutes: int, paid: bool = False, pv_nr: int = 212022, fee: float | None = 0) -> ParkingStatus:
    return ParkingStatus(inside=True, pv_nr=pv_nr, location="P4O", location_name="Car Park 4",
                         entry_time=ENTRY.strftime("%Y-%m-%d %H:%M"), park_minutes=minutes, paid=paid, fee=fee)


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


def test_one_miss_keeps_session_two_misses_close_on_hkia_clock(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(12), OUT, OUT]))
    run(tg, ENTRY + timedelta(minutes=12))
    run(tg, ENTRY + timedelta(minutes=20))
    assert get_open_parking_session(db_path) is not None
    run(tg, ENTRY + timedelta(minutes=21))
    assert get_open_parking_session(db_path) is None
    row = recent_parking_sessions(db_path, 1)[0]
    # HKIA said 12 minutes at the last sighting; the tick 8 minutes later that
    # missed the car only bounds the exit from above and is kept separately.
    assert row["exit_time"] == "2026-08-23 19:00" and row["free"] == 1
    assert row["gone_at"] == "2026-08-23 19:08:00"
    assert "已出閘 19:00，泊 12 分鐘" in texts(tg)[-1] and "免費（HKIA 計 $0）" in texts(tg)[-1]
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
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(1), inside(200, fee=192), OUT, OUT]))
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


def _freeze_clock(monkeypatch, when: datetime):
    """Pin bot.datetime.now() so a handler that reads the wall clock is testable."""
    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(bot, "datetime", _Fixed)


def _callback(data, chat_id=123):
    q = MagicMock()
    q.data = data
    q.message.chat_id = chat_id
    q.message.message_id = 7
    q.answer = AsyncMock()
    q.message.text = "已出閘 19:21，泊 33 分鐘 | 閘口找數（HKIA 計 $32）"
    q.message.reply_text = AsyncMock()
    q.message.edit_text = AsyncMock()
    q.message.edit_reply_markup = AsyncMock()
    upd = MagicMock()
    upd.callback_query = q
    ctx = MagicMock()
    ctx.application.bot = MagicMock()
    ctx.application.bot.send_message = AsyncMock()
    return upd, ctx, q


def test_pay_callback_sends_link_and_records(db_path, tg, monkeypatch):
    landed_order(db_path)
    client = FakeClient([inside(1), inside(10)])
    monkeypatch.setattr(bot, "_parking_client", client)
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    run(tg, ENTRY + timedelta(minutes=1))
    sid = get_open_parking_session(db_path)["id"]
    _freeze_clock(monkeypatch, ENTRY + timedelta(minutes=10))
    upd, ctx, q = _callback(f"park:pay:{sid}")
    asyncio.run(bot.handle_callback(upd, ctx))
    assert client.links == 1
    sent = ctx.application.bot.send_message.call_args
    assert "$32 泊到 19:48" in sent.kwargs["text"]
    assert sent.kwargs["reply_markup"].inline_keyboard[0][0].url.startswith("https://www.paydollar.com/")
    s = get_parking_session(db_path, sid)
    assert s["payment_ref"] == "REF1" and s["scheduled_exit"] == "2026-08-23 19:48" and s["auto_link_sent"] == 0
    q.answer.assert_awaited()


def test_pay_callback_when_not_inside_or_failed(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    upd, ctx, q = _callback("park:pay:999")
    asyncio.run(bot.handle_callback(upd, ctx))
    q.answer.assert_awaited()
    assert "搵唔到" in q.message.reply_text.call_args.args[0]

    client = FakeClient([inside(1), ParkingError("down")])
    monkeypatch.setattr(bot, "_parking_client", client)
    run(tg, ENTRY + timedelta(minutes=1))
    sid = get_open_parking_session(db_path)["id"]
    upd, ctx, q = _callback(f"park:pay:{sid}")
    asyncio.run(bot.handle_callback(upd, ctx))
    assert "再撳" in q.message.reply_text.call_args.args[0]


def _markups(q):
    return [c.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
            for c in q.message.edit_reply_markup.await_args_list]


def test_pay_callback_swaps_button_before_the_gateway_and_restores_after(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    seen_at_query = {}

    class Watching(FakeClient):
        async def query(self):
            # Snapshot what the operator's screen shows when the first gateway call goes out.
            seen_at_query["answer"] = q.answer.await_args.args[0]
            seen_at_query["markups"] = _markups(q)
            seen_at_query["busy"] = set(bot._parking_pay_busy)
            return await super().query()

    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(1)]))
    run(tg, ENTRY + timedelta(minutes=1))
    sid = get_open_parking_session(db_path)["id"]
    client = Watching([inside(10)])
    monkeypatch.setattr(bot, "_parking_client", client)
    upd, ctx, q = _callback(f"park:pay:{sid}")
    asyncio.run(bot.handle_callback(upd, ctx))
    assert seen_at_query == {"answer": "攞緊 link，等幾秒", "markups": ["park:busy"], "busy": {sid}}
    assert client.links == 1
    assert _markups(q) == ["park:busy", f"park:pay:{sid}"]
    assert bot._parking_pay_busy == set()


def test_second_tap_during_a_fetch_answers_busy_and_opens_no_second_order(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(1)]))
    run(tg, ENTRY + timedelta(minutes=1))
    sid = get_open_parking_session(db_path)["id"]

    class Slow(FakeClient):
        def __init__(self, replies):
            super().__init__(replies)
            self.gate = None

        async def query(self):
            await self.gate.wait()
            return await super().query()

    client = Slow([inside(10)])
    monkeypatch.setattr(bot, "_parking_client", client)
    first = _callback(f"park:pay:{sid}")
    second = _callback(f"park:pay:{sid}")

    async def double_tap():
        client.gate = asyncio.Event()
        t1 = asyncio.create_task(bot.handle_callback(*first[:2]))
        await asyncio.sleep(0)            # first tap is now parked on the gateway
        await bot.handle_callback(*second[:2])
        client.gate.set()
        await t1

    asyncio.run(double_tap())
    assert client.links == 1
    q2 = second[2]
    q2.answer.assert_awaited_once_with("攞緊，等陣")
    q2.message.edit_reply_markup.assert_not_awaited()
    q2.message.reply_text.assert_not_awaited()
    assert _markups(first[2]) == ["park:busy", f"park:pay:{sid}"]
    assert bot._parking_pay_busy == set()


def test_busy_button_tap_only_answers(db_path, monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    upd, ctx, q = _callback("park:busy")
    asyncio.run(bot.handle_callback(upd, ctx))
    q.answer.assert_awaited_once_with("攞緊，等陣")
    q.message.edit_reply_markup.assert_not_awaited()
    q.message.reply_text.assert_not_awaited()


def test_failed_fetch_restores_the_button_and_clears_busy(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    client = FakeClient([inside(1), ParkingError("down")])
    monkeypatch.setattr(bot, "_parking_client", client)
    run(tg, ENTRY + timedelta(minutes=1))
    sid = get_open_parking_session(db_path)["id"]
    upd, ctx, q = _callback(f"park:pay:{sid}")
    asyncio.run(bot.handle_callback(upd, ctx))
    assert "再撳" in q.message.reply_text.call_args.args[0]
    assert _markups(q) == ["park:busy", f"park:pay:{sid}"]
    assert bot._parking_pay_busy == set()


def _command(chat_id=123):
    upd = MagicMock()
    upd.message.chat_id = chat_id
    upd.message.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.args = []
    return upd, ctx


def test_parking_command_inside_and_history(db_path, tg, monkeypatch):
    landed_order(db_path)
    client = FakeClient([inside(1), inside(12)])
    monkeypatch.setattr(bot, "_parking_client", client)
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    run(tg, ENTRY + timedelta(minutes=1))
    upd, ctx = _command()
    asyncio.run(bot.handle_parking(upd, ctx))
    out = upd.message.reply_text.call_args.args[0]
    assert "Car Park 4" in out and "18:48" in out and "12 分鐘" in out
    assert "免費可用" in out
    assert "18:48" in out   # history line


def test_parking_command_when_unconfigured(db_path, monkeypatch):
    monkeypatch.setattr(bot, "_parking_client", None)
    monkeypatch.setattr(bot, "_parking_client_built", True)
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    upd, ctx = _command()
    asyncio.run(bot.handle_parking(upd, ctx))
    assert "CAR_PLATE" in upd.message.reply_text.call_args.args[0]


def test_landing_push_carries_allowance_line(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([]))
    info = {"scheduled": "18:50", "eta": "18:48", "gate": None, "status": "landed", "hall": "A"}
    asyncio.run(bot._notify_status_change(tg, 123, "O1", info, "est", "landed"))
    assert "停車場 免費可用" in texts(tg)[0]


def test_landing_push_has_no_allowance_line_when_off(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "_parking_client", None)
    monkeypatch.setattr(bot, "_parking_client_built", True)
    info = {"scheduled": "18:50", "eta": "18:48", "gate": None, "status": "landed", "hall": "A"}
    asyncio.run(bot._notify_status_change(tg, 123, "O1", info, "est", "landed"))
    assert "停車場" not in texts(tg)[0]


# --- HKIA readings, exit time and the manual verdict ---


def test_inside_tick_records_the_hkia_reading(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(1), inside(12, fee=32)]))
    run(tg, ENTRY + timedelta(minutes=1))
    s = get_open_parking_session(db_path)
    assert s["last_seen_at"] == "2026-08-23 18:49:00"
    assert s["last_park_minutes"] == 1 and s["last_fee"] == 0
    run(tg, ENTRY + timedelta(minutes=12))
    s = get_open_parking_session(db_path)
    assert s["last_seen_at"] == "2026-08-23 19:00:00"
    assert s["last_park_minutes"] == 12 and s["last_fee"] == 32


def test_close_falls_back_to_last_seen_when_park_minutes_is_missing(db_path, tg, monkeypatch):
    from ride_dispatch.db import update_parking_session
    sid = open_session(db_path)
    update_parking_session(db_path, sid, last_seen_at="2026-08-23 19:05:40")
    monkeypatch.setattr(bot, "_parking_client", FakeClient([OUT, OUT]))
    run(tg, ENTRY + timedelta(minutes=18))
    run(tg, ENTRY + timedelta(minutes=19))
    row = recent_parking_sessions(db_path, 1)[0]
    assert row["exit_time"] == "2026-08-23 19:05"        # entry + 17 whole minutes
    assert row["gone_at"] == "2026-08-23 19:06:00"
    assert "冇 HKIA 讀數，按 30 分鐘估" in texts(tg)[-1]


def test_close_uses_the_miss_tick_when_nothing_was_ever_read(db_path, tg, monkeypatch):
    open_session(db_path)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([OUT, OUT]))
    run(tg, ENTRY + timedelta(minutes=40))
    run(tg, ENTRY + timedelta(minutes=41))
    row = recent_parking_sessions(db_path, 1)[0]
    assert row["exit_time"] == "2026-08-23 19:28" and row["gone_at"] == "2026-08-23 19:28:00"
    assert row["free"] == 0                              # 40 min, no reading -> 30-minute rule


def test_a_33_minute_visit_hkia_prices_at_zero_is_free(db_path, tg, monkeypatch):
    """Entered 14:51, gate opened for nothing at 15:24 after 33 minutes.

    The 30-minute rule called this 閘口找數 and told the next pickup the free
    allowance was spent.
    """
    entry = datetime(2026, 8, 28, 14, 51)
    save_order(db_path, make_order(scheduled_time="2026-08-28 15:30:00"),
               telegram_msg_id=1, parking=32.0, source="携程")
    update_flight_info(db_path, "O1", "14:50", "14:45", None, "landed")

    def at(minutes):
        return ParkingStatus(inside=True, pv_nr=414, location="P4O", location_name="Car Park 4",
                             entry_time=entry.strftime("%Y-%m-%d %H:%M"),
                             park_minutes=minutes, paid=False, fee=0)

    monkeypatch.setattr(bot, "_parking_client", FakeClient([at(1), at(33), OUT, OUT]))
    for m in (1, 33, 34, 35):
        run(tg, entry + timedelta(minutes=m))
    row = recent_parking_sessions(db_path, 1)[0]
    assert row["exit_time"] == "2026-08-28 15:24" and row["free"] == 1
    assert row["last_park_minutes"] == 33 and row["last_fee"] == 0
    assert row["gone_at"] == "2026-08-28 15:25:00"
    assert "已出閘 15:24，泊 33 分鐘 | 免費（HKIA 計 $0）" in texts(tg)[-1]
    assert get_order_by_id(db_path, "O1")["parking_fee"] == 0


def _gate_close(db_path, tg, monkeypatch) -> int:
    """A visit HKIA priced at $32, closed and waiting to be corrected."""
    landed_order(db_path)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(33, fee=32), OUT, OUT]))
    for m in (33, 34, 35):
        run(tg, ENTRY + timedelta(minutes=m))
    return recent_parking_sessions(db_path, 1)[0]["id"]


def test_exit_message_offers_the_two_verdicts_it_did_not_pick(db_path, tg, monkeypatch):
    sid = _gate_close(db_path, tg, monkeypatch)
    assert "閘口找數（HKIA 計 $32）" in texts(tg)[-1]
    row = tg.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [b.text for b in row] == ["其實免費", "其實俾咗錢"]
    assert [b.callback_data for b in row] == [f"park:mark:{sid}:free", f"park:mark:{sid}:paid"]

    tg.send_message.reset_mock()
    monkeypatch.setattr(bot, "_parking_miss_at", None)
    monkeypatch.setattr(bot, "_parking_client", FakeClient([inside(20, pv_nr=2), OUT, OUT]))
    for m in (20, 21, 22):
        run(tg, ENTRY + timedelta(minutes=m))
    row = tg.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [b.text for b in row] == ["其實俾咗錢", "其實閘口找數"]


def test_a_payment_hkia_confirmed_offers_no_correction(db_path, tg, monkeypatch):
    landed_order(db_path)
    monkeypatch.setattr(bot, "_parking_client",
                        FakeClient([inside(20), inside(25, paid=True), OUT, OUT]))
    for m in (20, 25, 26, 27):
        run(tg, ENTRY + timedelta(minutes=m))
    assert tg.send_message.call_args.kwargs["reply_markup"] is None


def test_mark_callback_moves_the_allowance_and_zeroes_the_order(db_path, tg, monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    sid = _gate_close(db_path, tg, monkeypatch)
    assert recent_parking_sessions(db_path, 1)[0]["free"] == 0
    assert get_order_by_id(db_path, "O1")["parking_fee"] == 32.0

    upd, ctx, q = _callback(f"park:mark:{sid}:free")
    asyncio.run(bot.handle_callback(upd, ctx))

    row = recent_parking_sessions(db_path, 1)[0]
    assert row["observed"] == "free" and row["free"] == 1
    assert get_order_by_id(db_path, "O1")["parking_fee"] == 0
    edited = q.message.edit_text.call_args
    assert edited.args[0] == f"{q.message.text}\n人手改：免費"
    assert edited.kwargs["reply_markup"] is None
    q.answer.assert_awaited_with("人手改：免費")


def test_mark_callback_paid_leaves_the_order_cost_for_the_dashboard(db_path, tg, monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    sid = _gate_close(db_path, tg, monkeypatch)
    upd, ctx, q = _callback(f"park:mark:{sid}:paid")
    asyncio.run(bot.handle_callback(upd, ctx))
    row = recent_parking_sessions(db_path, 1)[0]
    assert row["observed"] == "paid" and row["free"] == 0
    # The amount is unknown to the system, so the ingested figure stands.
    assert get_order_by_id(db_path, "O1")["parking_fee"] == 32.0
    q.answer.assert_awaited_with("人手改：俾咗錢")


def test_mark_callback_on_an_unknown_session(db_path, monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    upd, ctx, q = _callback("park:mark:404:free")
    asyncio.run(bot.handle_callback(upd, ctx))
    q.answer.assert_awaited_once_with("搵唔到呢次泊車")
    q.message.edit_text.assert_not_awaited()


def test_parking_mark_command_corrects_a_visit_whose_buttons_are_gone(db_path, tg, monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    sid = _gate_close(db_path, tg, monkeypatch)
    upd, ctx = _command()
    ctx.args = ["mark", str(sid), "free"]
    asyncio.run(bot.handle_parking(upd, ctx))
    row = recent_parking_sessions(db_path, 1)[0]
    assert row["observed"] == "free" and row["free"] == 1
    assert get_order_by_id(db_path, "O1")["parking_fee"] == 0
    assert upd.message.reply_text.call_args.args[0] == "08-23 18:48 泊 33 分鐘 閘口 HKIA$32 → 人手改 免費"

    # A second, different verdict replaces the first.
    upd, ctx = _command()
    ctx.args = ["mark", str(sid), "gate"]
    asyncio.run(bot.handle_parking(upd, ctx))
    row = recent_parking_sessions(db_path, 1)[0]
    assert row["observed"] == "gate" and row["free"] == 0
    assert upd.message.reply_text.call_args.args[0] == "08-23 18:48 泊 33 分鐘 閘口 HKIA$32 ✓人手"


def test_parking_mark_command_unknown_session(db_path, monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    upd, ctx = _command()
    ctx.args = ["mark", "404", "free"]
    asyncio.run(bot.handle_parking(upd, ctx))
    assert upd.message.reply_text.call_args.args[0] == "搵唔到泊車 #404"


@pytest.mark.parametrize("args", [["mark"], ["mark", "1"], ["mark", "x", "free"],
                                  ["mark", "1", "maybe"], ["mark", "1", "free", "extra"], ["nonsense"]])
def test_parking_mark_bad_args_shows_usage(db_path, monkeypatch, args):
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    upd, ctx = _command()
    ctx.args = args
    asyncio.run(bot.handle_parking(upd, ctx))
    assert upd.message.reply_text.call_args.args[0] == bot.PARKING_MARK_USAGE


def test_history_line_shows_the_reading_and_the_manual_verdict(db_path):
    from ride_dispatch.db import (open_parking_session, update_parking_session,
                                  close_parking_session, mark_parking_observed)
    sid = open_parking_session(db_path, pv_nr=1, plate="YY3953", location="P4O",
                               location_name="Car Park 4", entry_time="2026-08-28 14:51",
                               order_id=None)
    assert bot._history_line(get_parking_session(db_path, sid)) == "08-28 14:51 泊緊"
    update_parking_session(db_path, sid, last_park_minutes=33, last_fee=32.0)
    close_parking_session(db_path, sid, "2026-08-28 15:24", 0)
    line = "08-28 14:51 泊 33 分鐘 閘口 HKIA$32"
    assert bot._history_line(get_parking_session(db_path, sid)) == line
    mark_parking_observed(db_path, sid, "free")
    assert bot._history_line(get_parking_session(db_path, sid)) == f"{line} → 人手改 免費"
    mark_parking_observed(db_path, sid, "gate")
    assert bot._history_line(get_parking_session(db_path, sid)) == f"{line} ✓人手"


def test_history_line_without_a_reading_keeps_the_old_shape(db_path):
    from ride_dispatch.db import open_parking_session, close_parking_session
    sid = open_parking_session(db_path, pv_nr=1, plate="YY3953", location="P4O",
                               location_name="Car Park 4", entry_time="2026-08-23 18:48",
                               order_id=None)
    close_parking_session(db_path, sid, "2026-08-23 19:03", 1)
    assert bot._history_line(get_parking_session(db_path, sid)) == "08-23 18:48 泊 15 分鐘 免費"


# --- heartbeat integration ---


@pytest.fixture
def poll_globals(monkeypatch):
    monkeypatch.setenv("NOTIFY_CHAT_ID", "123")
    monkeypatch.setattr(bot, "_next_poll_at", None)
    monkeypatch.setattr(bot, "_poll_running", False)
    monkeypatch.setattr(bot, "_parking_running", False)
    monkeypatch.setattr(bot, "_parking_miss_at", None)


@pytest.fixture
def ctx(tg):
    c = MagicMock()
    c.application.bot = tg
    return c


class CountingClient(FakeClient):
    def __init__(self, replies):
        super().__init__(replies)
        self.queries = 0

    async def query(self):
        self.queries += 1
        return await super().query()


def open_session(db_path):
    from ride_dispatch.db import open_parking_session
    return open_parking_session(db_path, pv_nr=212022, plate="YY3953", location="P4O",
                                location_name="Car Park 4", entry_time=ENTRY.strftime("%Y-%m-%d %H:%M"),
                                order_id=None)


def poll_spy(monkeypatch, interval: int = 600):
    calls = []

    async def spy(context):
        calls.append(context)
        return interval

    monkeypatch.setattr(bot, "_poll_and_notify", spy)
    return calls


def test_tick_checks_parking_while_the_flight_poll_is_gated(db_path, tg, ctx, poll_globals, monkeypatch):
    open_session(db_path)
    client = CountingClient([OUT, OUT])
    monkeypatch.setattr(bot, "_parking_client", client)
    monkeypatch.setattr(bot, "_next_poll_at", datetime(2099, 1, 1))
    calls = poll_spy(monkeypatch)

    asyncio.run(bot._poll_tick(ctx))
    assert get_open_parking_session(db_path) is not None   # first miss only

    asyncio.run(bot._poll_tick(ctx))
    assert get_open_parking_session(db_path) is None
    assert any("已出閘" in t for t in texts(tg))
    assert client.queries == 2
    assert calls == []                                     # flight poll stayed gated


def test_due_tick_checks_parking_once_and_polls(db_path, tg, ctx, poll_globals, monkeypatch):
    open_session(db_path)
    client = CountingClient([inside(30)])
    monkeypatch.setattr(bot, "_parking_client", client)
    monkeypatch.setattr(bot, "_next_poll_at", datetime(2000, 1, 1))
    calls = poll_spy(monkeypatch)

    asyncio.run(bot._poll_tick(ctx))

    assert client.queries == 1
    assert len(calls) == 1
    assert bot._next_poll_at > datetime.now()


def test_poll_and_notify_no_longer_checks_parking(db_path, tg, ctx, poll_globals, monkeypatch):
    open_session(db_path)
    client = CountingClient([inside(30)])
    monkeypatch.setattr(bot, "_parking_client", client)

    asyncio.run(bot._poll_and_notify(ctx))

    assert client.queries == 0


def test_tick_skips_parking_while_a_check_is_running(db_path, tg, ctx, poll_globals, monkeypatch):
    open_session(db_path)
    client = CountingClient([OUT])
    monkeypatch.setattr(bot, "_parking_client", client)
    monkeypatch.setattr(bot, "_parking_running", True)
    monkeypatch.setattr(bot, "_next_poll_at", datetime(2099, 1, 1))
    poll_spy(monkeypatch)

    asyncio.run(bot._poll_tick(ctx))

    assert client.queries == 0
    assert get_open_parking_session(db_path) is not None


def test_parking_failure_does_not_block_the_flight_poll(db_path, tg, ctx, poll_globals, monkeypatch):
    open_session(db_path)
    monkeypatch.setattr(bot, "_parking_client", CountingClient([RuntimeError("boom")]))
    calls = poll_spy(monkeypatch)

    asyncio.run(bot._poll_tick(ctx))

    assert len(calls) == 1
    assert bot._parking_running is False
