import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from .ingest import parse_any, parking_fee, banner_fee
from .db import init_db, resolve_db_path, save_or_revive_order, save_quick_order, order_status, update_price, update_cost, cancel_order, count_active_orders, get_orders_by_date, get_order_by_id, get_order_by_telegram_msg_id, get_pickup_flights, get_tracking_dates, update_flight_info, mark_reminder_sent, get_departure_reminders, open_parking_session, get_open_parking_session, get_parking_session, update_parking_session, close_parking_session, recent_parking_sessions, free_parking_entries_since, diff_order_against_row, update_order_from_message, DIFF_LABELS, SETTLED_LOCK_MSG, create_settlement, find_awaiting_settlements, mark_settlement_paid, settlement_candidates, get_settleable_recent
from .flight import fetch_arrivals, match_flights, calc_next_interval, svc_time, svc_reminder_due, departure_milestones_due, pending_reminder_times, clamp_interval, exit_urgency, depart_reminder_due, predicted_landing_hhmm
from . import parking
from .parking import (ParkingClient, ParkingStatus, ParkingError, free_available, next_free_at,
                      pay_plan, classify, arming_orders, pick_order, from_db_time, db_time,
                      FREE_MINUTES, GRACE_MINUTES, AUTO_LINK_MINUTE, FREE_WINDOW_HOURS, HOURLY_FEE)
from .phone import format_phone_e164
from .service import is_flight_pickup, label as service_label
from . import statement
from .whiteboard import generate as generate_whiteboard, qualifies_for_prompt, sanitize_name as sanitize_board_name, is_configured as whiteboard_configured, WhiteboardError, cache_load as whiteboard_cache_load, cache_store as whiteboard_cache_store, cache_discard as whiteboard_cache_discard

load_dotenv()

DB_PATH = resolve_db_path()
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

_allowed_raw = os.environ.get("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS: set[int] = (
    {int(x.strip()) for x in _allowed_raw.split(",") if x.strip()}
    if _allowed_raw.strip()
    else set()
)

pending: dict = {}
awaiting_price: dict[int, str] = {}
awaiting_cost: dict[int, tuple[str, str]] = {}
didi_state: dict[int, dict] = {}
uber_state: dict[int, dict] = {}

# Card message id → (chat_id, Reconciliation, statement JSON, image bytes).
# The screenshot is kept in memory until the operator confirms so it can be
# stored beside the batch; a skipped or expired card drops it.
pending_statements: dict[int, tuple] = {}


def format_card(order) -> str:
    type_label = service_label(order.service_type)
    time = order.scheduled_time
    if " " in time:
        time = time.split(" ")[1][:5]
    header = f"{type_label} | {order.flight_number}" if order.flight_number else type_label
    lines = [
        header,
        f"乘客: {order.passenger_name}",
        f"時間: {time}",
    ]
    if is_flight_pickup(order.service_type) and order.passenger_exit_minutes:
        exit_line = f"出場: {order.passenger_exit_minutes}分鐘"
        urgency = exit_urgency(order.passenger_exit_minutes)
        if urgency == "urgent":
            exit_line += " — 降落前要出發"
        elif urgency == "tight":
            exit_line += " — 降落即刻出發"
        lines.append(exit_line)
    lines += [
        f"上車: {order.pickup}",
        f"落車: {order.dropoff}",
    ]
    if order.distance_km:
        lines.append(f"里程: {order.distance_km}km")
    if order.driver_notes:
        lines.append(f"備註: {order.driver_notes}")
    return "\n".join(lines)


CHANGE_EMPTY = "—"


def format_change_lines(changes: list[tuple[str, str, str]]) -> str:
    """One "label：old → new" line per changed field."""
    return "\n".join(
        f"{DIFF_LABELS[field]}：{old or CHANGE_EMPTY} → {new or CHANGE_EMPTY}"
        for field, old, new in changes
    )


def _latest_pending_for_chat(pending_dict: dict, chat_id: int) -> tuple[int, tuple] | None:
    """Return (card_msg_id, entry) for the newest pending card in a chat, or None."""
    result = None
    for msg_id, entry in pending_dict.items():
        if entry[2] == chat_id:
            result = (msg_id, entry)
    return result


def _pending_is_update(entry: tuple) -> bool:
    """Whether a pending card offers an update rather than a new entry.

    A card is (order, source, chat_id); one raised for a message re-sent
    against a row that is already active carries a fourth element saying so.
    """
    return len(entry) > 3 and bool(entry[3])


async def _apply_update(message, context, order, source: str, price: float | None = None) -> bool:
    """Write a re-sent message onto its active row; True once it landed.

    The reply is sent before the write because its message id becomes the row's
    telegram_msg_id — a reply to it sets the price, exactly as on the confirm
    path.
    """
    short = order.order_id[-4:]
    row = get_order_by_id(DB_PATH, order.order_id)
    if row is None:
        await message.reply_text(f"訂單 #{short} 已經唔喺度，冇更新到。")
        return False
    if row["settlement_id"] is not None:
        await message.reply_text(f"#{short} {SETTLED_LOCK_MSG}")
        return False
    changed = diff_order_against_row(order, row, source)
    if not changed:
        await message.reply_text(f"#{short} 同 DB 一樣，冇嘢改")
        return False

    banner = banner_fee(order.additional_services)
    text = f"已更新 #{short}：" + "、".join(DIFF_LABELS[f] for f, _, _ in changed)
    if price is not None:
        text += f"\n價錢 ${price:g}"
        if banner:
            text += f"（+舉牌${banner:g}）"
    elif row["price"] is not None:
        text += f"\n價錢 ${row['price']:g} 保留，要改直接打新價"
    else:
        text += "\n直接打價錢。"
        if banner:
            text += f"（會自動加${banner:g}舉牌費）"
    sent = await message.reply_text(text)

    try:
        applied = update_order_from_message(DB_PATH, order, telegram_msg_id=sent.message_id,
                                            source=source)
    except ValueError as e:
        await message.reply_text(f"#{short} {e}")
        return False
    if applied is None:
        await message.reply_text(f"訂單 #{short} 已經唔喺度，冇更新到。")
        return False
    if price is not None:
        update_price(DB_PATH, order.order_id, price)
    else:
        awaiting_price[message.chat_id] = (order.order_id, banner)
    # A changed flight or 用車時間 needs the tracker to re-match right away.
    _kick_poll(context)
    return True


async def handle_message(update: Update, context):
    msg = update.message
    if not msg or not msg.text:
        return
    if ALLOWED_CHAT_IDS and msg.chat_id not in ALLOWED_CHAT_IDS:
        return

    if msg.reply_to_message:
        order = get_order_by_telegram_msg_id(DB_PATH, msg.reply_to_message.message_id)
        if order:
            text = msg.text.strip()
            try:
                price = float(text)
                update_price(DB_PATH, order["order_id"], price)
                await msg.reply_text(f"已更新價錢: ${price:g}")
                return
            except ValueError:
                pass

    chat_id = msg.chat_id
    if chat_id in didi_state:
        await _handle_didi_step(msg, chat_id)
        return
    if chat_id in uber_state:
        await _handle_uber_step(msg, chat_id)
        return

    order, source = parse_any(msg.text)
    if not order.order_id:
        if chat_id in awaiting_cost:
            text = msg.text.strip()
            try:
                amount = float(text)
                order_id, cost_type = awaiting_cost.pop(chat_id)
                label = "隧道費" if cost_type == "tunnel" else "停車費"
                update_cost(DB_PATH, order_id, cost_type, amount)
                await msg.reply_text(f"已記錄{label}: ${amount:g}")
            except ValueError:
                pass
            return
        if chat_id in awaiting_price:
            text = msg.text.strip()
            try:
                price = float(text)
                order_id, banner = awaiting_price.pop(chat_id)
                update_price(DB_PATH, order_id, price)
                if banner:
                    await msg.reply_text(f"已更新價錢: ${price:g}（+舉牌${banner:g}）")
                else:
                    await msg.reply_text(f"已更新價錢: ${price:g}")
            except ValueError:
                pass
            return
        # Shortcut: a bare number while a card is pending = confirm + price in one step
        text = msg.text.strip()
        try:
            price = float(text)
        except ValueError:
            return
        hit = _latest_pending_for_chat(pending, chat_id)
        if not hit:
            return
        card_msg_id, entry = hit
        pending.pop(card_msg_id)
        pending_order, source = entry[0], entry[1]
        if _pending_is_update(entry):
            await _apply_update(msg, context, pending_order, source, price=price)
        else:
            parking = parking_fee(pending_order, source)
            banner = banner_fee(pending_order.additional_services)
            try:
                revived = save_or_revive_order(DB_PATH, pending_order, telegram_msg_id=card_msg_id, parking=parking, source=source)
                update_price(DB_PATH, pending_order.order_id, price)
                _kick_poll(context)
                head = "已重新入單" if revived else "已入單"
                reply = f"{head} #{pending_order.order_id[-4:]}: ${price:g}"
                if banner:
                    reply += "（+舉牌$40）"
                await msg.reply_text(reply)
            except sqlite3.IntegrityError:
                await msg.reply_text("呢張單已經存在。")
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=card_msg_id, reply_markup=None
            )
        except Exception:
            pass
        return

    # The platform re-sends the whole message when the customer changes a
    # detail, so a message landing on a live row is an amendment, not a
    # duplicate: the card offers the diff instead of the parsed order.  A
    # cancelled row falls through to the normal card and the revive path.
    existing = get_order_by_id(DB_PATH, order.order_id)
    if existing:
        short = order.order_id[-4:]
        if existing["settlement_id"] is not None:
            await msg.reply_text(f"#{short} {SETTLED_LOCK_MSG}")
            return
        changes = diff_order_against_row(order, existing, source)
        if not changes:
            await msg.reply_text(f"#{short} 同 DB 一樣，冇嘢改")
            return
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("更新", callback_data="update"),
                InlineKeyboardButton("略過", callback_data="skip"),
            ]]
        )
        sent = await msg.reply_text(
            f"#{short} 已有此單，變更：\n" + format_change_lines(changes),
            reply_markup=keyboard,
        )
        pending[sent.message_id] = (order, source, chat_id, True)
        return

    text = format_card(order)
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("確認", callback_data="confirm"),
            InlineKeyboardButton("取消", callback_data="cancel"),
        ]]
    )
    sent = await msg.reply_text(text, reply_markup=keyboard)
    pending[sent.message_id] = (order, source, chat_id)


async def handle_callback(update: Update, context):
    query = update.callback_query
    if ALLOWED_CHAT_IDS and query.message.chat_id not in ALLOWED_CHAT_IDS:
        return
    msg_id = query.message.message_id

    if query.data == "confirm":
        entry = pending.pop(msg_id, None)
        if not entry:
            await query.answer("訂單已過期")
            return
        order, source, _ = entry
        try:
            parking = parking_fee(order, source)
            banner = banner_fee(order.additional_services)
            # Wording only: the prompt has to exist before the save because its
            # message id becomes the row's telegram_msg_id (a reply to it sets
            # the price).  The new-vs-revived decision itself stays inside
            # save_or_revive_order.
            head = "重新入單" if order_status(DB_PATH, order.order_id) == "cancelled" else "已保存"
            prompt = f"訂單 #{order.order_id[-4:]} {head}。直接打價錢。"
            if banner:
                prompt += f"（會自動加${banner:g}舉牌費）"
            sent = await query.message.reply_text(prompt)
            save_or_revive_order(DB_PATH, order, telegram_msg_id=sent.message_id, parking=parking, source=source)
            _kick_poll(context)
            awaiting_price[query.message.chat_id] = (order.order_id, banner)
            await query.message.edit_reply_markup(reply_markup=None)
            await query.answer("已確認")
        except sqlite3.IntegrityError:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.answer("訂單已存在")
            await query.message.reply_text("呢張單已經存在。")

    elif query.data == "update":
        entry = pending.pop(msg_id, None)
        if not entry:
            await query.answer("訂單已過期")
            return
        await query.message.edit_reply_markup(reply_markup=None)
        applied = await _apply_update(query.message, context, entry[0], entry[1])
        await query.answer("已更新" if applied else "冇更新到")

    elif query.data == "skip":
        pending.pop(msg_id, None)
        await query.message.edit_reply_markup(reply_markup=None)
        await query.answer("已略過")

    elif query.data == "cancel":
        pending.pop(msg_id, None)
        await query.message.edit_reply_markup(reply_markup=None)
        await query.answer("已取消")

    elif query.data.startswith("cancel:"):
        order_id = query.data.split(":", 1)[1]
        cancel_order(DB_PATH, order_id)
        await query.message.edit_text(f"已取消訂單 #{order_id[-4:]}")
        await query.answer("已取消")

    elif query.data.startswith("cost:"):
        _, cost_type, order_id = query.data.split(":", 2)
        label = "隧道費" if cost_type == "tunnel" else "停車費"
        awaiting_cost[query.message.chat_id] = (order_id, cost_type)
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.reply_text(f"打{label}金額：")
        await query.answer()

    elif query.data == "didi:notunnel":
        chat_id = query.message.chat_id
        if chat_id in didi_state:
            await query.message.edit_reply_markup(reply_markup=None)
            await _save_didi(query.message, chat_id, 0)
        await query.answer()

    elif query.data == "uber:notoll":
        chat_id = query.message.chat_id
        if chat_id in uber_state:
            await query.message.edit_reply_markup(reply_markup=None)
            await _save_uber(query.message, chat_id, 0)
        await query.answer()

    elif query.data.startswith("board:"):
        order_id = query.data.split(":", 1)[1]
        order = get_order_by_id(DB_PATH, order_id)
        if not order:
            await query.answer("搵唔到呢張單")
            return
        await query.message.edit_reply_markup(reply_markup=None)
        await query.answer()
        await query.message.reply_text("生成中…")
        context.application.create_task(
            _send_whiteboard(
                context.bot, query.message.chat_id, order_id, order,
                fail_text=f"舉牌相生成失敗 #{order_id[-4:]}，手動準備。",
            ),
        )

    elif query.data == "stmt:confirm":
        entry = pending_statements.pop(msg_id, None)
        if not entry:
            await query.answer("已過期，再 send 一次張圖")
            return
        _chat, rec, stmt_json, image = entry
        dates = sorted({d.date for d in rec.days})
        try:
            settlement_id = create_settlement(
                DB_PATH, "ride", rec.settle_ids, rec.confirmed or 0.0,
                datetime.now().strftime("%Y-%m-%d"), statement=stmt_json, image=image,
            )
        except ValueError as e:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.answer("結算唔到")
            await query.message.reply_text(f"結算唔到：{e}。再 send 一次張圖。")
            return
        await query.message.edit_reply_markup(reply_markup=None)
        await query.answer("已結算")
        await query.message.reply_text(statement.settled_reply(settlement_id, rec, dates))

    elif query.data == "stmt:skip":
        pending_statements.pop(msg_id, None)
        await query.message.edit_reply_markup(reply_markup=None)
        await query.answer("已略過")

    elif query.data.startswith("stmt:paid:"):
        settlement_id = int(query.data.rsplit(":", 1)[1])
        today = datetime.now().strftime("%Y-%m-%d")
        if not mark_settlement_paid(DB_PATH, settlement_id, today):
            await query.answer("搵唔到呢個 batch")
            return
        await query.message.edit_reply_markup(reply_markup=None)
        await query.answer("已到帳")
        await query.message.reply_text(f"批次 #{settlement_id} 已到帳（{today}）")

    elif query.data.startswith("park:pay:"):
        session_id = int(query.data.rsplit(":", 1)[1])
        session = get_parking_session(DB_PATH, session_id)
        await query.answer()
        if not session or session.get("exit_time"):
            await query.message.reply_text("搵唔到呢次泊車，或者已經出咗閘。")
            return
        client = _get_parking_client()
        if client is None:
            await query.message.reply_text("未設定 CAR_PLATE。")
            return
        now = datetime.now()
        try:
            status = await client.query()
            if not status.inside:
                await query.message.reply_text("架車已經唔喺停車場。")
                return
            # Every tap makes a new gateway order: a link's lifetime is
            # unknown and a stale one fails silently at the gateway.
            await _send_pay_link(context.application.bot, query.message.chat_id, session, status, now)
        except ParkingError as e:
            _parking_logger.warning("pay link failed: %s", e)
            await query.message.reply_text("出唔到 link，再撳。")

    elif query.data.startswith("waive:"):
        _, cost_type, order_id = query.data.split(":", 2)
        update_cost(DB_PATH, order_id, cost_type, 0)
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.reply_text(f"已免停車費 #{order_id[-4:]}")
        await query.answer()


async def handle_didi(update: Update, context):
    msg = update.message
    if ALLOWED_CHAT_IDS and msg.chat_id not in ALLOWED_CHAT_IDS:
        return
    didi_state[msg.chat_id] = {"step": "time"}
    await msg.reply_text("打時間（6位數，例如 143025 = 14:30:25）：")


async def _handle_didi_step(msg, chat_id):
    state = didi_state[chat_id]
    text = msg.text.strip()

    if state["step"] == "time":
        if len(text) != 6 or not text.isdigit():
            await msg.reply_text("要6位數字，例如 143025")
            return
        h, m, s = text[:2], text[2:4], text[4:6]
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59 and 0 <= int(s) <= 59):
            await msg.reply_text("時間格式唔啱，再試。")
            return
        from datetime import date, datetime, timedelta
        input_time = f"{h}:{m}:{s}"
        now_time = datetime.now().strftime("%H:%M:%S")
        day = date.today() if input_time <= now_time else date.today() - timedelta(days=1)
        state["time"] = f"{day.isoformat()} {input_time}"
        state["step"] = "fare"
        await msg.reply_text("打車費：")

    elif state["step"] == "fare":
        try:
            fare = float(text)
        except ValueError:
            await msg.reply_text("要數字。")
            return
        state["fare"] = fare
        state["step"] = "tunnel"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("冇隧道費", callback_data="didi:notunnel")]]
        )
        await msg.reply_text("隧道費？打數字，或者撳下面：", reply_markup=keyboard)

    elif state["step"] == "tunnel":
        try:
            tunnel = float(text)
        except ValueError:
            await msg.reply_text("要數字，或者撳「冇隧道費」。")
            return
        await _save_didi(msg, chat_id, tunnel)


async def _save_didi(msg, chat_id, tunnel_fee):
    state = didi_state.pop(chat_id)
    from datetime import datetime
    order_id = f"didi_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    save_quick_order(DB_PATH, order_id, "滴滴", state["time"], state["fare"], tunnel_fee, source="滴滴")
    net = state["fare"] - tunnel_fee
    summary = f"滴滴已記錄\n時間: {state['time'].split(' ')[1][:5]}\n車費: ${state['fare']:g}"
    if tunnel_fee:
        summary += f"\n隧道: ${tunnel_fee:g}\n淨收入: ${net:g}"
    await msg.reply_text(summary)


async def handle_uber(update: Update, context):
    msg = update.message
    if ALLOWED_CHAT_IDS and msg.chat_id not in ALLOWED_CHAT_IDS:
        return
    uber_state[msg.chat_id] = {"step": "time"}
    await msg.reply_text("打時間（例如 pm1006 = 下午10:06）：")


async def _handle_uber_step(msg, chat_id):
    state = uber_state[chat_id]
    text = msg.text.strip().lower()

    if state["step"] == "time":
        if len(text) != 6 or text[:2] not in ("am", "pm") or not text[2:].isdigit():
            await msg.reply_text("格式：am/pm + 4位數，例如 pm1006")
            return
        period = text[:2]
        h, m = int(text[2:4]), int(text[4:6])
        if not (1 <= h <= 12 and 0 <= m <= 59):
            await msg.reply_text("時間唔啱，再試。")
            return
        if period == "pm" and h != 12:
            h += 12
        elif period == "am" and h == 12:
            h = 0
        from datetime import date, datetime, timedelta
        input_time = f"{h:02d}:{m:02d}:00"
        now_time = datetime.now().strftime("%H:%M:%S")
        day = date.today() if input_time <= now_time else date.today() - timedelta(days=1)
        state["time"] = f"{day.isoformat()} {input_time}"
        state["step"] = "income"
        await msg.reply_text("打行程收入：")

    elif state["step"] == "income":
        try:
            income = float(text)
        except ValueError:
            await msg.reply_text("要數字。")
            return
        state["income"] = income
        state["step"] = "toll"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("冇通行費", callback_data="uber:notoll")]]
        )
        await msg.reply_text("通行費？打數字，或者撳下面：", reply_markup=keyboard)

    elif state["step"] == "toll":
        try:
            toll = float(text)
        except ValueError:
            await msg.reply_text("要數字，或者撳「冇通行費」。")
            return
        await _save_uber(msg, chat_id, toll)


async def _save_uber(msg, chat_id, toll_fee):
    state = uber_state.pop(chat_id)
    from datetime import datetime
    order_id = f"uber_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    total = state["income"] + toll_fee
    save_quick_order(DB_PATH, order_id, "Uber", state["time"], total, toll_fee, source="Uber")
    summary = f"Uber 已記錄\n時間: {state['time'].split(' ')[1][:5]}\n行程收入: ${state['income']:g}"
    if toll_fee:
        summary += f"\n通行費: ${toll_fee:g}\n總收入: ${total:g}"
    await msg.reply_text(summary)


async def handle_cancel(update: Update, context):
    msg = update.message
    if ALLOWED_CHAT_IDS and msg.chat_id not in ALLOWED_CHAT_IDS:
        return
    from datetime import date
    orders = get_orders_by_date(DB_PATH, date.today().isoformat())
    if not orders:
        await msg.reply_text("今日冇訂單。")
        return
    buttons = []
    for o in orders:
        t = o["scheduled_time"].split(" ")[1][:5] if " " in o["scheduled_time"] else ""
        label = f"{t} {o['passenger_name']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"cancel:{o['order_id']}")])
    await msg.reply_text("撳邊張要取消：", reply_markup=InlineKeyboardMarkup(buttons))


async def handle_board(update: Update, context):
    msg = update.message
    if ALLOWED_CHAT_IDS and msg.chat_id not in ALLOWED_CHAT_IDS:
        return
    if not whiteboard_configured():
        await msg.reply_text("舉牌相功能需要設定 FAL_KEY。")
        return
    from datetime import date
    orders = get_orders_by_date(DB_PATH, date.today().isoformat())
    pickups = [o for o in orders if is_flight_pickup(o.get("service_type") or "")]
    if not pickups:
        await msg.reply_text("今日冇接機單。")
        return
    buttons = []
    for o in pickups:
        t = o["scheduled_time"].split(" ")[1][:5] if " " in o["scheduled_time"] else ""
        flight = o.get("flight_number") or ""
        label = f"{t} {o['passenger_name']}"
        if flight:
            label += f" {flight}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"board:{o['order_id']}")])
    await msg.reply_text("揀邊張生成舉牌相：", reply_markup=InlineKeyboardMarkup(buttons))


async def handle_parking(update: Update, context):
    msg = update.message
    if ALLOWED_CHAT_IDS and msg.chat_id not in ALLOWED_CHAT_IDS:
        return
    client = _get_parking_client()
    if client is None:
        await msg.reply_text("未設定 CAR_PLATE，停車場功能關閉。")
        return
    now = datetime.now()
    lines = []
    session = get_open_parking_session(DB_PATH)
    if session:
        entry = _session_entry(session)
        try:
            status = await client.query()
            minutes = status.park_minutes if status.inside and status.park_minutes is not None else int((now - entry).total_seconds() // 60)
        except ParkingError:
            minutes = int((now - entry).total_seconds() // 60)
        state = "已付" if session.get("paid") else "未付"
        lines.append(f"喺 {session.get('location_name') or session.get('location')} 入咗 {entry.strftime('%H:%M')}，泊咗 {minutes} 分鐘，{state}")
    else:
        lines.append("架車唔喺停車場")
    lines.append(_allowance_line(now))
    history = recent_parking_sessions(DB_PATH, 5)
    if history:
        lines.append("")
        for s in history:
            entry = _session_entry(s)
            if s.get("exit_time"):
                exit_at = from_db_time(s["exit_time"])
                stayed = int((exit_at - entry).total_seconds() // 60)
                kind = "免費" if s.get("free") else ("已付" if s.get("paid") else "閘口")
                lines.append(f"{entry.strftime('%m-%d %H:%M')} 泊 {stayed} 分鐘 {kind}")
            else:
                lines.append(f"{entry.strftime('%m-%d %H:%M')} 泊緊")
    await msg.reply_text("\n".join(lines), reply_markup=_pay_button(session["id"]) if session and not session.get("paid") else None)


async def handle_statement_image(update: Update, context):
    msg = update.message
    if not msg:
        return
    if ALLOWED_CHAT_IDS and msg.chat_id not in ALLOWED_CHAT_IDS:
        return
    if not statement.ocr_available():
        await msg.reply_text(statement.fallback_report(get_settleable_recent(DB_PATH, days=14)))
        return
    file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
    tg_file = await context.bot.get_file(file_id)
    data = bytes(await tg_file.download_as_bytearray())
    # CPU-bound and ~2 s: off the event loop so the heartbeat keeps ticking.
    stmt = await asyncio.to_thread(statement.read_image, data)
    if not stmt.days:
        await msg.reply_text("讀唔到張圖（" + "；".join(stmt.warnings or ["冇日期 / 訂單行"]) + "）— 再 send 一次，或者用「Send as file」send 原檔")
        return
    dates = statement.dates_of(stmt)
    orders = settlement_candidates(DB_PATH, dates)
    rec = statement.reconcile(stmt, orders, datetime.now())
    text = statement.format_report(rec)
    if not rec.can_settle:
        await msg.reply_text(text)
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(statement.confirm_label(rec), callback_data="stmt:confirm"),
        InlineKeyboardButton("唔確認", callback_data="stmt:skip"),
    ]])
    sent = await msg.reply_text(text, reply_markup=keyboard)
    pending_statements[sent.message_id] = (msg.chat_id, rec, statement.corrected_json(stmt, rec), data)


async def handle_paid(update: Update, context):
    msg = update.message
    if ALLOWED_CHAT_IDS and msg.chat_id not in ALLOWED_CHAT_IDS:
        return
    parts = (msg.text or "").split(maxsplit=1)
    amount = None
    if len(parts) > 1:
        try:
            amount = float(parts[1].replace(",", "").replace("$", ""))
        except ValueError:
            await msg.reply_text("用法：/paid 2540")
            return
    awaiting = find_awaiting_settlements(DB_PATH)
    if not awaiting:
        await msg.reply_text("冇 batch 等緊過數。")
        return
    hits = [b for b in awaiting if amount is not None and abs(b["confirmed_amount"] - amount) < 0.005]
    if amount is not None and len(hits) == 1:
        b = hits[0]
        today = datetime.now().strftime("%Y-%m-%d")
        mark_settlement_paid(DB_PATH, b["id"], today)
        dates = sorted({o["scheduled_time"][:10] for o in b["orders"]})
        await msg.reply_text(f"批次 #{b['id']} 已到帳 {statement.money_str(b['confirmed_amount'])} · "
                             f"{statement.date_span_label(dates)} · {len(b['orders'])} 程")
        return
    if amount is not None and not hits:
        head = f"冇 {statement.money_str(amount)} 嘅 batch 等緊過數，等緊嘅係："
        choices = awaiting
    elif amount is not None:
        head = f"{len(hits)} 個 batch 都係 {statement.money_str(amount)}，揀邊個："
        choices = hits
    else:
        head = "等緊過數："
        choices = awaiting
    buttons = [[InlineKeyboardButton(statement.awaiting_label(b), callback_data=f"stmt:paid:{b['id']}")]
               for b in choices]
    await msg.reply_text(head, reply_markup=InlineKeyboardMarkup(buttons))


async def handle_start(update: Update, context):
    msg = update.message
    if ALLOWED_CHAT_IDS and msg.chat_id not in ALLOWED_CHAT_IDS:
        return
    args = context.args
    if args and args[0].startswith("order_"):
        order_id = args[0][len("order_"):]
        order = get_order_by_id(DB_PATH, order_id)
        if not order:
            await msg.reply_text("搵唔到呢張單。")
            return
        t = order["scheduled_time"].split(" ")[1][:5] if " " in order["scheduled_time"] else ""
        type_label = service_label(order["service_type"])
        flight = order["flight_number"]
        lines = [
            f"{type_label} | {flight}" if flight else type_label,
            f"乘客: {order['passenger_name']}",
            f"時間: {t}",
        ]
        if order.get("price"):
            lines.append(f"收入: ${order['price']:g}")
        tunnel = order.get("tunnel_fee") or 0
        parking = order.get("parking_fee") or 0
        if tunnel or parking:
            lines.append(f"成本: 隧道${tunnel:g} 停車${parking:g}")
        is_pickup = is_flight_pickup(order["service_type"])
        if is_pickup:
            parking_btn = InlineKeyboardButton("免停車費", callback_data=f"waive:parking:{order_id}")
        else:
            parking_btn = InlineKeyboardButton("停車費", callback_data=f"cost:parking:{order_id}")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("隧道費", callback_data=f"cost:tunnel:{order_id}"),
                parking_btn,
            ],
            [InlineKeyboardButton("取消訂單", callback_data=f"cancel:{order_id}")],
        ])
        await msg.reply_text("\n".join(lines), reply_markup=keyboard)
        return
    await msg.reply_text(
        "Ride Dispatch Bot\n直接 paste 訂單 message 就得。"
    )


logger = logging.getLogger("flight_poller")

# Poll cadence is owned by an immortal 60s heartbeat (run_repeating), not a
# self-chaining run_once: APScheduler discards jobs that fire >1s late
# (default misfire_grace_time=1), which silently killed the old chain — see
# "was missed by" warnings in bot.log. The heartbeat always ticks; these
# globals decide whether a tick does real work. They gate flight polling
# only: work whose latency must not scale with the flight interval runs on
# every tick instead.
POLL_ERROR_BACKOFF = 300
_JOB_KWARGS = {"misfire_grace_time": None, "coalesce": True}
_next_poll_at: datetime | None = None
_poll_running = False
_last_state: str | None = None
_warned_statuses: set[tuple[str, str]] = set()
_kick_server = None

# Car park visit tracking. The client is built on first use from CAR_PLATE;
# None means the feature is off. _parking_miss_at is the tick time of the
# first "not inside" reply while a session is open — a second consecutive
# miss confirms the exit. In-memory on purpose: losing it on a restart costs
# one extra tick, nothing more.
#
# The check runs on every heartbeat tick, never behind _next_poll_at: an exit
# takes two consecutive misses to confirm, so gating it on the flight-poll
# schedule would make exit latency twice the flight interval, which ranges
# from a minute to hours. _parking_running keeps a slow HKIA query from
# overlapping the next tick.
_parking_client: ParkingClient | None = None
_parking_client_built = False
_parking_miss_at: datetime | None = None
_parking_running = False
_parking_logger = logging.getLogger("parking")


def _get_parking_client() -> ParkingClient | None:
    global _parking_client, _parking_client_built
    # Build at most once, and never over a client that is already in place:
    # _parking_client is also assigned directly from outside this module.
    if _parking_client is None and not _parking_client_built:
        _parking_client_built = True
        if parking.is_configured():
            _parking_client = ParkingClient(parking.CAR_PLATE, parking.PARKING_EMAIL)
    return _parking_client


def _log_state(state: str | None):
    # Idle states recur every tick; log transitions only.
    global _last_state
    if state and state != _last_state:
        logger.info(state)
    _last_state = state


def _notify_chat_id() -> int:
    """Chat every push goes to; 0 when the bot has no configured destination."""
    return int(os.environ.get("NOTIFY_CHAT_ID", list(ALLOWED_CHAT_IDS)[0] if ALLOWED_CHAT_IDS else "0"))


def _kick_poll(context):
    global _next_poll_at
    _next_poll_at = None
    context.application.job_queue.run_once(_poll_tick, 5, job_kwargs=_JOB_KWARGS)


async def _poll_tick(context):
    global _next_poll_at, _poll_running, _parking_running
    if not _parking_running:
        _parking_running = True
        try:
            chat_id = _notify_chat_id()
            if chat_id:
                await _check_parking(context.application.bot, chat_id, datetime.now())
        except Exception:
            logger.exception("parking check error")
        finally:
            _parking_running = False
    if _poll_running:
        return
    if _next_poll_at and datetime.now() < _next_poll_at:
        return
    _poll_running = True
    try:
        interval = await _poll_and_notify(context)
        _next_poll_at = datetime.now() + timedelta(seconds=interval)
    except Exception:
        logger.exception("Flight poll error")
        _next_poll_at = datetime.now() + timedelta(seconds=POLL_ERROR_BACKOFF)
    finally:
        _poll_running = False


def _orders_in(dates: list[str]) -> list[dict]:
    orders = []
    for d in dates:
        orders.extend(get_orders_by_date(DB_PATH, d))
    return orders


async def _check_svc_reminders(bot, chat_id: int, now: datetime):
    dates = get_tracking_dates(DB_PATH, now=now)
    if not dates:
        return
    for order in _orders_in(dates):
        try:
            svc_hhmm = svc_reminder_due(order, now)
            if not svc_hhmm:
                continue
            # No arrival_hhmm: the headline already carries the 用車 time.
            msg = f"用車時間到 {svc_hhmm}"
            msg += _order_lines(order)
            await bot.send_message(chat_id=chat_id, text=msg)
            mark_reminder_sent(DB_PATH, order['order_id'], 'svc')
            logger.info("svc reminder sent for %s", order['order_id'][-4:])
        except Exception:
            logger.exception("svc reminder failed for %s", order.get('order_id', '?')[-4:])


async def _check_depart_reminders(bot, chat_id: int, now: datetime):
    dates = get_tracking_dates(DB_PATH, now=now)
    if not dates:
        return
    for order in _orders_in(dates):
        try:
            hhmm = depart_reminder_due(order, now)
            if not hhmm:
                continue
            landing = predicted_landing_hhmm(order)
            msg = f"出發接機 | 預計降落 {landing}"
            msg += _order_lines(order, landing)
            await bot.send_message(chat_id=chat_id, text=msg)
            mark_reminder_sent(DB_PATH, order['order_id'], 'depart')
            logger.info("depart reminder sent for %s", order['order_id'][-4:])
        except Exception:
            logger.exception("depart reminder failed for %s", order.get('order_id', '?')[-4:])


async def _check_departure_reminders(bot, chat_id: int, now: datetime):
    for order in get_departure_reminders(DB_PATH, now):
        try:
            tags = departure_milestones_due(order, now)
            if not tags:
                continue
            sched = datetime.strptime(order['scheduled_time'], '%Y-%m-%d %H:%M:%S')
            t = sched.strftime('%H:%M')
            headline = f"{service_label(order.get('service_type') or '')}提醒 {t} 出發"
            # A late-entered order can have both milestones due at once —
            # one push, mark them all, no duplicate messages.
            msg = headline + _order_lines(order)
            await bot.send_message(chat_id=chat_id, text=msg)
            for tag in tags:
                mark_reminder_sent(DB_PATH, order['order_id'], tag)
            logger.info("departure reminder %s sent for %s", "+".join(tags), order['order_id'][-4:])
        except Exception:
            logger.exception("departure reminder failed for %s", order.get('order_id', '?')[-4:])


def _session_entry(session: dict) -> datetime:
    return from_db_time(session["entry_time"])


def _free_entries(now: datetime) -> list[datetime]:
    cutoff = db_time(now - timedelta(hours=FREE_WINDOW_HOURS))
    return [from_db_time(s) for s in free_parking_entries_since(DB_PATH, cutoff)]


def _day_word(dt: datetime, now: datetime) -> str:
    delta = (dt.date() - now.date()).days
    if delta == 0:
        return "今日"
    if delta == 1:
        return "明日"
    if delta == -1:
        return "昨日"
    return dt.strftime("%m-%d")


def _when(dt: datetime, now: datetime, day_always: bool = False) -> str:
    # Same-day times read as bare HH:MM. A time the driver has to act before
    # always carries its day word: a bare "13:40 後" is read as today.
    hhmm = dt.strftime("%H:%M")
    if day_always or dt.date() != now.date():
        return f"{_day_word(dt, now)} {hhmm}"
    return hhmm


def _allowance_line(now: datetime) -> str:
    entries = _free_entries(now)
    if free_available(entries, now):
        return "停車場 免費可用"
    used = max(entries)
    nxt = next_free_at(entries)
    return f"停車場 免費已用 {_when(used, now)}，{_when(nxt, now, day_always=True)} 後先有"


def _entry_message(session: dict, status: ParkingStatus, now: datetime) -> str:
    entry = _session_entry(session)
    lines = [f"已入 {status.location_name or status.location} {entry.strftime('%H:%M')}"]
    if free_available(_free_entries(now), now):
        lines.append(f"免費可用，{(entry + timedelta(minutes=FREE_MINUTES)).strftime('%H:%M')} 前出閘")
    else:
        hours, exit_at = pay_plan(entry, now)
        lines.append(f"免費已用，泊 {hours} 粒鐘 ${HOURLY_FEE * hours:g} 到 {exit_at.strftime('%H:%M')}")
    if session.get("order_id"):
        o = get_order_by_id(DB_PATH, session["order_id"])
        if o:
            who = o.get("passenger_name") or ""
            flight = o.get("flight_number") or ""
            lines.append(f"乘客: {who} | {flight}" if flight else f"乘客: {who}")
    return "\n".join(lines)


async def _send_pay_link(bot, chat_id: int, session: dict, status: ParkingStatus,
                         now: datetime, prefix: str = "") -> bool:
    """Generate a fresh PayDollar link for the plan at this minute and send it.

    Returns True once the message is out; the caller decides what to mark.
    """
    client = _get_parking_client()
    if client is None:
        return False
    entry = _session_entry(session)
    _, exit_at = pay_plan(entry, now)
    fee = await client.fee_for_exit(status, exit_at)
    amount = fee.fee if fee.fee is not None else 0
    url, payment = await client.pay_link(status, exit_at, amount, now)
    grace = exit_at + timedelta(minutes=GRACE_MINUTES)
    text = f"{prefix}${amount:g} 泊到 {exit_at.strftime('%H:%M')}（寬限到 {grace.strftime('%H:%M')}）"
    await bot.send_message(
        chat_id=chat_id, text=text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Apple Pay ${amount:g}", url=url)]]),
    )
    update_parking_session(DB_PATH, session["id"], payment_ref=payment["payment_ref"],
                           scheduled_exit=db_time(exit_at), paid_amount=amount, link_sent_at=db_time(now))
    return True


def _pay_button(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("出 payment link", callback_data=f"park:pay:{session_id}")]])


async def _close_visit(bot, chat_id: int, session: dict, exit_at: datetime, now: datetime):
    entry = _session_entry(session)
    kind = classify(bool(session.get("paid")), entry, exit_at)
    close_parking_session(DB_PATH, session["id"], db_time(exit_at), 1 if kind == "free" else 0)
    stayed = int((exit_at - entry).total_seconds() // 60)
    if kind == "free":
        tail = f"免費，下次 {_when(entry + timedelta(hours=FREE_WINDOW_HOURS), now, day_always=True)} 後"
    elif kind == "paid":
        tail = f"已付 ${(session.get('paid_amount') or 0):g}" if session.get("paid_amount") else "已付（網上）"
    else:
        tail = "閘口找數"
    msg = f"已出閘 {exit_at.strftime('%H:%M')}，泊 {stayed} 分鐘 | {tail}"
    order_id = session.get("order_id")
    if order_id and kind == "free":
        update_cost(DB_PATH, order_id, "parking", 0)
        msg += f"\n#{order_id[-4:]} 停車費已改 $0"
    elif order_id and kind == "paid" and session.get("paid_amount"):
        update_cost(DB_PATH, order_id, "parking", float(session["paid_amount"]))
    await bot.send_message(chat_id=chat_id, text=msg)


async def _check_parking(bot, chat_id: int, now: datetime):
    global _parking_miss_at
    client = _get_parking_client()
    if client is None:
        return
    session = get_open_parking_session(DB_PATH)
    # An open visit outranks the arming window: the car is inside and its
    # exit still has to be seen, however the pickup's window has moved.
    orders = _orders_in(get_tracking_dates(DB_PATH, now)) if session is None else []
    if session is None and not arming_orders(orders, now):
        return
    try:
        status = await client.query()
    except ParkingError as e:
        # Unknown: neither inside nor outside. Leave every state untouched.
        _parking_logger.warning("parking query failed: %s", e)
        return

    if not status.inside:
        if session is None:
            _parking_miss_at = None
            return
        if _parking_miss_at is None:
            _parking_miss_at = now
            return
        exit_at = _parking_miss_at
        _parking_miss_at = None
        await _close_visit(bot, chat_id, session, exit_at, now)
        return

    _parking_miss_at = None
    if session is not None and session["pv_nr"] != status.pv_nr:
        # Left and re-entered within one tick; the old visit ended "now".
        await _close_visit(bot, chat_id, session, now, now)
        session = None
    if session is None:
        if not orders:
            orders = _orders_in(get_tracking_dates(DB_PATH, now))
        linked = pick_order(arming_orders(orders, now), from_db_time(status.entry_time))
        sid = open_parking_session(
            DB_PATH, pv_nr=status.pv_nr, plate=client.plate, location=status.location,
            location_name=status.location_name, entry_time=status.entry_time,
            order_id=linked["order_id"] if linked else None,
        )
        session = get_parking_session(DB_PATH, sid)
        await bot.send_message(chat_id=chat_id, text=_entry_message(session, status, now),
                               reply_markup=_pay_button(sid))
        return

    if status.paid and not session.get("paid"):
        update_parking_session(DB_PATH, session["id"], paid=1)
        session["paid"] = 1
        msg = f"已收到付款 ${(session.get('paid_amount') or 0):g}" if session.get("paid_amount") else "已收到付款"
        if session.get("scheduled_exit"):
            # Only a payment made through our own link has a known paid-until
            # time; a gate or QR payment leaves this empty.
            until = from_db_time(session["scheduled_exit"])
            grace = until + timedelta(minutes=GRACE_MINUTES)
            msg += f"，{until.strftime('%H:%M')} 前出閘（寬限到 {grace.strftime('%H:%M')}）"
        await bot.send_message(chat_id=chat_id, text=msg)
        return

    minutes = status.park_minutes if status.park_minutes is not None else int((now - _session_entry(session)).total_seconds() // 60)
    if (not session.get("paid")) and (not session.get("auto_link_sent")) and minutes >= AUTO_LINK_MINUTE:
        try:
            sent = await _send_pay_link(bot, chat_id, session, status, now, prefix=f"泊咗 {AUTO_LINK_MINUTE} 分鐘未俾錢，")
        except ParkingError as e:
            # Marked sent only after a successful send, so this retries next tick.
            _parking_logger.warning("auto pay link failed: %s", e)
            sent = False
        if sent:
            update_parking_session(DB_PATH, session["id"], auto_link_sent=1)


def _clamp_for_reminders(interval: int, now: datetime) -> int:
    all_orders: list[dict] = []
    dates = get_tracking_dates(DB_PATH, now=now)
    if dates:
        all_orders.extend(_orders_in(dates))
    all_orders.extend(get_departure_reminders(DB_PATH, now))
    pending = pending_reminder_times(all_orders, now)
    return clamp_interval(interval, pending, now)


async def _poll_and_notify(context) -> int:
    """One polling pass; returns seconds until the next pass is due."""
    bot = context.application.bot
    chat_id = _notify_chat_id()
    if not chat_id:
        _log_state("No NOTIFY_CHAT_ID/ALLOWED_CHAT_IDS configured, not polling")
        return 3600

    now = datetime.now()

    # Reminders run every tick, before flight-tracking early returns
    try:
        await _check_svc_reminders(bot, chat_id, now)
    except Exception:
        logger.exception("svc reminder check error")
    try:
        await _check_depart_reminders(bot, chat_id, now)
    except Exception:
        logger.exception("depart reminder check error")
    try:
        await _check_departure_reminders(bot, chat_id, now)
    except Exception:
        logger.exception("departure reminder check error")

    # Flight tracking
    dates = get_tracking_dates(DB_PATH)
    if not dates:
        _log_state("idle: no orders in tracking window")
        return _clamp_for_reminders(60, now)

    enriched = _orders_in(dates)
    if calc_next_interval(enriched) is None:
        _log_state("idle: all tracking windows closed")
        return _clamp_for_reminders(60, now)
    _log_state(None)

    old_statuses = {
        o["order_id"]: o.get("flight_status")
        for o in enriched
        if is_flight_pickup(o.get("service_type") or "") and o.get("flight_number")
    }

    all_updates = {}
    for d in dates:
        day_orders = get_pickup_flights(DB_PATH, d)
        if not day_orders:
            continue
        try:
            day_arrivals = fetch_arrivals(d)
        except Exception:
            logger.exception("Failed to fetch arrivals for %s", d)
            continue
        day_updates = match_flights(day_orders, day_arrivals)
        for order_id, info in day_updates.items():
            update_flight_info(DB_PATH, order_id, info["scheduled"], info["eta"], info["gate"], info["status"], hall=info.get("hall"))
        all_updates.update(day_updates)

    for order_id, info in all_updates.items():
        old = old_statuses.get(order_id)
        new = info["status"]
        raw = info.get("raw_status", "")
        if new is None and raw and (order_id, raw) not in _warned_statuses:
            # Status HKIA sent but we don't parse — surface it instead of
            # silently tracking a flight that may never arrive (e.g. Diverted).
            _warned_statuses.add((order_id, raw))
            logger.warning("Unrecognized flight status for %s: %r", order_id[-4:], raw)
        if old == new:
            continue
        logger.info("Flight %s status: %s -> %s", order_id[-4:], old, new)
        try:
            await _notify_status_change(bot, chat_id, order_id, info, old, new)
        except Exception:
            # One malformed order must not eat the other orders' pushes.
            logger.exception("Notify failed for order %s", order_id[-4:])

    if all_updates:
        logger.info("Updated %d flight(s): %s", len(all_updates), list(all_updates.keys()))

    interval = calc_next_interval(_orders_in(dates))
    if interval is None:
        _log_state("idle: all tracking windows closed")
        return _clamp_for_reminders(60, now)
    logger.info("Next poll in %ds", interval)
    return _clamp_for_reminders(interval, now)


_BRACKET_LABEL_RE = re.compile(r'【(.+?)】\s*(.*)')


def collect_contact_lines(order_data: dict) -> list[tuple[str, str]]:
    """Collect distinct labelled phone entries from all four contact fields.

    Returns (label, display_value) pairs.  Deduplication key is the
    digits-only form of the number — first occurrence wins in field
    order: passenger_phone, overseas_phone, third_party_contact,
    more_contacts.
    """
    seen: set[str] = set()
    result: list[tuple[str, str]] = []

    def _digits(s: str) -> str:
        return re.sub(r'\D', '', s)

    def _add(label: str, raw: str, fmt: bool = True):
        key = _digits(raw)
        if key and key in seen:
            return
        if key:
            seen.add(key)
        display = format_phone_e164(raw) if fmt else raw.strip()
        result.append((label, display))

    p_phone = order_data.get("passenger_phone") or ""
    o_phone = order_data.get("overseas_phone") or ""
    tp_contact = order_data.get("third_party_contact") or ""
    more = order_data.get("more_contacts") or ""

    if p_phone.strip():
        _add("電話", p_phone)
    if o_phone.strip():
        _add("境外", o_phone)
    if tp_contact.strip():
        m = _BRACKET_LABEL_RE.match(tp_contact.strip())
        if m:
            _add(m.group(1), m.group(2).strip())
        else:
            _add("聯絡", tp_contact, fmt=False)
    if more.strip():
        m = _BRACKET_LABEL_RE.match(more.strip())
        if m:
            _add(m.group(1), m.group(2).strip())
        else:
            _add("更多", more)

    return result


def _order_lines(order_data: dict, arrival_hhmm: str | None = None) -> str:
    lines = ""
    flight = order_data.get("flight_number")
    if flight:
        lines += f"\n航班: {flight}"
    if order_data.get("passenger_name"):
        lines += f"\n乘客: {order_data['passenger_name']}"
    for label, display in collect_contact_lines(order_data):
        lines += f"\n{label}: {display}"
    if is_flight_pickup(order_data.get("service_type") or ""):
        if order_data.get("passenger_exit_minutes") and arrival_hhmm:
            svc = svc_time(arrival_hhmm, order_data["passenger_exit_minutes"])
            if svc:
                lines += f"\n用車: {svc}"
        if order_data.get("passenger_exit_minutes"):
            lines += f"\n出場: {order_data['passenger_exit_minutes']}分鐘"
        if "举牌" in (order_data.get("additional_services") or ""):
            lines += f"\n舉牌: {order_data.get('passenger_name', '')}"
        if order_data.get("dropoff"):
            lines += f"\n目的地: {order_data['dropoff']}"
    else:
        if order_data.get("pickup"):
            lines += f"\n上車: {order_data['pickup']}"
        if order_data.get("dropoff"):
            lines += f"\n目的地: {order_data['dropoff']}"
    return lines


async def _prompt_whiteboard(bot, chat_id: int, order_id: str, order_data: dict):
    """Offer whiteboard generation behind a button.

    The message previews the two lines that will be written so a wrong name or
    flight is caught before a paid generation call, and ignoring it costs
    nothing. Reuses the board: callback, which is also the manual retry path.
    """
    lines = [f"舉牌相 #{order_id[-4:]}"]
    name = sanitize_board_name(order_data.get("passenger_name") or "")
    if name:
        lines.append(name)
    flight = order_data.get("flight_number") or ""
    if flight:
        lines.append(flight)
    await bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("生成舉牌相", callback_data=f"board:{order_id}")]]
        ),
    )


DELIVERY_RETRY_DELAYS = (2, 5)  # seconds to wait before each retry


def _is_transient_network_error(err: BaseException) -> bool:
    """Whether a Telegram error is a dropped connection rather than a refusal.

    BadRequest subclasses NetworkError in this library but means the request
    itself is unacceptable, so it must not be treated as transient.
    """
    return isinstance(err, NetworkError) and not isinstance(err, BadRequest)


async def _deliver_whiteboard(bot, chat_id: int, order_id: str, img_bytes: bytes, caption: str):
    """Send an already-generated board, retrying dropped connections.

    The bytes are in hand, so a retry costs nothing while the generation that
    produced them cost credits — worth several attempts before giving up.
    """
    attempts = len(DELIVERY_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            await bot.send_photo(chat_id=chat_id, photo=img_bytes, caption=caption)
            return
        except Exception as err:
            if attempt == attempts - 1 or not _is_transient_network_error(err):
                raise
            logging.getLogger("whiteboard").warning(
                "Whiteboard delivery attempt %d/%d failed for %s: %s: %s",
                attempt + 1, attempts, order_id[-4:], type(err).__name__, err,
            )
            await asyncio.sleep(DELIVERY_RETRY_DELAYS[attempt])


async def _send_whiteboard(bot, chat_id: int, order_id: str, order_data: dict,
                           fail_text: str | None = None):
    """Fire-and-forget: generate whiteboard image and send to chat."""
    name = sanitize_board_name(order_data.get("passenger_name") or "")
    flight = order_data.get("flight_number", "")
    if fail_text is None:
        fail_text = f"舉牌相自動生成失敗 #{order_id[-4:]}，用 /board 重試。"
    log = logging.getLogger("whiteboard")

    img_bytes = whiteboard_cache_load(order_id, name, flight)
    if img_bytes is None:
        try:
            img_bytes = await generate_whiteboard(name, flight)
        except Exception:
            log.exception("Whiteboard gen failed for %s", order_id[-4:])
            await bot.send_message(chat_id=chat_id, text=fail_text)
            return
        # Cached before the first send attempt so the image survives a delivery
        # failure at any point after this line.
        whiteboard_cache_store(order_id, name, flight, img_bytes)

    try:
        await _deliver_whiteboard(bot, chat_id, order_id, img_bytes, f"舉牌 | {name} {flight}")
    except Exception:
        log.exception("Whiteboard delivery failed for %s", order_id[-4:])
        await bot.send_message(
            chat_id=chat_id,
            text=f"舉牌相已生成 #{order_id[-4:]}，但傳送失敗。用 /board 重發，唔使再生成。",
        )
        return
    whiteboard_cache_discard(order_id, name, flight)


async def _notify_status_change(bot, chat_id: int, order_id: str, info: dict, old: str | None, new: str | None):
    order_data = get_order_by_id(DB_PATH, order_id)
    should_notify_landed = (new == "landed" and old != "landed") or (new == "gate" and old not in ("landed", "gate"))
    if should_notify_landed:
        eta = info["eta"] or (order_data.get("flight_eta") if order_data else None) or "?"
        hall = info.get("hall")
        msg = f"已降落 {eta}"
        if hall:
            msg += f" | 大堂{hall}"
        if order_data:
            msg += _order_lines(order_data, eta)
        # The driver decides here whether to wait outside for the free half
        # hour or go in and rest, so the verdict rides on this message.
        if _get_parking_client() is not None:
            msg += "\n" + _allowance_line(datetime.now())
        await bot.send_message(chat_id=chat_id, text=msg)
        # Offer the whiteboard sign on landing. The tag is written here, before
        # any image exists, so the landed→gate double transition cannot prompt
        # twice; the board: callback ignores it and stays the retry path.
        if order_data and qualifies_for_prompt(order_data):
            mark_reminder_sent(DB_PATH, order_id, "whiteboard")
            await _prompt_whiteboard(bot, chat_id, order_id, order_data)
    if new == "gate" and old != "gate":
        gate_time = info["gate"] or "?"
        hall = info.get("hall")
        msg = f"已到閘口 {gate_time}"
        if hall:
            msg += f" | 大堂{hall}"
        if order_data:
            msg += _order_lines(order_data, order_data.get("flight_eta"))
        if _get_parking_client() is not None:
            msg += "\n" + _allowance_line(datetime.now())
        await bot.send_message(chat_id=chat_id, text=msg)
    if new == "cancelled" and old != "cancelled":
        msg = "航班取消"
        if order_data:
            msg += _order_lines(order_data, None)
        await bot.send_message(chat_id=chat_id, text=msg)


async def _set_commands(app):
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("didi", "滴滴快速入單"),
        BotCommand("uber", "Uber 快速入單"),
        BotCommand("cancel", "取消訂單"),
        BotCommand("board", "生成舉牌相"),
        BotCommand("parking", "停車場狀態"),
        BotCommand("paid", "過咗數：/paid 金額"),
    ])


def _sock_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "bot.sock")


async def _start_kick_server(app):
    """Wake the poller the moment another process inserts an order.

    Without this, an order written directly to the DB (web paste) sits
    unnoticed until _next_poll_at expires — up to the full poll interval.
    """
    global _kick_server
    path = _sock_path()
    try:
        os.unlink(path)  # stale socket left behind by a crash
    except FileNotFoundError:
        pass

    async def handle(reader, writer):
        global _next_poll_at
        try:
            line = await reader.readline()
            if line.strip() == b"kick":
                _next_poll_at = None
                app.job_queue.run_once(_poll_tick, 0, job_kwargs=_JOB_KWARGS)
                logger.info("kick received via socket")
        finally:
            writer.close()

    _kick_server = await asyncio.start_unix_server(handle, path=path)


async def _post_shutdown(app):
    if _kick_server:
        _kick_server.close()
    try:
        os.unlink(_sock_path())
    except FileNotFoundError:
        pass


async def _post_init(app):
    await _set_commands(app)
    await _start_kick_server(app)
    app.job_queue.run_repeating(_poll_tick, interval=60, first=5, job_kwargs=_JOB_KWARGS)


async def _on_error(update, context):
    # Without a handler PTB dumps "No error handlers are registered" plus a
    # full traceback for every transient network blip.
    err = context.error
    if _is_transient_network_error(err):
        # Polling recovers from these on its own and they arrive in bursts;
        # a traceback per blip buries the errors that need acting on.
        logging.getLogger("bot").warning("%s: %s", type(err).__name__, err)
        return
    logging.getLogger("bot").error("Unhandled error", exc_info=err)


def main():
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            RotatingFileHandler("logs/bot.log", maxBytes=5_000_000, backupCount=3),
        ],
    )
    # httpx logs every getUpdates URL — bot token included — at INFO, and the
    # 60s heartbeat adds two apscheduler lines per tick. WARNING+ only;
    # misfire warnings stay visible.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    init_db(DB_PATH)
    # Relative DB_PATH depends on cwd; if .env fails to load this line makes
    # a silently-created empty DB obvious.
    logging.getLogger("bot").info("DB: %s (%d active orders)", os.path.abspath(DB_PATH), count_active_orders(DB_PATH))
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("didi", handle_didi))
    app.add_handler(CommandHandler("uber", handle_uber))
    app.add_handler(CommandHandler("cancel", handle_cancel))
    app.add_handler(CommandHandler("board", handle_board))
    app.add_handler(CommandHandler("parking", handle_parking))
    app.add_handler(CommandHandler("paid", handle_paid))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_statement_image))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(_on_error)
    app.post_init = _post_init
    app.post_shutdown = _post_shutdown
    app.run_polling()


if __name__ == "__main__":
    main()
