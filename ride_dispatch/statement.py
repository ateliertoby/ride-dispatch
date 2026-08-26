"""Platform settlement statements (結算單): reading the screenshot and
reconciling it against the orders in the database.

Two layers that never touch each other's concerns:

- the reader turns an image into a `Statement` — what the platform says,
  line by line, with its own subtotals.  It knows nothing about orders.
- `reconcile` turns a `Statement` plus candidate orders into a verdict.
  It is a pure function: no I/O, no Telegram, no SQLite, so every branch
  is unit-testable and the reader can be swapped without touching it.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta

from .service import expected_of


# ---- what the platform said ----

@dataclass
class StatementRow:
    date: str                 # service date (from the day header), YYYY-MM-DD
    order_id: str             # as read; a 舉牌 line repeats its trip's id
    amount: float             # 司機應結算金額
    time: str | None = None   # 用車時間, HH:MM
    settle_date: str | None = None  # 應結算日期, informational only


_ROW_KEYS = ("date", "order_id", "amount", "time", "settle_date")


@dataclass
class StatementDay:
    date: str
    rows: list[StatementRow]
    count: int | None = None  # 记录数, None when unreadable
    sum: float | None = None  # 求和 of 司機應結算金額, None when unreadable


@dataclass
class Statement:
    days: list[StatementDay]
    account: str | None = None   # the code inside 【…】; the name beside it is not kept
    total: float | None = None   # grand 求和
    reader: str = ""
    warnings: list[str] = field(default_factory=list)  # transient, not stored

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("warnings")
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Statement":
        days = [StatementDay(date=x["date"], count=x.get("count"), sum=x.get("sum"),
                             rows=[StatementRow(**{k: v for k, v in r.items() if k in _ROW_KEYS})
                                   for r in x.get("rows", [])])
                for x in d.get("days", [])]
        return cls(days=days, account=d.get("account"), total=d.get("total"), reader=d.get("reader", ""))


def dates_of(stmt: Statement) -> list[str]:
    return sorted({d.date for d in stmt.days})


# ---- the verdict ----

@dataclass
class Entry:
    """One order as the statement sees it (舉牌 lines already folded in)."""
    kind: str
    statement_id: str           # id as read off the statement
    order_id: str               # corrected id when matched, else statement_id
    date: str
    platform_amount: float
    expected: float | None      # expected_of(order) when an order was found
    order: dict | None
    settlement_id: int | None = None
    fuzzy: bool = False
    reason: str | None = None   # for not_ready: 未入價 / 未完成


@dataclass
class Reconciliation:
    checksum: str               # ok | fail | unverified
    checksum_notes: list[str]
    entries: list[Entry]
    missing: list[dict]         # settleable orders on the statement's dates that it does not list
    settle_ids: list[str]
    expected: float
    confirmed: float | None
    days: list[StatementDay]
    account: str | None

    @property
    def diff(self) -> float:
        return round((self.confirmed or 0.0) - self.expected, 2)

    @property
    def can_settle(self) -> bool:
        return self.checksum == "ok" and bool(self.settle_ids)

    @property
    def clean(self) -> bool:
        return self.can_settle and not self.missing and all(e.kind == "matched" for e in self.entries)


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _same(a: float, b: float) -> bool:
    return abs(a - b) < 0.005


def _md(date: str) -> str:
    # Only ever called to build operator-facing text, including the text that
    # reports an unreadable statement, so a malformed date degrades to itself
    # rather than raising over the error it was about to describe.
    try:
        return f"{int(date[5:7])}月{int(date[8:10])}日"
    except ValueError:
        return date


def _checksum(stmt: Statement) -> tuple[str, list[str]]:
    """Does the statement agree with itself?  The platform prints its own
    subtotals, so a reader that mis-read a digit is caught here rather than
    reported as a discrepancy with the operator's records."""
    notes: list[str] = []
    verifiable = False
    day_figures: list[float] = []
    for day in stmt.days:
        rows_sum = round(sum(r.amount for r in day.rows), 2)
        if day.sum is not None:
            verifiable = True
            if not _same(rows_sum, day.sum):
                notes.append(f"{_md(day.date)} 行加埋 ${rows_sum:g}，求和 ${day.sum:g}")
        if day.count is not None and day.count != len(day.rows):
            notes.append(f"{_md(day.date)} 記錄數 {day.count}，讀到 {len(day.rows)} 行")
        day_figures.append(day.sum if day.sum is not None else rows_sum)
    if stmt.total is not None:
        verifiable = True
        agg = round(sum(day_figures), 2)
        if not _same(agg, stmt.total):
            notes.append(f"逐日加埋 ${agg:g}，總數 ${stmt.total:g}")
    if notes:
        return "fail", notes
    return ("ok" if verifiable else "unverified"), notes


def _confirmed(stmt: Statement) -> float | None:
    if stmt.total is not None:
        return stmt.total
    if not stmt.days:
        return None
    return round(sum(d.sum if d.sum is not None else sum(r.amount for r in d.rows) for d in stmt.days), 2)


def _date_near(order: dict, date: str) -> bool:
    od = (order.get("scheduled_time") or "")[:10]
    try:
        gap = abs((datetime.strptime(od, "%Y-%m-%d") - datetime.strptime(date, "%Y-%m-%d")).days)
    except ValueError:
        return False
    return gap <= 1


def _nearest(statement_id: str, date: str, candidates: list[dict]) -> dict | None:
    """The unique nearest candidate on that date within two edits.
    Ambiguity is reported as no match — a wrong order would be batched
    silently, an unmatched line is visible on the card."""
    scored = sorted(
        ((levenshtein(statement_id, o["order_id"]), o) for o in candidates if _date_near(o, date)),
        key=lambda t: t[0],
    )
    if not scored or scored[0][0] > 2:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None
    return scored[0][1]


def _match(merged: dict[str, tuple[str, float]], by_id: dict[str, dict],
           orders: list[dict]) -> dict[str, tuple[dict, bool]]:
    """Bind statement lines to orders: {statement id: (order, was fuzzy)}.

    Exact ids are settled first so a near-miss can never take an order that
    another line names outright, and an order can be claimed only once — two
    lines reaching for the same order leaves both unbound, the same verdict
    that ambiguity gets everywhere else.  Both rules are decided over the whole
    statement rather than line by line, so the result does not depend on the
    order the lines or the candidates arrive in."""
    bound = {sid: (by_id[sid], False) for sid in merged if sid in by_id}
    claimed = {o["order_id"] for o, _ in bound.values()}
    free = [o for o in orders if o["order_id"] not in claimed]

    picks: dict[str, dict] = {}
    contenders: dict[str, int] = {}
    for sid, (date, _amount) in merged.items():
        if sid in bound:
            continue
        order = _nearest(sid, date, free)
        if order is None:
            continue
        picks[sid] = order
        contenders[order["order_id"]] = contenders.get(order["order_id"], 0) + 1
    for sid, order in picks.items():
        if contenders[order["order_id"]] == 1:
            bound[sid] = (order, True)
    return bound


def _settleable(order: dict, now: datetime) -> str | None:
    """None when the order can enter a batch, else the reason it cannot."""
    if not (order.get("price") or 0) > 0:
        return "未入價"
    if (order.get("scheduled_time") or "") >= now.strftime("%Y-%m-%d %H:%M:%S"):
        return "未完成"
    return None


def reconcile(stmt: Statement, orders: list[dict], now: datetime) -> Reconciliation:
    checksum, notes = _checksum(stmt)
    by_id = {o["order_id"]: o for o in orders}

    # Fold the statement to one line per order id, keeping first-seen order
    # (a 舉牌 line sits under the same id as its trip).
    merged: dict[str, tuple[str, float]] = {}
    for day in stmt.days:
        for r in day.rows:
            date, amt = merged.get(r.order_id, (r.date, 0.0))
            merged[r.order_id] = (date, round(amt + r.amount, 2))

    bound = _match(merged, by_id, orders)

    entries: list[Entry] = []
    matched_ids: set[str] = set()
    settle_ids: list[str] = []
    expected_total = 0.0
    for sid, (date, amount) in merged.items():
        if sid not in bound:
            entries.append(Entry(kind="unknown", statement_id=sid, order_id=sid, date=date,
                                 platform_amount=amount, expected=None, order=None))
            continue
        order, fuzzy = bound[sid]
        oid = order["order_id"]
        matched_ids.add(oid)
        exp = expected_of(order)
        base = dict(statement_id=sid, order_id=oid, date=date, platform_amount=amount,
                    expected=exp, order=order, fuzzy=fuzzy)
        if (order.get("status") or "active") != "active":
            entries.append(Entry("cancelled", **base))
        elif order.get("settlement_id") is not None:
            entries.append(Entry("already_settled", settlement_id=order["settlement_id"], **base))
        elif (reason := _settleable(order, now)) is not None:
            entries.append(Entry("not_ready", reason=reason, **base))
        else:
            entries.append(Entry("matched" if _same(amount, exp) else "amount_diff", **base))
            settle_ids.append(oid)
            expected_total += exp

    dates = set(dates_of(stmt))
    missing = sorted(
        (o for o in orders
         if o["order_id"] not in matched_ids
         and (o.get("status") or "active") == "active"
         and o.get("settlement_id") is None
         and _settleable(o, now) is None
         and (o.get("scheduled_time") or "")[:10] in dates),
        key=lambda o: o.get("scheduled_time") or "",
    )

    return Reconciliation(
        checksum=checksum, checksum_notes=notes, entries=entries, missing=missing,
        settle_ids=settle_ids, expected=round(expected_total, 2),
        confirmed=_confirmed(stmt),
        days=stmt.days, account=stmt.account,
    )


# ---- the reader ----

# Thousands separator may be read as "." on a compressed photo ("2.540.00");
# lookarounds keep a dotted date ("2026.08.25") and sub-runs of a longer
# number from matching.
_MONEY_RE = re.compile(r"(?<![\d.])\d+(?:[,.]\d{3})*\.\d{2}(?![\d.])")
_DATE_RE = re.compile(r"20\d\d-\d\d-\d\d")
_TIME_RE = re.compile(r"(?<!\d)\d\d:\d\d(?!\d)")
_ID_RE = re.compile(r"(?<![\dA-Z])(?:SPACE)?(?=(?:[SOIlB]*\d){8})[\dSOIlB]{12,19}(?![\d])")
_ACCOUNT_RE = re.compile(r"【([^】]+)】")
_COUNT_RE = re.compile(r"(\d{1,3})$")
_DIGIT_FIX = str.maketrans({"S": "5", "O": "0", "I": "1", "l": "1", "B": "8"})


def _money(text: str) -> float:
    s = text.replace(",", "")
    if s.count(".") > 1:
        head, tail = s.rsplit(".", 1)
        s = head.replace(".", "") + "." + tail
    return float(s)


def _normalise_id(token: str) -> str:
    if token.startswith("SPACE"):
        return "SPACE" + token[5:].translate(_DIGIT_FIX)
    return token.translate(_DIGIT_FIX)


def _rows_from_boxes(boxes: list) -> list[list[tuple[float, float, str]]]:
    """Group boxes into table rows by vertical centre; each row is (x, y, text) sorted by x."""
    items = []
    for quad, text, _score in boxes:
        ys = [p[1] for p in quad]
        xs = [p[0] for p in quad]
        items.append((min(xs), (min(ys) + max(ys)) / 2, max(ys) - min(ys), str(text)))
    if not items:
        return []
    heights = sorted(h for _, _, h, _ in items)
    tol = max(4.0, 0.45 * heights[len(heights) // 2])
    items.sort(key=lambda t: t[1])
    rows: list[list] = []
    centre = None
    for x, y, _h, text in items:
        if centre is None or y - centre > tol:
            rows.append([])
            centre = y
        rows[-1].append((x, y, text))
    return [sorted(r) for r in rows]


def parse_boxes(boxes: list, width: int) -> Statement:
    """RapidOCR boxes → Statement.  Only the numbers are trusted: labels such
    as 求和 / 记录数 come out garbled on a Telegram-compressed photo, so rows
    are classified by shape (a date at the left, an order id, an account
    code) rather than by reading the labels."""
    stmt = Statement(days=[])
    current: StatementDay | None = None
    for row in _rows_from_boxes(boxes):
        text = " ".join(t for _, _, t in row)
        moneys = [(x, m) for x, _, t in row for m in _MONEY_RE.findall(t)]
        rightmost = _money(max(moneys, key=lambda t: t[0])[1]) if moneys else None
        account = _ACCOUNT_RE.search(text)
        ids = [(x, _normalise_id(m)) for x, _, t in row for m in _ID_RE.findall(t)]
        dates = [(x, m) for x, _, t in row for m in _DATE_RE.findall(t)]
        leading_date = dates and dates[0][0] < width * 0.15 and row and _DATE_RE.match(row[0][2].strip()) is not None
        if account:
            stmt.account = account.group(1).strip()
            if rightmost is not None:
                stmt.total = rightmost
        elif leading_date and not ids:
            current = StatementDay(date=dates[0][1], rows=[], sum=rightmost)
            first_money_x = min((x for x, _ in moneys), default=width)
            for x, _, t in row[1:]:
                if x >= first_money_x:
                    break
                m = _COUNT_RE.search(t.strip())
                if m:
                    current.count = int(m.group(1))
                    break
            stmt.days.append(current)
        elif ids:
            if current is None:
                stmt.warnings.append(f"row before any day header: {text[:40]}")
                continue
            if rightmost is None:
                stmt.warnings.append(f"row without amount: {text[:40]}")
                continue
            times = _TIME_RE.findall(text)
            right_dates = [m for x, m in dates if x > width * 0.5]
            current.rows.append(StatementRow(
                date=current.date, order_id=ids[0][1], amount=rightmost,
                time=times[0] if times else None,
                settle_date=right_dates[-1] if right_dates else None,
            ))
    return stmt


_ocr = None
_ocr_lock = threading.Lock()
_ocr_broken_logged = False


def _engine():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    return _ocr


def ocr_available() -> bool:
    global _ocr_broken_logged
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    except Exception:
        # An installed but unusable package — a shared library onnxruntime
        # cannot load — raises something other than ImportError.  Callers treat
        # it as "no OCR" so the bot still answers, but unlike a plain absence
        # it is a broken host worth a line in the log, once rather than once
        # per forwarded screenshot.
        if not _ocr_broken_logged:
            _ocr_broken_logged = True
            logging.getLogger("statement").warning("OCR installed but unusable", exc_info=True)
        return False
    return True


def reader_name() -> str:
    from importlib.metadata import version, PackageNotFoundError
    try:
        return f"rapidocr-onnxruntime {version('rapidocr-onnxruntime')}"
    except PackageNotFoundError:
        return "rapidocr-onnxruntime"


def _undecodable() -> Statement:
    stmt = Statement(days=[])
    stmt.warnings.append("image could not be decoded")
    stmt.reader = reader_name()
    return stmt


def read_image(data: bytes) -> Statement:
    """Decode and OCR one screenshot.  CPU-bound (~2 s on the server) and
    serialised: callers run it in a worker thread."""
    import cv2
    import numpy as np
    # Whatever the operator sent, this returns a Statement: empty input and a
    # non-image both reach the warning path, which Telegram callers report as
    # an unreadable screenshot rather than a crash.
    if not data:
        return _undecodable()
    try:
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        return _undecodable()
    if img is None:
        return _undecodable()
    with _ocr_lock:
        result, _elapse = _engine()(img)
    stmt = parse_boxes(result or [], img.shape[1])
    stmt.reader = reader_name()
    return stmt


# ---- text for the bot ----

_KIND_LABEL = {
    "amount_diff": "金額唔同",
    "already_settled": "已結算",
    "cancelled": "已取消",
    "not_ready": "未可結算",
    "unknown": "唔喺系統",
}


def short_id(order_id: str) -> str:
    return "#…" + order_id[-4:]


def money_str(v: float) -> str:
    return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"


def date_span_label(dates: list[str]) -> str:
    """8月23日 · 8月23–24日 · 8月30日–9月1日 · 8月30日、9月1日 (non-contiguous)."""
    s = sorted(dates)
    if not s:
        return ""
    if len(s) == 1:
        return _md(s[0])
    try:
        contiguous = all(
            datetime.strptime(s[i], "%Y-%m-%d") - datetime.strptime(s[i - 1], "%Y-%m-%d") == timedelta(days=1)
            for i in range(1, len(s))
        )
    except ValueError:
        # A date that will not parse cannot be proven adjacent to anything, and
        # this builds operator-facing text: list the days rather than raise.
        return "、".join(_md(d) for d in s)
    if not contiguous:
        return "、".join(_md(d) for d in s)
    a, z = s[0], s[-1]
    if a[:7] == z[:7]:
        return f"{int(a[5:7])}月{int(a[8:10])}–{int(z[8:10])}日"
    return f"{_md(a)}–{_md(z)}"


def format_report(rec: Reconciliation) -> str:
    n_rows = sum(len(d.rows) for d in rec.days)
    head = f"結算單 {rec.account or '?'} · {len(rec.days)} 日 {n_rows} 行"
    if rec.confirmed is not None:
        head += f" · 平台 {money_str(rec.confirmed)}"
    lines = [head, ""]
    by_date: dict[str, list[Entry]] = {}
    for e in rec.entries:
        by_date.setdefault(e.date, []).append(e)
    missing_by_date: dict[str, list[dict]] = {}
    for o in rec.missing:
        missing_by_date.setdefault(o["scheduled_time"][:10], []).append(o)
    for day in rec.days:
        problems = [e for e in by_date.get(day.date, []) if e.kind != "matched"]
        held = missing_by_date.get(day.date, [])
        day_sum = day.sum if day.sum is not None else round(sum(r.amount for r in day.rows), 2)
        mark = " ✓" if not problems and not held and rec.checksum == "ok" else ""
        lines.append(f"{_md(day.date)} · {len(day.rows)} 行 · {money_str(day_sum)}{mark}")
        for e in problems:
            label = _KIND_LABEL[e.kind]
            if e.kind == "amount_diff":
                detail = f"平台 {money_str(e.platform_amount)} · 系統 {money_str(e.expected)}"
            elif e.kind == "already_settled":
                detail = f"批次 #{e.settlement_id}"
            elif e.kind == "not_ready":
                detail = f"{e.reason} · {money_str(e.platform_amount)}"
            else:
                detail = money_str(e.platform_amount)
            lines.append(f"  {label}  {short_id(e.statement_id)}  {detail}")
            lines.append(f"  {e.statement_id}")
        for o in held:
            lines.append(f"  抽起  {short_id(o['order_id'])}  {money_str(expected_of(o))}（今次冇計）")
            lines.append(f"  {o['order_id']}")
    lines.append("")
    if rec.checksum == "fail":
        lines.append("讀圖唔一致（" + "；".join(rec.checksum_notes) + "）— 再 send 一次，或者用「Send as file」send 原檔")
    elif rec.checksum == "unverified":
        lines.append("讀唔到 求和 / 總數，冇得核對 — 再 send 一次，或者用「Send as file」send 原檔")
    elif not rec.settle_ids:
        # Nothing can enter a batch, so there is no 系統應收 to compare against:
        # a 差額 measured against an empty batch reads as a shortfall to chase.
        lines.append("冇單可以入 batch")
    else:
        lines.append(f"系統應收 {money_str(rec.expected)} · 差額 {_signed(rec.diff)}")
    return "\n".join(lines)


def _signed(v: float) -> str:
    if abs(v) < 0.005:
        return "$0"
    return ("+" if v > 0 else "−") + money_str(abs(v))


def corrected_json(stmt: Statement, rec: Reconciliation) -> dict:
    """Statement JSON for storage, with ids as the system knows them.

    A line the matcher bound by near-miss keeps what was actually read under
    `read_as`; everything downstream (batch detail) keys on `order_id`, so the
    platform figure lands on the order it belongs to instead of appearing as
    an unmatched extra.
    """
    fixes = {e.statement_id: e.order_id for e in rec.entries if e.statement_id != e.order_id}
    d = stmt.to_json()
    for day in d["days"]:
        for row in day["rows"]:
            if row["order_id"] in fixes:
                row["read_as"] = row["order_id"]
                row["order_id"] = fixes[row["order_id"]]
    return d


def confirm_label(rec: Reconciliation) -> str:
    n = len(rec.settle_ids)
    amount = money_str(rec.confirmed or 0.0)
    if rec.clean:
        return f"確認結算 · {n} 程 · {amount}"
    return f"照平台數確認 · {n} 程 · {amount}（差額 {_signed(rec.diff)}）"


def settled_reply(settlement_id: int, rec: Reconciliation, dates: list[str]) -> str:
    return (f"已結算 批次 #{settlement_id} · {date_span_label(dates)} · {len(rec.settle_ids)} 程 · "
            f"{money_str(rec.confirmed or 0.0)}\n\n{confirmation_line(rec, dates)}")


def confirmation_line(rec: Reconciliation, dates: list[str]) -> str:
    """One line to paste back to the platform.

    The figure keeps its cents: this line is quoted back as the amount agreed,
    so it must equal the statement's total to the cent, not a rounded version
    of it."""
    return (f"{date_span_label(dates)} 共{len(rec.settle_ids)}程 "
            f"HKD {money_str(rec.confirmed or 0.0)[1:]} 確認無誤")


def awaiting_label(batch: dict) -> str:
    dates = sorted({o["scheduled_time"][:10] for o in batch["orders"]})
    return f"#{batch['id']} · {date_span_label(dates)} · {len(batch['orders'])} 程 · {money_str(batch['confirmed_amount'])}"


def fallback_report(orders: list[dict]) -> str:
    """What the bot can offer without OCR: the unsettled legs, by day, to compare by eye."""
    lines = ["OCR 未裝，讀唔到張圖。未結算嘅接送單："]
    by_date: dict[str, list[dict]] = {}
    for o in orders:
        by_date.setdefault(o["scheduled_time"][:10], []).append(o)
    for date in sorted(by_date):
        rows = by_date[date]
        lines.append("")
        lines.append(f"{_md(date)} · {len(rows)} 程 · {money_str(sum(expected_of(o) for o in rows))}")
        for o in rows:
            t = o["scheduled_time"][11:16]
            lines.append(f"  {t} {short_id(o['order_id'])} {money_str(expected_of(o))}")
            lines.append(f"  {o['order_id']}")
    if len(lines) == 1:
        lines.append("（冇）")
    return "\n".join(lines)
