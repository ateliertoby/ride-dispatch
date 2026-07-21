import os
import re
import secrets
import socket
import sqlite3
import time
from dataclasses import asdict
from datetime import date, datetime
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, jsonify
from .db import (
    init_db,
    count_active_orders,
    get_orders_by_date,
    get_order_by_id,
    order_id_exists,
    save_order,
    save_quick_order,
    update_order_fields,
    update_price,
)
from .flight import depart_hhmm, exit_urgency
from .ingest import parse_any, parking_fee, banner_fee
from .pricing import suggest_price

load_dotenv()

DB_PATH = os.environ.get("RIDE_DB_PATH", "orders.db")

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
)


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/orders")
def api_orders():
    date_str = request.args.get("date", date.today().isoformat())
    orders = get_orders_by_date(DB_PATH, date_str)
    for o in orders:
        is_pickup = o.get("service_type") == "接机"
        o["depart_hhmm"] = depart_hhmm(o) if is_pickup else None
        o["exit_urgency"] = exit_urgency(o.get("passenger_exit_minutes")) if is_pickup else None
    return jsonify({"orders": orders, "date": date_str})


# ---- Write ops ----

QUICK_TYPES = {
    "didi": ("滴滴", "滴滴"),
    "uber": ("Uber", "Uber"),
    "foodpanda": ("foodpanda", "foodpanda"),
}

_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def _parse_money(value, field: str) -> tuple[float | None, tuple | None]:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None, (jsonify({"error": f"{field} must be a number"}), 400)
    if amount < 0:
        return None, (jsonify({"error": f"{field} must be >= 0"}), 400)
    return amount, None


@app.post("/api/orders/parse")
def api_parse_order():
    body = request.get_json(silent=True) or {}
    text = str(body.get("text", "")).strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    order, source = parse_any(text)
    if not order.order_id:
        return jsonify({"error": "認唔到格式"}), 400
    return jsonify({
        "order": asdict(order),
        "source": source,
        "parking_fee": parking_fee(order, source),
        "banner_fee": banner_fee(order.additional_services),
        "duplicate": order_id_exists(DB_PATH, order.order_id),
        "suggested_price": suggest_price(DB_PATH, order),
        "exit_urgency": exit_urgency(order.passenger_exit_minutes),
    })


@app.post("/api/orders")
def api_create_order():
    body = request.get_json(silent=True) or {}
    qtype = body.get("type")
    if qtype == "paste":
        return _create_paste_order(body)
    if qtype not in QUICK_TYPES:
        return jsonify({"error": f"type must be one of {sorted(QUICK_TYPES)}"}), 400
    date_str = str(body.get("date", ""))
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    time_str = str(body.get("time", ""))
    if not _TIME_RE.fullmatch(time_str):
        return jsonify({"error": "time must be HH:MM"}), 400
    price, perr = _parse_money(body.get("price"), "price")
    if perr:
        return perr
    tunnel, terr = _parse_money(body.get("tunnel_fee", 0), "tunnel_fee")
    if terr:
        return terr

    service_type, source = QUICK_TYPES[qtype]
    scheduled = f"{date_str} {time_str}:00"
    order_id = f"{qtype}_{date_str.replace('-', '')}{time_str.replace(':', '')}_{secrets.token_hex(2)}"
    save_quick_order(DB_PATH, order_id, service_type, scheduled, price, tunnel, source=source)
    return jsonify({"order_id": order_id}), 201


def _create_paste_order(body):
    text = str(body.get("text", "")).strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    order, source = parse_any(text)
    if not order.order_id:
        return jsonify({"error": "認唔到格式"}), 400
    if not order.scheduled_time or " " not in order.scheduled_time:
        return jsonify({"error": "單冇用車時間"}), 400
    price = None
    if body.get("price") is not None:
        price, perr = _parse_money(body.get("price"), "price")
        if perr:
            return perr
    try:
        save_order(DB_PATH, order, telegram_msg_id=None,
                   parking=parking_fee(order, source), source=source)
    except sqlite3.IntegrityError:
        return jsonify({"error": "訂單已存在"}), 409
    if price is not None:
        update_price(DB_PATH, order.order_id, price)
    _kick_bot()
    return jsonify({"order_id": order.order_id,
                    "date": order.scheduled_time.split(" ")[0]}), 201


def _sock_path():
    # Resolved per call, not at import: tests monkeypatch DB_PATH, and the
    # socket path must follow it — a path frozen at import would point at
    # the real bot's socket during test runs.
    return os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "bot.sock")


def _kick_bot():
    """Ask the bot to poll now (flight tracking / reminders).

    Best-effort: if the bot is down this is a silent no-op — its first
    poll on startup covers whatever this kick would have triggered.
    """
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(_sock_path())
        s.sendall(b"kick\n")
        s.close()
    except OSError:
        pass


@app.patch("/api/orders/<order_id>")
def api_update_order(order_id):
    body = request.get_json(silent=True) or {}
    fields = {}
    for key in ("price", "tunnel_fee", "parking_fee", "banner_fee"):
        if key in body:
            amount, merr = _parse_money(body[key], key)
            if merr:
                return merr
            fields[key] = amount
    if "time" in body:
        time_str = str(body["time"])
        if not _TIME_RE.fullmatch(time_str):
            return jsonify({"error": "time must be HH:MM"}), 400
        order = get_order_by_id(DB_PATH, order_id)
        if not order:
            return jsonify({"error": "order not found"}), 404
        day = order["scheduled_time"].split(" ")[0]
        fields["scheduled_time"] = f"{day} {time_str}:00"
    if "status" in body:
        if body["status"] != "cancelled":
            return jsonify({"error": "status can only be set to cancelled"}), 400
        fields["status"] = "cancelled"
    if not fields:
        return jsonify({"error": "no updatable fields in body"}), 400
    if not update_order_fields(DB_PATH, order_id, fields):
        return jsonify({"error": "order not found"}), 404
    return jsonify({"ok": True})


def _fingerprint():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT count(*), coalesce(max(id),0), coalesce(sum(price),0), "
        "count(case when status='cancelled' then 1 end), "
        "coalesce(sum(tunnel_fee),0), coalesce(sum(parking_fee),0), coalesce(sum(banner_fee),0), "
        "coalesce(group_concat(coalesce(scheduled_time,'') || coalesce(flight_eta,'') || coalesce(flight_gate,'') || coalesce(flight_status,'')),'') FROM orders"
    ).fetchone()
    conn.close()
    return "-".join(str(v) for v in row)


@app.route("/api/events")
def events():
    def stream():
        yield "data: connected\n\n"
        last = _fingerprint()
        while True:
            time.sleep(2)
            current = _fingerprint()
            if current != last:
                last = current
                yield "data: refresh\n\n"

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def main():
    os.makedirs("logs", exist_ok=True)
    init_db(DB_PATH)
    print(f"DB: {os.path.abspath(DB_PATH)} ({count_active_orders(DB_PATH)} active orders)", flush=True)
    port = int(os.environ.get("RIDE_WEB_PORT", "3200"))
    app.run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
