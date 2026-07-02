import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from .parser import Order

COARSE_WINDOW_HOURS = 24


@contextmanager
def _conn(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str):
    with _conn(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE,
                service_type TEXT,
                vehicle_type TEXT,
                passenger_name TEXT,
                scheduled_time TEXT,
                passenger_phone TEXT,
                overseas_phone TEXT,
                flight_number TEXT,
                pickup TEXT,
                dropoff TEXT,
                distance_km INTEGER,
                notes TEXT,
                driver_notes TEXT,
                additional_services TEXT,
                passenger_exit_minutes INTEGER,
                third_party_contact TEXT,
                more_contacts TEXT,
                price REAL,
                raw_message TEXT,
                telegram_msg_id INTEGER,
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        for col in [
            "status TEXT DEFAULT 'active'",
            "tunnel_fee REAL DEFAULT 0",
            "parking_fee REAL DEFAULT 0",
            "banner_fee REAL DEFAULT 0",
            "estimated_landing TEXT",
            "flight_scheduled TEXT",
            "flight_eta TEXT",
            "flight_gate TEXT",
            "flight_status TEXT",
            "flight_hall TEXT",
            "source TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        conn.commit()


_INSERT_SQL = """
    INSERT INTO orders (
        order_id, service_type, vehicle_type, passenger_name,
        scheduled_time, passenger_phone, overseas_phone, flight_number,
        pickup, dropoff, distance_km, notes, driver_notes,
        additional_services, passenger_exit_minutes,
        third_party_contact, more_contacts, raw_message, telegram_msg_id,
        parking_fee, banner_fee, source
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def save_order(db_path: str, order: Order, telegram_msg_id: int, parking: float = 0.0, source: str = "") -> int:
    banner = 40.0 if "举牌" in (order.additional_services or "") else 0.0
    with _conn(db_path) as conn:
        cur = conn.execute(
            _INSERT_SQL,
            (
                order.order_id, order.service_type, order.vehicle_type,
                order.passenger_name, order.scheduled_time, order.passenger_phone,
                order.overseas_phone, order.flight_number, order.pickup,
                order.dropoff, order.distance_km, order.notes, order.driver_notes,
                order.additional_services, order.passenger_exit_minutes,
                order.third_party_contact, order.more_contacts, order.raw_message,
                telegram_msg_id, parking, banner, source,
            ),
        )
        conn.commit()
        return cur.lastrowid


def save_quick_order(db_path: str, order_id: str, service_type: str, scheduled_time: str, price: float, tunnel_fee: float, source: str = "") -> int:
    with _conn(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO orders (order_id, service_type, scheduled_time, price, tunnel_fee, passenger_name, source)
               VALUES (?, ?, ?, ?, ?, '', ?)""",
            (order_id, service_type, scheduled_time, price, tunnel_fee, source),
        )
        conn.commit()
        return cur.lastrowid


def update_price(db_path: str, order_id: str, price: float):
    with _conn(db_path) as conn:
        conn.execute("UPDATE orders SET price = ? WHERE order_id = ?", (price, order_id))
        conn.commit()


def update_cost(db_path: str, order_id: str, cost_type: str, amount: float):
    col = {"tunnel": "tunnel_fee", "parking": "parking_fee"}[cost_type]
    with _conn(db_path) as conn:
        conn.execute(f"UPDATE orders SET {col} = ? WHERE order_id = ?", (amount, order_id))
        conn.commit()


def cancel_order(db_path: str, order_id: str):
    with _conn(db_path) as conn:
        conn.execute("UPDATE orders SET status = 'cancelled' WHERE order_id = ?", (order_id,))
        conn.commit()


def get_orders_by_date(db_path: str, date_str: str) -> list[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE scheduled_time LIKE ? AND coalesce(status,'active') = 'active' ORDER BY scheduled_time",
            (f"{date_str}%",),
        ).fetchall()
        return [dict(r) for r in rows]


def get_order_by_id(db_path: str, order_id: str) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id = ? AND coalesce(status,'active') = 'active'", (order_id,)
        ).fetchone()
        return dict(row) if row else None


def get_order_by_telegram_msg_id(db_path: str, msg_id: int) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE telegram_msg_id = ?", (msg_id,)
        ).fetchone()
        return dict(row) if row else None


def get_tracking_dates(db_path: str, now: datetime | None = None) -> list[str]:
    # Coarse time gate only — flight_status deliberately has no say here,
    # so a stale/wrong status can never stop the poll loop (MU5017 2026-07-02).
    # Fine-grained termination lives in flight.calc_next_interval.
    now = now or datetime.now()
    cutoff = (now - timedelta(hours=COARSE_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(scheduled_time, 1, 10) AS d FROM orders "
            "WHERE service_type = '接机' AND flight_number != '' "
            "AND coalesce(status,'active') = 'active' "
            "AND scheduled_time >= ? "
            "ORDER BY d",
            (cutoff,),
        ).fetchall()
        return [r["d"] for r in rows]


def get_pickup_flights(db_path: str, date_str: str) -> list[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT order_id, flight_number FROM orders "
            "WHERE scheduled_time LIKE ? AND service_type = '接机' "
            "AND flight_number != '' AND coalesce(status,'active') = 'active'",
            (f"{date_str}%",),
        ).fetchall()
        return [dict(r) for r in rows]


def update_flight_info(db_path: str, order_id: str, scheduled: str, eta: str | None, gate: str | None, status: str | None, hall: str | None = None):
    with _conn(db_path) as conn:
        sets = ["flight_scheduled = ?", "flight_status = ?"]
        params = [scheduled, status]
        if eta is not None:
            sets.append("flight_eta = ?")
            params.append(eta)
        if gate is not None:
            sets.append("flight_gate = ?")
            params.append(gate)
        if hall:
            sets.append("flight_hall = ?")
            params.append(hall)
        params.append(order_id)
        conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE order_id = ?", params)
        conn.commit()
