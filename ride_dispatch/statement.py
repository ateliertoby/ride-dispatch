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
    truncated: bool = False   # the platform's UI cut the id short with an ellipsis


_ROW_KEYS = ("date", "order_id", "amount", "time", "settle_date", "truncated")


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
    penalty: float = 0.0        # ≤ 0: the 判罰賠款 rows folded into this order


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
    # The statement lines a confirm would record on the batch itself, in the
    # order the statement printed them: {order_ref, date, amount}.
    adjustments: list[dict] = field(default_factory=list)

    @property
    def diff(self) -> float:
        return round((self.confirmed or 0.0) - self.expected, 2)

    @property
    def can_settle(self) -> bool:
        return self.checksum == "ok" and bool(self.settle_ids)

    @property
    def clean(self) -> bool:
        # A statement fully explained by penalties settles for exactly what
        # arrived, so it reads as clean: nothing on it is the operator's to chase.
        # An adjustment is explained the same way — the line is recorded on the
        # batch, so it is accounted for rather than left for human eyes.
        return (self.can_settle and not self.missing
                and all(e.kind in ("matched", "penalty", "adjustment") for e in self.entries))


# The entry kinds whose negative rows a confirm records against their order.
# An already_settled order's fine is deliberately absent: writing it would have
# to reopen a batch whose expected_amount is frozen.  It is not lost — the fine
# is money on the transfer being confirmed now, so it is recorded on this batch
# as an adjustment instead (see _late_fine), which leaves the frozen batch
# alone.  Two mechanisms, one boundary: a leg's own cost belongs on the order,
# a cost the transfer carries belongs on the batch.
PENALTY_KINDS = ("penalty", "amount_diff")


def penalties_of(rec: "Reconciliation") -> dict[str, float]:
    """The fines a confirm would record: positive amounts by order id.

    One definition, because the button's label has to promise exactly what the
    confirm writes.
    """
    return {e.order_id: -e.penalty for e in rec.entries
            if e.penalty < 0 and e.kind in PENALTY_KINDS}


def _late_fine(e: "Entry") -> bool:
    """Whether an entry is a fine against a trip some other batch already holds.

    Equal figures mean the fine has already been taken off the order — a second
    read of a statement that was confirmed — and re-recording it would take the
    same money off twice, so only a disagreement is a fine still to record.
    """
    return (e.kind == "already_settled" and e.penalty < 0
            and not _same(e.platform_amount, e.expected))


def _as_adjustments(order_ref: str, rows: list[StatementRow]) -> list[dict]:
    """Statement lines as the batch will record them, one row per printed line.

    The platform's own structure is kept rather than netted: a 判罰 and the
    免責 line that cancels it are two facts, and a pair that happens to net to
    zero must still be readable as the pair it was.
    """
    return [{"order_ref": order_ref, "date": r.date, "amount": r.amount} for r in rows]


# A near miss is allowed the same two edits wherever it is measured — over a
# whole id, or over the opening of a truncated one — so the passes that use it
# cannot drift apart.
MAX_EDITS = 2


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
    """The unique nearest candidate on that date within MAX_EDITS edits.
    Ambiguity is reported as no match — a wrong order would be batched
    silently, an unmatched line is visible on the card."""
    scored = sorted(
        ((levenshtein(statement_id, o["order_id"]), o) for o in candidates if _date_near(o, date)),
        key=lambda t: t[0],
    )
    if not scored or scored[0][0] > MAX_EDITS:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None
    return scored[0][1]


def _prefix(statement_id: str, date: str, candidates: list[dict]) -> dict | None:
    """The one candidate on that date whose id starts with what was read.

    The platform's table truncates a long code, so the line names its order
    without spelling it out.  Two candidates sharing the prefix is the same
    ambiguity a near-miss gets: no match, and the line stays visible on the
    card rather than being batched against a guess."""
    hits = [o for o in candidates
            if o["order_id"].startswith(statement_id) and _date_near(o, date)]
    return hits[0] if len(hits) == 1 else None


def _prefix_near(statement_id: str, date: str, candidates: list[dict]) -> dict | None:
    """The one candidate whose opening agrees with a code read imperfectly.

    Telegram's photo compression turns letters into other letters — on the
    operator's normal path a K came back as X and a B as 8 — and in an
    alphanumeric code those are letters, so _DIGIT_FIX must not touch them and
    the exact-prefix pass finds nothing on a compressed copy of an image whose
    original binds.  The opening is therefore compared with the same tolerance
    a whole id gets, against each candidate cut to the token's length.

    Stricter than _nearest in one way: a second candidate anywhere inside the
    bound leaves the line unknown rather than letting the nearer one win.  A
    prefix is already partial evidence, and money must not be batched against
    two codes that both nearly agree.
    """
    n = len(statement_id)
    hits = [o for o in candidates
            if _date_near(o, date)
            and levenshtein(statement_id, o["order_id"][:n]) <= MAX_EDITS]
    return hits[0] if len(hits) == 1 else None


def _bind_round(merged: dict[str, tuple[str, float]], bound: dict, orders: list[dict],
                choose) -> None:
    """Bind every line `choose` can place, refusing any order two lines want.

    Decided over the whole statement rather than line by line, so the result
    does not depend on the order the lines or the candidates arrive in."""
    claimed = {o["order_id"] for o, _ in bound.values()}
    free = [o for o in orders if o["order_id"] not in claimed]
    picks: dict[str, dict] = {}
    contenders: dict[str, int] = {}
    for sid, (date, _amount) in merged.items():
        if sid in bound:
            continue
        order = choose(sid, date, free)
        if order is None:
            continue
        picks[sid] = order
        contenders[order["order_id"]] = contenders.get(order["order_id"], 0) + 1
    for sid, order in picks.items():
        if contenders[order["order_id"]] == 1:
            bound[sid] = (order, True)


def _match(merged: dict[str, tuple[str, float]], by_id: dict[str, dict],
           orders: list[dict], truncated: set[str]) -> dict[str, tuple[dict, bool]]:
    """Bind statement lines to orders: {statement id: (order, was inexact)}.

    Four passes, weakest evidence last, so nothing weaker can take an order a
    stronger rule already claimed: exact ids; then a line that was cut short
    (or is long enough that a shared prefix would be a coincidence) binding to
    the order it is the start of; then the same opening allowed the near-miss
    tolerance, for a code the image mangled; then the near-miss over whole ids.
    """
    bound = {sid: (by_id[sid], False) for sid in merged if sid in by_id}
    prefixable = {sid: v for sid, v in merged.items()
                  if sid in truncated or len(sid) >= MIN_PREFIX}
    _bind_round(prefixable, bound, orders, _prefix)
    # Only a code needs the tolerant opening: the platform truncates those and
    # nothing else, and a digit run's mis-reads are already covered by
    # _DIGIT_FIX and then by _nearest over the whole id.  For a digit id read
    # at full length this round would change nothing anyway — binding on a
    # unique candidate inside the bound is a subset of _nearest's unique
    # nearest — so all the gate withholds is a shortened digit token binding
    # on an opening the whole-id comparison would have rejected.
    mangled = {sid: v for sid, v in prefixable.items() if _REAL_LETTER_RE.search(sid)}
    _bind_round(mangled, bound, orders, _prefix_near)
    _bind_round(merged, bound, orders, _nearest)
    return bound


def leg_amount(batch: dict, order: dict) -> float:
    """What the platform is paying for one leg of a batch.

    The platform's own figure whenever the batch carries its statement, because
    the transfer is the sum of those figures and nothing else.  A leg the
    platform priced differently from the system (an amount_diff line) would
    otherwise never account for what the bank actually sent, and the operator
    would be told his ticks do not add up over a discrepancy that is the
    platform's.  A 舉牌 line shares its trip's id, so the rows are summed
    exactly as reconcile folds them, and the ids are the corrected ones
    (corrected_json rewrites a near-miss and keeps the original as read_as).
    Without a statement row to read there is no platform figure, so the
    system's own is the only answer available.
    """
    stmt = batch.get("statement") or {}
    rows = [r for day in stmt.get("days", []) for r in day.get("rows", [])
            if r.get("order_id") == order["order_id"]]
    if rows:
        return round(sum(r["amount"] for r in rows), 2)
    return expected_of(order)


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
    # (a 舉牌 line sits under the same id as its trip).  The negative rows are
    # summed apart as well: a 判罰賠款 also shares its trip's id, and telling a
    # fine from an underpayment is the difference between recording a cost and
    # reporting a discrepancy.
    merged: dict[str, tuple[str, float]] = {}
    negatives: dict[str, float] = {}
    lines: dict[str, list[StatementRow]] = {}
    truncated: set[str] = set()
    for day in stmt.days:
        for r in day.rows:
            date, amt = merged.get(r.order_id, (r.date, 0.0))
            merged[r.order_id] = (date, round(amt + r.amount, 2))
            lines.setdefault(r.order_id, []).append(r)
            if r.amount < 0:
                negatives[r.order_id] = round(negatives.get(r.order_id, 0.0) + r.amount, 2)
            if r.truncated:
                truncated.add(r.order_id)

    bound = _match(merged, by_id, orders, truncated)

    entries: list[Entry] = []
    matched_ids: set[str] = set()
    settle_ids: list[str] = []
    adjustments: list[dict] = []
    expected_total = 0.0
    for sid, (date, amount) in merged.items():
        neg = negatives.get(sid, 0.0)
        if sid not in bound:
            # An id the book does not know is a line of the transfer only when
            # its rows cost money: a fine, or a fine and the row that offsets
            # it.  A group that nets positive is income-shaped, and income
            # under an unknown number is most likely a real leg the book never
            # got — booking it here would read as fully explained while the
            # money that came in is not.  The unknown flag is the only alarm
            # for that, so anything netting above zero keeps it.
            kind = "adjustment" if neg < 0 and (amount < 0 or _same(amount, 0)) else "unknown"
            entries.append(Entry(kind=kind, statement_id=sid, order_id=sid, date=date,
                                 platform_amount=amount, expected=None, order=None, penalty=neg))
            if kind == "adjustment":
                adjustments += _as_adjustments(sid, lines[sid])
            continue
        order, fuzzy = bound[sid]
        oid = order["order_id"]
        matched_ids.add(oid)
        exp = expected_of(order)
        base = dict(statement_id=sid, order_id=oid, date=date, platform_amount=amount,
                    expected=exp, order=order, fuzzy=fuzzy, penalty=neg)
        if (order.get("status") or "active") != "active":
            # The number is verified real and is not a leg of anything: a
            # cancelled trip cannot enter a batch, so whatever the platform
            # booked under it is money on this transfer and nothing else.
            entries.append(Entry("adjustment", **base))
            adjustments += _as_adjustments(oid, lines[sid])
        elif order.get("settlement_id") is not None:
            e = Entry("already_settled", settlement_id=order["settlement_id"], **base)
            entries.append(e)
            # The trip's own money belongs to the batch that holds the leg and
            # stays a read; only the fine is new, and it came off this transfer.
            if _late_fine(e):
                adjustments += _as_adjustments(oid, [r for r in lines[sid] if r.amount < 0])
        elif (reason := _settleable(order, now)) is not None:
            entries.append(Entry("not_ready", reason=reason, **base))
        else:
            if _same(amount, exp):
                # Covers both "no penalty" and "the fine is already recorded":
                # expected_of nets a stored penalty_fee, so re-reading the same
                # image after a confirm agrees rather than settling twice.
                kind = "matched"
            elif neg < 0 and _same(round(amount - neg, 2), exp):
                kind = "penalty"
            else:
                kind = "amount_diff"
            entries.append(Entry(kind, **base))
            settle_ids.append(oid)
            # Net of the fine this confirm is about to record, because the same
            # transaction freezes the batch's expected_amount from the stored
            # rows: the card and the batch have to name the same figure.  A
            # `matched` line adds nothing — either there is no fine, or one is
            # already stored and expected_of has already taken it off.
            expected_total += exp + (neg if kind in PENALTY_KINDS else 0.0)

    # An adjustment is recorded on a batch, so a statement that cannot produce
    # one has nowhere to put its lines and claims none of them.
    if not settle_ids:
        adjustments = []
    expected_total += sum(a["amount"] for a in adjustments)

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
        days=stmt.days, account=stmt.account, adjustments=adjustments,
    )


# ---- the reader ----

# Thousands separator may be read as "." on a compressed photo ("2.540.00");
# lookarounds keep a dotted date ("2026.08.25") and sub-runs of a longer
# number from matching.  A figure carrying a "%" is a rate (派單風險率 prints
# "0.00%"), never money, so it must not be able to stand in for an amount.
# The sign is optional because a 判罰賠款 makes money negative — a day of
# nothing but penalties prints a negative 求和 — and the platform's minus
# arrives as a hyphen or as U+2212 depending on the render.
_MONEY_RE = re.compile(r"(?<![\d.])[-−]?\d+(?:[,.]\d{3})*\.\d{2}(?![\d.])(?!\s*%)")
_DATE_RE = re.compile(r"20\d\d-\d\d-\d\d")
_TIME_RE = re.compile(r"(?<!\d)\d\d:\d\d(?!\d)")
# Three shapes of order number reach this reader, and one pattern has to know
# all three or a perfectly readable statement fails its checksum over a line it
# never saw:
#   SPACE…  a short digit run behind a fixed prefix, as few as eight digits;
#   a long digit run, where S O I l B may be mis-read digits (see _DIGIT_FIX);
#   an alphanumeric code, which the platform's own UI truncates with an
#   ellipsis, so it is recognised by shape rather than by length.
# The last one needs at least four digits and one letter that is not an
# OCR-confusable digit, or a mangled digit run would be taken for a code and
# left un-normalised.  None of the three can hold "." "-" or ":", which is what
# keeps amounts, dates and times out.
_ID_RE = re.compile(
    r"(?<![\dA-Z])SPACE\d{8,}(?![\dA-Z])"
    r"|(?<![\dA-Z])(?=(?:[SOIlB]*\d){8})[\dSOIlB]{12,19}(?![\d])"
    r"|(?<![A-Z0-9])(?=(?:[A-Z0-9]*\d){4})(?=[A-Z0-9]*[AC-HJ-NP-RT-Z])[A-Z0-9]{10,}(?![A-Z0-9])"
)
# The platform truncates a long code in its own table; the ellipsis is the only
# sign that what was read is a prefix rather than the whole number.
_TRUNCATED_RE = re.compile(r"\s*(?:\.\.\.|…)")
_ACCOUNT_RE = re.compile(r"【([^】]+)】")
_COUNT_RE = re.compile(r"(\d{1,3})$")
_DIGIT_FIX = str.maketrans({"S": "5", "O": "0", "I": "1", "l": "1", "B": "8"})
# A letter that is nobody's mis-read digit: its presence says the token is a
# code, so the digit fixes must not touch it — and, downstream, that a mangled
# opening is worth measuring against the candidates (see _prefix_near, which
# reads this to tell a code from a digit run).
_REAL_LETTER_RE = re.compile(r"[AC-HJ-NP-RT-Z]")
# What makes an id long enough to bind on its opening alone.  A code the
# platform truncated qualifies whatever its length; a whole id needs this many
# characters before a shared opening stops being a coincidence.  The opening
# then binds exactly (_prefix) or within MAX_EDITS (_prefix_near), because
# photo compression rewrites letters the digit fixes are not allowed to touch.
MIN_PREFIX = 10


# A money cell, read off the end of the text that landed in it: whatever
# precedes the figure is a neighbouring cell OCR merged in, or the cell's own
# label (求和), or noise.  A decimal point drawn at ~7 px comes back as ":", so
# both separators are accepted, and a thousands comma read as "." ("2.540.00")
# is still a thousands group.
# A 判罰賠款 row is a negative 司機應結算金額 under its trip's own order id, and
# the sign is the only reliable sign of it: OCR renders the category chip as
# garbage.  Dropping it turns a fine into income, which fails the day's own 求和
# and leaves the operator resending an image that can never reconcile.
_CELL_AMOUNT_RE = re.compile(
    r"(?P<sign>[-−])?(?P<whole>\d{1,3}(?:[,.]\d{3})+|\d+)[.:](?P<cents>\d{2})\s*$")


def _amount_in_cell(text: str) -> float | None:
    m = _CELL_AMOUNT_RE.search(text)
    if m is None:
        return None
    value = float(m["whole"].replace(",", "").replace(".", "") + "." + m["cents"])
    return -value if m["sign"] else value


def _money(text: str) -> float:
    # float() knows the ASCII hyphen only, so the platform's other minus glyph
    # is normalised before the thousands separators are collapsed.
    s = text.replace(",", "").replace("−", "-")
    if s.count(".") > 1:
        head, tail = s.rsplit(".", 1)
        s = head.replace(".", "") + "." + tail
    return float(s)


def _normalise_id(token: str) -> str:
    if token.startswith("SPACE"):
        return "SPACE" + token[5:].translate(_DIGIT_FIX)
    # An alphanumeric code means its letters, so B is a B and not an 8.
    if _REAL_LETTER_RE.search(token):
        return token
    return token.translate(_DIGIT_FIX)


def _ids_in(text: str) -> list[tuple[str, bool]]:
    """Order numbers in one table row, each with whether it was cut short."""
    out = []
    for m in _ID_RE.finditer(text):
        out.append((_normalise_id(m.group()),
                    _TRUNCATED_RE.match(text, m.end()) is not None))
    return out


def _rows_from_boxes(boxes: list) -> list[list[tuple[float, float, float, str]]]:
    """Group boxes into table rows by vertical centre; each row is
    (left, right, y, text) sorted left to right.

    Both horizontal edges are kept: the columns are reconstructed from where
    cells overlap each other, and a cell's left edge alone says nothing about
    which column it is in — the platform right-aligns its figures and
    left-aligns its labels, so the two edges of one column disagree.
    """
    items = []
    for quad, text, _score in boxes:
        ys = [p[1] for p in quad]
        xs = [p[0] for p in quad]
        items.append((min(xs), max(xs), (min(ys) + max(ys)) / 2, max(ys) - min(ys), str(text)))
    if not items:
        return []
    heights = sorted(it[3] for it in items)
    tol = max(4.0, 0.45 * heights[len(heights) // 2])
    items.sort(key=lambda t: t[2])
    rows: list[list] = []
    centre = None
    for x0, x1, y, _h, text in items:
        if centre is None or y - centre > tol:
            rows.append([])
            centre = y
        rows[-1].append((x0, x1, y, text))
    return [sorted(r) for r in rows]


# ---- the grid ----

# The reader binds to the table's schema, never to a position in the row.  The
# platform adds columns without notice — a 結算狀態 column appeared to the right
# of 司機應結算金額 and every data row read as amountless — so the table's own
# structure is reconstructed instead: bands of x inferred from the rows that
# carry structure, named from the header row where it is legible, and every
# cell read out of the band that means it.

# The columns as the platform prints them, in printed order, each with the
# simplified spelling OCR returns instead.  Both are needed because every single
# character differs between the two scripts, so an edit distance measured
# against one spelling alone rejects a clean read of the other.
# Columns this reader takes nothing from are still listed, with no role: they
# are here to compete for a garbled header cell, so a mangled neighbour lands on
# its own name rather than drifting onto a column the reader acts on.
_ROLE_TIME = "time"
_ROLE_ID = "order_id"
_ROLE_SETTLE = "settle_date"
_ROLE_AMOUNT = "amount"

_COLUMNS: tuple[tuple[str | None, tuple[str, str]], ...] = (
    (None, ("訂單服務日期", "订单服务日期")),
    (_ROLE_TIME, ("用車時間", "用车时间")),
    (None, ("出賬單日期", "出账单日期")),
    (None, ("類目", "类目")),
    (_ROLE_ID, ("訂單號", "订单号")),
    (None, ("行程費", "行程费")),
    (None, ("高峰加價", "高峰加价")),
    (None, ("平峰加價", "平峰加价")),
    (None, ("舉牌服務", "举牌服务")),
    (None, ("兒童座椅", "儿童座椅")),
    (None, ("其他調整", "其他调整")),
    (None, ("司機預估收入", "司机预估收入")),
    (None, ("履約遲到", "履约迟到")),
    (None, ("遲到時間", "迟到时间")),
    (None, ("司機端操作", "司机端操作")),
    (None, ("質量問題", "质量问题")),
    (None, ("派單風險率", "派单风险率")),
    (None, ("司機責任", "司机责任")),
    (None, ("司機等級", "司机等级")),
    (None, ("司機風險", "司机风险")),
    (None, ("結算比例", "结算比例")),
    (None, ("結算類型", "结算类型")),
    (_ROLE_SETTLE, ("應結算日期", "应结算日期")),
    (None, ("收款幣種", "收款币种")),
    (None, ("司機端包狀態", "司机端包状态")),
    (_ROLE_AMOUNT, ("司機應結算金額", "司机应结算金额")),
    (None, ("結算狀態", "结算状态")),
)

# Anything outside the CJK block is stripped off the ends of a header cell
# before it is measured: OCR routinely opens or closes such a cell with a stray
# bracket or table rule, and those edits would otherwise be spent on punctuation
# rather than on the characters that identify the column.
_HEADER_EDGE_RE = re.compile(r"^[^一-鿿]+|[^一-鿿]+$")


def _header_tolerance(name: str) -> int:
    """How many edits a header cell may be off its column's printed name.

    Two fifths of the name, which is what a four-character name mis-read into
    two other characters needs (結算狀態 comes back as 结真状能).  It may not go
    much higher: 司機預估收入 and 司機應結算金額 are five edits apart, and reading a
    day off the estimate column would settle a wrong figure while every subtotal
    on the image still agreed with itself.
    """
    return max(1, (2 * len(name) + 2) // 5)


def _column_match(cell: str) -> tuple[int, int] | None:
    """(column index, edits) for a header cell, or None when it is too garbled.

    A tie between two columns is no match, for the reason a tie is no match
    anywhere else in this module: a column named by a coin toss is worse than a
    column left anonymous, because nothing downstream can see the toss.
    """
    text = _HEADER_EDGE_RE.sub("", cell)
    if not text:
        return None
    scored = []
    for i, (_role, names) in enumerate(_COLUMNS):
        d = min(levenshtein(text, name) for name in names)
        if d <= _header_tolerance(names[0]):
            scored.append((d, i))
    if not scored:
        return None
    scored.sort()
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None
    return scored[0][1], scored[0][0]


def _bands(rows: list[list]) -> list[tuple[float, float]]:
    """The table's columns as x intervals, left to right.

    Two cells belong to the same column when they overlap horizontally, which
    is the one relation that survives OCR: a cell's own box is drawn tight
    around its text, so its width tells you nothing, but a figure and the label
    above it always share ground.  Only rows the reader could classify are fed
    in — the platform's watermark prints its own boxes across the table, and a
    stray one landing between two columns would fuse them for good.
    """
    spans = sorted((x0, x1) for row in rows for x0, x1, _y, _t in row)
    bands: list[list[float]] = []
    for x0, x1 in spans:
        if bands and x0 < bands[-1][1]:
            bands[-1][1] = max(bands[-1][1], x1)
        else:
            bands.append([x0, x1])
    return [(a, b) for a, b in bands]


def _band_of(bands: list[tuple[float, float]], x0: float, x1: float) -> int | None:
    """The column a box sits in: the one it overlaps most."""
    best, best_overlap = None, 0.0
    for i, (a, b) in enumerate(bands):
        overlap = min(x1, b) - max(x0, a)
        if overlap > best_overlap:
            best, best_overlap = i, overlap
    return best


def _cells(row: list, bands: list[tuple[float, float]]) -> dict[int, str]:
    """One row as {column: text}, boxes sharing a column joined left to right."""
    out: dict[int, list[str]] = {}
    for x0, x1, _y, text in row:
        i = _band_of(bands, x0, x1)
        if i is not None:
            out.setdefault(i, []).append(text)
    return {i: " ".join(parts) for i, parts in out.items()}


def _day_head(row: list, width: int) -> tuple[int, str] | None:
    """(box index, date) when the row opens a day group, else None.

    A day group opens with its date at the left edge, but not always in the
    row's first box: the ▾ expand caret beside it can be recognised as a box of
    its own, so the date is looked for rather than assumed to lead.
    """
    for i, (x0, _x1, _y, text) in enumerate(row):
        if x0 < width * 0.15 and (m := _DATE_RE.match(text.strip())):
            return i, m.group()
    return None


# How many of its cells must land on a printed column name before a row is read
# as the header.  Three, because two is within reach of a row of category chips.
_MIN_HEADER_CELLS = 3

_STRUCTURE = ("header", "account", "day", "data")


def _classify(rows: list[list], width: int) -> list[str]:
    """What each row is: header, account, day, data, or noise.

    Shape first and labels last, because a Telegram-compressed photo garbles
    the labels: a row carrying an order id is a data row whatever else it holds
    (OCR renders the platform's category chips with 【】, and a matched pair
    does land in a data row, which the account pattern must not be able to
    claim), and the account code with the grand total prints above every day
    group, so a bracket met after one has opened is chip noise.
    Noise is silent by design — the watermark prints a row of its own on every
    screenshot, and it names nothing the operator could act on.
    """
    kinds = []
    seen_day = False
    for row in rows:
        text = " ".join(t for _, _, _, t in row)
        if _ids_in(text):
            kinds.append("data")
        elif not seen_day and _ACCOUNT_RE.search(text):
            kinds.append("account")
        elif _day_head(row, width) is not None:
            seen_day = True
            kinds.append("day")
        elif sum(_column_match(t) is not None for _, _, _, t in row) >= _MIN_HEADER_CELLS:
            kinds.append("header")
        else:
            kinds.append("noise")
    return kinds


def _named_bands(rows: list[list], kinds: list[str],
                 bands: list[tuple[float, float]]) -> dict[str, int]:
    """{role: column} for the columns the header names.

    Only the four columns this reader acts on get a role; the rest are matched
    solely so they cannot be mistaken for one of the four.  A role claimed twice
    goes to the closer read.
    """
    named: dict[str, int] = {}
    edits: dict[str, int] = {}
    for row, kind in zip(rows, kinds):
        if kind != "header":
            continue
        for x0, x1, _y, text in row:
            m = _column_match(text)
            if m is None:
                continue
            role = _COLUMNS[m[0]][0]
            band = _band_of(bands, x0, x1)
            if role is None or band is None:
                continue
            if role not in edits or m[1] < edits[role]:
                named[role], edits[role] = band, m[1]
    return named


def _money_columns(kinds: list[str], cells: list[dict[int, str]]) -> set[int]:
    """The columns that hold money, by what the platform printed in them.

    A 求和 in the group row and a money-shaped figure in the data rows both
    count; a percentage counts as neither (派單風險率 prints "0.00%"), which is
    what keeps a rate column from passing as the rightmost money column.
    Header rows are excluded: a label is not a figure.
    """
    out = set()
    for kind, cell in zip(kinds, cells):
        if kind in ("account", "day", "data"):
            out |= {i for i, text in cell.items() if _MONEY_RE.search(text)}
    return out


def _amount_column(named: dict[str, int], money: set[int],
                   bands: list[tuple[float, float]]) -> tuple[int | None, str | None]:
    """Which column is 司機應結算金額, and what to warn about if it is unclear.

    Two independent readings have to agree: the column the header names, and
    the rightmost column holding money.  The second one carries the read alone
    when the header is missing or too garbled to match — a cropped screenshot
    has no header row at all — but when both are present and name different
    columns, neither is trusted.  The alternative is picking one, and a wrong
    pick is invisible: the rows would be summed off the wrong column and the
    day's own 求和, read off that same column, would agree with them.
    """
    by_name = named.get(_ROLE_AMOUNT)
    by_shape = max(money) if money else None
    if by_name is None:
        return by_shape, None
    if by_shape is None or by_name == by_shape:
        return by_name, None
    return None, ("amount column unclear: 司機應結算金額 header over "
                  f"x={bands[by_name][0]:.0f}, rightmost money column at "
                  f"x={bands[by_shape][0]:.0f}")


def _id_of(row_text: str, cell: dict[int, str], named: dict[str, int]) -> tuple[str, bool]:
    """The order number a data row names, and whether it was cut short.

    Preferably out of the 訂單號 column, which is what keeps the watermark's
    digits — printed across the table, sometimes close enough to a row to join
    it — from being read as an order number.  Read off joined text rather than
    box by box either way: OCR puts the platform's truncating ellipsis in
    whichever box it lands in, and the id is only known to be a prefix if that
    ellipsis is still beside it.
    Only called for a row `_classify` read as data, so the row always holds one.
    """
    band = named.get(_ROLE_ID)
    ids = _ids_in(cell.get(band, "")) if band is not None else []
    return (ids or _ids_in(row_text))[0]


def _time_of(row_text: str, cell: dict[int, str], named: dict[str, int]) -> str | None:
    """用車時間, out of its column when the header named one."""
    band = named.get(_ROLE_TIME)
    found = _TIME_RE.findall(cell.get(band, "")) if band is not None else []
    found = found or _TIME_RE.findall(row_text)
    return found[0] if found else None


def _settle_date_of(row: list, cell: dict[int, str], named: dict[str, int],
                    width: int) -> str | None:
    """應結算日期, out of its column when the header named one.

    Without a named column the right half of the row is the best available
    stand-in: 訂單服務日期 and 出賬單日期 print on the left, and this is the last
    date before the amount.
    """
    band = named.get(_ROLE_SETTLE)
    if band is not None and (m := _DATE_RE.search(cell.get(band, ""))):
        return m.group()
    right = [m for x0, _x1, _y, t in row for m in _DATE_RE.findall(t) if x0 > width * 0.5]
    return right[-1] if right else None


def _count_in(row: list, head_i: int, bands: list[tuple[float, float]],
              money: set[int]) -> int | None:
    """记录数 off a day group's row: the first countable cell after the date.

    The scan stops at the first money column because the aggregates that follow
    are 求和 figures, whose own trailing digits would read as a count.
    """
    for x0, x1, _y, text in row[head_i + 1:]:
        if _MONEY_RE.search(text) or _band_of(bands, x0, x1) in money:
            return None
        if (m := _COUNT_RE.search(text.strip())):
            return int(m.group(1))
    return None


def parse_boxes(boxes: list, width: int) -> Statement:
    """RapidOCR boxes → Statement.

    The table is reconstructed before it is read: rows are classified by shape,
    their columns inferred from where the cells line up, and the columns that
    matter named off the header row.  Every figure then comes out of the column
    that means it — the day's 求和 and its rows' amounts out of the same
    司機應結算金額 column, which is what makes `_checksum` a check on one column
    rather than on whatever each row happened to end with.
    """
    stmt = Statement(days=[])
    rows = _rows_from_boxes(boxes)
    kinds = _classify(rows, width)
    bands = _bands([r for r, k in zip(rows, kinds) if k in _STRUCTURE])
    cells = [_cells(r, bands) if k in _STRUCTURE else {} for r, k in zip(rows, kinds)]
    named = _named_bands(rows, kinds, bands)
    money = _money_columns(kinds, cells)
    amount_col, complaint = _amount_column(named, money, bands)
    if complaint:
        stmt.warnings.append(complaint)
    current: StatementDay | None = None
    for row, kind, cell in zip(rows, kinds, cells):
        text = " ".join(t for _, _, _, t in row)
        amount = _amount_in_cell(cell.get(amount_col, "")) if amount_col is not None else None
        if kind == "data":
            if current is None:
                stmt.warnings.append(f"row before any day header: {text[:40]}")
                continue
            if amount is None:
                stmt.warnings.append(f"row without amount: {text[:40]}")
                continue
            order_id, truncated = _id_of(text, cell, named)
            current.rows.append(StatementRow(
                date=current.date, order_id=order_id, amount=amount,
                time=_time_of(text, cell, named),
                settle_date=_settle_date_of(row, cell, named, width),
                truncated=truncated,
            ))
        elif kind == "account":
            stmt.account = _ACCOUNT_RE.search(text).group(1).strip()
            stmt.total = amount
        elif kind == "day":
            head_i, head_date = _day_head(row, width)
            current = StatementDay(date=head_date, rows=[], sum=amount,
                                   count=_count_in(row, head_i, bands, money))
            stmt.days.append(current)
    return stmt


_ocr = None
_ocr_lock = threading.Lock()
_ocr_broken_logged = False


def _engine():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        # Above a width/height ratio of its own (8 by default) the engine skips
        # detection and recognises the whole frame as a single line, so a wide
        # screenshot comes back with no boxes at all.  A statement day with few
        # rows is that wide.  -1 turns the threshold off.
        _ocr = RapidOCR(width_height_ratio=-1)
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


def _decode(data: bytes):
    """The screenshot as a BGR array, or None for anything that is not one."""
    import cv2
    import numpy as np
    if not data:
        return None
    try:
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of an encoded screenshot, None when it will not decode."""
    img = _decode(data)
    return None if img is None else (img.shape[1], img.shape[0])


# The widest frame handed to the engine, as a multiple of its height: a margin
# below the ratio at which the engine stops detecting.
_MAX_ASPECT = 6.0


def _pad_for_detection(img):
    """Extend a very wide frame downwards with white rows until it is no wider
    than `_MAX_ASPECT` times its height.

    A guard against the detector's width/height threshold that does not depend
    on the engine's settings: past that threshold nothing is detected and the
    whole frame is recognised as one line.  Only rows are added, below the
    content, so the width and every original pixel stay as they were and the
    blank rows contribute no boxes of their own.
    """
    import numpy as np
    h, w = img.shape[:2]
    if h <= 0 or w <= _MAX_ASPECT * h:
        return img
    rows = int(np.ceil(w / _MAX_ASPECT)) - h
    return np.vstack([img, np.full((rows, w) + img.shape[2:], 255, dtype=img.dtype)])


def read_image(data: bytes) -> Statement:
    """Decode and OCR one screenshot.  CPU-bound (~2 s on the server) and
    serialised: callers run it in a worker thread."""
    # Whatever the operator sent, this returns a Statement: empty input and a
    # non-image both reach the warning path, which Telegram callers report as
    # an unreadable screenshot rather than a crash.
    img = _decode(data)
    if img is None:
        return _undecodable()
    img = _pad_for_detection(img)
    with _ocr_lock:
        result, _elapse = _engine()(img)
    stmt = parse_boxes(result or [], img.shape[1])
    stmt.reader = reader_name()
    return stmt


# ---- text for the bot ----

# One label per kind, plus the words a line borrows when its kind is not what
# the operator needs to read: an adjustment against a trip the system cancelled
# says 已取消, a fine on a trip another batch holds says 判罰.  "cancelled" is
# such a borrowed word rather than a kind of its own.
_KIND_LABEL = {
    "adjustment": "判罰",
    "amount_diff": "金額唔同",
    "already_settled": "已結算",
    "cancelled": "已取消",
    "not_ready": "未可結算",
    "penalty": "判罰",
    "unknown": "唔喺系統",
}

# What every recorded line ends with: the operator is agreeing to a figure, so
# the card has to say which lines the same tap writes into the batch.
_RECORDED = "— 記入今次"


def short_id(order_id: str) -> str:
    return "#…" + order_id[-4:]


def money_str(v: float) -> str:
    """An amount, exact to the cent, sign outside the currency symbol.

    "$-97.38" reads as a corrupted figure; "−$97.38" reads as money taken off.
    """
    sign = "−" if v < 0 else ""
    v = abs(v)
    return f"{sign}${v:,.0f}" if float(v).is_integer() else f"{sign}${v:,.2f}"


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


def _adjustment_detail(e: Entry) -> str:
    """What a line the batch records itself does to the transfer.

    The platform prints a fine and the 免責 line that cancels it as two rows
    under one order number, and the category chip that tells them apart is
    unreadable, so the offsetting figure is named by what it does to the money
    rather than by what the platform called it.
    """
    if e.penalty < 0:
        offset = round(e.platform_amount - e.penalty, 2)
        if abs(offset) < 0.005:
            return _signed(e.penalty)
        return f"{_signed(e.penalty)} · 抵銷 {_signed(offset)} · 淨 {_signed(e.platform_amount)}"
    return money_str(e.platform_amount)


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
    # Which lines the confirm actually writes, rather than which ones qualify:
    # a statement no batch can come out of records nothing.
    recorded = {a["order_ref"] for a in rec.adjustments}
    for day in rec.days:
        problems = [e for e in by_date.get(day.date, []) if e.kind != "matched"]
        held = missing_by_date.get(day.date, [])
        day_sum = day.sum if day.sum is not None else round(sum(r.amount for r in day.rows), 2)
        mark = " ✓" if not problems and not held and rec.checksum == "ok" else ""
        lines.append(f"{_md(day.date)} · {len(day.rows)} 行 · {money_str(day_sum)}{mark}")
        for e in problems:
            label = _KIND_LABEL[e.kind]
            kept = _RECORDED if e.order_id in recorded else ""
            if e.kind == "adjustment":
                # A cancelled trip is the fact worth leading with; every other
                # adjustment is money taken off the transfer.
                if e.order is not None:
                    label = _KIND_LABEL["cancelled"]
                detail = f"{_adjustment_detail(e)} {kept}".rstrip()
            elif e.kind == "penalty":
                gross = round(e.platform_amount - e.penalty, 2)
                detail = (f"{_signed(e.penalty)} · 該程 {money_str(gross)} → "
                          f"淨 {money_str(e.platform_amount)}")
            elif e.kind == "amount_diff":
                detail = f"平台 {money_str(e.platform_amount)} · 系統 {money_str(e.expected)}"
                if e.penalty < 0:
                    detail += f"（內含判罰 {_signed(e.penalty)}）"
            elif _late_fine(e):
                # A fine on an order whose batch is already frozen, and the
                # platform's figure says it is not one this system has taken
                # off yet — re-reading a statement that was confirmed lands in
                # the plain branch below, because there the two figures agree.
                # The frozen batch stays untouched: the money left this
                # transfer, so this batch records it.  Without a batch to
                # record it on the line can only name where the trip went.
                label = _KIND_LABEL["penalty"]
                detail = (f"{_signed(e.penalty)}（單已喺批次 #{e.settlement_id}）"
                          + (kept or "— 要人手處理"))
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


def confirm_label(rec: Reconciliation, credit: bool = False,
                  short: tuple[float, float] | None = None) -> str:
    """The button that writes the batch, stating the scale of what it writes.

    The same tap also records every 判罰賠款 the statement carries, so the verb
    names that too: money leaving an order is not something to discover after
    the fact.  The lines the batch carries itself are named for the same
    reason.  `credit` when a bank credit matched the statement's total: the
    same tap allocates it, and the label has to say so before it is pressed.
    `short` is (what would be allocated, what would still be owed) when the
    credit does not cover the statement — the amounts replace the batch's own
    figures because agreeing to a part payment is the decision being taken.
    """
    n = len(rec.settle_ids)
    amount = money_str(rec.confirmed or 0.0)
    verb = (("確認結算" if rec.clean else "照平台數確認")
            + (" + 記判罰" if penalties_of(rec) else "")
            + (" + 記帳項" if rec.adjustments else "")
            + (" + 對入數" if credit else ""))
    if short is not None:
        return f"{verb} {money_str(short[0])}（差 {money_str(short[1])}）"
    if rec.clean:
        return f"{verb} · {n} 程 · {amount}"
    return f"{verb} · {n} 程 · {amount}（差額 {_signed(rec.diff)}）"


def settled_reply(settlement_id: int, rec: Reconciliation, dates: list[str]) -> str:
    head = (f"已結算 批次 #{settlement_id} · {date_span_label(dates)} · {len(rec.settle_ids)} 程 · "
            f"{money_str(rec.confirmed or 0.0)}")
    # A fine is written by the same tap that writes the batch, so the receipt
    # for that tap has to name it.
    fines = penalties_of(rec)
    if fines:
        head += f"\n已記判罰 {_signed(-round(sum(fines.values()), 2))}"
    # The lines the batch carries itself move the same money and are written by
    # the same tap, so the receipt names them apart from the order-level fines.
    if rec.adjustments:
        total = round(sum(a["amount"] for a in rec.adjustments), 2)
        head += f"\n已記帳項 {_signed(total)} · {len(rec.adjustments)} 行"
    return f"{head}\n\n{confirmation_line(rec, dates)}"


def confirmation_line(rec: Reconciliation, dates: list[str]) -> str:
    """One line to paste back to the platform.

    The figure keeps its cents: this line is quoted back as the amount agreed,
    so it must equal the statement's total to the cent, not a rounded version
    of it.  Only the currency symbol is dropped — a statement whose penalties
    outweigh its fares is negative, and its sign is part of the figure."""
    return (f"{date_span_label(dates)} 共{len(rec.settle_ids)}程 "
            f"HKD {money_str(rec.confirmed or 0.0).replace('$', '')} 確認無誤")


def batch_head(batch: dict) -> str:
    """A batch named without a figure: which one, which days, how many legs."""
    dates = sorted({o["scheduled_time"][:10] for o in batch["orders"]})
    return f"#{batch['id']} · {date_span_label(dates)} · {len(batch['orders'])} 程"


def batch_label(batch: dict) -> str:
    """A batch as a button.

    The figure is what the batch is still owed, not what it was worth: a batch
    paid short is offered for the difference, which is the only part any credit
    can still pay.
    """
    return f"{batch_head(batch)} · {money_str(batch['outstanding'])}"


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
