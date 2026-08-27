import ast
import asyncio
import json
import os
import pathlib
import tempfile
import threading
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import ride_dispatch.bot as bot
from ride_dispatch import db as db_module
from ride_dispatch import credits, statement
from ride_dispatch.db import (
    init_db, save_order, update_price, open_batches, create_settlement, get_settlement,
    get_credit, insert_credit, unallocated_credits, allocate,
)
from ride_dispatch.parser import Order
from ride_dispatch.statement import Statement, StatementDay, StatementRow

CHAT = 123
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
TWO_DAYS = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
TODAY = datetime.now().strftime("%Y-%m-%d")
MONTHS_AGO = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
LAST_MONTH = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")


def make_order(order_id, scheduled, service_type="送机"):
    return Order(order_id=order_id, service_type=service_type, vehicle_type="经济5座",
                 passenger_name="TEST/USER", scheduled_time=scheduled,
                 passenger_phone="86 13800000000", overseas_phone="", flight_number="",
                 pickup="尖沙咀", dropoff="香港国际机场 T1", distance_km=30, notes="",
                 driver_notes="", additional_services="", passenger_exit_minutes=None,
                 third_party_contact="", more_contacts="", raw_message="raw")


@pytest.fixture
def db_path(monkeypatch, tmp_path):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "orders.db")
    init_db(path)
    monkeypatch.setattr(bot, "DB_PATH", path)
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    monkeypatch.setattr(bot, "FEED_PATH", str(tmp_path / "ride-dispatch.jsonl"))
    bot.pending_statements.clear()
    credits._feed_seen.clear()
    credits._feed_missing_logged.clear()
    yield path


def seed(db_path, oid, scheduled, price=210.0):
    save_order(db_path, make_order(oid, scheduled), telegram_msg_id=1, parking=0.0, source="携程")
    update_price(db_path, oid, price)


def batch(db_path, oid, scheduled, amount):
    seed(db_path, oid, scheduled, amount)
    return create_settlement(db_path, "ride", [oid], amount, TODAY)


def feed_line(ref, amount, value_date, **over):
    d = {"v": 1, "feed": "ride-dispatch", "ref": ref, "platform": "ride", "amount": amount,
         "currency": "HKD", "value_date": value_date, "payer": "A B**** C***** L",
         "memo": "SUPPLIERPAY", "email_id": "m", "received_at": "2026-08-27T00:00:54Z",
         "recorded_at": "2026-08-27T00:15:03Z"}
    d.update(over)
    return json.dumps(d)


def write_feed(*lines):
    with open(bot.FEED_PATH, "w", encoding="utf-8") as f:
        f.write("".join(l + "\n" for l in lines))


def fake_bot():
    b = MagicMock()
    b.send_message = AsyncMock()
    return b


def tick(b):
    asyncio.run(bot._check_credits(b, CHAT))


def sent(b, n=0):
    return b.send_message.call_args_list[n].kwargs["text"]


def sent_buttons(b, n=0):
    markup = b.send_message.call_args_list[n].kwargs.get("reply_markup")
    return [] if markup is None else [btn for row in markup.inline_keyboard for btn in row]


def callback_update(data, message_id=777, text="舊訊息"):
    q = MagicMock()
    q.data = data
    q.message.message_id = message_id
    q.message.chat_id = CHAT
    q.message.text = text
    q.message.edit_text = AsyncMock()
    q.message.edit_reply_markup = AsyncMock()
    q.message.reply_text = AsyncMock()
    q.answer = AsyncMock()
    return MagicMock(callback_query=q), q


def edited(q):
    return q.message.edit_text.call_args.args[0]


def edited_buttons(q):
    markup = q.message.edit_text.call_args.kwargs.get("reply_markup")
    return [] if markup is None else [btn for row in markup.inline_keyboard for btn in row]


def command_update(*args):
    msg = MagicMock()
    msg.chat_id = CHAT
    msg.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.args = list(args)
    return MagicMock(message=msg), msg, ctx


def run_credits(*args):
    upd, msg, ctx = command_update(*args)
    asyncio.run(bot.handle_credits(upd, ctx))
    return msg


def reply_text(msg):
    return msg.reply_text.call_args.args[0]


def reply_buttons(msg):
    markup = msg.reply_text.call_args.kwargs.get("reply_markup")
    return [] if markup is None else [btn for row in markup.inline_keyboard for btn in row]


# ---- the heartbeat ----

def test_tick_proposes_the_exact_batch_but_links_nothing(db_path):
    """Money arriving needs no confirmation as an event; which orders it pays
    for does.  The heartbeat announces and asks — it never links."""
    sid = batch(db_path, "A1", f"{TWO_DAYS} 09:00:00", 2540.0)
    write_feed(feed_line("R1", 2540.0, YESTERDAY))
    b = fake_bot()
    tick(b)
    lines = sent(b).split("\n")
    assert lines[0] == f"入數 $2,540 · {credits.md(YESTERDAY)} · 對到 批次 #{sid}？"
    assert lines[1] == "撳確認："
    assert lines[2] == "send 結算圖入嚟都會提議"
    assert [btn.callback_data for btn in sent_buttons(b)] == [f"credit:link:1:{sid}"]
    assert get_settlement(db_path, sid)["received"] == 0.0
    assert get_settlement(db_path, sid)["paid_on"] is None


def test_tick_unresolved_sends_card_with_candidates(db_path):
    s1 = batch(db_path, "A1", f"{TWO_DAYS} 09:00:00", 930.0)
    s2 = batch(db_path, "A2", f"{YESTERDAY} 09:00:00", 1080.0)
    s3 = batch(db_path, "A3", f"{YESTERDAY} 10:00:00", 1450.0)
    write_feed(feed_line("R1", 2950.0, TODAY))
    b = fake_bot()
    tick(b)
    lines = sent(b).split("\n")
    assert lines[0] == f"入數 $2,950 · {credits.md(TODAY)} · 未對"
    assert lines[1] == "等緊過數："
    assert lines[2] == "send 結算圖入嚟都會提議"
    # Newest anchor first, and every batch fits under the credit.
    assert [btn.callback_data for btn in sent_buttons(b)] == [
        f"credit:link:1:{s3}", f"credit:link:1:{s2}", f"credit:link:1:{s1}"]
    assert sent_buttons(b)[0].text == statement.batch_label(get_settlement(db_path, s3))


def test_tick_card_says_when_there_is_nothing_to_pay(db_path):
    write_feed(feed_line("R1", 100.0, TODAY))
    b = fake_bot()
    tick(b)
    assert sent(b).split("\n")[1] == "冇 batch 啱銀碼"
    assert sent_buttons(b) == []


def test_tick_card_offers_a_batch_the_credit_can_only_part_pay(db_path):
    """Round 2 offered nothing here: a credit smaller than every batch could
    not be linked.  Money is allocated in amounts now, so it is a part
    payment, and the button says what the tap would leave owing."""
    sid = batch(db_path, "A1", f"{YESTERDAY} 09:00:00", 3460.0)
    write_feed(feed_line("R1", 2950.0, TODAY))
    b = fake_bot()
    tick(b)
    assert sent(b).split("\n")[1] == "等緊過數："
    buttons = sent_buttons(b)
    assert [btn.callback_data for btn in buttons] == [f"credit:link:1:{sid}"]
    assert buttons[0].text.endswith("· 對 $2,950（差 $510）")


def test_tick_backfill_summary(db_path):
    batch(db_path, "A1", f"{YESTERDAY} 09:00:00", 2540.0)
    write_feed(feed_line("R1", 2540.0, YESTERDAY), feed_line("R2", 100.0, "2026-06-05"),
               feed_line("R3", 200.0, "2026-06-06"), feed_line("R4", 300.0, "2026-06-09"))
    b = fake_bot()
    tick(b)
    assert b.send_message.await_count == 1
    assert sent(b) == "入咗 4 筆入數紀錄 · 1 筆有啱數嘅 batch（#1） · /credits 睇"
    # A backfill reports what the matcher found, never what it did.
    assert get_settlement(db_path, 1)["allocations"] == []


def test_tick_backfill_with_nothing_matched(db_path):
    write_feed(*[feed_line(f"R{i}", 100.0 + i, "2026-06-05") for i in range(1, 6)])
    b = fake_bot()
    tick(b)
    assert sent(b) == "入咗 5 筆入數紀錄 · 0 筆有啱數嘅 batch · /credits 睇"


def test_tick_quiet_feed_costs_nothing(db_path, monkeypatch):
    write_feed(feed_line("R1", 100.0, "2026-08-26"))
    b = fake_bot()
    tick(b)
    b.send_message.reset_mock()

    def boom(*a, **k):
        raise AssertionError("an unchanged feed must not be re-read")

    monkeypatch.setattr(credits, "ingest_feed", boom)
    tick(b)
    b.send_message.assert_not_awaited()


def test_tick_without_feed_path_is_a_noop(db_path, monkeypatch):
    write_feed(feed_line("R1", 100.0, "2026-08-26"))
    monkeypatch.setattr(bot, "FEED_PATH", "")
    b = fake_bot()
    tick(b)
    b.send_message.assert_not_awaited()
    assert unallocated_credits(db_path) == []


def test_one_bad_credit_does_not_silence_the_others(db_path, monkeypatch):
    write_feed(feed_line("R1", 100.0, TODAY), feed_line("R2", 200.0, TODAY),
               feed_line("R3", 300.0, TODAY))
    real = credits.propose_credit

    def flaky(db, credit_id):
        if credit_id == 2:
            raise RuntimeError("boom")
        return real(db, credit_id)

    monkeypatch.setattr(credits, "propose_credit", flaky)
    monkeypatch.setattr(bot, "BACKFILL_THRESHOLD", 5)   # per-credit path, not the summary
    b = fake_bot()
    tick(b)
    assert b.send_message.await_count == 2
    assert sent(b, 0).startswith("入數 $100")
    assert sent(b, 1).startswith("入數 $300")


def test_a_failed_read_is_retried_on_the_next_tick(db_path, monkeypatch):
    """feed_changed marks the file seen before it is read, so a reader that
    fails has to undo that or the file waits for its next append."""
    write_feed(feed_line("R1", 100.0, TODAY))
    calls = []
    real = credits.ingest_feed

    def once(db, path):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("disk hiccup")
        return real(db, path)

    monkeypatch.setattr(credits, "ingest_feed", once)
    b = fake_bot()
    with pytest.raises(OSError):
        tick(b)
    tick(b)          # same file, untouched since
    assert len(calls) == 2
    assert sent(b).startswith("入數 $100")


def test_heartbeat_checks_the_feed_ahead_of_the_flight_gate(db_path, monkeypatch):
    """Latency must not scale with the flight poll interval, so the check runs
    on every tick rather than behind the gate."""
    seen = []
    monkeypatch.setattr(bot, "_check_credits", AsyncMock(side_effect=lambda b, c: seen.append(c)))
    monkeypatch.setattr(bot, "_check_parking", AsyncMock())
    monkeypatch.setattr(bot, "_poll_and_notify", AsyncMock(return_value=60))
    monkeypatch.setenv("NOTIFY_CHAT_ID", str(CHAT))
    bot._next_poll_at = datetime.now() + timedelta(hours=1)   # flight poll gated shut
    asyncio.run(bot._poll_tick(MagicMock()))
    assert seen == [CHAT]
    bot._poll_and_notify.assert_not_awaited()


def test_heartbeat_survives_a_broken_feed_check(db_path, monkeypatch, caplog):
    monkeypatch.setattr(bot, "_check_credits", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(bot, "_check_parking", AsyncMock())
    monkeypatch.setattr(bot, "_poll_and_notify", AsyncMock(return_value=60))
    monkeypatch.setenv("NOTIFY_CHAT_ID", str(CHAT))
    bot._next_poll_at = None
    with caplog.at_level("ERROR", logger="flight_poller"):
        asyncio.run(bot._poll_tick(MagicMock()))
    assert "credit feed check error" in caplog.text
    bot._poll_and_notify.assert_awaited_once()


def test_tick_survives_a_missing_feed(db_path, monkeypatch):
    monkeypatch.setattr(bot, "FEED_PATH", str(db_path) + ".nope")
    b = fake_bot()
    tick(b)
    b.send_message.assert_not_awaited()


# ---- the credit card ----

def test_card_tap_links_and_rerenders(db_path):
    s1 = batch(db_path, "A1", f"{TWO_DAYS} 09:00:00", 930.0)
    s2 = batch(db_path, "A2", f"{YESTERDAY} 09:00:00", 1080.0)
    s3 = batch(db_path, "A3", f"{YESTERDAY} 10:00:00", 1450.0)
    write_feed(feed_line("R1", 2950.0, TODAY))
    tick(fake_bot())
    cb, q = callback_update(f"credit:link:1:{s3}")
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    assert get_settlement(db_path, s3)["received"] == 1450.0
    lines = edited(q).split("\n")
    assert lines[0] == f"已對 批次 #{s3} · $1,450 · 剩 $1,500"
    assert lines[1] == f"入數 $2,950 · {credits.md(TODAY)} · 已對 $1,450 · 剩 $1,500"
    assert [btn.callback_data for btn in edited_buttons(q)] == [
        f"credit:link:1:{s2}", f"credit:link:1:{s1}"]


def test_card_tap_offers_a_batch_that_now_matches_exactly(db_path):
    """After a partial link the remainder can equal one awaiting batch to the
    cent. That is a resolved match, which carries its batch in `linked` and
    leaves `candidates` empty, so a card drawn from candidates alone would
    wrongly say nothing fits."""
    # Two combinations pay this credit (1,000+1,500 and 1,500+1,000), so it is
    # ambiguous and the operator gets a card rather than an automatic link.
    s1 = batch(db_path, "A1", f"{TWO_DAYS} 09:00:00", 1000.0)
    s2 = batch(db_path, "A2", f"{YESTERDAY} 09:00:00", 1500.0)
    batch(db_path, "A3", f"{YESTERDAY} 10:00:00", 1000.0)
    write_feed(feed_line("R1", 2500.0, TODAY))
    tick(fake_bot())
    cb, q = callback_update(f"credit:link:1:{s1}")
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    lines = edited(q).split("\n")
    assert lines[0] == f"已對 批次 #{s1} · $1,000 · 剩 $1,500"
    assert lines[1] == f"入數 $2,500 · {credits.md(TODAY)} · 剩 $1,500 · 對到 批次 #{s2}？"
    assert lines[2] == "撳確認："
    # The proposal leads; the batch that does not fill the remainder stays
    # reachable, because the matcher is only guessing.
    assert [btn.callback_data for btn in edited_buttons(q)][0] == f"credit:link:1:{s2}"


def test_credits_detail_offers_a_batch_that_matches_exactly(db_path):
    sid = batch(db_path, "A1", f"{YESTERDAY} 09:00:00", 1450.0)
    insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 1450.0, "currency": "HKD",
                            "value_date": TODAY, "payer": None, "memo": None, "email_id": None,
                            "received_at": None, "recorded_at": None})
    msg = run_credits("1")
    assert [b.callback_data for b in reply_buttons(msg)] == [f"credit:link:1:{sid}"]


def test_card_tap_to_zero_removes_the_buttons(db_path):
    s1 = batch(db_path, "A1", f"{TWO_DAYS} 09:00:00", 930.0)
    batch(db_path, "A2", f"{YESTERDAY} 09:00:00", 930.0)   # a second 930: no exact link
    write_feed(feed_line("R1", 930.0, TODAY))
    tick(fake_bot())
    cb, q = callback_update(f"credit:link:1:{s1}")
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    assert edited(q) == f"入數 $930 · {credits.md(TODAY)} · 已全部對齊"
    assert q.message.edit_text.call_args.kwargs["reply_markup"] is None


def test_card_stale_tap_says_why_and_redraws(db_path):
    s1 = batch(db_path, "A1", f"{TWO_DAYS} 09:00:00", 930.0)
    batch(db_path, "A2", f"{YESTERDAY} 09:00:00", 1080.0)
    write_feed(feed_line("R1", 2950.0, TODAY))
    tick(fake_bot())
    other = insert_credit(db_path, {"ref": "R9", "platform": "ride", "amount": 930.0,
                                    "currency": "HKD", "value_date": TODAY, "payer": None,
                                    "memo": None, "email_id": None, "received_at": None,
                                    "recorded_at": None})
    allocate(db_path, other, s1)
    cb, q = callback_update(f"credit:link:1:{s1}")
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    q.answer.assert_awaited_once_with("批次已收齊")
    assert edited(q).startswith(f"入數 $2,950 · {credits.md(TODAY)} · 未對")
    assert get_settlement(db_path, s1)["allocations"][0]["credit_id"] == other


def test_card_tap_on_an_archived_credit_offers_nothing(db_path):
    s1 = batch(db_path, "A1", f"{TWO_DAYS} 09:00:00", 930.0)
    batch(db_path, "A2", f"{YESTERDAY} 09:00:00", 1080.0)
    write_feed(feed_line("R1", 2950.0, TODAY))
    tick(fake_bot())
    run_credits("archive", "1", "唔係我哋嘅")
    cb, q = callback_update(f"credit:link:1:{s1}")
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    q.answer.assert_awaited_once_with("入數已收埋")
    assert edited(q).split("\n")[1] == "冇 batch 啱銀碼"
    assert edited_buttons(q) == []


def test_card_tap_on_a_vanished_credit_only_answers(db_path):
    s1 = batch(db_path, "A1", f"{TWO_DAYS} 09:00:00", 930.0)
    cb, q = callback_update(f"credit:link:999:{s1}")
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    q.answer.assert_awaited_once_with("搵唔到入數")
    q.message.edit_text.assert_not_awaited()


# ---- the statement flow ----

def stmt_for(rows_by_date, total):
    days = []
    for d, rows in rows_by_date.items():
        days.append(StatementDay(date=d, count=len(rows), sum=round(sum(a for _, a in rows), 2),
                                 rows=[StatementRow(date=d, order_id=oid, amount=a, time="09:00",
                                                    settle_date=d) for oid, a in rows]))
    return Statement(days=days, account="YY0000", total=total, reader="test")


def use_statement(monkeypatch, stmt):
    monkeypatch.setattr(statement, "ocr_available", lambda: True)
    monkeypatch.setattr(statement, "read_image", lambda data: stmt)


def photo_update(message_id=500):
    msg = MagicMock()
    msg.chat_id = CHAT
    msg.message_id = message_id
    msg.photo = [MagicMock(file_id="big")]
    msg.document = None
    msg.reply_text = AsyncMock(return_value=MagicMock(message_id=777))
    return MagicMock(message=msg), msg


def context_with_file(data=b"\xff\xd8img"):
    ctx = MagicMock()
    f = MagicMock()
    f.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    ctx.bot.get_file = AsyncMock(return_value=f)
    return ctx


def confirm_a_statement(db_path, monkeypatch, order_id, amount, when=YESTERDAY):
    seed(db_path, order_id, f"{when} 09:00:00", amount)
    use_statement(monkeypatch, stmt_for({when: [(order_id, amount)]}, amount))
    upd, msg = photo_update()
    asyncio.run(bot.handle_statement_image(upd, context_with_file()))
    cb, q = callback_update("stmt:confirm")
    asyncio.run(bot.handle_callback(cb, context_with_file()))
    return q


def test_stmt_confirm_says_the_batch_is_paid_in_full(db_path, monkeypatch):
    insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 2540.0, "currency": "HKD",
                            "value_date": YESTERDAY, "payer": None, "memo": None,
                            "email_id": None, "received_at": None, "recorded_at": None})
    q = confirm_a_statement(db_path, monkeypatch, "A1", 2540.0)
    reply = q.message.reply_text.call_args.args[0]
    assert reply.endswith(f"\n批次 #1 收齊 · 已到帳 {credits.md(YESTERDAY)}")
    assert q.message.reply_text.call_args.kwargs["reply_markup"] is None


def test_stmt_confirm_offers_candidates(db_path, monkeypatch):
    insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 2950.0, "currency": "HKD",
                            "value_date": TODAY, "payer": None, "memo": None,
                            "email_id": None, "received_at": None, "recorded_at": None})
    q = confirm_a_statement(db_path, monkeypatch, "A1", 1450.0)
    reply = q.message.reply_text.call_args.args[0]
    assert reply.endswith("\n等緊過數 · 可能係：")
    markup = q.message.reply_text.call_args.kwargs["reply_markup"]
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["credit:pick:1:1"]
    assert buttons[0].text == f"入數 $2,950 · {credits.md(TODAY)}"


def test_pick_appends_to_the_settled_reply_instead_of_replacing_it(db_path):
    """The settled reply carries the line pasted back to the platform, so a tap
    on its credit buttons must leave it standing."""
    sid = batch(db_path, "A1", f"{YESTERDAY} 09:00:00", 1450.0)
    cid = insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 2950.0,
                                  "currency": "HKD", "value_date": "2026-08-24", "payer": None,
                                  "memo": None, "email_id": None, "received_at": None,
                                  "recorded_at": None})
    settled = "已結算 批次 #1 · 8月26日 · 1 程 · $1,450\n\n8月26日 共1程 HKD 1,450 確認無誤\n等緊過數 · 可能係："
    cb, q = callback_update(f"credit:pick:{cid}:{sid}", text=settled)
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    q.answer.assert_awaited_once_with("已對")
    assert edited(q) == settled + f"\n批次 #{sid} 收齊 · 已到帳 08-24"
    assert q.message.edit_text.call_args.kwargs["reply_markup"] is None
    assert get_settlement(db_path, sid)["state"] == "paid"


def test_pick_offers_the_change_left_on_the_credit(db_path):
    """A make-up payment arrives bundled into a bigger transfer, so the tap
    that spends the first part of a credit is what asks about the rest."""
    sid = batch(db_path, "A1", f"{YESTERDAY} 09:00:00", 1450.0)
    rest = batch(db_path, "A2", f"{TWO_DAYS} 09:00:00", 1500.0)
    cid = insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 2950.0,
                                  "currency": "HKD", "value_date": TODAY, "payer": None,
                                  "memo": None, "email_id": None, "received_at": None,
                                  "recorded_at": None})
    cb, q = callback_update(f"credit:pick:{cid}:{sid}", text="已結算 批次 #1")
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    lines = edited(q).split("\n")
    assert lines[-2] == f"批次 #{sid} 收齊 · 已到帳 {credits.md(TODAY)}"
    assert lines[-1] == "剩 $1,500 · 可能係："
    assert [b.callback_data for b in edited_buttons(q)] == [f"credit:link:{cid}:{rest}"]


def test_a_refused_pick_leaves_the_settled_reply_alone(db_path):
    sid = batch(db_path, "A1", f"{YESTERDAY} 09:00:00", 1450.0)
    cid = insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 2950.0,
                                  "currency": "HKD", "value_date": "2026-08-24", "payer": None,
                                  "memo": None, "email_id": None, "received_at": None,
                                  "recorded_at": None})
    run_credits("archive", str(cid), "唔係我哋嘅")
    cb, q = callback_update(f"credit:pick:{cid}:{sid}", text="已結算 批次 #1")
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    q.answer.assert_awaited_once_with("入數已收埋")
    q.message.edit_text.assert_not_awaited()
    assert get_settlement(db_path, sid)["received"] == 0.0


def test_stmt_confirm_offers_a_credit_that_would_pay_only_part(db_path, monkeypatch):
    """The batch-side card says what a tap would leave the batch owed, the same
    way the credit-side one does.  The money arrived a month after the service,
    which is too late for the window to prove it: it stays a suggestion the
    operator picks instead of an answer the tap acts on by itself."""
    insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 900.0, "currency": "HKD",
                            "value_date": LAST_MONTH, "payer": None, "memo": None,
                            "email_id": None, "received_at": None, "recorded_at": None})
    q = confirm_a_statement(db_path, monkeypatch, "A1", 1450.0, when=MONTHS_AGO)
    markup = q.message.reply_text.call_args.kwargs["reply_markup"]
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert buttons[0].text == f"入數 $900 · {credits.md(LAST_MONTH)}（差 $550）"


def test_stmt_confirm_reply_is_unchanged_without_credits(db_path, monkeypatch):
    q = confirm_a_statement(db_path, monkeypatch, "A1", 1450.0)
    reply = q.message.reply_text.call_args.args[0]
    assert "入數" not in reply and "等緊過數" not in reply
    assert q.message.reply_text.call_args.kwargs["reply_markup"] is None


# ---- the statement card against the ledger, before the batch exists ----

def credit_row(db_path, ref, amount, value_date):
    return insert_credit(db_path, {"ref": ref, "platform": "ride", "amount": amount,
                                   "currency": "HKD", "value_date": value_date,
                                   "payer": "A B**** C***** L", "memo": "SUPPLIERPAY",
                                   "email_id": None, "received_at": None, "recorded_at": None})


def card_for(monkeypatch, stmt):
    use_statement(monkeypatch, stmt)
    upd, msg = photo_update()
    asyncio.run(bot.handle_statement_image(upd, context_with_file()))
    return msg


def test_statement_card_names_the_credit_its_total_agrees_with(db_path, monkeypatch):
    """Case A: the money is already in the ledger, so the tap that confirms the
    statement is the tap that says which orders it paid for."""
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 2540.0)
    cid = credit_row(db_path, "R1", 2540.0, TODAY)
    msg = card_for(monkeypatch, stmt_for({YESTERDAY: [("A1", 2540.0)]}, 2540.0))
    assert reply_text(msg).endswith(f"\n對到入數 {credits.md(TODAY)} $2,540")
    assert [b.text for b in reply_buttons(msg)] == ["確認結算 + 對入數 · 1 程 · $2,540", "唔確認"]
    assert bot.pending_statements[777][4] == cid


def test_statement_card_says_when_the_platform_figure_disagrees_too(db_path, monkeypatch):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 2500.0)
    credit_row(db_path, "R1", 2540.0, TODAY)
    msg = card_for(monkeypatch, stmt_for({YESTERDAY: [("A1", 2540.0)]}, 2540.0))
    assert [b.text for b in reply_buttons(msg)] == [
        "照平台數確認 + 對入數 · 1 程 · $2,540（差額 +$40）", "唔確認"]


def test_statement_card_lists_the_credits_that_could_contain_it(db_path, monkeypatch):
    """Case B: nothing agrees to the cent, so the card names what could and
    leaves the choice to the reply's buttons after the batch exists."""
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 1450.0)
    credit_row(db_path, "R1", 2950.0, TODAY)
    msg = card_for(monkeypatch, stmt_for({YESTERDAY: [("A1", 1450.0)]}, 1450.0))
    lines = reply_text(msg).split("\n")
    assert lines[-2] == "入數可能係："
    assert lines[-1] == f"入數 $2,950 · {credits.md(TODAY)}"
    assert [b.text for b in reply_buttons(msg)] == ["確認結算 · 1 程 · $1,450", "唔確認"]
    assert bot.pending_statements[777][4] is None


def test_statement_card_says_the_money_has_not_arrived(db_path, monkeypatch):
    """Case C: the ordinary one — the platform pays days after the statement."""
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 1450.0)
    msg = card_for(monkeypatch, stmt_for({YESTERDAY: [("A1", 1450.0)]}, 1450.0))
    assert reply_text(msg).endswith("\n未收到呢筆數")
    assert [b.text for b in reply_buttons(msg)] == ["確認結算 · 1 程 · $1,450", "唔確認"]
    assert bot.pending_statements[777][4] is None


def test_statement_card_will_not_offer_a_credit_older_than_the_service(db_path, monkeypatch):
    """Case C again, with the queue full of old unmatched money: a credit that
    predates the legs is not what paid for them, so the card still says the
    money has not arrived rather than listing June's leftovers."""
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 1450.0)
    credit_row(db_path, "R1", 1450.0, MONTHS_AGO)
    msg = card_for(monkeypatch, stmt_for({YESTERDAY: [("A1", 1450.0)]}, 1450.0))
    assert reply_text(msg).endswith("\n未收到呢筆數")
    assert bot.pending_statements[777][4] is None


def test_statement_with_no_orders_offers_to_archive_the_credit(db_path, monkeypatch):
    """Case D: the money is ours but the legs never reached the system, so
    there is no batch to create — only a credit to take out of the queue."""
    cid = credit_row(db_path, "R1", 1000.0, TODAY)
    msg = card_for(monkeypatch, stmt_for({YESTERDAY: [("Z1", 600.0), ("Z2", 400.0)]}, 1000.0))
    assert reply_text(msg).endswith(
        f"\n對到入數 {credits.md(TODAY)} $1,000，但圖入面 2 張單唔喺系統")
    buttons = reply_buttons(msg)
    assert [b.callback_data for b in buttons] == [f"credit:archive:{cid}:no-orders"]
    assert buttons[0].text == "收埋入數（單未入系統）"
    assert bot.pending_statements == {}


def test_statement_with_no_orders_and_no_credit_offers_nothing(db_path, monkeypatch):
    """Case E."""
    credit_row(db_path, "R1", 999.0, TODAY)
    msg = card_for(monkeypatch, stmt_for({YESTERDAY: [("Z1", 1000.0)]}, 1000.0))
    assert reply_text(msg).endswith("冇單可以入 batch")
    assert reply_buttons(msg) == []


def test_credit_archive_takes_the_credit_out_of_the_queue(db_path, monkeypatch):
    cid = credit_row(db_path, "R1", 1000.0, TODAY)
    card_for(monkeypatch, stmt_for({YESTERDAY: [("Z1", 1000.0)]}, 1000.0))
    cb, q = callback_update(f"credit:archive:{cid}:no-orders")
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    q.answer.assert_awaited_once_with("已收埋")
    q.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    assert get_credit(db_path, cid)["archived_reason"] == "no-orders"
    assert unallocated_credits(db_path) == []


def test_stmt_confirm_links_the_credit_the_card_named(db_path, monkeypatch):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 2540.0)
    cid = credit_row(db_path, "R1", 2540.0, TODAY)
    card_for(monkeypatch, stmt_for({YESTERDAY: [("A1", 2540.0)]}, 2540.0))
    cb, q = callback_update("stmt:confirm")
    asyncio.run(bot.handle_callback(cb, context_with_file()))
    credit = get_credit(db_path, cid)
    assert len(credit["allocations"]) == 1
    sid = credit["allocations"][0]["settlement_id"]
    assert get_settlement(db_path, sid)["paid_on"] == TODAY
    reply = q.message.reply_text.call_args.args[0]
    assert reply.endswith(f"\n批次 #{sid} 收齊 · 已到帳 {credits.md(TODAY)}")
    assert q.message.reply_text.call_args.kwargs["reply_markup"] is None


def test_stmt_confirm_says_so_when_the_credit_was_spent_since_the_card(db_path, monkeypatch):
    """The card is a snapshot. A refused link must not cost the batch: it is
    created either way, and the reply falls back to the credits still free."""
    other = batch(db_path, "A2", f"{TWO_DAYS} 09:00:00", 2540.0)
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 2540.0)
    cid = credit_row(db_path, "R1", 2540.0, TODAY)
    spare = credit_row(db_path, "R2", 3000.0, TODAY)
    card_for(monkeypatch, stmt_for({YESTERDAY: [("A1", 2540.0)]}, 2540.0))
    allocate(db_path, cid, other)
    cb, q = callback_update("stmt:confirm")
    asyncio.run(bot.handle_callback(cb, context_with_file()))
    born = open_batches(db_path, "ride")
    assert [b["id"] for b in born] == [other + 1]
    reply = q.message.reply_text.call_args.args[0]
    assert "對唔到入數：" in reply and reply.endswith("等緊過數 · 可能係：")
    markup = q.message.reply_text.call_args.kwargs["reply_markup"]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [
        f"credit:pick:{spare}:{born[0]['id']}"]


def _call_sites(name):
    """Every (module, function) in the package that calls `name`."""
    package = pathlib.Path(bot.__file__).parent
    callers = set()
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for scope in ast.walk(tree):
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(scope):
                called = getattr(node, "func", None)
                if isinstance(node, ast.Call) and (
                        getattr(called, "id", None) == name
                        or getattr(called, "attr", None) == name):
                    callers.add((path.name, scope.name))
    return callers


def test_allocate_is_only_called_by_a_tapped_callback():
    """paid_on is written by allocate and by nothing else, and the operator's
    tap is the only thing allowed to call it: a matcher that moved money on its
    own would put it against orders nobody had checked.  A new call site has to
    be added here deliberately."""
    assert _call_sites("allocate") == {("bot.py", "handle_callback")}


def test_mark_unpaid_is_only_called_from_the_web_handler():
    """Naming the legs a short payment left out is a dashboard operation, not a
    chat interaction.  A new call site has to be added here deliberately."""
    assert _call_sites("mark_unpaid") == {("web.py", "api_mark_unpaid")}


def test_round_two_linking_is_gone_rather_than_unused():
    """One credit paying one batch in full cannot record a short payment, so
    the functions that only did that are removed: a caller of either would be
    writing a model the ledger no longer has."""
    assert not hasattr(db_module, "link_credit")
    assert not hasattr(db_module, "unlink_credit")
    assert _call_sites("link_credit") == set()
    assert _call_sites("unlink_credit") == set()


# ---- a statement the platform paid short ----

def replies(msg):
    return [c.args[0] for c in msg.reply_text.call_args_list]


def reply_markups(msg):
    return [c.kwargs.get("reply_markup") for c in msg.reply_text.call_args_list]


def buttons_of(markup):
    return [] if markup is None else [b for row in markup.inline_keyboard for b in row]


def short_statement(db_path, monkeypatch):
    """The case from production: $3,460 of statement, $2,950 paid, because the
    platform failed to submit …1704 ($210) and …3137 ($300)."""
    seed(db_path, "1128150000000001", f"{TWO_DAYS} 09:00:00", 2950.0)
    seed(db_path, "1128150000001704", f"{TWO_DAYS} 10:00:00", 210.0)
    seed(db_path, "1128150000003137", f"{TWO_DAYS} 11:00:00", 300.0)
    cid = credit_row(db_path, "R1", 2950.0, YESTERDAY)
    stmt = stmt_for({TWO_DAYS: [("1128150000000001", 2950.0),
                                ("1128150000001704", 210.0),
                                ("1128150000003137", 300.0)]}, 3460.0)
    return cid, stmt


def test_statement_card_offers_the_credit_that_pays_it_short(db_path, monkeypatch):
    """Case A': the money in the ledger does not cover the statement, which is
    what the platform said would happen.  The tap records what did arrive."""
    _cid, stmt = short_statement(db_path, monkeypatch)
    msg = card_for(monkeypatch, stmt)
    assert reply_text(msg).endswith(f"\n對到入數 {credits.md(YESTERDAY)} $2,950，差 $510")
    assert [b.text for b in reply_buttons(msg)] == [
        "確認結算 + 對入數 $2,950（差 $510）", "唔確認"]
    assert bot.pending_statements[777][4] == 1


def test_an_exact_credit_beats_a_short_one(db_path, monkeypatch):
    seed(db_path, "A1", f"{TWO_DAYS} 09:00:00", 1450.0)
    credit_row(db_path, "R1", 900.0, YESTERDAY)
    exact = credit_row(db_path, "R2", 1450.0, YESTERDAY)
    msg = card_for(monkeypatch, stmt_for({TWO_DAYS: [("A1", 1450.0)]}, 1450.0))
    assert reply_text(msg).endswith(f"\n對到入數 {credits.md(YESTERDAY)} $1,450")
    assert bot.pending_statements[777][4] == exact


def confirm_short(db_path, monkeypatch):
    """Confirm the short statement and return (settlement_id, query)."""
    cid, stmt = short_statement(db_path, monkeypatch)
    card_for(monkeypatch, stmt)
    cb, q = callback_update("stmt:confirm")
    asyncio.run(bot.handle_callback(cb, context_with_file()))
    sid = get_credit(db_path, cid)["allocations"][0]["settlement_id"]
    return sid, q


def tap(data, message_id):
    cb, q = callback_update(data, message_id=message_id)
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    return q


def test_confirming_a_short_statement_ends_with_the_dashboard_line(db_path, monkeypatch):
    """Round 4: the short allocation says where to name the legs, not here."""
    sid, q = confirm_short(db_path, monkeypatch)
    batch_ = get_settlement(db_path, sid)
    assert batch_["received"] == 2950.0 and batch_["outstanding"] == 510.0
    assert batch_["paid_on"] is None
    said = replies(q.message)
    assert len(said) == 1       # one reply, no tick card
    assert "已收 $2,950 · 未收 $510 · 平台查完喺 dashboard 入返邊張單" in said[0]


def test_tick_vocabulary_is_gone_from_the_bot():
    """Round 4: the tick card, its state, and /credits short are all gone."""
    assert not hasattr(bot, "pending_unpaid")
    assert not hasattr(bot, "unpaid_markup")
    assert not hasattr(bot, "_send_tick_card")
    assert "short" not in bot.CREDITS_USAGE


def test_credits_short_is_not_a_command(db_path):
    """The /credits short form is gone; typing it shows the usage string."""
    assert reply_text(run_credits("short", "1")) == bot.CREDITS_USAGE


def test_the_make_up_payment_closes_the_batch_and_names_the_legs(db_path, monkeypatch):
    """The whole point of the round: the $510 arrives later, alone or as the
    leftover of a bigger transfer, and the batch closes naming the two legs."""
    from ride_dispatch.db import mark_unpaid
    sid, _q = confirm_short(db_path, monkeypatch)
    mark_unpaid(db_path, sid, ["1128150000001704", "1128150000003137"])
    later = credit_row(db_path, "R2", 510.0, TODAY)
    q = tap(f"credit:link:{later}:{sid}", 900)
    said = replies(q.message)
    assert said[0] == (f"批次 #{sid} 收齊 · 已到帳 {credits.md(TODAY)}\n"
                       "到帳：…1704、…3137")
    batch_ = get_settlement(db_path, sid)
    assert batch_["state"] == "paid" and batch_["paid_on"] == TODAY
    assert all(o["unpaid"] == 0 for o in batch_["orders"])


def test_a_part_payment_from_the_credit_card_ends_with_the_dashboard_line(db_path):
    """The statement can be settled before the money lands, so the short
    payment arrives at the credit card instead: same reply, dashboard line."""
    seed(db_path, "1128150000000001", f"{TWO_DAYS} 09:00:00", 2950.0)
    seed(db_path, "1128150000001704", f"{TWO_DAYS} 10:00:00", 510.0)
    sid = create_settlement(db_path, "ride", ["1128150000000001", "1128150000001704"],
                            3460.0, TODAY)
    write_feed(feed_line("R1", 2950.0, TODAY))
    tick(fake_bot())
    q = tap(f"credit:link:1:{sid}", 901)
    assert edited(q) == f"入數 $2,950 · {credits.md(TODAY)} · 已全部對齊"
    said = replies(q.message)
    assert len(said) == 1
    assert "已收 $2,950 · 未收 $510 · 平台查完喺 dashboard 入返邊張單" in said[0]


def test_a_leftover_credit_is_chained_onto_the_batch_that_is_short(db_path, monkeypatch):
    """A make-up payment bundled into a later day's transfer: the tap that
    spends the first part of the credit is what offers the rest."""
    sid, _q = confirm_short(db_path, monkeypatch)
    other = batch(db_path, "B1", f"{YESTERDAY} 09:00:00", 1000.0)
    bundle = credit_row(db_path, "R2", 1510.0, TODAY)
    q = tap(f"credit:pick:{bundle}:{other}", 902)
    lines = edited(q).split("\n")
    # `other` is fully paid by the $1,510 credit ($1,000 used), so no
    # dashboard line for it; the remaining $510 is offered against `sid`.
    assert lines[-1] == "剩 $510 · 可能係："
    assert [b.callback_data for b in edited_buttons(q)] == [f"credit:link:{bundle}:{sid}"]
    assert edited_buttons(q)[0].text.endswith("· $510")


# ---- /credits ----

def test_credits_queue_is_empty(db_path):
    assert reply_text(run_credits()) == "全部對齊。"


def test_credits_queue_lists_oldest_first(db_path):
    insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 1000.0, "currency": "HKD",
                            "value_date": "2026-06-05", "payer": None, "memo": None,
                            "email_id": None, "received_at": None, "recorded_at": None})
    s1 = batch(db_path, "A1", f"{YESTERDAY} 09:00:00", 500.0)
    cid = insert_credit(db_path, {"ref": "R2", "platform": "ride", "amount": 2000.0,
                                  "currency": "HKD", "value_date": "2026-08-24", "payer": None,
                                  "memo": None, "email_id": None, "received_at": None,
                                  "recorded_at": None})
    allocate(db_path, cid, s1)
    lines = reply_text(run_credits()).split("\n")
    assert lines == ["未對 2 筆 · $2,500 · 最舊 06-05",
                     "#1 · 06-05 · $1,000",
                     "#2 · 08-24 · $2,000 · 剩 $1,500"]


def test_credits_queue_truncates(db_path):
    for i in range(1, 24):
        insert_credit(db_path, {"ref": f"R{i}", "platform": "ride", "amount": 100.0,
                                "currency": "HKD", "value_date": "2026-06-05", "payer": None,
                                "memo": None, "email_id": None, "received_at": None,
                                "recorded_at": None})
    lines = reply_text(run_credits()).split("\n")
    assert len(lines) == 1 + credits.QUEUE_LIMIT + 1
    assert lines[-1] == "…仲有 3 筆"


def test_credits_detail_shows_links_and_offers_buttons(db_path):
    s1 = batch(db_path, "A1", f"{YESTERDAY} 09:00:00", 1450.0)
    batch(db_path, "A2", f"{TWO_DAYS} 09:00:00", 500.0)
    cid = insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 2540.0,
                                  "currency": "HKD", "value_date": "2026-08-26",
                                  "payer": "A B**** C***** L", "memo": "SUPPLIERPAY",
                                  "email_id": None, "received_at": None, "recorded_at": None})
    allocate(db_path, cid, s1)
    msg = run_credits("1")
    lines = reply_text(msg).split("\n")
    assert lines[0] == "入數 #1 · $2,540 · 08-26 · SUPPLIERPAY"
    assert lines[1] == f"已對：批次 #{s1} $1,450"
    assert lines[2] == "剩 $1,090"
    assert [b.callback_data for b in reply_buttons(msg)] == ["credit:link:1:2"]


def test_credits_detail_of_an_unlinked_credit(db_path):
    insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 1000.0, "currency": "HKD",
                            "value_date": "2026-06-05", "payer": None, "memo": None,
                            "email_id": None, "received_at": None, "recorded_at": None})
    assert reply_text(run_credits("1")).split("\n") == ["入數 #1 · $1,000 · 06-05", "未對", "剩 $1,000"]


def test_credits_detail_unknown_id(db_path):
    assert reply_text(run_credits("9")) == "搵唔到入數 #9"


def test_credits_archive_before_and_unarchive(db_path):
    for ref, day in (("R1", "2026-06-05"), ("R2", "2026-06-26"), ("R3", "2026-06-27")):
        insert_credit(db_path, {"ref": ref, "platform": "ride", "amount": 1000.0,
                                "currency": "HKD", "value_date": day, "payer": None, "memo": None,
                                "email_id": None, "received_at": None, "recorded_at": None})
    assert reply_text(run_credits("archive", "before", "2026-06-27")) == "收埋 2 筆 · $2,000（06-27 之前）"
    assert [c["ref"] for c in unallocated_credits(db_path)] == ["R3"]
    assert get_credit(db_path, 1)["archived_reason"] == "pre-system"
    assert reply_text(run_credits("unarchive", "1")) == "#1 返返嚟"
    assert [c["ref"] for c in unallocated_credits(db_path)] == ["R1", "R3"]
    assert reply_text(run_credits("unarchive", "9")) == "搵唔到入數 #9"


def test_credits_archive_one_with_a_note(db_path):
    insert_credit(db_path, {"ref": "R1", "platform": "ride", "amount": 1000.0, "currency": "HKD",
                            "value_date": "2026-06-05", "payer": None, "memo": None,
                            "email_id": None, "received_at": None, "recorded_at": None})
    assert reply_text(run_credits("archive", "1", "唔係我哋嘅")) == "收埋 #1"
    assert get_credit(db_path, 1)["archived_reason"] == "manual: 唔係我哋嘅"
    assert reply_text(run_credits("1")).endswith("已收埋（manual: 唔係我哋嘅）")
    assert reply_text(run_credits("archive", "9")) == "搵唔到入數 #9"


def test_credits_unlink_returns_a_batch_to_awaiting(db_path):
    sid = batch(db_path, "A1", f"{YESTERDAY} 09:00:00", 2540.0)
    write_feed(feed_line("R1", 2540.0, YESTERDAY))
    tick(fake_bot())
    cb, _q = callback_update(f"credit:link:1:{sid}")
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    assert get_settlement(db_path, sid)["paid_on"] == YESTERDAY
    assert reply_text(run_credits("unlink", str(sid))) == f"批次 #{sid} 解除咗入數，返回等過數"
    assert get_settlement(db_path, sid)["paid_on"] is None
    assert get_credit(db_path, 1)["remaining"] == 2540.0
    assert reply_text(run_credits("unlink", str(sid))) == f"批次 #{sid} 冇對住入數"


def test_credits_unknown_form_shows_the_usage(db_path):
    assert reply_text(run_credits("archive", "before", "nonsense")) == bot.CREDITS_USAGE
    assert reply_text(run_credits("blah")) == bot.CREDITS_USAGE
    assert reply_text(run_credits("unlink")) == bot.CREDITS_USAGE


def test_credits_is_registered_as_a_command(db_path):
    app = MagicMock()
    app.bot.set_my_commands = AsyncMock()
    asyncio.run(bot._set_commands(app))
    assert "credits" in {c.command for c in app.bot.set_my_commands.call_args.args[0]}
