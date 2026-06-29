import os
import sqlite3
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
from .db import init_db, save_order, save_quick_order, update_price, update_cost, cancel_order, get_orders_by_date, get_order_by_telegram_msg_id
from .flight import match_order_from_cache

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
                match_order_from_cache(DB_PATH, order.order_id, order.flight_number)
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
        from datetime import date
        orders = get_orders_by_date(DB_PATH, date.today().isoformat())
        order = next((o for o in orders if o["order_id"] == order_id), None)
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


async def _set_commands(app):
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("didi", "滴滴快速入單"),
        BotCommand("uber", "Uber 快速入單"),
        BotCommand("cancel", "取消訂單"),
    ])


def main():
    os.makedirs("logs", exist_ok=True)
    init_db(DB_PATH)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("didi", handle_didi))
    app.add_handler(CommandHandler("uber", handle_uber))
    app.add_handler(CommandHandler("cancel", handle_cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.post_init = _set_commands
    app.run_polling()


if __name__ == "__main__":
    main()
