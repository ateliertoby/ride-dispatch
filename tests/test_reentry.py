"""Re-entering an order through the bot: revive a cancelled one, update a live one."""
import asyncio
import os
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import ride_dispatch.bot as bot
from ride_dispatch.db import (SETTLED_LOCK_MSG, cancel_order, create_settlement, get_order_by_id,
                             save_or_revive_order, update_price)
from ride_dispatch.ingest import parking_fee, parse_any
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


# ---- re-entry of a live order: the customer changed a detail ----

TC_ORIGINAL = """订单号：VBKSYNTHETIC0000003-同程
车型：经济5座
用车时间：2026-08-25 17:25:00
出发地：香港国际机场 T1
目的地：坑口地铁站A1出入口
航班号：￥ 3U3959
乘客姓名CHAN,TAIMAN"""

# Same booking, re-sent after the customer gave a full address: the 订单号
# suffix names the service type instead of the channel.
TC_RESENT = """订单号：VBKSYNTHETIC0000003（接机）
车型：经济5座
用车时间：2026-08-25 17:25:00
出发地：香港国际机场 T1
目的地：新界坑口裕明苑裕昌閣B座
订单里程：49.105 km
行驶时长：44 分钟
航班号：￥ 3U3959
乘客姓名CHAN,TAIMAN"""

TC_ID = "VBKSYNTHETIC0000003"
TC_SHORT = TC_ID[-4:]


def seed_live(db_path, price=280.0):
    order, source = parse_any(TC_ORIGINAL)
    save_or_revive_order(db_path, order, telegram_msg_id=1,
                         parking=parking_fee(order, source), source=source)
    if price is not None:
        update_price(db_path, TC_ID, price)


def _paste_message(text, reply_id=77):
    msg = MagicMock()
    msg.text = text
    msg.chat_id = CHAT
    msg.reply_to_message = None
    msg.reply_text = AsyncMock(return_value=MagicMock(message_id=reply_id))
    upd = MagicMock()
    upd.message = msg
    ctx = MagicMock()
    ctx.bot.edit_message_reply_markup = AsyncMock()
    return upd, ctx, msg


def _card_callback(data):
    q = MagicMock()
    q.data = data
    q.message.chat_id = CHAT
    q.message.message_id = CARD_MSG_ID
    q.answer = AsyncMock()
    q.message.edit_reply_markup = AsyncMock()
    q.message.reply_text = AsyncMock(return_value=MagicMock(message_id=99))
    upd = MagicMock()
    upd.callback_query = q
    return upd, MagicMock(), q


def pend_update(db_path):
    order, source = parse_any(TC_RESENT)
    bot.pending[CARD_MSG_ID] = (order, source, CHAT, True)


def test_resend_offers_an_update_card(db_path):
    seed_live(db_path)
    upd, ctx, msg = _paste_message(TC_RESENT)
    asyncio.run(bot.handle_message(upd, ctx))

    text = msg.reply_text.call_args.args[0]
    assert text.startswith(f"#{TC_SHORT} 已有此單，變更：")
    assert "目的地：坑口地铁站A1出入口 → 新界坑口裕明苑裕昌閣B座" in text
    assert "里程：— → 49.105 km" in text
    buttons = msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["update", "skip"]
    assert bot._pending_is_update(bot.pending[77])
    # the card decides nothing on its own
    assert get_order_by_id(db_path, TC_ID)["dropoff"] == "坑口地铁站A1出入口"


def test_identical_resend_reports_no_change(db_path):
    seed_live(db_path)
    upd, ctx, msg = _paste_message(TC_ORIGINAL)
    asyncio.run(bot.handle_message(upd, ctx))

    assert msg.reply_text.call_args.args[0] == f"#{TC_SHORT} 同 DB 一樣，冇嘢改"
    assert msg.reply_text.call_args.kwargs == {}      # no card, nothing to confirm
    assert bot.pending == {}


def test_resend_of_a_settled_order_is_refused(db_path):
    seed_live(db_path)
    create_settlement(db_path, "ride", [TC_ID], 280.0, "2026-08-26",
                      now=datetime(2026, 8, 26, 9, 0))
    upd, ctx, msg = _paste_message(TC_RESENT)
    asyncio.run(bot.handle_message(upd, ctx))

    assert msg.reply_text.call_args.args[0] == f"#{TC_SHORT} {SETTLED_LOCK_MSG}"
    assert bot.pending == {}
    assert get_order_by_id(db_path, TC_ID)["dropoff"] == "坑口地铁站A1出入口"


def test_update_button_writes_and_keeps_the_price(db_path):
    seed_live(db_path)
    pend_update(db_path)
    upd, ctx, q = _card_callback("update")
    asyncio.run(bot.handle_callback(upd, ctx))

    reply = q.message.reply_text.call_args.args[0]
    assert reply.startswith(f"已更新 #{TC_SHORT}：目的地、里程")
    assert "價錢 $280 保留，要改直接打新價" in reply
    row = get_order_by_id(db_path, TC_ID)
    assert row["dropoff"] == "新界坑口裕明苑裕昌閣B座"
    assert row["distance_km"] == 49.105
    assert row["price"] == 280.0
    assert row["telegram_msg_id"] == 99          # replying to the prompt sets the price
    assert bot.awaiting_price[CHAT] == (TC_ID, 0.0)
    q.message.edit_reply_markup.assert_awaited_with(reply_markup=None)
    assert CARD_MSG_ID not in bot.pending


def test_update_button_without_a_price_asks_for_one(db_path):
    seed_live(db_path, price=None)
    pend_update(db_path)
    upd, ctx, q = _card_callback("update")
    asyncio.run(bot.handle_callback(upd, ctx))

    assert "直接打價錢。" in q.message.reply_text.call_args.args[0]
    assert bot.awaiting_price[CHAT] == (TC_ID, 0.0)
    assert get_order_by_id(db_path, TC_ID)["price"] is None


def test_skip_button_leaves_the_row_alone(db_path):
    seed_live(db_path)
    pend_update(db_path)
    upd, ctx, q = _card_callback("skip")
    asyncio.run(bot.handle_callback(upd, ctx))

    q.answer.assert_awaited_with("已略過")
    assert bot.pending == {}
    assert get_order_by_id(db_path, TC_ID)["dropoff"] == "坑口地铁站A1出入口"


def test_bare_price_applies_an_update_card(db_path):
    seed_live(db_path)
    pend_update(db_path)
    upd, ctx, msg = _paste_message("450")
    asyncio.run(bot.handle_message(upd, ctx))

    reply = msg.reply_text.call_args.args[0]
    assert reply.startswith(f"已更新 #{TC_SHORT}：目的地、里程")
    assert "價錢 $450" in reply
    row = get_order_by_id(db_path, TC_ID)
    assert row["dropoff"] == "新界坑口裕明苑裕昌閣B座"
    assert row["price"] == 450.0
    assert CHAT not in bot.awaiting_price     # the price came with the tap
    ctx.bot.edit_message_reply_markup.assert_awaited()


def test_update_of_a_vanished_row_does_not_crash(db_path):
    seed_live(db_path)
    pend_update(db_path)
    cancel_order(db_path, TC_ID)
    upd, ctx, q = _card_callback("update")
    asyncio.run(bot.handle_callback(upd, ctx))

    assert q.message.reply_text.call_args.args[0] == f"訂單 #{TC_SHORT} 已經唔喺度，冇更新到。"
    q.answer.assert_awaited_with("冇更新到")
