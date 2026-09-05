import logging
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
from dataclasses import asdict
from datetime import date, datetime
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, jsonify, send_file
from .db import (
    init_db,
    resolve_db_path,
    allocate,
    count_active_orders,
    deallocate,
    delete_settlement,
    diff_order_against_row,
    get_credit,
    get_orders_by_date,
    get_order_by_id,
    get_settle_month,
    get_settlement,
    image_extension,
    list_credits,
    mark_unpaid,
    open_batches,
    save_or_revive_order,
    save_quick_order,
    statements_dir,
    unallocated_credits,
    update_order_fields,
    update_order_from_message,
    update_price,
    DIFF_LABELS,
)
from .credits import CENT, guess_unpaid, offer, propose_batch, propose_credit
from .flight import depart_hhmm, exit_urgency, row_time
from .ingest import parse_any, parking_fee, banner_fee
from .pricing import suggest_price
from .service import PLATFORMS, is_flight_pickup
from . import statement
from . import statement_flow
from .statement import leg_amount

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


@app.get("/api/orders/<order_id>")
def api_order(order_id):
    """One order, whole.  The settle month payload carries settle columns only,
    so its detail sheet reads the order itself from here; a trimmed field list
    would have to grow with every field that sheet learns to show."""
    order = get_order_by_id(DB_PATH, order_id)
    if not order:
        return jsonify({"error": "搵唔到單"}), 404
    return jsonify(order)


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


def _decorate_batch(batch: dict) -> dict:
    """The derived fields the settle page reads a batch by.

    The platform's own figure per order so the page never re-derives it, and
    the guesses at which legs a short payment left out.
    """
    for o in batch["orders"]:
        o["platform_amount"] = leg_amount(batch, o)
    batch["unpaid_guesses"] = guess_unpaid(batch)
    return batch


def _batch_proposals(batch: dict) -> list[dict]:
    """The credits that could pay what a batch is still owed, best first.

    Carried inside the batch rather than fetched per sheet: the whole ledger is
    a few hundred rows a year, so one round trip answers every open batch.
    """
    if batch["outstanding"] <= CENT:
        return []
    m = propose_batch(DB_PATH, batch["id"])
    return [{"id": c["id"], "amount": c["amount"], "value_date": c["value_date"],
             "remaining": c["remaining"], "exact": c["id"] in m.exact}
            for c in offer(m, unallocated_credits(DB_PATH, batch["platform"]))]


def _credit_proposals(credit: dict, platform: str) -> list[dict]:
    """The batches a credit could pay, best first.  The mirror of the above."""
    m = propose_credit(DB_PATH, credit["id"])
    return [{"id": b["id"], "outstanding": b["outstanding"],
             "confirmed_amount": b["confirmed_amount"],
             "dates": sorted({(o["scheduled_time"] or "")[:10] for o in b["orders"]}),
             "orders": len(b["orders"]), "exact": b["id"] in m.exact}
            for b in offer(m, open_batches(DB_PATH, platform))]


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
        _decorate_batch(batch)
        batch["proposals"] = _batch_proposals(batch)
    return jsonify({"month": month, "platform": platform, **data})


# ---- reading a statement (讀結算圖) ----

# A batch is created from a statement image and nowhere else: every batch has
# to trace back to what the platform said, and on to the orders that statement
# lists.  The page can still undo one.

MAX_STATEMENT_BYTES = 10 * 1024 * 1024
PENDING_TTL = 30 * 60

# Statements read but not yet confirmed, by token.  In memory like the bot's
# own pending cards: a restart forgets them and costs one re-upload, and
# create_settlement revalidates every leg, so a stale token can only fail
# rather than write a wrong batch.  SSE responses run on their own threads,
# so the dict is only ever touched under the lock.
_pending_statements: dict[str, tuple[float, statement_flow.Prepared, bytes]] = {}
_pending_lock = threading.Lock()


def _sweep_pending(now: float) -> None:
    for token in [t for t, (at, _p, _i) in _pending_statements.items()
                  if now - at > PENDING_TTL]:
        del _pending_statements[token]


def _upload_meta(filename: str | None, data: bytes) -> str:
    """What a statement read is logged by: enough to find the file again, and
    the decoded size, because a reader failure is usually about the pixels."""
    parts = [f"source=upload name={filename}",
             f"bytes={len(data)} ext={image_extension(data)}"]
    size = statement.image_size(data)
    if size:
        parts.append(f"decoded={size[0]}x{size[1]}")
    return " ".join(parts)


@app.post("/api/statements/read")
def api_read_statement():
    """Read a statement screenshot and say what confirming it would write.

    The browser hands over the platform's own file byte for byte; the same
    screenshot forwarded as a chat photo has been recompressed first, which is
    what the reader mis-reads.  Nothing is written here — the token is the
    promise that a confirm can follow, and a statement no batch can come out
    of gets none.
    """
    upload = request.files.get("file")
    data = upload.read(MAX_STATEMENT_BYTES + 1) if upload is not None else b""
    if not data:
        return jsonify({"error": "冇揀到圖"}), 400
    if len(data) > MAX_STATEMENT_BYTES:
        return jsonify({"error": "張圖大過 10 MB"}), 413
    if not statement.ocr_available():
        return jsonify({"error": "OCR 未裝，讀唔到張圖"}), 503
    meta = _upload_meta(upload.filename, data)
    # ~2 s of CPU; statement._ocr_lock serialises the engine across the
    # threaded server, so concurrent uploads queue rather than collide.
    stmt = statement.read_image(data)
    if not stmt.days:
        logging.getLogger("web").warning("statement unreadable: %s warnings=%s",
                                         meta, stmt.warnings)
        statement_flow.keep_unread_image(DB_PATH, upload.filename or "", data)
        return jsonify({"error": statement_flow.unreadable_text(stmt) + "— 再上載一次"}), 400
    logging.getLogger("web").info("statement read: %d days %d rows · %s", len(stmt.days),
                                  sum(len(d.rows) for d in stmt.days), meta)
    prepared = statement_flow.prepare(DB_PATH, stmt, datetime.now())
    token = None
    if prepared.can_settle:
        token = secrets.token_urlsafe(16)
        now = time.time()
        with _pending_lock:
            _sweep_pending(now)
            _pending_statements[token] = (now, prepared, data)
    archive = prepared.no_orders_credit
    return jsonify({
        "token": token,
        "report": prepared.report,
        "credit_line": prepared.credit_line,
        "confirm_label": prepared.confirm_label,
        "can_settle": prepared.can_settle,
        "no_orders_offer": ({"credit_id": archive["id"],
                             "label": statement_flow.NO_ORDERS_ARCHIVE_LABEL}
                            if archive else None),
    })


@app.post("/api/statements/confirm")
def api_confirm_statement():
    """Write the batch the read statement describes.

    The token is spent on the way in, so a double tap reaches the same 410 an
    expired one does rather than a second batch.
    """
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    with _pending_lock:
        _sweep_pending(time.time())
        entry = _pending_statements.pop(token, None) if isinstance(token, str) else None
    if entry is None:
        return jsonify({"error": "已過期，再上載一次"}), 410
    _read_at, prepared, image = entry
    try:
        done = statement_flow.confirm(DB_PATH, prepared, image, datetime.now())
    except ValueError as e:
        return jsonify({"error": f"結算唔到：{e}"}), 409
    return jsonify({"settlement_id": done.settlement_id, "text": done.text})


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
            c["proposals"] = _credit_proposals(c, platform)
        else:
            c["proposals"] = []
            if c["state"] == "done":
                sums["done"] += c["amount"]
    return jsonify({
        "platform": platform,
        "counts": counts,
        "sums": {k: round(v, 2) for k, v in sums.items()},
        "credits": [{k: c[k] for k in ("id", "ref", "amount", "value_date", "payer", "allocated",
                                       "remaining", "state", "archived_reason", "memo",
                                       "batches", "proposals")} for c in credits],
    })


@app.post("/api/credits/<int:credit_id>/allocate")
def api_allocate_credit(credit_id):
    """Put a credit against a batch from the settle page.

    The amount is not the client's to choose: as much of the batch as the
    credit can still pay, the same default the chat card allocates on.  An
    unknown id is a 404 and every refusal a 400, because the refusals are
    things the operator can act on and a missing row is not.
    """
    body = request.get_json(silent=True) or {}
    settlement_id = body.get("settlement_id")
    if not isinstance(settlement_id, int) or isinstance(settlement_id, bool):
        return jsonify({"error": "settlement_id required"}), 400
    if get_credit(DB_PATH, credit_id) is None:
        return jsonify({"error": "credit not found"}), 404
    if get_settlement(DB_PATH, settlement_id) is None:
        return jsonify({"error": "settlement not found"}), 404
    try:
        batch = allocate(DB_PATH, credit_id, settlement_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _decorate_batch(batch)
    batch["proposals"] = _batch_proposals(batch)
    batch["credit"] = get_credit(DB_PATH, credit_id)
    return jsonify(batch)


@app.delete("/api/settlements/<int:settlement_id>/allocations/<int:credit_id>")
def api_deallocate_credit(settlement_id, credit_id):
    """Take one credit's money back off a batch; the batch and the credit both
    get their figures back from the allocations that are left."""
    if not deallocate(DB_PATH, settlement_id, credit_id):
        return jsonify({"error": "allocation not found"}), 404
    return jsonify({"ok": True})


@app.delete("/api/settlements/<int:settlement_id>")
def api_delete_settlement(settlement_id):
    if not delete_settlement(DB_PATH, settlement_id):
        return jsonify({"error": "settlement not found"}), 404
    return jsonify({"ok": True})


@app.post("/api/settlements/<int:settlement_id>/unpaid")
def api_mark_unpaid(settlement_id):
    """Name the legs a short-paid batch is missing.

    An empty list would add up to $0, which differs from the outstanding by
    definition (the batch is partial), so mark_unpaid already refuses it —
    the marks can be replaced but not cleared while money is owed.
    """
    batch = get_settlement(DB_PATH, settlement_id)
    if batch is None:
        return jsonify({"error": "settlement not found"}), 404
    body = request.get_json(silent=True) or {}
    order_ids = body.get("order_ids", [])
    try:
        batch = mark_unpaid(DB_PATH, settlement_id, order_ids)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(_decorate_batch(batch))


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
        "coalesce(sum(settlement_id),0), count(case when unpaid = 1 then 1 end), "
        "coalesce(group_concat(coalesce(scheduled_time,'') || coalesce(flight_eta,'') || coalesce(flight_gate,'') || coalesce(flight_status,'')),'') FROM orders"
    ).fetchone()
    # max(id) rather than count alone: ids are AUTOINCREMENT, so undoing a batch
    # and settling again is a visible change instead of a wash.
    settle_row = conn.execute(
        "SELECT count(*), coalesce(max(id),0), count(paid_on) FROM settlements"
    ).fetchone()
    # The ledger changes without any order or batch moving — a credit lands, a
    # credit is archived, money is put against a batch — and an open settle
    # page has to repaint for all of them.
    credit_row = conn.execute(
        "SELECT count(*), count(archived_reason) FROM bank_credits"
    ).fetchone()
    alloc_row = conn.execute(
        "SELECT count(*), coalesce(sum(amount),0) FROM credit_allocations"
    ).fetchone()
    conn.close()
    return "-".join(str(v) for v in (*row, *settle_row, *credit_row, *alloc_row))


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
