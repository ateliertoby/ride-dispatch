"""Re-entering a cancelled order through the bot: revive, not duplicate."""
import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

import ride_dispatch.bot as bot
from ride_dispatch.db import cancel_order, get_order_by_id, save_or_revive_order, update_price
from ride_dispatch.parser import Order

CHAT = 123
CARD_MSG_ID = 55


def make_order(**overrides) -> Order:
    defaults = dict(
        order_id="1128000000000099", service_type="接机", vehicle_type="经济5座",
        passenger_name="WANG/XIAOMING", scheduled_time="2026-08-23 19:30:00",
        passenger_phone="86 13800000000", overseas_phone="", flight_number="CA727",
        pickup="香港国际机场 T1", dropoff="尖沙咀", distance_km=30, notes="",
        driver_notes="", additional_services="", passenger_exit_minutes=30,
        third_party_contact="", more_contacts="", raw_message="raw",
    )
    defaults.update(overrides)
    return Order(**defaults)


@pytest.fixture
def db_path(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from ride_dispatch.db import init_db
    init_db(path)
    monkeypatch.setattr(bot, "DB_PATH", path)
    monkeypatch.setattr(bot, "ALLOWED_CHAT_IDS", set())
    monkeypatch.setattr(bot, "pending", {})
    monkeypatch.setattr(bot, "awaiting_price", {})
    monkeypatch.setattr(bot, "awaiting_cost", {})
    yield path
    os.unlink(path)


def seed_cancelled(db_path, price=500.0):
    save_or_revive_order(db_path, make_order(), telegram_msg_id=1, parking=32.0, source="携程")
    update_price(db_path, make_order().order_id, price)
    cancel_order(db_path, make_order().order_id)


def _confirm_callback():
    q = MagicMock()
    q.data = "confirm"
    q.message.chat_id = CHAT
    q.message.message_id = CARD_MSG_ID
    q.answer = AsyncMock()
    q.message.edit_reply_markup = AsyncMock()
    q.message.reply_text = AsyncMock(return_value=MagicMock(message_id=99))
    upd = MagicMock()
    upd.callback_query = q
    return upd, MagicMock(), q


def _price_message(text="600"):
    msg = MagicMock()
    msg.text = text
    msg.chat_id = CHAT
    msg.reply_to_message = None
    msg.reply_text = AsyncMock()
    upd = MagicMock()
    upd.message = msg
    ctx = MagicMock()
    ctx.bot.edit_message_reply_markup = AsyncMock()
    return upd, ctx, msg


# ---- confirm button ----

def test_confirm_revives_cancelled_order(db_path):
    seed_cancelled(db_path)
    order = make_order(scheduled_time="2026-08-24 07:00:00", flight_number="CX255")
    bot.pending[CARD_MSG_ID] = (order, "携程", CHAT)
    upd, ctx, q = _confirm_callback()
    asyncio.run(bot.handle_callback(upd, ctx))

    assert "重新入單" in q.message.reply_text.call_args.args[0]
    row = get_order_by_id(db_path, order.order_id)
    assert row["scheduled_time"] == "2026-08-24 07:00:00"
    assert row["flight_number"] == "CX255"
    assert row["telegram_msg_id"] == 99
    assert row["price"] is None
    assert bot.awaiting_price[CHAT] == (order.order_id, 0.0)


def test_confirm_still_rejects_active_duplicate(db_path):
    save_or_revive_order(db_path, make_order(), telegram_msg_id=1, source="携程")
    update_price(db_path, make_order().order_id, 500.0)
    bot.pending[CARD_MSG_ID] = (make_order(dropoff="中環"), "携程", CHAT)
    upd, ctx, q = _confirm_callback()
    asyncio.run(bot.handle_callback(upd, ctx))

    assert q.message.reply_text.call_args.args[0] == "呢張單已經存在。"
    q.answer.assert_awaited_with("訂單已存在")
    row = get_order_by_id(db_path, make_order().order_id)
    assert row["dropoff"] == "尖沙咀" and row["price"] == 500.0


# ---- bare-price shortcut ----

def test_bare_price_revives_cancelled_order(db_path):
    seed_cancelled(db_path)
    order = make_order(dropoff="中環")
    bot.pending[CARD_MSG_ID] = (order, "携程", CHAT)
    upd, ctx, msg = _price_message("600")
    asyncio.run(bot.handle_message(upd, ctx))

    assert msg.reply_text.call_args.args[0].startswith("已重新入單")
    row = get_order_by_id(db_path, order.order_id)
    assert row["dropoff"] == "中環"
    assert row["price"] == 600.0
    assert row["telegram_msg_id"] == CARD_MSG_ID


def test_bare_price_still_rejects_active_duplicate(db_path):
    save_or_revive_order(db_path, make_order(), telegram_msg_id=1, source="携程")
    bot.pending[CARD_MSG_ID] = (make_order(dropoff="中環"), "携程", CHAT)
    upd, ctx, msg = _price_message("600")
    asyncio.run(bot.handle_message(upd, ctx))

    assert msg.reply_text.call_args.args[0] == "呢張單已經存在。"
    row = get_order_by_id(db_path, make_order().order_id)
    assert row["dropoff"] == "尖沙咀" and row["price"] is None
