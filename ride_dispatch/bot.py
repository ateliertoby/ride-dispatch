import logging
import os
import sqlite3
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from .parser import parse_order, parse_feizhu, parse_tongcheng
from .db import init_db, save_order, save_quick_order, update_price, update_cost, cancel_order, count_active_orders, get_orders_by_date, get_order_by_id, get_order_by_telegram_msg_id, get_pickup_flights, get_tracking_dates, update_flight_info
from .flight import fetch_arrivals, match_flights, calc_next_interval, svc_time

load_dotenv()

DB_PATH = os.environ.get("RIDE_DB_PATH", "orders.db")
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


def format_card(order) -> str:
    type_map = {"接机": "接機", "送机": "送機"}
    type_label = type_map.get(order.service_type, "單程")
    time = order.scheduled_time
    if " " in time:
        time = time.split(" ")[1][:5]
    lines = [
        f"{type_label} | {order.flight_number}",
        f"乘客: {order.passenger_name}",
        f"時間: {time}",
        f"上車: {order.pickup}",
        f"落車: {order.dropoff}",
    ]
    if order.distance_km:
        lines.append(f"里程: {order.distance_km}km")
    if order.driver_notes:
        lines.append(f"備註: {order.driver_notes}")
    return "\n".join(lines)


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

    order = parse_order(msg.text)
    source = "携程"
    if not (order.order_id and order.pickup):
        order = parse_feizhu(msg.text)
        source = "飛豬"
    if not order.order_id:
        order = parse_tongcheng(msg.text)
        source = "同程"
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
                order_id, banner_fee = awaiting_price.pop(chat_id)
                update_price(DB_PATH, order_id, price)
                if banner_fee:
                    await msg.reply_text(f"已更新價錢: ${price:g}（+舉牌${banner_fee:g}）")
                else:
                    await msg.reply_text(f"已更新價錢: ${price:g}")
            except ValueError:
                pass
            return
        return

    text = format_card(order)
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("確認", callback_data="confirm"),
            InlineKeyboardButton("取消", callback_data="cancel"),
        ]]
    )
    sent = await msg.reply_text(text, reply_markup=keyboard)
    pending[sent.message_id] = (order, source)


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
        order, source = entry
        try:
            parking = 32.0 if source == "携程" and order.service_type == "接机" else 0.0
            banner_fee = 40.0 if "举牌" in (order.additional_services or "") else 0.0
            prompt = f"訂單 #{order.order_id[-4:]} 已保存。直接打價錢。"
            if banner_fee:
                prompt += f"（會自動加${banner_fee:g}舉牌費）"
            sent = await query.message.reply_text(prompt)
            save_order(DB_PATH, order, telegram_msg_id=sent.message_id, parking=parking, source=source)
            if order.service_type == "接机" and order.flight_number:
                _kick_poll(context)
            awaiting_price[query.message.chat_id] = (order.order_id, banner_fee)
            await query.message.edit_reply_markup(reply_markup=None)
            await query.answer("已確認")
        except sqlite3.IntegrityError:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.answer("訂單已存在")
            await query.message.reply_text("呢張單已經存在。")

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
        type_label = {"接机": "接機", "送机": "送機"}.get(order["service_type"], "單程")
        lines = [
            f"{type_label} | {order['flight_number']}",
            f"乘客: {order['passenger_name']}",
            f"時間: {t}",
        ]
        if order.get("price"):
            lines.append(f"收入: ${order['price']:g}")
        tunnel = order.get("tunnel_fee") or 0
        parking = order.get("parking_fee") or 0
        if tunnel or parking:
            lines.append(f"成本: 隧道${tunnel:g} 停車${parking:g}")
        is_pickup = order["service_type"] == "接机"
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
# globals decide whether a tick does real work.
POLL_ERROR_BACKOFF = 300
_JOB_KWARGS = {"misfire_grace_time": None, "coalesce": True}
_next_poll_at: datetime | None = None
_poll_running = False
_last_state: str | None = None
_warned_statuses: set[tuple[str, str]] = set()


def _log_state(state: str | None):
    # Idle states recur every tick; log transitions only.
    global _last_state
    if state and state != _last_state:
        logger.info(state)
    _last_state = state


def _kick_poll(context):
    global _next_poll_at
    _next_poll_at = None
    context.application.job_queue.run_once(_poll_tick, 5, job_kwargs=_JOB_KWARGS)


async def _poll_tick(context):
    global _next_poll_at, _poll_running
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


async def _poll_and_notify(context) -> int:
    """One polling pass; returns seconds until the next pass is due."""
    bot = context.application.bot
    chat_id = int(os.environ.get("NOTIFY_CHAT_ID", list(ALLOWED_CHAT_IDS)[0] if ALLOWED_CHAT_IDS else "0"))
    if not chat_id:
        _log_state("No NOTIFY_CHAT_ID/ALLOWED_CHAT_IDS configured, not polling")
        return 3600

    dates = get_tracking_dates(DB_PATH)
    if not dates:
        _log_state("idle: no orders in tracking window")
        return 60

    enriched = _orders_in(dates)
    if calc_next_interval(enriched) is None:
        _log_state("idle: all tracking windows closed")
        return 60
    _log_state(None)

    old_statuses = {
        o["order_id"]: o.get("flight_status")
        for o in enriched
        if o.get("service_type") == "接机" and o.get("flight_number")
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
        return 60
    logger.info("Next poll in %ds", interval)
    return interval


def _order_lines(order_data: dict, arrival_hhmm: str | None) -> str:
    lines = f"\n航班: {order_data['flight_number']}"
    if order_data.get("passenger_name"):
        lines += f"\n乘客: {order_data['passenger_name']}"
    phone = order_data.get("passenger_phone") or order_data.get("overseas_phone")
    if phone:
        lines += f"\n電話: {phone}"
    if order_data.get("passenger_exit_minutes"):
        svc = svc_time(arrival_hhmm, order_data["passenger_exit_minutes"])
        if svc:
            lines += f"\n用車: {svc}"
    if "举牌" in (order_data.get("additional_services") or ""):
        lines += f"\n舉牌: {order_data.get('passenger_name', '')}"
    return lines


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
        await bot.send_message(chat_id=chat_id, text=msg)
    if new == "gate" and old != "gate":
        gate_time = info["gate"] or "?"
        hall = info.get("hall")
        msg = f"已到閘口 {gate_time}"
        if hall:
            msg += f" | 大堂{hall}"
        if order_data:
            msg += _order_lines(order_data, order_data.get("flight_eta"))
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
    ])


async def _post_init(app):
    await _set_commands(app)
    app.job_queue.run_repeating(_poll_tick, interval=60, first=5, job_kwargs=_JOB_KWARGS)


async def _on_error(update, context):
    # Without a handler PTB dumps "No error handlers are registered" plus a
    # full traceback for every transient network blip.
    logging.getLogger("bot").error("Unhandled error", exc_info=context.error)


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
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(_on_error)
    app.post_init = _post_init
    app.run_polling()


if __name__ == "__main__":
    main()
