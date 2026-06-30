import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, jsonify
from .db import init_db, get_orders_by_date, get_pickup_flights, update_flight_info, save_arrivals_cache
from .flight import fetch_arrivals, match_flights, build_cache_entries

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


def _calc_poll_interval(orders: list[dict]) -> int | None:
    now = datetime.now()
    active = [o for o in orders if o.get("flight_status") != "gate"]
    if not active:
        return None

    min_minutes = float("inf")
    has_landed = False
    for o in active:
        if o.get("flight_status") == "landed":
            has_landed = True
            break
        eta = o.get("flight_eta") or o.get("flight_scheduled")
        if not eta:
            continue
        h, m = int(eta[:2]), int(eta[3:5])
        landing = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if landing < now - timedelta(hours=6):
            landing += timedelta(days=1)
        diff = (landing - now).total_seconds() / 60
        min_minutes = min(min_minutes, diff)

    if has_landed or min_minutes <= 5:
        return 60
    if min_minutes <= 30:
        return 120
    if min_minutes <= 120:
        return 300
    return 1800


def _poll_flights():
    logger = logging.getLogger("flight_poller")
    while True:
        try:
            today = date.today().isoformat()
            dates = [today]
            if datetime.now().hour < 2:
                yesterday = (date.today() - timedelta(days=1)).isoformat()
                dates.insert(0, yesterday)

            orders = []
            for d in dates:
                orders.extend(get_pickup_flights(DB_PATH, d))

            if not orders:
                time.sleep(1800)
                continue

            arrivals = []
            for d in dates:
                try:
                    arrivals.extend(fetch_arrivals(d))
                except Exception:
                    logger.exception("Failed to fetch arrivals for %s", d)

            save_arrivals_cache(DB_PATH, build_cache_entries(arrivals))

            updates = match_flights(orders, arrivals)
            for order_id, info in updates.items():
                update_flight_info(DB_PATH, order_id, info["scheduled"], info["eta"], info["gate"], info["status"])

            if updates:
                logger.info("Updated %d flight(s): %s", len(updates), list(updates.keys()))

            enriched = get_orders_by_date(DB_PATH, today)
            pickup_enriched = [o for o in enriched if o.get("service_type") == "接机" and o.get("flight_number")]
            interval = _calc_poll_interval(pickup_enriched)
            if interval is None:
                logger.info("All flights at gate, pausing poller")
                time.sleep(1800)
                continue
            logger.info("Next poll in %ds (closest ETA-based)", interval)
            time.sleep(interval)
            continue
        except Exception:
            logger.exception("Flight poll error")

        time.sleep(300)


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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler("logs/web.log"),
            logging.StreamHandler(),
        ],
    )
    init_db(DB_PATH)
    t = threading.Thread(target=_poll_flights, daemon=True)
    t.start()
    port = int(os.environ.get("RIDE_WEB_PORT", "3200"))
    app.run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
