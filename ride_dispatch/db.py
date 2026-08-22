import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from .parser import Order
from .ingest import banner_fee
from .service import PLATFORMS, expected_of, needs_departure_reminder, platform_of

COARSE_WINDOW_HOURS = 24


# Callers keep the result in a module-level constant and derive sibling paths
# from it (the bot/web rendezvous socket) via os.path.abspath, which would turn
# a leading "~" into a literal directory under cwd. Expansion therefore belongs
# at the env read, not at sqlite3.connect.
def resolve_db_path() -> str:
    return os.path.expanduser(os.environ.get("RIDE_DB_PATH", "orders.db"))


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
            "reminders_sent TEXT DEFAULT ''",
            "settlement_id INTEGER",
        ]:
            try:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT,
                expected_amount REAL,
                confirmed_amount REAL,
                settled_on TEXT,
                paid_on TEXT,
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
        third_party_contact, more_contacts, raw_message, telegram_msg_id,
        parking_fee, banner_fee, source
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def save_order(db_path: str, order: Order, telegram_msg_id: int, parking: float = 0.0, source: str = "") -> int:
    banner = banner_fee(order.additional_services)
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


UPDATABLE_FIELDS = {"price", "tunnel_fee", "parking_fee", "banner_fee", "scheduled_time", "status"}

# A settled batch's expected_amount is frozen at creation, so the fields it was
# summed from — and cancellation, which would remove the order from the sum
# entirely — are locked while the order belongs to a batch.  parking_fee and
# scheduled_time never feed the sum and stay editable.
BATCH_LOCKED_FIELDS = {"price", "tunnel_fee", "banner_fee", "status"}

SETTLED_LOCK_MSG = "已結算嘅單要先撤銷結算"


def _assert_unbatched(conn, order_id: str):
    row = conn.execute(
        "SELECT settlement_id FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    if row and row["settlement_id"] is not None:
        raise ValueError(SETTLED_LOCK_MSG)


def update_order_fields(db_path: str, order_id: str, fields: dict) -> bool:
    bad = set(fields) - UPDATABLE_FIELDS
    if bad:
        raise ValueError(f"non-updatable fields: {sorted(bad)}")
    if not fields:
        return False
    cols = sorted(fields)
    sets = ", ".join(f"{c} = ?" for c in cols)
    params = [fields[c] for c in cols] + [order_id]
    with _conn(db_path) as conn:
        if BATCH_LOCKED_FIELDS & set(fields):
            _assert_unbatched(conn, order_id)
        cur = conn.execute(f"UPDATE orders SET {sets} WHERE order_id = ?", params)
        conn.commit()
        return cur.rowcount > 0


def cancel_order(db_path: str, order_id: str):
    with _conn(db_path) as conn:
        _assert_unbatched(conn, order_id)
        conn.execute("UPDATE orders SET status = 'cancelled' WHERE order_id = ?", (order_id,))
        conn.commit()


def count_active_orders(db_path: str) -> int:
    with _conn(db_path) as conn:
        return conn.execute(
            "SELECT count(*) FROM orders WHERE coalesce(status,'active') = 'active'"
        ).fetchone()[0]


def get_orders_by_date(db_path: str, date_str: str) -> list[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT o.*, s.paid_on AS settlement_paid_on, s.settled_on AS settlement_settled_on "
            "FROM orders o LEFT JOIN settlements s ON s.id = o.settlement_id "
            "WHERE o.scheduled_time LIKE ? AND coalesce(o.status,'active') = 'active' "
            "ORDER BY o.scheduled_time",
            (f"{date_str}%",),
        ).fetchall()
        return [dict(r) for r in rows]


def get_order_by_id(db_path: str, order_id: str) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id = ? AND coalesce(status,'active') = 'active'", (order_id,)
        ).fetchone()
        return dict(row) if row else None


def order_id_exists(db_path: str, order_id: str) -> bool:
    with _conn(db_path) as conn:
        row = conn.execute("SELECT 1 FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        return row is not None


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
            "SELECT order_id, flight_number, scheduled_time FROM orders "
            "WHERE scheduled_time LIKE ? AND service_type = '接机' "
            "AND flight_number != '' AND coalesce(status,'active') = 'active'",
            (f"{date_str}%",),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_reminder_sent(db_path: str, order_id: str, tag: str):
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT reminders_sent FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if not row:
            return
        current = row["reminders_sent"] or ""
        tags = set(filter(None, current.split(",")))
        tags.add(tag)
        conn.execute(
            "UPDATE orders SET reminders_sent = ? WHERE order_id = ?",
            (",".join(sorted(tags)), order_id),
        )
        conn.commit()


def get_departure_reminders(db_path: str, now: datetime) -> list[dict]:
    low = now.strftime("%Y-%m-%d %H:%M:%S")
    high = (now + timedelta(minutes=65)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn(db_path) as conn:
        # Service-type filtering is deliberately omitted from the SQL: the
        # rule lives in service.needs_departure_reminder, and restating it
        # as an IN list here would be a second place to update.  Volume is
        # 3-7 orders/day so the extra rows from a coarse filter are free.
        rows = conn.execute(
            "SELECT * FROM orders "
            "WHERE coalesce(status,'active') = 'active' "
            "AND scheduled_time >= ? AND scheduled_time <= ? "
            "ORDER BY scheduled_time",
            (low, high),
        ).fetchall()
        return [dict(r) for r in rows if needs_departure_reminder(r["service_type"] or "")]


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


# ---- Settlement (埋數) ----

# Columns the settle page needs per order; the batch total is recomputed from
# price/banner_fee/tunnel_fee, so all three travel with every row.
_SETTLE_ORDER_COLS = (
    "order_id, scheduled_time, service_type, flight_number, "
    "pickup, dropoff, price, banner_fee, tunnel_fee, settlement_id"
)

# Only a finished, priced, not-yet-batched leg can enter a batch.  The clock is
# a parameter so the settle page and the server agree on which legs are done.
_SETTLEABLE_SQL = (
    "coalesce(status,'active') = 'active' AND settlement_id IS NULL "
    "AND coalesce(price,0) > 0 AND scheduled_time < ?"
)


def _now_str(now: datetime | None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def create_settlement(db_path: str, platform: str, order_ids: list[str],
                      confirmed_amount: float, settled_on: str,
                      now: datetime | None = None) -> int:
    """Batch one platform's settleable orders; returns the settlement id.

    All-or-nothing: any order that is missing, cancelled, unpriced, still in
    the future, already batched, or belonging to another platform aborts the
    call with ValueError naming it, and nothing is written.  expected_amount is
    summed from the stored rows rather than taken from the caller, so a stale
    client cannot disagree with the DB about what is owed.
    """
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform: {platform}")
    if not order_ids:
        raise ValueError("order_ids required")
    cutoff = _now_str(now)
    with _conn(db_path) as conn:
        expected = 0.0
        seen = set()
        for order_id in order_ids:
            if order_id in seen:
                raise ValueError(f"{order_id}: 重複")
            seen.add(order_id)
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"{order_id}: 搵唔到單")
            if (row["status"] or "active") != "active":
                raise ValueError(f"{order_id}: 已取消")
            if row["settlement_id"] is not None:
                raise ValueError(f"{order_id}: 已經結算咗")
            if not (row["price"] or 0) > 0:
                raise ValueError(f"{order_id}: 未入價")
            if (row["scheduled_time"] or "") >= cutoff:
                raise ValueError(f"{order_id}: 未完成")
            if platform_of(row["service_type"]) != platform:
                raise ValueError(f"{order_id}: 唔屬於呢個平台")
            expected += expected_of(dict(row))
        cur = conn.execute(
            "INSERT INTO settlements (platform, expected_amount, confirmed_amount, settled_on) "
            "VALUES (?, ?, ?, ?)",
            (platform, expected, confirmed_amount, settled_on),
        )
        settlement_id = cur.lastrowid
        conn.execute(
            "UPDATE orders SET settlement_id = ? WHERE order_id IN "
            f"({', '.join('?' * len(order_ids))})",
            [settlement_id, *order_ids],
        )
        conn.commit()
        return settlement_id


def mark_settlement_paid(db_path: str, settlement_id: int, paid_on: str) -> bool:
    """Record the payout date.  False when the id is unknown.

    Re-marking a paid batch keeps the original date: the platform pays once,
    and a second tap (other device, stale page) must not rewrite history.
    """
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT paid_on FROM settlements WHERE id = ?", (settlement_id,)
        ).fetchone()
        if row is None:
            return False
        if row["paid_on"]:
            return True
        conn.execute(
            "UPDATE settlements SET paid_on = ? WHERE id = ?", (paid_on, settlement_id)
        )
        conn.commit()
        return True


def delete_settlement(db_path: str, settlement_id: int) -> bool:
    """Undo a settlement: unlink its orders and drop the batch, or False if unknown."""
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM settlements WHERE id = ?", (settlement_id,)
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE orders SET settlement_id = NULL WHERE settlement_id = ?", (settlement_id,)
        )
        conn.execute("DELETE FROM settlements WHERE id = ?", (settlement_id,))
        conn.commit()
        return True


def _settlement_orders(conn, settlement_ids: list[int]) -> dict[int, list[dict]]:
    if not settlement_ids:
        return {}
    rows = conn.execute(
        f"SELECT {_SETTLE_ORDER_COLS} FROM orders WHERE settlement_id IN "
        f"({', '.join('?' * len(settlement_ids))}) ORDER BY scheduled_time",
        settlement_ids,
    ).fetchall()
    grouped: dict[int, list[dict]] = {sid: [] for sid in settlement_ids}
    for row in rows:
        grouped[row["settlement_id"]].append(dict(row))
    return grouped


def get_settlement(db_path: str, settlement_id: int) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM settlements WHERE id = ?", (settlement_id,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["orders"] = _settlement_orders(conn, [settlement_id])[settlement_id]
        return out


def get_settle_month(db_path: str, month: str, platform: str,
                     now: datetime | None = None) -> dict:
    """Everything the settle page draws for one month of one platform.

    Batches come back whole even when only part of them falls inside the
    month — a batch can straddle months and the day sheet labels it by its
    full date range.  counts and totals deliberately span all time: the point
    of the page is clearing old days, which the month on screen would hide.
    """
    cutoff = _now_str(now)
    with _conn(db_path) as conn:
        month_rows = conn.execute(
            f"SELECT {_SETTLE_ORDER_COLS} FROM orders "
            "WHERE scheduled_time LIKE ? AND coalesce(status,'active') = 'active' "
            "ORDER BY scheduled_time",
            (f"{month}%",),
        ).fetchall()
        orders = [dict(r) for r in month_rows if platform_of(r["service_type"]) == platform]

        settlement_ids = sorted({o["settlement_id"] for o in orders if o["settlement_id"]})
        settlements = []
        if settlement_ids:
            members = _settlement_orders(conn, settlement_ids)
            rows = conn.execute(
                "SELECT * FROM settlements WHERE id IN "
                f"({', '.join('?' * len(settlement_ids))}) ORDER BY id",
                settlement_ids,
            ).fetchall()
            for row in rows:
                batch = dict(row)
                batch["orders"] = members[row["id"]]
                settlements.append(batch)

        counts = {p: 0 for p in PLATFORMS}
        unsettled = 0.0
        for row in conn.execute(
            f"SELECT {_SETTLE_ORDER_COLS} FROM orders WHERE {_SETTLEABLE_SQL}", (cutoff,)
        ):
            p = platform_of(row["service_type"])
            counts[p] += 1
            if p == platform:
                unsettled += expected_of(dict(row))

        awaiting = conn.execute(
            "SELECT coalesce(sum(confirmed_amount), 0) FROM settlements "
            "WHERE platform = ? AND paid_on IS NULL",
            (platform,),
        ).fetchone()[0]

    return {
        "now": cutoff,
        "counts": counts,
        "totals": {"unsettled": unsettled, "awaiting": awaiting},
        "orders": orders,
        "settlements": settlements,
    }
