import os
import sqlite3
import time
from datetime import date
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, jsonify
from .db import init_db, count_active_orders, get_orders_by_date

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
    return jsonify({"orders": orders, "date": date_str})


def _fingerprint():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT count(*), coalesce(max(id),0), coalesce(sum(price),0), "
        "count(case when status='cancelled' then 1 end), "
        "coalesce(sum(tunnel_fee),0), coalesce(sum(parking_fee),0), "
        "coalesce(group_concat(coalesce(flight_eta,'') || coalesce(flight_gate,'') || coalesce(flight_status,'')),'') FROM orders"
    ).fetchone()
    conn.close()
    return f"{row[0]}-{row[1]}-{row[2]}-{row[3]}-{row[4]}-{row[5]}-{row[6]}"


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
