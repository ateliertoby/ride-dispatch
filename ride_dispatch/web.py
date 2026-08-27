import os
import re
import secrets
import socket
import sqlite3
import time
from dataclasses import asdict
from datetime import date, datetime
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, jsonify, send_file
from .db import (
    init_db,
    resolve_db_path,
    count_active_orders,
    delete_settlement,
    diff_order_against_row,
    get_orders_by_date,
    get_order_by_id,
    get_settle_month,
    get_settlement,
    list_credits,
    save_or_revive_order,
    save_quick_order,
    statements_dir,
    update_order_fields,
    update_order_from_message,
    update_price,
    DIFF_LABELS,
)
from .flight import depart_hhmm, exit_urgency, row_time
from .ingest import parse_any, parking_fee, banner_fee
from .pricing import suggest_price
from .service import PLATFORMS, is_flight_pickup

load_dotenv()

DB_PATH = resolve_db_path()

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
)


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/settle")
def settle():
    return render_template("settle.html")


@app.route("/api/orders")
def api_orders():
    date_str = request.args.get("date", date.today().isoformat())
    orders = get_orders_by_date(DB_PATH, date_str)
    for o in orders:
        # Sort key and payload field are the same value: the dashboard places
        # its NOW line against it, so re-deriving it there could disagree.
        o["row_time"] = row_time(o)
        is_pickup = is_flight_pickup(o.get("service_type") or "")
        o["depart_hhmm"] = depart_hhmm(o) if is_pickup else None
        o["exit_urgency"] = exit_urgency(o.get("passenger_exit_minutes")) if is_pickup else None
    orders.sort(key=lambda o: o["row_time"])
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


# Zero padding is enforced on top of strptime, which accepts "2026-7-1".
# A month is compared as a string prefix, so an unpadded value matches nothing
# and reads as an empty month instead of failing loudly.
_MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


@app.post("/api/orders/parse")
def api_parse_order():
    body = request.get_json(silent=True) or {}
    text = str(body.get("text", "")).strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    order, source = parse_any(text)
    if not order.order_id:
        return jsonify({"error": "認唔到格式"}), 400
    # A live row on the same order_id means the platform re-sent the booking
    # after the customer changed something: the paste is an amendment, and
    # changes says what it would rewrite.
    row = get_order_by_id(DB_PATH, order.order_id)
    changes = diff_order_against_row(order, row, source) if row else []
    return jsonify({
        "order": asdict(order),
        "source": source,
        "parking_fee": parking_fee(order, source),
        "banner_fee": banner_fee(order.additional_services),
        "duplicate": row is not None,
        "changes": [{"field": f, "label": DIFF_LABELS[f], "old": old, "new": new}
                    for f, old, new in changes],
        "locked": bool(row and row["settlement_id"] is not None),
        "current_price": row["price"] if row else None,
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
    date_str = order.scheduled_time.split(" ")[0]
    # A message landing on a live row is the customer's amendment, not a
    # duplicate — apply it in place.  None means no live row holds the id, so
    # this is a first entry or the re-entry of a cancelled leg.
    try:
        changed = update_order_from_message(DB_PATH, order, telegram_msg_id=None, source=source)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    if changed is not None:
        kept = (get_order_by_id(DB_PATH, order.order_id) or {}).get("price")
        if price is not None:
            update_price(DB_PATH, order.order_id, price)
        if changed:
            _kick_bot()
        return jsonify({"order_id": order.order_id,
                        "date": date_str,
                        "updated": bool(changed),
                        "changed": [DIFF_LABELS[f] for f in changed],
                        "price_kept": kept if price is None else None}), 200
    try:
        revived = save_or_revive_order(DB_PATH, order, telegram_msg_id=None,
                                       parking=parking_fee(order, source), source=source)
    except sqlite3.IntegrityError:
        return jsonify({"error": "訂單已存在"}), 409
    if price is not None:
        update_price(DB_PATH, order.order_id, price)
    _kick_bot()
    return jsonify({"order_id": order.order_id,
                    "date": date_str,
                    "revived": revived}), 201


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
    try:
        updated = update_order_fields(DB_PATH, order_id, fields)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not updated:
        return jsonify({"error": "order not found"}), 404
    return jsonify({"ok": True})


# ---- Settlement (埋數) ----


@app.get("/api/settle")
def api_settle():
    month = request.args.get("month", date.today().strftime("%Y-%m"))
    if not _MONTH_RE.fullmatch(month):
        return jsonify({"error": "month must be YYYY-MM"}), 400
    platform = request.args.get("platform", "ride")
    if platform not in PLATFORMS:
        return jsonify({"error": f"platform must be one of {sorted(PLATFORMS)}"}), 400
    data = get_settle_month(DB_PATH, month, platform)
    for batch in data["settlements"]:
        # The page only asks whether a screenshot exists; the file name it is
        # stored under is not something the client can do anything with.
        batch["statement_image"] = bool(batch.get("statement_image"))
    return jsonify({"month": month, "platform": platform, **data})


# A batch is created by the bot's statement flow and nowhere else: every batch
# has to trace back to the statement image it was read from, and on to the
# orders that statement lists.  The page can still undo one.


@app.get("/api/credits")
def api_credits():
    platform = request.args.get("platform", "ride")
    if platform not in PLATFORMS:
        return jsonify({"error": f"platform must be one of {sorted(PLATFORMS)}"}), 400
    credits = list_credits(DB_PATH, platform)
    counts = {state: 0 for state in ("open", "partial", "done", "archived")}
    sums = {"open": 0.0, "done": 0.0}
    for c in credits:
        counts[c["state"]] += 1
        if c["state"] in ("open", "partial"):
            sums["open"] += c["remaining"]
        elif c["state"] == "done":
            sums["done"] += c["amount"]
    return jsonify({
        "platform": platform,
        "counts": counts,
        "sums": {k: round(v, 2) for k, v in sums.items()},
        "credits": [{k: c[k] for k in ("id", "ref", "amount", "value_date", "linked",
                                       "remaining", "state", "archived_reason", "memo",
                                       "batches")} for c in credits],
    })


@app.delete("/api/settlements/<int:settlement_id>")
def api_delete_settlement(settlement_id):
    if not delete_settlement(DB_PATH, settlement_id):
        return jsonify({"error": "settlement not found"}), 404
    return jsonify({"ok": True})


_IMAGE_MIMETYPES = {"jpg": "image/jpeg", "png": "image/png", "heic": "image/heic"}


@app.get("/api/settlements/<int:settlement_id>/image")
def api_settlement_image(settlement_id):
    batch = get_settlement(DB_PATH, settlement_id)
    if not batch or not batch.get("statement_image"):
        return jsonify({"error": "no statement image"}), 404
    name = batch["statement_image"]
    # Only create_settlement ever writes this column, but it is still a stored
    # value being joined onto a path: anything that could climb out of the
    # statements directory is refused rather than resolved.
    if os.sep in name or "/" in name or ".." in name:
        return jsonify({"error": "no statement image"}), 404
    path = os.path.join(statements_dir(DB_PATH), name)
    if not os.path.exists(path):
        return jsonify({"error": "no statement image"}), 404
    ext = name.rsplit(".", 1)[-1].lower()
    return send_file(path, mimetype=_IMAGE_MIMETYPES.get(ext, "application/octet-stream"))


def _fingerprint():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT count(*), coalesce(max(id),0), coalesce(sum(price),0), "
        "count(case when status='cancelled' then 1 end), "
        "coalesce(sum(tunnel_fee),0), coalesce(sum(parking_fee),0), coalesce(sum(banner_fee),0), "
        "coalesce(sum(settlement_id),0), "
        "coalesce(group_concat(coalesce(scheduled_time,'') || coalesce(flight_eta,'') || coalesce(flight_gate,'') || coalesce(flight_status,'')),'') FROM orders"
    ).fetchone()
    # max(id) rather than count alone: ids are AUTOINCREMENT, so undoing a batch
    # and settling again is a visible change instead of a wash.
    settle_row = conn.execute(
        "SELECT count(*), coalesce(max(id),0), count(bank_credit_id) FROM settlements"
    ).fetchone()
    # The ledger changes without any order or batch moving — a credit lands, a
    # credit is archived — and an open settle page has to repaint for both.
    credit_row = conn.execute(
        "SELECT count(*), count(archived_reason) FROM bank_credits"
    ).fetchone()
    conn.close()
    return "-".join(str(v) for v in (*row, *settle_row, *credit_row))


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
