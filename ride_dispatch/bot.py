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
from .parser import parse_order, parse_tongcheng
from .db import init_db, save_order, update_price, update_cost, cancel_order, get_orders_by_date, get_order_by_telegram_msg_id

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


def format_card(order) -> str:
    type_label = "接機" if order.service_type == "接机" else "送機"
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
                await msg.reply_text(f"已更新價錢: ${price:.0f}")
                return
            except ValueError:
                pass

    order = parse_order(msg.text)
    if not (order.order_id and order.pickup):
        order = parse_tongcheng(msg.text)
    if not order.order_id:
        chat_id = msg.chat_id
        if chat_id in awaiting_cost:
            text = msg.text.strip()
            try:
                amount = float(text)
                order_id, cost_type = awaiting_cost.pop(chat_id)
                label = "隧道費" if cost_type == "tunnel" else "停車費"
                update_cost(DB_PATH, order_id, cost_type, amount)
                await msg.reply_text(f"已記錄{label}: ${amount:.0f}")
            except ValueError:
                pass
            return
        if chat_id in awaiting_price:
            text = msg.text.strip()
            try:
                price = float(text)
                order_id = awaiting_price.pop(chat_id)
                update_price(DB_PATH, order_id, price)
                await msg.reply_text(f"已更新價錢: ${price:.0f}")
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
    pending[sent.message_id] = order


async def handle_callback(update: Update, context):
    query = update.callback_query
    if ALLOWED_CHAT_IDS and query.message.chat_id not in ALLOWED_CHAT_IDS:
        return
    msg_id = query.message.message_id

    if query.data == "confirm":
        order = pending.pop(msg_id, None)
        if not order:
            await query.answer("訂單已過期")
            return
        try:
            sent = await query.message.reply_text(
                f"訂單 #{order.order_id[-4:]} 已保存。直接打價錢。"
            )
            save_order(DB_PATH, order, telegram_msg_id=sent.message_id)
            awaiting_price[query.message.chat_id] = order.order_id
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

    elif query.data.startswith("waive:"):
        _, cost_type, order_id = query.data.split(":", 2)
        update_cost(DB_PATH, order_id, cost_type, 0)
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.reply_text(f"已免停車費 #{order_id[-4:]}")
        await query.answer()


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
        type_label = "接機" if order["service_type"] == "接机" else "送機"
        lines = [
            f"{type_label} | {order['flight_number']}",
            f"乘客: {order['passenger_name']}",
            f"時間: {t}",
        ]
        if order.get("price"):
            lines.append(f"收入: ${order['price']:.0f}")
        tunnel = order.get("tunnel_fee") or 0
        parking = order.get("parking_fee") or 0
        if tunnel or parking:
            lines.append(f"成本: 隧道${tunnel:.0f} 停車${parking:.0f}")
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


def main():
    os.makedirs("logs", exist_ok=True)
    init_db(DB_PATH)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("cancel", handle_cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
