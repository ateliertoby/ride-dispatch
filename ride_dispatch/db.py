import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from .parser import Order
from .ingest import banner_fee
from .service import PLATFORMS, expected_of, needs_departure_reminder, platform_of
from .statement import leg_amount

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
            # 判罰賠款: the fine the platform charged back, stored positive like
            # the other fee columns and netted off by expected_of.  Nullable
            # with no default, so "never fined" stays distinguishable from
            # "fined nothing" on rows that predate the column.
            "penalty_fee REAL",
            "estimated_landing TEXT",
            "flight_scheduled TEXT",
            "flight_eta TEXT",
            "flight_gate TEXT",
            "flight_status TEXT",
            "flight_hall TEXT",
            "source TEXT DEFAULT ''",
            "reminders_sent TEXT DEFAULT ''",
            "settlement_id INTEGER",
            "unpaid INTEGER DEFAULT 0",
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
                statement TEXT,
                statement_image TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Existing databases predate the statement columns; ALTER is the only
        # migration mechanism this project has (see the orders loop above).
        for col in ["statement TEXT", "statement_image TEXT", "bank_credit_id INTEGER"]:
            try:
                conn.execute(f"ALTER TABLE settlements ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        # One row per bank credit from the feed, whether or not any batch or
        # order exists for it: the ledger must be complete for the backfill to
        # be workable.  ref is the bank's own reference and the identity, so a
        # re-read of the feed collapses onto one row.  What is allocated and
        # what is left are derived from credit_allocations, never stored.
        # archived_reason takes a credit out of the work queue without touching
        # its allocations.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bank_credits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ref TEXT UNIQUE NOT NULL,
                platform TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT DEFAULT 'HKD',
                value_date TEXT NOT NULL,
                payer TEXT,
                memo TEXT,
                email_id TEXT,
                received_at TEXT,
                recorded_at TEXT,
                imported_at TEXT DEFAULT (datetime('now')),
                archived_reason TEXT,
                archived_on TEXT
            )
        """)
        # Money is allocated in amounts, not linked: one payout can pay several
        # batches and one batch can be paid by several payouts, because the
        # platform pays a statement short when it failed to submit some of its
        # own legs and makes up the difference later, alone or inside a bigger
        # transfer.  The pair is unique so a second tap on the same button is
        # refused rather than doubling the money.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS credit_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                credit_id INTEGER NOT NULL,
                settlement_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(credit_id, settlement_id)
            )
        """)
        # settlements.bank_credit_id recorded one credit paying one batch in
        # full, which is an allocation of the batch's own total.  The column
        # stays (SQLite has no cheap drop and this ALTER list is the only
        # migration mechanism) but nothing reads it after this, so the move has
        # to survive being repeated on every start.
        conn.execute(
            "INSERT OR IGNORE INTO credit_allocations (credit_id, settlement_id, amount) "
            "SELECT bank_credit_id, id, coalesce(confirmed_amount, 0) FROM settlements "
            "WHERE bank_credit_id IS NOT NULL"
        )
        conn.execute("UPDATE settlements SET bank_credit_id = NULL WHERE bank_credit_id IS NOT NULL")
        # One row per car park visit. pv_nr is HKIA's own visit number, so a
        # bot restart mid-visit finds the open row again instead of opening
        # a second one. `free` is derived exactly once, at close, from paid
        # and HKIA's last fee reading; storing it keeps the 24h allowance
        # check a single indexed read, and a manual `observed` verdict
        # overwrites it so the allowance follows what the driver saw.
        # last_* hold the most recent reply taken while the car was inside:
        # HKIA's own clock and its price for leaving at that moment.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS parking_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pv_nr INTEGER UNIQUE,
                plate TEXT,
                location TEXT,
                location_name TEXT,
                entry_time TEXT,
                exit_time TEXT,
                paid INTEGER DEFAULT 0,
                paid_amount REAL,
                scheduled_exit TEXT,
                payment_ref TEXT,
                link_sent_at TEXT,
                auto_link_sent INTEGER DEFAULT 0,
                free INTEGER,
                order_id TEXT,
                last_seen_at TEXT,
                last_park_minutes INTEGER,
                last_fee REAL,
                gone_at TEXT,
                observed TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Databases created before the readings were kept; ALTER is the only
        # migration mechanism this project has (see the orders loop above).
        for col in ["last_seen_at TEXT", "last_park_minutes INTEGER", "last_fee REAL",
                    "gone_at TEXT", "observed TEXT"]:
            try:
                conn.execute(f"ALTER TABLE parking_sessions ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_parking_free ON parking_sessions(free, entry_time)")
        conn.commit()


# Every column an entered order writes, in one list: the insert and the revive
# must stay in step, so both are built from it.
_ORDER_COLS = (
    "order_id", "service_type", "vehicle_type", "passenger_name",
    "scheduled_time", "passenger_phone", "overseas_phone", "flight_number",
    "pickup", "dropoff", "distance_km", "notes", "driver_notes",
    "additional_services", "passenger_exit_minutes",
    "third_party_contact", "more_contacts", "raw_message", "telegram_msg_id",
    "parking_fee", "banner_fee", "source",
)

_INSERT_SQL = (
    f"INSERT INTO orders ({', '.join(_ORDER_COLS)}) "
    f"VALUES ({', '.join('?' * len(_ORDER_COLS))})"
)

# Re-entering a cancelled leg overwrites its row instead of adding one: the
# platform re-books under the same order number, and the new message is the
# current truth.  Everything the old booking derived is dropped with it —
# price/tunnel_fee because the re-booked leg may be priced differently and a
# stale price would silently suppress the 未入價 nudge, and the flight and
# reminder state so the tracker re-matches from scratch.  created_at answers
# "when was this active order entered", which is now.  settlement_id is
# necessarily NULL here (a batched order cannot be cancelled, and a cancelled
# one cannot be batched) and is deliberately left alone.
_REVIVE_SQL = (
    f"UPDATE orders SET {', '.join(f'{c} = ?' for c in _ORDER_COLS)}, "
    "status = 'active', price = NULL, tunnel_fee = 0, "
    "flight_scheduled = NULL, flight_eta = NULL, flight_gate = NULL, "
    "flight_status = NULL, flight_hall = NULL, estimated_landing = NULL, "
    "reminders_sent = '', created_at = datetime('now') "
    "WHERE order_id = ? AND status = 'cancelled'"
)


def _order_params(order: Order, telegram_msg_id: int | None, parking: float, source: str) -> tuple:
    return (
        order.order_id, order.service_type, order.vehicle_type,
        order.passenger_name, order.scheduled_time, order.passenger_phone,
        order.overseas_phone, order.flight_number, order.pickup,
        order.dropoff, order.distance_km, order.notes, order.driver_notes,
        order.additional_services, order.passenger_exit_minutes,
        order.third_party_contact, order.more_contacts, order.raw_message,
        telegram_msg_id, parking, banner_fee(order.additional_services), source,
    )


def save_order(db_path: str, order: Order, telegram_msg_id: int, parking: float = 0.0, source: str = "") -> int:
    """Plain insert; any existing row on the order_id raises sqlite3.IntegrityError.

    Order entry goes through save_or_revive_order instead — a cancelled row
    holding the order_id must not block re-entry.
    """
    with _conn(db_path) as conn:
        cur = conn.execute(_INSERT_SQL, _order_params(order, telegram_msg_id, parking, source))
        conn.commit()
        return cur.lastrowid


def save_or_revive_order(db_path: str, order: Order, telegram_msg_id: int | None,
                         parking: float = 0.0, source: str = "") -> bool:
    """Save an entered order; returns True when it revived a cancelled row.

    An active row on the same order_id is still a duplicate and raises
    sqlite3.IntegrityError.  order_id is UNIQUE, so the insert doubles as the
    existence check and the revive's own WHERE decides the outcome: two
    simultaneous re-entries cannot both resurrect the row.
    """
    params = _order_params(order, telegram_msg_id, parking, source)
    with _conn(db_path) as conn:
        try:
            conn.execute(_INSERT_SQL, params)
        except sqlite3.IntegrityError:
            cur = conn.execute(_REVIVE_SQL, (*params, order.order_id))
            if cur.rowcount == 0:
                raise
            conn.commit()
            return True
        conn.commit()
        return False


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


# A platform re-sends the whole order message when the customer changes a
# detail, so the fields the message carries follow the message and the values
# the operator typed in are kept.  parking_fee is therefore absent from the
# overwrite even though entry derives it: it is money the operator can edit
# afterwards (update_cost), same as price.  banner_fee has no such edit worth
# defending — it is a pure function of 附加服务 — so it rides along with the
# rest and is re-derived from the new message.
#
# A re-send does not have to repeat everything the first message carried: the
# contact numbers in particular are often dropped from it.  An empty field
# therefore means "not mentioned", never "cleared" — it is left out of the diff
# and out of the write, so the stored value survives.  raw_message and
# telegram_msg_id are the exceptions and are always written: they describe this
# entry of the order rather than the booking.
_UPDATE_COLS = tuple(c for c in _ORDER_COLS if c != "parking_fee")

_ALWAYS_UPDATE_COLS = ("raw_message", "telegram_msg_id")

_FLIGHT_STATE_COLS = (
    "flight_scheduled", "flight_eta", "flight_gate", "flight_status",
    "flight_hall", "estimated_landing",
)

# The fields worth telling the operator about, in the order they are shown.
# raw_message and telegram_msg_id are deliberately absent: they differ on every
# re-send and would report a change where the booking has none.  banner_fee is
# absent too — it is written, but it moves only with 附加服务, which is listed,
# and diffing it as well would report an operator's waived fee as a change.
DIFF_LABELS = {
    "service_type": "類型",
    "scheduled_time": "時間",
    "passenger_name": "乘客",
    "passenger_phone": "電話",
    "overseas_phone": "境外電話",
    "flight_number": "航班",
    "pickup": "上車",
    "dropoff": "目的地",
    "distance_km": "里程",
    "passenger_exit_minutes": "出場",
    "vehicle_type": "車型",
    "additional_services": "附加服務",
    "driver_notes": "備註",
    "notes": "訂單備註",
    "third_party_contact": "第三方聯絡",
    "more_contacts": "更多聯絡",
    "source": "平台",
}

_NUMERIC_DIFF_FIELDS = {"distance_km", "passenger_exit_minutes"}


def _diff_key(field: str, value):
    """Comparison form of a field: what makes two entries the same booking."""
    if field in _NUMERIC_DIFF_FIELDS:
        return None if value is None or value == "" else float(value)
    # A field the parser leaves empty is stored as "" by one entry path and as
    # NULL by another; neither means the booking changed.
    return (value or "").strip()


def _diff_text(field: str, value) -> str:
    """Display form of a field; "" when there is nothing to show."""
    if value is None or value == "":
        return ""
    if field == "distance_km":
        return f"{float(value):g} km"
    if field == "passenger_exit_minutes":
        return f"{int(value)}分鐘"
    return str(value).strip()


def _diff_absent(key) -> bool:
    """Whether a normalised value (see _diff_key) says nothing at all."""
    return key is None or key == ""


def _message_carries(field: str, values: dict) -> bool:
    """Whether a re-sent message says anything about one column.

    banner_fee is decided by 附加服务, the field it is derived from: a message
    silent about the service must not zero the fee derived from the service
    still stored.
    """
    if field in _ALWAYS_UPDATE_COLS:
        return True
    if field == "banner_fee":
        field = "additional_services"
    return not _diff_absent(_diff_key(field, values[field]))


def diff_order_against_row(order: Order, row, source: str = "") -> list[tuple[str, str, str]]:
    """What a re-sent message changes on the row already holding its order_id.

    Returns (field, old_display, new_display) for every changed field, in
    DIFF_LABELS order.  Bot and web both ask this so the two can never disagree
    about what "changed" means.
    """
    changes = []
    for field in DIFF_LABELS:
        new = source if field == "source" else getattr(order, field)
        new_key = _diff_key(field, new)
        # Silence is not an instruction to erase: a re-send that omits the
        # field leaves whatever is stored in place, so there is nothing to
        # report and nothing to write.
        if _diff_absent(new_key):
            continue
        old = row[field]
        if _diff_key(field, old) != new_key:
            changes.append((field, _diff_text(field, old), _diff_text(field, new)))
    return changes


def update_order_from_message(db_path: str, order: Order, telegram_msg_id: int | None,
                              source: str = "") -> list[str] | None:
    """Apply a re-sent message to the active row holding its order_id.

    Returns the changed field names, [] when the message says nothing new (and
    then nothing is written), or None when no active row holds the order_id any
    more.  Raises ValueError(SETTLED_LOCK_MSG) for a batched row: its fields
    were summed into a frozen expected_amount.
    """
    values = dict(zip(_ORDER_COLS, _order_params(order, telegram_msg_id, 0.0, source)))
    # The parking fee handed to _order_params above is a placeholder: this call
    # does not own that column.  Dropping it turns a future attempt to write it
    # from here into a KeyError instead of a silent zero.
    del values["parking_fee"]
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id = ? AND coalesce(status,'active') = 'active'",
            (order.order_id,),
        ).fetchone()
        if row is None:
            return None
        _assert_unbatched(conn, order.order_id)
        changed = [f for f, _, _ in diff_order_against_row(order, row, source)]
        if not changed:
            return []
        cols = [c for c in _UPDATE_COLS if _message_carries(c, values)]
        sets = [f"{c} = ?" for c in cols]
        params = [values[c] for c in cols]
        # An already-matched tracker is worth more than a clean slate, so the
        # flight state is dropped only when the message moved the flight or the
        # pickup time — the two inputs the match is made from.
        if {"flight_number", "scheduled_time"} & set(changed):
            sets += [f"{c} = NULL" for c in _FLIGHT_STATE_COLS] + ["reminders_sent = ''"]
        # The status/settlement guard is repeated in the WHERE so a row
        # cancelled or batched since the SELECT cannot be written behind
        # the other writer's back.
        cur = conn.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE order_id = ? "
            "AND coalesce(status,'active') = 'active' AND settlement_id IS NULL",
            (*params, order.order_id),
        )
        if cur.rowcount == 0:
            return None
        conn.commit()
        return changed


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


def order_status(db_path: str, order_id: str) -> str | None:
    """Stored status of the row holding this order_id, or None if no row does."""
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT coalesce(status,'active') AS s FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        return row["s"] if row else None


# A cancelled row keeps the order_id but no longer blocks entry (see
# save_or_revive_order), so only a live row counts as a duplicate.
def active_order_id_exists(db_path: str, order_id: str) -> bool:
    return order_status(db_path, order_id) == "active"


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
# price/banner_fee/tunnel_fee/penalty_fee, so all four travel with every row.
_SETTLE_ORDER_COLS = (
    "order_id, scheduled_time, service_type, flight_number, "
    "pickup, dropoff, price, banner_fee, tunnel_fee, penalty_fee, settlement_id, "
    "coalesce(unpaid, 0) AS unpaid"
)

# Only a finished, priced, not-yet-batched leg can enter a batch.  The clock is
# a parameter so the settle page and the server agree on which legs are done.
_SETTLEABLE_SQL = (
    "coalesce(status,'active') = 'active' AND settlement_id IS NULL "
    "AND coalesce(price,0) > 0 AND scheduled_time < ?"
)


def _now_str(now: datetime | None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def statements_dir(db_path: str) -> str:
    """Where statement screenshots live: a sibling of the database file."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "statements")


def image_extension(data: bytes) -> str:
    """The screenshot's type, from its magic bytes.

    Telegram re-encodes a photo to JPEG but hands a file back untouched, so a
    screenshot sent as a file arrives as whatever the phone made — PNG or HEIC
    on iOS.  The extension is what the dashboard serves the Content-Type from,
    so it has to describe the bytes rather than assume them.
    """
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"\x89PNG":
        return "png"
    header = data[4:12]
    if header[:4] == b"ftyp" and header[4:] in (b"heic", b"heix", b"mif1"):
        return "heic"
    return "bin"


def statement_image_path(db_path: str, settlement_id: int, ext: str = "jpg") -> str:
    return os.path.join(statements_dir(db_path), f"{settlement_id}.{ext}")


def create_settlement(db_path: str, platform: str, order_ids: list[str],
                      confirmed_amount: float, settled_on: str,
                      now: datetime | None = None,
                      statement: dict | None = None, image: bytes | None = None,
                      penalties: dict[str, float] | None = None) -> int:
    """Batch one platform's settleable orders; returns the settlement id.

    All-or-nothing: any order that is missing, cancelled, unpriced, still in
    the future, already batched, or belonging to another platform aborts the
    call with ValueError naming it, and nothing is written.  expected_amount is
    summed from the stored rows rather than taken from the caller, so a stale
    client cannot disagree with the DB about what is owed.

    `penalties` are 判罰賠款 amounts (positive) by order id, each of which must
    be in `order_ids`.  They are added to the order's own penalty_fee inside
    this transaction and BEFORE the sum is taken, because a fine is a cost of
    its order and the frozen expected_amount has to be net of it.  Sharing the
    transaction is also what keeps a fine from ever being recorded against an
    order whose batch failed to be created.

    `statement` is what the platform's statement said (stored as JSON, shown
    beside the system's numbers in batch detail); `image` is the screenshot
    it was read from.  The screenshot is written after the commit: a failed
    file write must not undo a batch the operator has just confirmed, so it
    only leaves statement_image NULL.
    """
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform: {platform}")
    if not order_ids:
        raise ValueError("order_ids required")
    penalties = penalties or {}
    cutoff = _now_str(now)
    with _conn(db_path) as conn:
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
        for order_id in penalties:
            if order_id not in seen:
                raise ValueError(f"{order_id}: 判罰唔喺呢個 batch 入面")
        for order_id, amount in penalties.items():
            conn.execute(
                "UPDATE orders SET penalty_fee = coalesce(penalty_fee, 0) + ? WHERE order_id = ?",
                (amount, order_id),
            )
        # A second read rather than a running total in the loop above: the sum
        # has to see the penalties just written, and reading it back from the
        # rows keeps the one definition of what an order is worth.
        expected = 0.0
        for order_id in order_ids:
            row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            expected += expected_of(dict(row))
        cur = conn.execute(
            "INSERT INTO settlements (platform, expected_amount, confirmed_amount, settled_on, statement) "
            "VALUES (?, ?, ?, ?, ?)",
            (platform, expected, confirmed_amount, settled_on,
             json.dumps(statement, ensure_ascii=False) if statement is not None else None),
        )
        settlement_id = cur.lastrowid
        conn.execute(
            "UPDATE orders SET settlement_id = ? WHERE order_id IN "
            f"({', '.join('?' * len(order_ids))})",
            [settlement_id, *order_ids],
        )
        conn.commit()
        if image is not None:
            path = statement_image_path(db_path, settlement_id, image_extension(image))
            try:
                os.makedirs(statements_dir(db_path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(image)
                conn.execute("UPDATE settlements SET statement_image = ? WHERE id = ?",
                             (os.path.basename(path), settlement_id))
                conn.commit()
            except (OSError, sqlite3.Error):
                logging.getLogger("db").exception("statement image not stored for batch %s", settlement_id)
        return settlement_id


def delete_settlement(db_path: str, settlement_id: int) -> bool:
    """Undo a settlement: unlink its orders and drop the batch, or False if unknown.

    The money follows the batch out: its allocations go, so every credit that
    paid it gets its remainder back, and the legs the platform said it had not
    paid stop being owed by a batch that no longer exists.
    """
    with _conn(db_path) as conn:
        # The file name has to be read before the row goes: it is the only
        # record of which extension the screenshot was stored under.
        row = conn.execute(
            "SELECT statement_image FROM settlements WHERE id = ?", (settlement_id,)
        ).fetchone()
        if row is None:
            return False
        image_name = row["statement_image"]
        conn.execute(
            "UPDATE orders SET settlement_id = NULL, unpaid = 0 WHERE settlement_id = ?",
            (settlement_id,),
        )
        conn.execute("DELETE FROM credit_allocations WHERE settlement_id = ?", (settlement_id,))
        conn.execute("DELETE FROM settlements WHERE id = ?", (settlement_id,))
        conn.commit()
        # The batch rows are already gone, so a file that will not go away must
        # not fail the call: the caller has nothing left to retry.
        if image_name:
            try:
                os.remove(os.path.join(statements_dir(db_path), image_name))
            except FileNotFoundError:
                pass
            except OSError:
                logging.getLogger("db").warning(
                    "statement image not removed for batch %s", settlement_id, exc_info=True)
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


def _settlement_dict(row) -> dict:
    """Row → dict with the statement JSON decoded (None when the batch has none)."""
    out = dict(row)
    raw = out.get("statement")
    out["statement"] = json.loads(raw) if raw else None
    return out


def _batch_allocations(conn, settlement_ids: list[int]) -> dict[int, list[dict]]:
    """Each batch's allocations, oldest first, carrying the bank's value date."""
    if not settlement_ids:
        return {}
    rows = conn.execute(
        "SELECT a.settlement_id, a.credit_id, a.amount, c.value_date "
        "FROM credit_allocations a JOIN bank_credits c ON c.id = a.credit_id "
        f"WHERE a.settlement_id IN ({', '.join('?' * len(settlement_ids))}) ORDER BY a.id",
        settlement_ids,
    ).fetchall()
    grouped: dict[int, list[dict]] = {sid: [] for sid in settlement_ids}
    for row in rows:
        grouped[row["settlement_id"]].append(
            {"credit_id": row["credit_id"], "amount": row["amount"], "value_date": row["value_date"]})
    return grouped


def _derive_batch(batch: dict, allocations: list[dict]) -> dict:
    """Add what the bank has paid this batch and what it still owes.

    Never stored: the allocations are the record, so undoing one gives both
    sides their money back without a second write that could disagree.  A batch
    is 部分 while some of its money has arrived and some has not — the platform
    pays a statement short when it failed to submit legs of its own.
    """
    batch["allocations"] = allocations
    received = round(sum(a["amount"] for a in allocations), 2)
    batch["received"] = received
    batch["outstanding"] = round((batch["confirmed_amount"] or 0.0) - received, 2)
    if batch["outstanding"] <= CENT:
        batch["state"] = "paid"
    elif received > CENT:
        batch["state"] = "partial"
    else:
        batch["state"] = "awaiting"
    return batch


def get_settlement(db_path: str, settlement_id: int) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM settlements WHERE id = ?", (settlement_id,)
        ).fetchone()
        if row is None:
            return None
        out = _settlement_dict(row)
        out["orders"] = _settlement_orders(conn, [settlement_id])[settlement_id]
        return _derive_batch(out, _batch_allocations(conn, [settlement_id])[settlement_id])


# ---- bank credits (入數) ----

# What a credit has paid out is derived, never stored: the allocations are the
# record, so taking one back or undoing a batch entirely gives the credit its
# money back without a second write that could disagree.
_CREDIT_SQL = (
    "SELECT c.*, coalesce((SELECT sum(a.amount) FROM credit_allocations a "
    "WHERE a.credit_id = c.id), 0) AS allocated FROM bank_credits c"
)

_CREDIT_COLS = (
    "ref", "platform", "amount", "currency", "value_date", "payer", "memo",
    "email_id", "received_at", "recorded_at",
)

# Money is compared to the cent everywhere: a difference above this is a
# question for the operator, never a rounding artefact to absorb.
CENT = 0.005


def _credit_dict(conn, row) -> dict:
    out = dict(row)
    out["allocated"] = round(out["allocated"], 2)
    out["remaining"] = round(out["amount"] - out["allocated"], 2)
    out["allocations"] = [{"settlement_id": r["settlement_id"], "amount": r["amount"]}
                          for r in conn.execute(
        "SELECT settlement_id, amount FROM credit_allocations WHERE credit_id = ? ORDER BY id",
        (out["id"],))]
    return out


def insert_credit(db_path: str, credit: dict) -> int | None:
    """Record one bank credit; returns its id, or None when the ref is known.

    The bank's reference is the identity, so re-reading the feed, a producer
    re-run and a backfill all land on the row that is already there.
    """
    with _conn(db_path) as conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO bank_credits ({', '.join(_CREDIT_COLS)}) "
            f"VALUES ({', '.join('?' * len(_CREDIT_COLS))})",
            [credit.get(c) for c in _CREDIT_COLS],
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount else None


def get_credit(db_path: str, credit_id: int) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(f"{_CREDIT_SQL} WHERE c.id = ?", (credit_id,)).fetchone()
        return _credit_dict(conn, row) if row else None


def unallocated_credits(db_path: str, platform: str | None = None) -> list[dict]:
    """Credits still owing an allocation, oldest value date first.

    remaining is derived, so the filter cannot live in SQL: the rows are read
    and then filtered.  Volume is a few hundred a year.
    """
    with _conn(db_path) as conn:
        rows = conn.execute(
            f"{_CREDIT_SQL} WHERE c.archived_reason IS NULL "
            "AND (? IS NULL OR c.platform = ?) ORDER BY c.value_date, c.id",
            (platform, platform),
        ).fetchall()
        credits = [_credit_dict(conn, r) for r in rows]
        return [c for c in credits if c["remaining"] > CENT]


def open_batches(db_path: str, platform: str) -> list[dict]:
    """Batches the bank still owes money on, oldest first, each with its orders.

    A part-paid batch stays here: the rest of its money is exactly what the
    ledger is for, and the platform pays it later, alone or inside a bigger
    transfer.
    """
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM settlements WHERE platform = ? ORDER BY id", (platform,)
        ).fetchall()
        batches = [_settlement_dict(r) for r in rows]
        ids = [b["id"] for b in batches]
        members = _settlement_orders(conn, ids)
        allocations = _batch_allocations(conn, ids)
        for b in batches:
            b["orders"] = members[b["id"]]
            _derive_batch(b, allocations[b["id"]])
        return [b for b in batches if b["outstanding"] > CENT]


def _load_batch(conn, settlement_id: int) -> dict:
    """The batch with its orders and its derived figures, inside a transaction."""
    row = conn.execute("SELECT * FROM settlements WHERE id = ?", (settlement_id,)).fetchone()
    if row is None:
        raise ValueError("搵唔到批次")
    batch = _settlement_dict(row)
    batch["orders"] = _settlement_orders(conn, [settlement_id])[settlement_id]
    return _derive_batch(batch, _batch_allocations(conn, [settlement_id])[settlement_id])


def allocate(db_path: str, credit_id: int, settlement_id: int,
             amount: float | None = None) -> dict:
    """Put part or all of a credit against a batch; returns the batch afterwards.

    Default amount is as much of the batch as the credit can still pay, which
    is the whole of it in the ordinary case and the shortfall when a make-up
    payment lands.  Every refusal raises rather than returning False: each one
    is a different thing for the operator to do, so the reason has to reach
    them.  Checks and write share one connection, so money allocated in between
    cannot be allocated twice.

    paid_on is written here and nowhere else, at the moment the batch is whole,
    and it is the bank's own value date rather than when anybody noticed.  The
    unpaid flags are left alone: they name the legs the platform held back on
    this statement, which stays true once the make-up payment lands, and are
    what lets the day sheet say which legs that last allocation paid for.
    """
    with _conn(db_path) as conn:
        row = conn.execute(f"{_CREDIT_SQL} WHERE c.id = ?", (credit_id,)).fetchone()
        if row is None:
            raise ValueError("搵唔到入數")
        credit = _credit_dict(conn, row)
        if credit["archived_reason"]:
            raise ValueError("入數已收埋")
        batch = _load_batch(conn, settlement_id)
        if batch["platform"] != credit["platform"]:
            raise ValueError("唔同平台")
        if any(a["credit_id"] == credit_id for a in batch["allocations"]):
            raise ValueError("已經對過呢筆入數")
        outstanding = batch["outstanding"]
        if outstanding <= CENT:
            raise ValueError("批次已收齊")
        remaining = credit["remaining"]
        if amount is None:
            amount = min(remaining, outstanding)
        # A non-positive amount can only mean the credit has nothing left to
        # give, which is the same refusal as trying to give more than it has.
        if amount <= CENT or amount > remaining + CENT:
            raise ValueError(f"入數剩 ${remaining:g} 唔夠")
        if amount > outstanding + CENT:
            raise ValueError(f"批次淨係差 ${outstanding:g}")
        conn.execute(
            "INSERT INTO credit_allocations (credit_id, settlement_id, amount) VALUES (?, ?, ?)",
            (credit_id, settlement_id, round(amount, 2)),
        )
        if round(outstanding - amount, 2) <= CENT:
            conn.execute("UPDATE settlements SET paid_on = ? WHERE id = ?",
                         (credit["value_date"], settlement_id))
        conn.commit()
        return _load_batch(conn, settlement_id)


def deallocate(db_path: str, settlement_id: int, credit_id: int | None = None) -> int:
    """Take money back off a batch; returns how many allocations were removed.

    paid_on goes with the money: it means "the bank paid all of this", so a
    batch that is owed anything again must not keep a date.  The unpaid flags
    stay — which legs the platform failed to submit is a fact about the
    statement, not about who paid for it.
    """
    with _conn(db_path) as conn:
        sql = "DELETE FROM credit_allocations WHERE settlement_id = ?"
        params: list = [settlement_id]
        if credit_id is not None:
            sql += " AND credit_id = ?"
            params.append(credit_id)
        cur = conn.execute(sql, params)
        if cur.rowcount:
            conn.execute("UPDATE settlements SET paid_on = NULL WHERE id = ?", (settlement_id,))
        conn.commit()
        return cur.rowcount


def mark_unpaid(db_path: str, settlement_id: int, order_ids: list[str]) -> dict:
    """Name the legs the platform said it has not paid; returns the batch.

    Replaces the whole set rather than adding to it, so a mis-tick is corrected
    by ticking again.  The ticks are accepted only when they account for the
    shortfall to the cent: the platform said which legs it failed to submit and
    the money says how much they were worth, so a set that does not add up
    means one of the two was misread.
    """
    with _conn(db_path) as conn:
        batch = _load_batch(conn, settlement_id)
        if batch["state"] == "paid":
            raise ValueError("批次已收齊")
        if batch["state"] == "awaiting":
            raise ValueError("批次未收過錢")
        by_id = {o["order_id"]: o for o in batch["orders"]}
        total = 0.0
        for order_id in order_ids:
            if order_id not in by_id:
                raise ValueError(f"{order_id}: 唔喺呢個批次")
            # The platform's figure for the leg, not the system's: the
            # shortfall is money the platform did not send, so it is measured
            # in the amounts the platform itself printed.
            total += leg_amount(batch, by_id[order_id])
        total = round(total, 2)
        if abs(total - batch["outstanding"]) > CENT:
            raise ValueError(f"剔咗 ${total:g}，差額係 ${batch['outstanding']:g}")
        conn.execute("UPDATE orders SET unpaid = 0 WHERE settlement_id = ?", (settlement_id,))
        if order_ids:
            conn.execute(
                f"UPDATE orders SET unpaid = 1 WHERE order_id IN ({', '.join('?' * len(order_ids))})",
                order_ids,
            )
        conn.commit()
        return _load_batch(conn, settlement_id)


def archive_credit(db_path: str, credit_id: int, reason: str, on: str) -> bool:
    """Take a credit out of the work queue.  Allocations are deliberately untouched."""
    with _conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE bank_credits SET archived_reason = ?, archived_on = ? WHERE id = ?",
            (reason, on, credit_id),
        )
        conn.commit()
        return cur.rowcount > 0


def unarchive_credit(db_path: str, credit_id: int) -> bool:
    with _conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE bank_credits SET archived_reason = NULL, archived_on = NULL WHERE id = ?",
            (credit_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def archive_credits_before(db_path: str, date: str, reason: str, on: str) -> list[dict]:
    """Archive every unallocated credit whose value date precedes `date`.

    Returns the credits as they were before archiving, which is what the reply
    to the operator is built from.
    """
    done = [c for c in unallocated_credits(db_path) if c["value_date"] < date]
    for c in done:
        archive_credit(db_path, c["id"], reason, on)
    return done


def list_credits(db_path: str, platform: str) -> list[dict]:
    """The whole ledger for one platform, oldest value date first.

    Every credit, not only the queue and not only a month: the page's question
    is how much money came in and how much of it a statement accounts for, and
    an answer scoped to a month would leave the oldest ones invisible — which
    is the reason they are still unaccounted for.
    """
    with _conn(db_path) as conn:
        rows = conn.execute(
            f"{_CREDIT_SQL} WHERE c.platform = ? ORDER BY c.value_date, c.id", (platform,)
        ).fetchall()
        credits = []
        for row in rows:
            c = dict(row)
            c["allocated"] = round(c["allocated"], 2)
            c["remaining"] = round(c["amount"] - c["allocated"], 2)
            if c["archived_reason"]:
                c["state"] = "archived"
            elif c["remaining"] <= CENT:
                c["state"] = "done"
            elif c["allocated"] > CENT:
                c["state"] = "partial"
            else:
                c["state"] = "open"
            c["batches"] = []
            credits.append(c)
        by_credit = {c["id"]: c for c in credits}
        batch_rows = conn.execute(
            "SELECT id, confirmed_amount, statement_image FROM settlements "
            "WHERE platform = ? ORDER BY id", (platform,)
        ).fetchall()
        batches = {r["id"]: r for r in batch_rows}
        dates: dict[int, set] = {sid: set() for sid in batches}
        counts: dict[int, int] = {sid: 0 for sid in batches}
        received: dict[int, float] = {sid: 0.0 for sid in batches}
        if batches:
            for r in conn.execute(
                "SELECT settlement_id, scheduled_time FROM orders WHERE settlement_id IN "
                f"({', '.join('?' * len(batches))})", list(batches)
            ):
                dates[r["settlement_id"]].add((r["scheduled_time"] or "")[:10])
                counts[r["settlement_id"]] += 1
            for r in conn.execute(
                "SELECT settlement_id, credit_id, amount FROM credit_allocations "
                f"WHERE settlement_id IN ({', '.join('?' * len(batches))}) ORDER BY id",
                list(batches)
            ):
                received[r["settlement_id"]] = round(received[r["settlement_id"]] + r["amount"], 2)
                # allocate refuses a batch and a credit of different platforms,
                # so both sides of this join are already scoped to one platform.
                owner = by_credit.get(r["credit_id"])
                if owner is None:
                    continue
                sid = r["settlement_id"]
                owner["batches"].append({
                    "id": sid, "confirmed_amount": batches[sid]["confirmed_amount"],
                    "amount": r["amount"],
                    "dates": sorted(d for d in dates[sid] if d), "orders": counts[sid],
                    "has_image": bool(batches[sid]["statement_image"]),
                })
        # The batch's own state, not the credit's: a credit can be spent while
        # the batch it paid part of is still owed money.
        for c in credits:
            for b in c["batches"]:
                total = b["confirmed_amount"] or 0.0
                got = received[b["id"]]
                b["outstanding"] = round(max(total - got, 0.0), 2)
                b["state"] = ("paid" if total - got <= CENT else
                              "partial" if got > CENT else "awaiting")
        return credits


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
            allocations = _batch_allocations(conn, settlement_ids)
            rows = conn.execute(
                "SELECT * FROM settlements WHERE id IN "
                f"({', '.join('?' * len(settlement_ids))}) ORDER BY id",
                settlement_ids,
            ).fetchall()
            for row in rows:
                batch = _settlement_dict(row)
                batch["orders"] = members[row["id"]]
                settlements.append(_derive_batch(batch, allocations[row["id"]]))

        counts = {p: 0 for p in PLATFORMS}
        unsettled = 0.0
        for row in conn.execute(
            f"SELECT {_SETTLE_ORDER_COLS} FROM orders WHERE {_SETTLEABLE_SQL}", (cutoff,)
        ):
            p = platform_of(row["service_type"])
            counts[p] += 1
            if p == platform:
                unsettled += expected_of(dict(row))

        # Waiting for money is what a batch is still owed, not what it is
        # worth: a batch paid short contributes only its shortfall.  paid_on is
        # written from a completed allocation and would agree, but only one of
        # the two can be the definition, and the allocations are it.
        awaiting = 0.0
        for row in conn.execute(
            "SELECT s.confirmed_amount - coalesce(sum(a.amount), 0) AS outstanding "
            "FROM settlements s LEFT JOIN credit_allocations a ON a.settlement_id = s.id "
            "WHERE s.platform = ? GROUP BY s.id",
            (platform,),
        ):
            if row["outstanding"] > CENT:
                awaiting += row["outstanding"]
        awaiting = round(awaiting, 2)

    unallocated = unallocated_credits(db_path, platform)

    return {
        "now": cutoff,
        "counts": counts,
        "totals": {"unsettled": unsettled, "awaiting": awaiting},
        "credits": {"unallocated": len(unallocated),
                    "unallocated_sum": round(sum(c["remaining"] for c in unallocated), 2)},
        "orders": orders,
        "settlements": settlements,
    }


def settlement_candidates(db_path: str, dates: list[str], now: datetime | None = None) -> list[dict]:
    """Ride-platform orders a statement listing `dates` could refer to.

    Every active or cancelled ride order scheduled within a day of one of the
    dates, plus every settleable ride order of any date: a leg the platform
    held back reappears under its own service date on a later statement, so
    it is normally in the window already, and the tail is a cheap safety net.
    Cancelled rows are included on purpose — a statement that pays for a
    cancelled trip is exactly the kind of thing reconciliation must surface.
    """
    cutoff = _now_str(now)
    days: set[str] = set()
    for d in dates:
        try:
            base = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            # Dates arrive from an OCR reader that matches on shape, so a
            # mis-read digit can yield a well-formed but impossible date.  It
            # costs that one day's window; the rest of the statement, and the
            # settleable tail, still reconcile.
            logging.getLogger("db").warning("statement date not usable: %s", d)
            continue
        for delta in (-1, 0, 1):
            days.add((base + timedelta(days=delta)).strftime("%Y-%m-%d"))
    cols = f"{_SETTLE_ORDER_COLS}, coalesce(status,'active') AS status"
    with _conn(db_path) as conn:
        by_id: dict[str, dict] = {}
        if days:
            placeholders = " OR ".join("scheduled_time LIKE ?" for _ in days)
            for row in conn.execute(
                f"SELECT {cols} FROM orders WHERE ({placeholders}) ORDER BY scheduled_time",
                [f"{d}%" for d in sorted(days)],
            ):
                if platform_of(row["service_type"]) == "ride":
                    by_id[row["order_id"]] = dict(row)
        for row in conn.execute(
            f"SELECT {cols} FROM orders WHERE {_SETTLEABLE_SQL} ORDER BY scheduled_time", (cutoff,)
        ):
            if platform_of(row["service_type"]) == "ride" and row["order_id"] not in by_id:
                by_id[row["order_id"]] = dict(row)
        return sorted(by_id.values(), key=lambda r: r["scheduled_time"] or "")


def get_settleable_recent(db_path: str, days: int, now: datetime | None = None) -> list[dict]:
    """Settleable ride orders scheduled in the last `days` days, oldest first."""
    cutoff = _now_str(now)
    since = ((now or datetime.now()) - timedelta(days=days)).strftime("%Y-%m-%d")
    with _conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_SETTLE_ORDER_COLS} FROM orders WHERE {_SETTLEABLE_SQL} AND scheduled_time >= ? "
            "ORDER BY scheduled_time",
            (cutoff, since),
        ).fetchall()
        return [dict(r) for r in rows if platform_of(r["service_type"]) == "ride"]


# --- car park visits ---

PARKING_UPDATABLE = {"paid", "paid_amount", "scheduled_exit", "payment_ref",
                     "link_sent_at", "auto_link_sent", "order_id",
                     "last_seen_at", "last_park_minutes", "last_fee"}


def open_parking_session(db_path: str, *, pv_nr: int, plate: str, location: str | None,
                         location_name: str | None, entry_time: str, order_id: str | None) -> int:
    with _conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO parking_sessions (pv_nr, plate, location, location_name, entry_time, order_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pv_nr, plate, location, location_name, entry_time, order_id),
        )
        conn.commit()
        return cur.lastrowid


def get_open_parking_session(db_path: str) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM parking_sessions WHERE exit_time IS NULL ORDER BY entry_time DESC, id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_parking_session(db_path: str, session_id: int) -> dict | None:
    with _conn(db_path) as conn:
        row = conn.execute("SELECT * FROM parking_sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None


def update_parking_session(db_path: str, session_id: int, **fields):
    bad = set(fields) - PARKING_UPDATABLE
    if bad:
        raise ValueError(f"not updatable: {sorted(bad)}")
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with _conn(db_path) as conn:
        conn.execute(
            f"UPDATE parking_sessions SET {assignments} WHERE id = ?",
            (*fields.values(), session_id),
        )
        conn.commit()


def close_parking_session(db_path: str, session_id: int, exit_time: str, free: int,
                          gone_at: str | None = None):
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE parking_sessions SET exit_time = ?, free = ?, gone_at = ? WHERE id = ?",
            (exit_time, free, gone_at, session_id),
        )
        conn.commit()


def mark_parking_observed(db_path: str, session_id: int, observed: str) -> bool:
    """Record what the driver actually saw at the gate, overriding the guess.

    The 24h free allowance is read off `free`, so an observation that
    contradicts the automatic verdict has to move that column with it or the
    next visit is told the wrong thing.
    """
    with _conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE parking_sessions SET observed = ?, free = ? WHERE id = ?",
            (observed, 1 if observed == "free" else 0, session_id),
        )
        conn.commit()
        return cur.rowcount > 0


def recent_parking_sessions(db_path: str, limit: int = 5) -> list[dict]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM parking_sessions ORDER BY entry_time DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def free_parking_entries_since(db_path: str, cutoff: str) -> list[str]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT entry_time FROM parking_sessions WHERE free = 1 AND entry_time > ? ORDER BY entry_time",
            (cutoff,),
        ).fetchall()
        return [r["entry_time"] for r in rows]
