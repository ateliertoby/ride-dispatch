import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, jsonify
from .db import (
    init_db,
    count_active_orders,
    get_orders_by_date,
    get_order_by_id,
    save_quick_order,
    update_order_fields,
)

load_dotenv()

DB_PATH = os.environ.get("RIDE_DB_PATH", "orders.db")
# Web write ops are gated behind this PIN; unset means the dashboard is
# read-only, same as before writes existed.
WEB_PIN = os.environ.get("RIDE_WEB_PIN", "")

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
    return jsonify({"orders": orders, "date": date_str})


# ---- Auth ----
# Single user, so no sessions: the token is a stable HMAC derived from the
# PIN. Verifying recomputes it — nothing stored, restarts don't log out.

_auth_fails: list[float] = []
AUTH_FAIL_LIMIT = 5
AUTH_FAIL_WINDOW = 60.0


def _token() -> str:
    return hmac.new(WEB_PIN.encode(), b"ride-dispatch-web-auth", hashlib.sha256).hexdigest()


def _require_auth():
    if not WEB_PIN:
        return jsonify({"error": "RIDE_WEB_PIN not configured"}), 403
    header = request.headers.get("Authorization", "")
    if not (header.startswith("Bearer ") and hmac.compare_digest(header[7:], _token())):
        return jsonify({"error": "auth required"}), 401
    return None


@app.post("/api/auth")
def api_auth():
    if not WEB_PIN:
        return jsonify({"error": "RIDE_WEB_PIN not configured"}), 403
    now = time.monotonic()
    _auth_fails[:] = [t for t in _auth_fails if now - t < AUTH_FAIL_WINDOW]
    if len(_auth_fails) >= AUTH_FAIL_LIMIT:
        return jsonify({"error": "too many attempts"}), 429
    pin = str((request.get_json(silent=True) or {}).get("pin", ""))
    if not hmac.compare_digest(pin, WEB_PIN):
        _auth_fails.append(now)
        return jsonify({"error": "wrong pin"}), 401
    return jsonify({"token": _token()})


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


@app.post("/api/orders")
def api_create_order():
    err = _require_auth()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    qtype = body.get("type")
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


@app.patch("/api/orders/<order_id>")
def api_update_order(order_id):
    err = _require_auth()
    if err:
        return err
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
    if not WEB_PIN:
        print("RIDE_WEB_PIN not set — dashboard is read-only", flush=True)
    port = int(os.environ.get("RIDE_WEB_PORT", "3200"))
    app.run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
