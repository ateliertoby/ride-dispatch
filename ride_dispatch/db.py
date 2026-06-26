import sqlite3
from contextlib import contextmanager
from .parser import Order


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
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


_INSERT_SQL = """
    INSERT INTO orders (
        order_id, service_type, vehicle_type, passenger_name,
        scheduled_time, passenger_phone, overseas_phone, flight_number,
        pickup, dropoff, distance_km, notes, driver_notes,
        additional_services, passenger_exit_minutes,
        third_party_contact, more_contacts, raw_message, telegram_msg_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def save_order(db_path: str, order: Order, telegram_msg_id: int) -> int:
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
                telegram_msg_id,
            ),
        )
        conn.commit()
        return cur.lastrowid


def update_price(db_path: str, order_id: str, price: float):
    with _conn(db_path) as conn:
        conn.execute("UPDATE orders SET price = ? WHERE order_id = ?", (price, order_id))
        conn.commit()


def get_orders_by_date(db_path: str, date_str: str) -> list[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE scheduled_time LIKE ? ORDER BY scheduled_time",
            (f"{date_str}%",),
        ).fetchall()
        return [dict(r) for r in rows]


def get_order_by_telegram_msg_id(db_path: str, msg_id: int) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE telegram_msg_id = ?", (msg_id,)
        ).fetchone()
        return dict(row) if row else None
