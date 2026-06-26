import os
from datetime import date
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
from .db import init_db, get_orders_by_date

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


def main():
    init_db(DB_PATH)
    port = int(os.environ.get("RIDE_WEB_PORT", "3200"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
