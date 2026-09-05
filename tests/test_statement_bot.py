import asyncio
import os
import tempfile
import threading
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import ride_dispatch.bot as bot
from ride_dispatch import statement, statement_flow
from ride_dispatch.db import (
    init_db, save_order, update_price, get_settlement, get_order_by_id, create_settlement,
    open_batches, statement_image_path, statements_dir,
)
from ride_dispatch.parser import Order
from ride_dispatch.statement import Statement, StatementDay, StatementRow, format_report, date_span_label

CHAT = 123
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
TWO_DAYS = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")


def make_order(order_id, scheduled, service_type="送机"):
    return Order(order_id=order_id, service_type=service_type, vehicle_type="经济5座", passenger_name="TEST/USER",
                 scheduled_time=scheduled, passenger_phone="86 13800000000", overseas_phone="", flight_number="",
                 pickup="尖沙咀", dropoff="香港国际机场 T1", distance_km=30, notes="", driver_notes="",
                 additional_services="", passenger_exit_minutes=None, third_party_contact="", more_contacts="",
                 raw_message="raw")


@pytest.fixture
def db_path(monkeypatch):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "orders.db")
    init_db(path)
    monkeypatch.setattr(bot, "DB_PATH", path)
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    bot.pending_statements.clear()
    yield path


def seed(db_path, oid, scheduled, price=210.0):
    save_order(db_path, make_order(oid, scheduled), telegram_msg_id=1, parking=0.0, source="携程")
    update_price(db_path, oid, price)


def _commands():
    """The command list the bot registers with Telegram."""
    app = MagicMock()
    app.bot.set_my_commands = AsyncMock()
    asyncio.run(bot._set_commands(app))
    return app.bot.set_my_commands.call_args.args[0]


def stmt_for(rows_by_date, total):
    days = []
    for date, rows in rows_by_date.items():
        days.append(StatementDay(date=date, count=len(rows), sum=round(sum(a for _, a in rows), 2),
                                 rows=[StatementRow(date=date, order_id=oid, amount=a, time="09:00",
                                                    settle_date=date) for oid, a in rows]))
    return Statement(days=days, account="YY0000", total=total, reader="test")


def photo_update(message_id=500):
    msg = MagicMock()
    msg.chat_id = CHAT
    msg.message_id = message_id
    msg.photo = [MagicMock(file_id="small"), MagicMock(file_id="big")]
    msg.document = None
    sent = MagicMock(message_id=777)
    msg.reply_text = AsyncMock(return_value=sent)
    upd = MagicMock(message=msg)
    return upd, msg


def context_with_file(data=b"\xff\xd8img"):
    ctx = MagicMock()
    f = MagicMock()
    f.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    ctx.bot.get_file = AsyncMock(return_value=f)
    return ctx


def callback_update(data, message_id=777):
    q = MagicMock()
    q.data = data
    q.message.message_id = message_id
    q.message.chat_id = CHAT
    q.message.edit_reply_markup = AsyncMock()
    q.message.reply_text = AsyncMock()
    q.answer = AsyncMock()
    return MagicMock(callback_query=q), q


def use_statement(monkeypatch, stmt):
    """Returns a list that records, per call, whether read_image ran on the
    main thread — the OCR must stay off the event loop."""
    on_main = []
    monkeypatch.setattr(statement, "ocr_available", lambda: True)

    def read(data):
        on_main.append(threading.current_thread() is threading.main_thread())
        return stmt

    monkeypatch.setattr(statement, "read_image", read)
    return on_main


def buttons_of(msg):
    markup = msg.reply_text.call_args.kwargs.get("reply_markup")
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


def sent_text(msg):
    return msg.reply_text.call_args.args[0]


# ---- card ----

def test_clean_statement_card_and_confirm(db_path, monkeypatch):
    seed(db_path, "A1", f"{TWO_DAYS} 09:00:00", 280.0)
    seed(db_path, "B1", f"{YESTERDAY} 10:00:00", 210.0)
    on_main = use_statement(monkeypatch, stmt_for({TWO_DAYS: [("A1", 280.0)], YESTERDAY: [("B1", 210.0)]}, 490.0))
    upd, msg = photo_update()
    ctx = context_with_file()
    asyncio.run(bot.handle_statement_image(upd, ctx))
    ctx.bot.get_file.assert_awaited_once_with("big")
    assert on_main == [False]
    text = sent_text(msg)
    assert text.startswith("結算單 YY0000 · 2 日 2 行 · 平台 $490")
    assert "✓" in text and "差額 $0" in text
    assert buttons_of(msg) == ["確認結算 · 2 程 · $490", "唔確認"]
    assert 777 in bot.pending_statements

    cb, q = callback_update("stmt:confirm")
    asyncio.run(bot.handle_callback(cb, ctx))
    batches = open_batches(db_path, "ride")
    assert len(batches) == 1
    b = batches[0]
    assert b["confirmed_amount"] == 490.0 and b["expected_amount"] == 490.0
    assert b["statement"]["total"] == 490.0 and b["statement"]["reader"] == "test"
    assert os.path.exists(statement_image_path(db_path, b["id"], "jpg"))
    assert get_order_by_id(db_path, "A1")["settlement_id"] == b["id"]
    reply = q.message.reply_text.call_args.args[0]
    assert reply.startswith(f"已結算 批次 #{b['id']}")
    assert f"{date_span_label([TWO_DAYS, YESTERDAY])} 共2程 HKD 490 確認無誤" in reply
    q.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    assert 777 not in bot.pending_statements


def test_confirm_stores_corrected_ids(db_path, monkeypatch):
    """A near-miss id is stored as the order it was bound to, so batch detail
    puts the platform figure on that order instead of listing it as an extra."""
    seed(db_path, "A1", f"{YESTERDAY} 10:00:00", 210.0)
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A2", 210.0)]}, 210.0))
    upd, msg = photo_update()
    ctx = context_with_file()
    asyncio.run(bot.handle_statement_image(upd, ctx))
    cb, q = callback_update("stmt:confirm")
    asyncio.run(bot.handle_callback(cb, ctx))

    b = open_batches(db_path, "ride")[0]
    stored = b["statement"]["days"][0]["rows"][0]
    assert stored["order_id"] == "A1" and stored["read_as"] == "A2"
    assert get_order_by_id(db_path, "A1")["settlement_id"] == b["id"]


def test_confirmation_line_keeps_cents(db_path, monkeypatch):
    seed(db_path, "A1", f"{TWO_DAYS} 09:00:00", 280.0)
    seed(db_path, "B1", f"{YESTERDAY} 10:00:00", 210.0)
    use_statement(monkeypatch, stmt_for({TWO_DAYS: [("A1", 280.5)], YESTERDAY: [("B1", 210.0)]}, 490.5))
    upd, msg = photo_update()
    ctx = context_with_file()
    asyncio.run(bot.handle_statement_image(upd, ctx))
    cb, q = callback_update("stmt:confirm")
    asyncio.run(bot.handle_callback(cb, ctx))
    reply = q.message.reply_text.call_args.args[0]
    assert "HKD 490.50 確認無誤" in reply
    assert open_batches(db_path, "ride")[0]["confirmed_amount"] == 490.5


def test_mixed_statement_lists_problems_and_offers_platform_figure(db_path, monkeypatch):
    seed(db_path, "OK", f"{YESTERDAY} 09:00:00", 210.0)
    seed(db_path, "DIFF", f"{YESTERDAY} 10:00:00", 250.0)
    seed(db_path, "HELD", f"{YESTERDAY} 11:00:00", 210.0)
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("OK", 210.0), ("DIFF", 210.0), ("NEW", 100.0)]}, 520.0))
    upd, msg = photo_update()
    asyncio.run(bot.handle_statement_image(upd, context_with_file()))
    text = sent_text(msg)
    assert "金額唔同" in text and "#…DIFF" in text and "平台 $210 · 系統 $250" in text
    assert "唔喺系統" in text and "#…NEW" in text
    assert "抽起" in text and "#…HELD" in text
    assert buttons_of(msg) == ["照平台數確認 · 2 程 · $520（差額 +$60）", "唔確認"]


def test_checksum_failure_has_no_button(db_path, monkeypatch):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 280.0)
    s = stmt_for({YESTERDAY: [("A1", 280.0)]}, 280.0)
    s.days[0].sum = 290.0
    use_statement(monkeypatch, s)
    upd, msg = photo_update()
    asyncio.run(bot.handle_statement_image(upd, context_with_file()))
    text = sent_text(msg)
    assert "讀圖唔一致" in text and "Send as file" in text
    assert buttons_of(msg) == []
    assert bot.pending_statements == {}


def test_resend_after_settling_shows_no_button(db_path, monkeypatch):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 280.0)
    create_settlement(db_path, "ride", ["A1"], 280.0, YESTERDAY)
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 280.0)]}, 280.0))
    upd, msg = photo_update()
    asyncio.run(bot.handle_statement_image(upd, context_with_file()))
    assert "已結算" in sent_text(msg) and "批次 #1" in sent_text(msg)
    assert "冇單可以入 batch" in sent_text(msg) and "差額" not in sent_text(msg)
    assert buttons_of(msg) == []


def test_expired_confirm(db_path):
    cb, q = callback_update("stmt:confirm", message_id=999)
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    q.answer.assert_awaited_once_with("已過期，再 send 一次張圖")


def test_confirm_reports_create_settlement_error(db_path, monkeypatch):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 280.0)
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 280.0)]}, 280.0))
    upd, msg = photo_update()
    asyncio.run(bot.handle_statement_image(upd, context_with_file()))

    def refuse(*a, **k):
        raise ValueError("A1: 已經結算咗")

    monkeypatch.setattr(statement_flow, "create_settlement", refuse)
    cb, q = callback_update("stmt:confirm")
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    q.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    q.answer.assert_awaited_once_with("結算唔到")
    assert q.message.reply_text.call_args.args[0].startswith("結算唔到：A1: 已經結算咗")
    assert open_batches(db_path, "ride") == []
    assert 777 not in bot.pending_statements


def test_skip_drops_pending(db_path, monkeypatch):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 280.0)
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 280.0)]}, 280.0))
    upd, msg = photo_update()
    asyncio.run(bot.handle_statement_image(upd, context_with_file()))
    cb, q = callback_update("stmt:skip")
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    assert bot.pending_statements == {}
    assert open_batches(db_path, "ride") == []


def test_document_image_is_accepted(db_path, monkeypatch):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 280.0)
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 280.0)]}, 280.0))
    upd, msg = photo_update()
    msg.photo = []
    msg.document = MagicMock(file_id="doc1")
    ctx = context_with_file()
    asyncio.run(bot.handle_statement_image(upd, ctx))
    ctx.bot.get_file.assert_awaited_once_with("doc1")
    assert buttons_of(msg) == ["確認結算 · 1 程 · $280", "唔確認"]


def test_unreadable_image(db_path, monkeypatch):
    use_statement(monkeypatch, Statement(days=[], warnings=["image could not be decoded"]))
    upd, msg = photo_update()
    asyncio.run(bot.handle_statement_image(upd, context_with_file()))
    assert "讀唔到" in sent_text(msg) and buttons_of(msg) == []


def test_unreadable_image_is_kept_and_logged(db_path, monkeypatch, caplog):
    """A screenshot the reader cannot read is the only evidence of the bug that
    stopped it, and the operator's copy scrolls away."""
    use_statement(monkeypatch, Statement(days=[], warnings=["冇日期 / 訂單行"]))
    upd, msg = photo_update()
    with caplog.at_level("WARNING", logger="bot"):
        asyncio.run(bot.handle_statement_image(upd, context_with_file()))
    assert "讀唔到" in sent_text(msg)
    kept = os.listdir(os.path.join(statements_dir(db_path), "failed"))
    assert len(kept) == 1 and kept[0].endswith("-big.jpg")
    with open(os.path.join(statements_dir(db_path), "failed", kept[0]), "rb") as f:
        assert f.read() == b"\xff\xd8img"
    assert "statement unreadable" in caplog.text
    assert "chat=123" in caplog.text and "file_id=big" in caplog.text
    assert "冇日期 / 訂單行" in caplog.text


def test_a_readable_image_keeps_nothing(db_path, monkeypatch):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 280.0)
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 280.0)]}, 280.0))
    upd, msg = photo_update()
    asyncio.run(bot.handle_statement_image(upd, context_with_file()))
    assert not os.path.isdir(os.path.join(statements_dir(db_path), "failed"))


def test_download_failure_is_reported_not_raised(db_path, monkeypatch, caplog):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 280.0)
    use_statement(monkeypatch, stmt_for({YESTERDAY: [("A1", 280.0)]}, 280.0))
    upd, msg = photo_update()
    ctx = context_with_file()
    ctx.bot.get_file = AsyncMock(side_effect=RuntimeError("expired"))
    with caplog.at_level("ERROR", logger="bot"):
        asyncio.run(bot.handle_statement_image(upd, ctx))
    assert "statement image failed" in caplog.text
    text = sent_text(msg)
    assert "讀圖出錯" in text and "RuntimeError" in text and "Send as file" in text
    assert bot.pending_statements == {}


def test_read_failure_is_reported_not_raised(db_path, monkeypatch):
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 280.0)
    monkeypatch.setattr(statement, "ocr_available", lambda: True)

    def boom(data):
        raise ValueError("bad rectangle")

    monkeypatch.setattr(statement, "read_image", boom)
    upd, msg = photo_update()
    asyncio.run(bot.handle_statement_image(upd, context_with_file()))
    text = sent_text(msg)
    assert "讀圖出錯" in text and "ValueError" in text
    assert bot.pending_statements == {}


def test_fallback_when_ocr_missing(db_path, monkeypatch):
    old = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
    recent = (datetime.now() - timedelta(days=13)).strftime("%Y-%m-%d")
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 280.0)
    seed(db_path, "B1", f"{TWO_DAYS} 10:00:00", 210.0)
    seed(db_path, "OLD1", f"{old} 09:00:00", 300.0)
    seed(db_path, "NEW9", f"{recent} 09:00:00", 320.0)
    monkeypatch.setattr(statement, "ocr_available", lambda: False)
    upd, msg = photo_update()
    ctx = context_with_file()
    asyncio.run(bot.handle_statement_image(upd, ctx))
    ctx.bot.get_file.assert_not_awaited()
    text = sent_text(msg)
    assert text.startswith("OCR 未裝")
    assert "#…A1" in text and "$280" in text and "#…B1" in text
    # The report covers the last 14 days: anything older is out of the window.
    assert "#…NEW9" in text
    assert "OLD1" not in text
    assert buttons_of(msg) == []


# ---- 判罰賠款 ----

def penalty_stmt():
    """One clean leg plus a trip the platform fined: the fine is a second row
    under the trip's own order id, so the day nets to 482.62."""
    day1 = StatementDay(date=TWO_DAYS, count=1, sum=300.0,
                        rows=[StatementRow(date=TWO_DAYS, order_id="A1", amount=300.0, time="09:00")])
    day2 = StatementDay(date=YESTERDAY, count=2, sum=182.62, rows=[
        StatementRow(date=YESTERDAY, order_id="B1", amount=280.0, time="13:00"),
        StatementRow(date=YESTERDAY, order_id="B1", amount=-97.38, time="13:00"),
    ])
    return Statement(days=[day1, day2], account="YY0000", total=482.62, reader="test")


def test_penalty_card_names_the_fine_and_confirms_it(db_path, monkeypatch):
    seed(db_path, "A1", f"{TWO_DAYS} 09:00:00", 300.0)
    seed(db_path, "B1", f"{YESTERDAY} 13:00:00", 280.0)
    use_statement(monkeypatch, penalty_stmt())
    upd, msg = photo_update()
    ctx = context_with_file()
    asyncio.run(bot.handle_statement_image(upd, ctx))
    text = sent_text(msg)
    assert "判罰  #…B1  −$97.38 · 該程 $280 → 淨 $182.62" in text
    assert "差額 $0" in text
    assert buttons_of(msg) == ["確認結算 + 記判罰 · 2 程 · $482.62", "唔確認"]

    cb, q = callback_update("stmt:confirm")
    asyncio.run(bot.handle_callback(cb, ctx))
    b = open_batches(db_path, "ride")[0]
    assert get_order_by_id(db_path, "B1")["penalty_fee"] == 97.38
    assert get_order_by_id(db_path, "A1")["penalty_fee"] is None
    # The frozen figure is net: the batch is owed what the transfer will carry.
    assert b["expected_amount"] == 482.62 and b["confirmed_amount"] == 482.62
    assert "已記判罰 −$97.38" in q.message.reply_text.call_args.args[0]


def test_resending_a_confirmed_penalty_statement_settles_nothing(db_path, monkeypatch):
    """Idempotency: the recorded fine nets into what the system expects, so the
    second read agrees with the platform instead of chasing a shortfall."""
    seed(db_path, "A1", f"{TWO_DAYS} 09:00:00", 300.0)
    seed(db_path, "B1", f"{YESTERDAY} 13:00:00", 280.0)
    use_statement(monkeypatch, penalty_stmt())
    upd, msg = photo_update()
    ctx = context_with_file()
    asyncio.run(bot.handle_statement_image(upd, ctx))
    cb, q = callback_update("stmt:confirm")
    asyncio.run(bot.handle_callback(cb, ctx))

    upd2, msg2 = photo_update(message_id=501)
    asyncio.run(bot.handle_statement_image(upd2, context_with_file()))
    text = sent_text(msg2)
    assert "已結算" in text and "冇單可以入 batch" in text
    assert "判罰" not in text
    assert buttons_of(msg2) == []
    assert get_order_by_id(db_path, "B1")["penalty_fee"] == 97.38


def test_a_late_penalty_on_a_settled_order_writes_nothing(db_path, monkeypatch):
    seed(db_path, "B1", f"{YESTERDAY} 13:00:00", 280.0)
    sid = create_settlement(db_path, "ride", ["B1"], 280.0, YESTERDAY)
    day = StatementDay(date=YESTERDAY, count=1, sum=-97.38,
                       rows=[StatementRow(date=YESTERDAY, order_id="B1", amount=-97.38, time="13:00")])
    use_statement(monkeypatch, Statement(days=[day], account="YY0000", total=-97.38, reader="test"))
    upd, msg = photo_update()
    asyncio.run(bot.handle_statement_image(upd, context_with_file()))
    text = sent_text(msg)
    assert f"判罰  #…B1  −$97.38（單已喺批次 #{sid}）— 要人手處理" in text
    assert buttons_of(msg) == []
    assert get_order_by_id(db_path, "B1")["penalty_fee"] is None


def test_the_order_card_shows_the_fine_and_the_net(db_path):
    seed(db_path, "B1", f"{YESTERDAY} 13:00:00", 280.0)
    create_settlement(db_path, "ride", ["B1"], 182.62, YESTERDAY, penalties={"B1": 97.38})
    msg = MagicMock()
    msg.chat_id = CHAT
    msg.reply_text = AsyncMock()
    ctx = MagicMock()
    ctx.args = ["order_B1"]
    asyncio.run(bot.handle_start(MagicMock(message=msg), ctx))
    lines = msg.reply_text.call_args.args[0].split("\n")
    assert "收入: $280" in lines
    assert lines[lines.index("收入: $280") + 1] == "判罰: −$97.38（淨收 $182.62）"


# ---- the manual paid mark is gone ----

def test_no_handler_marks_a_batch_paid_by_hand(db_path):
    """A batch becomes paid by money allocated to it, so the bot offers no
    command and no handler that sets the date itself.  Round 4 moved the tick
    card to the dashboard, so no paid-related symbols remain in the bot."""
    assert [n for n in dir(bot) if "paid" in n] == []
    assert "paid" not in {c.command for c in _commands()}


def test_a_retired_settlement_button_is_inert(db_path):
    """Buttons stay in the chat history after the branch behind them is gone;
    a tap on one must change nothing rather than raise."""
    seed(db_path, "A1", f"{YESTERDAY} 09:00:00", 280.0)
    sid = create_settlement(db_path, "ride", ["A1"], 280.0, YESTERDAY)
    cb, q = callback_update(f"stmt:retired:{sid}")
    asyncio.run(bot.handle_callback(cb, MagicMock()))
    assert get_settlement(db_path, sid)["paid_on"] is None
    q.message.reply_text.assert_not_awaited()


# ---- pure text builders ----

def test_date_span_label():
    assert date_span_label(["2026-08-23"]) == "8月23日"
    assert date_span_label(["2026-08-23", "2026-08-24"]) == "8月23–24日"
    assert date_span_label(["2026-08-30", "2026-09-01"]) == "8月30日、9月1日"
    assert date_span_label(["2026-08-30", "2026-08-31", "2026-09-01"]) == "8月30日–9月1日"


def test_money_str_puts_the_minus_outside_the_dollar_sign():
    """Reconciliation money is exact to the cent, and a negative amount reads
    as a deduction rather than as a mangled figure."""
    assert statement.money_str(97.38) == "$97.38"
    assert statement.money_str(-97.38) == "−$97.38"
    assert statement.money_str(-280.0) == "−$280"
    assert statement.money_str(-1234.5) == "−$1,234.50"
    assert statement.money_str(0.0) == "$0"


def test_confirmation_line_survives_a_negative_total():
    """The line is quoted back to the platform as the amount agreed, so a
    statement worth less than nothing must not lose its sign to the $ strip."""
    rec = statement.reconcile(
        Statement(days=[StatementDay(date="2026-08-23", count=1, sum=-97.38,
                                     rows=[StatementRow(date="2026-08-23", order_id="A1", amount=-97.38)])],
                  account="YY0000", total=-97.38),
        [], datetime(2026, 8, 26, 12, 0))
    assert statement.confirmation_line(rec, ["2026-08-23"]) == "8月23日 共0程 HKD −97.38 確認無誤"
