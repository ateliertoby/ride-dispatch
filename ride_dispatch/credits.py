"""Bank credits (入數): feed ingestion and the credit ↔ batch matcher.

A batch is paid by money allocated to it, and the only thing that allocates is
a tap: everything here proposes.  Nothing in this module writes to settlements,
so a matcher that grows a new rule cannot start moving money on its own.

The matcher is pure so both directions — a credit arriving from the feed, a
statement being read before its batch exists — share one set of rules and one
set of tests.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import combinations

from .db import (get_credit, get_settlement, insert_credit, open_batches,
                 unallocated_credits)
from .service import PLATFORMS
from .statement import batch_head, batch_label, leg_amount, money_str

logger = logging.getLogger("credits")

# The platform pays a statement two days after its settle date, and holds a
# weekend's statements for the Monday transfer: a week covers every observed
# gap without reaching back into the batches before it.
#
# The window is what separates a match from a coincidence, not a tie-breaker.
# Amounts here are round hundreds and the ledger holds months of them, so a
# lone amount that agrees to the cent is a coincidence more often than a
# payment unless the dates agree too.  A match inside the window is proposed
# as the answer; one outside it is only offered among the alternatives.
WINDOW_DAYS = 7
MAX_CANDIDATES = 8
# One transfer has never covered more than a long weekend of statements, and
# the combination search is exponential: past four the cost buys nothing.
MAX_SUBSET = 4
# A card the operator has to scroll is a card he taps the wrong row of; the
# part payments are a shortlist, the rest of the pool is behind them.
MAX_SHORT = 4
# guess_unpaid tests subsets up to this size and returns at most this many.
MAX_GUESS_SIZE = 5
MAX_GUESSES = 8

# Money is compared to the cent everywhere: a difference above this is a
# question for the operator, never a rounding artefact to absorb.
CENT = 0.005

_REQUIRED = ("ref", "platform", "amount", "value_date")

_feed_seen: dict[str, tuple[int, int]] = {}
_feed_missing_logged: set[str] = set()


def _same(a: float, b: float) -> bool:
    return abs(a - b) < CENT


# ---- the feed ----

def feed_changed(path: str) -> bool:
    """True when the file's (mtime, size) differs from the last call.

    The bot asks every heartbeat, so a stat per minute is the whole cost of a
    quiet feed.  A missing file is logged once per disappearance rather than
    once per tick.
    """
    try:
        st = os.stat(path)
    except OSError:
        if path not in _feed_missing_logged:
            logger.warning("feed not readable: %s", path)
            _feed_missing_logged.add(path)
        return False
    _feed_missing_logged.discard(path)
    key = (st.st_mtime_ns, st.st_size)
    if _feed_seen.get(path) == key:
        return False
    _feed_seen[path] = key
    return True


def forget_feed(path: str) -> None:
    """Drop the seen mark so the next tick reads the file again.

    feed_changed records a file as seen before its contents are used, so a
    reader that fails afterwards has to undo that or the lines it never
    processed wait for the next append that may be days away.
    """
    _feed_seen.pop(path, None)


def ingest_feed(db_path: str, path: str) -> list[dict]:
    """Read the whole feed, insert unknown refs, return the credits that were new.

    Re-reading the whole file is cheap (a few hundred lines a year) and needs no
    offset state on this side: ref is unique, so a re-read is a no-op.  A
    trailing fragment without its newline is a line the producer is still
    writing; it is skipped and picked up complete on a later tick.  A line that
    cannot be used is logged with its number and never raises: one corrupt line
    must not stop the ledger.
    """
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if not text.endswith("\n"):
        text = text[: text.rfind("\n") + 1]
    new = []
    for n, raw in enumerate(text.split("\n"), start=1):
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            logger.warning("feed line %d: not JSON", n)
            continue
        if d.get("v") != 1:
            logger.warning("feed line %d: unknown version %r", n, d.get("v"))
            continue
        if any(k not in d for k in _REQUIRED):
            logger.warning("feed line %d: missing field", n)
            continue
        if d["platform"] not in PLATFORMS:
            logger.warning("feed line %d: unknown platform %r", n, d["platform"])
            continue
        credit_id = insert_credit(db_path, {
            "ref": str(d["ref"]), "platform": d["platform"], "amount": float(d["amount"]),
            "currency": d.get("currency", "HKD"), "value_date": d["value_date"],
            "payer": d.get("payer"), "memo": d.get("memo"), "email_id": d.get("email_id"),
            "received_at": d.get("received_at"), "recorded_at": d.get("recorded_at"),
        })
        if credit_id is not None:
            new.append(get_credit(db_path, credit_id))
    return new


# ---- the matcher ----

def anchor(batch: dict) -> str | None:
    """The date the platform paid a batch against, or None when it has no date.

    Its statement's latest settle date when it has one, else its latest service
    date: a held-back order's service date is already stale by the time the
    statement carrying it is paid.  A statement read before its batch exists
    carries no orders, which is why the statement is asked first.
    """
    stmt = batch.get("statement") or {}
    settle_dates = [r.get("settle_date") for day in stmt.get("days", [])
                    for r in day.get("rows", []) if r.get("settle_date")]
    if settle_dates:
        return max(settle_dates)
    orders = batch.get("orders") or []
    if orders:
        return max(o["scheduled_time"][:10] for o in orders)
    return None


def in_window(anchor_date: str | None, value_date: str) -> bool:
    # No date at all is outside every window: the dates have to agree, and a
    # batch that cannot say when it was earned never can.
    if anchor_date is None:
        return False
    v = datetime.strptime(value_date, "%Y-%m-%d")
    a = datetime.strptime(anchor_date, "%Y-%m-%d")
    return v - timedelta(days=WINDOW_DAYS) <= a <= v


@dataclass
class Match:
    """A proposal, never an instruction.

    `exact` holds the ids that agree to the cent inside the window (batches
    from match_credit, credits from match_batch); for reason 'subset' it is the
    one combination that sums to the credit.  `short` holds the ones that would
    leave the other side still owed money — the platform pays a statement short
    when it failed to submit legs of its own, so a proposal that does not cover
    everything is a real answer rather than a failure to find one.  Nothing
    acts on either without a tap.
    """
    exact: list[int] = field(default_factory=list)
    short: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    reason: str = "none"


def _by_anchor(batches: list[dict]) -> list[dict]:
    return sorted(batches, key=lambda b: (anchor(b) or "", b["id"]), reverse=True)


def _lead_with(leading: list[dict], rest: list[dict]) -> list[dict]:
    """`leading` first, then whatever `rest` adds, deduped and capped."""
    seen = {x["id"] for x in leading}
    return (leading + [x for x in rest if x["id"] not in seen])[:MAX_CANDIDATES]


def _others(pool: list[dict], taken: list[dict], order) -> list[dict]:
    """Everything the proposals did not take, best first.

    Nothing is filtered out by amount any more: money is allocated in amounts,
    so a batch bigger than the credit is a part payment rather than an
    impossible one, and a batch smaller than it leaves change behind.
    """
    seen = {x["id"] for x in taken}
    return order([x for x in pool if x["id"] not in seen])[:MAX_CANDIDATES]


def match_credit(credit: dict, batches: list[dict]) -> Match:
    """What this credit could pay for, best proposal first.

    A near miss is never proposed as the answer: an amount that does not agree
    to the cent is a question about a fee or a held-back order, so it lands
    among the alternatives instead.
    """
    remaining = credit["remaining"]
    pool = [b for b in batches
            if b["platform"] == credit["platform"] and b["outstanding"] > CENT]
    windowed = [b for b in pool if in_window(anchor(b), credit["value_date"])]
    exact = _by_anchor([b for b in windowed if _same(b["outstanding"], remaining)])
    # A batch this credit cannot cover is the short-payment case: the tap puts
    # the whole credit against it and the batch stays owed the difference.
    short = _by_anchor([b for b in windowed
                        if b["outstanding"] > remaining + CENT])[:MAX_SHORT]
    if exact:
        return Match(exact=[b["id"] for b in exact], short=short,
                     candidates=_others(pool, exact + short, _by_anchor),
                     reason="exact" if len(exact) == 1 else "ambiguous")
    # A Monday transfer covers a weekend's statements, so a combination that
    # sums to the credit is a real payment rather than a coincidence — but only
    # when it is the sole combination that does.
    hits = []
    for size in range(2, min(MAX_SUBSET, len(windowed)) + 1):
        for combo in combinations(windowed, size):
            if _same(sum(b["outstanding"] for b in combo), remaining):
                hits.append(combo)
    if len(hits) == 1:
        combo = list(hits[0])
        return Match(exact=sorted(b["id"] for b in combo), short=short,
                     candidates=_others(pool, combo + short, _by_anchor), reason="subset")
    if hits:
        union = {b["id"]: b for combo in hits for b in combo}
        return Match(short=short,
                     candidates=_lead_with(_others(list(union.values()), short, _by_anchor),
                                           _others(pool, short, _by_anchor)),
                     reason="ambiguous")
    return Match(short=short, candidates=_others(pool, short, _by_anchor), reason="none")


def _by_value_date(credits: list[dict]) -> list[dict]:
    return sorted(credits, key=lambda c: (c["value_date"], c["id"]), reverse=True)


def match_batch(batch: dict, unallocated: list[dict]) -> Match:
    """Which credit could have paid this batch.

    The mirror of match_credit with one rule fewer: there is nothing to
    combine, because a credit larger than the batch is an ordinary candidate —
    allocating takes only what the batch is owed and the rest stays on the
    credit for whatever else it paid for.
    """
    outstanding = batch["outstanding"]
    pool = [c for c in unallocated
            if c["platform"] == batch["platform"] and c["remaining"] > CENT]
    windowed = [c for c in pool if in_window(anchor(batch), c["value_date"])]
    exact = _by_value_date([c for c in windowed if _same(c["remaining"], outstanding)])
    short = _by_value_date([c for c in windowed
                            if c["remaining"] < outstanding - CENT])[:MAX_SHORT]
    if exact:
        return Match(exact=[c["id"] for c in exact], short=short,
                     candidates=_others(pool, exact + short, _by_value_date),
                     reason="exact" if len(exact) == 1 else "ambiguous")
    return Match(short=short, candidates=_others(pool, short, _by_value_date), reason="none")


def propose_credit(db_path: str, credit_id: int) -> Match:
    """What the batches waiting for money say about this credit.  Never writes."""
    credit = get_credit(db_path, credit_id)
    if credit is None or credit["archived_reason"] or credit["remaining"] <= CENT:
        return Match()
    return match_credit(credit, open_batches(db_path, credit["platform"]))


def propose_batch(db_path: str, settlement_id: int) -> Match:
    """What the ledger says about a batch that is still owed money.  Never writes."""
    batch = get_settlement(db_path, settlement_id)
    if batch is None or batch["outstanding"] <= CENT:
        return Match()
    return match_batch(batch, unallocated_credits(db_path, batch["platform"]))


def propose_statement(db_path: str, platform: str, confirmed_amount: float,
                      statement_json: dict) -> Match:
    """What the ledger says about a statement whose batch does not exist yet.

    The card has to name the credit before anything is written, so the
    statement stands in for the batch it is about to become: its settle dates
    are the anchor and its total is what nothing has paid yet.  A statement
    whose total could not be read has nothing to match on and matches nothing.
    """
    if confirmed_amount <= CENT:
        return Match()
    batch = {"id": 0, "platform": platform, "confirmed_amount": confirmed_amount,
             "outstanding": confirmed_amount, "statement": statement_json, "orders": []}
    return match_batch(batch, unallocated_credits(db_path, platform))


def offer(m: Match, pool: list[dict]) -> list[dict]:
    """The proposals a card shows: what agrees to the cent, then what would pay
    part of it, then the rest.

    Every one of them is a button the operator may tap; leading with `exact`
    says which one the matcher believes without acting on the belief.
    """
    by_id = {x["id"]: x for x in pool}
    return _lead_with([by_id[i] for i in m.exact if i in by_id] + list(m.short), m.candidates)


# ---- text for the bot ----

# The bank's value date, short: the year is never in question and the operator
# reads these beside the platform's own MM-DD figures.
def md(date: str) -> str:
    return f"{date[5:7]}-{date[8:10]}"


def _slash(date: str) -> str:
    """A service date as the platform's own statement writes it: 8/22."""
    return f"{int(date[5:7])}/{int(date[8:10])}"


def _tail(order_id: str) -> str:
    return "…" + order_id[-4:]


def offer_batch_label(batch: dict, remaining: float | None = None) -> str:
    """A batch as a button, saying what a tap would actually do.

    A credit that cannot cover the batch says so on the button: the operator is
    agreeing to a part payment, and the difference is what he then has to
    account for leg by leg.
    """
    outstanding = batch["outstanding"]
    if remaining is None or remaining >= outstanding - CENT:
        return batch_label(batch)
    return (f"{batch_head(batch)} · 對 {money_str(remaining)}"
            f"（差 {money_str(round(outstanding - remaining, 2))}）")


def credit_label(credit: dict, outstanding: float | None = None) -> str:
    """A credit as a button: what landed, when, and what is left of it."""
    label = f"入數 {money_str(credit['amount'])} · {md(credit['value_date'])}"
    if credit["allocated"] > CENT:
        label += f" · 剩 {money_str(credit['remaining'])}"
    if outstanding is not None and credit["remaining"] < outstanding - CENT:
        label += f"（差 {money_str(round(outstanding - credit['remaining'], 2))}）"
    return label


def credit_head(credit: dict) -> str:
    head = f"入數 {money_str(credit['amount'])} · {md(credit['value_date'])}"
    if credit["remaining"] <= CENT:
        return head + " · 已全部對齊"
    if credit["allocated"] > CENT:
        return head + f" · 已對 {money_str(credit['allocated'])} · 剩 {money_str(credit['remaining'])}"
    return head + " · 未對"


def credit_card_text(credit: dict, m: Match, offered: list[dict]) -> str:
    """The card for one credit: what the matcher believes, and what it offers.

    A believed match still needs the tap, so the head asks rather than states.
    """
    if credit["remaining"] <= CENT:
        return credit_head(credit)
    if m.exact and m.reason in ("exact", "subset"):
        head = f"入數 {money_str(credit['amount'])} · {md(credit['value_date'])}"
        if credit["allocated"] > CENT:
            head += f" · 剩 {money_str(credit['remaining'])}"
        head += " · 對到 批次 " + "、".join(f"#{i}" for i in m.exact) + "？"
        second = "撳確認："
    else:
        head = credit_head(credit)
        second = "等緊過數：" if offered else "冇 batch 啱銀碼"
    return "\n".join([head, second, "send 結算圖入嚟都會提議"])


# What a statement card says about the credit its total agrees with, before
# any batch exists: the operator reads this and the report above it together,
# and one tap confirms both.
def statement_match_text(credit: dict) -> str:
    return f"對到入數 {md(credit['value_date'])} {money_str(credit['amount'])}"


def statement_short_text(credit: dict, total: float) -> str:
    """The same, for money that does not cover the statement it arrived for.

    The platform pays short when its own system failed to submit some of the
    legs; the difference named here is what the operator then ticks off.
    """
    return f"{statement_match_text(credit)}，差 {money_str(round(total - credit['remaining'], 2))}"


def statement_offer_text(offered: list[dict]) -> str:
    return "\n".join(["入數可能係："] + [credit_label(c) for c in offered])


def no_orders_text(credit: dict, rows: int) -> str:
    """A statement whose money is in the ledger but whose legs are not in the
    system: there is nothing to settle, so the credit is what has to be dealt
    with."""
    return f"{statement_match_text(credit)}，但圖入面 {rows} 張單唔喺系統"


# The batch is about to be created against money that has not arrived, which
# is the ordinary case: the platform pays days after the statement.
NO_CREDIT_YET = "未收到呢筆數"


def part_paid_line(batch: dict) -> str:
    """What a batch has and has not been paid, once some of its money is in."""
    return f"已收 {money_str(batch['received'])} · 未收 {money_str(batch['outstanding'])}"


def completed_text(batch: dict, cleared: list[str]) -> str:
    """A batch reaching zero outstanding, and the legs that were owed with it.

    The legs are named because they are what the operator chased the platform
    over: seeing the batch close without them would leave that open.
    """
    line = f"批次 #{batch['id']} 收齊 · 已到帳 {md(batch['paid_on'])}"
    if cleared:
        line += "\n到帳：" + "、".join(_tail(o) for o in cleared)
    return line


def leftover_text(credit: dict) -> str:
    """The change left on a credit, offered against whatever else is owed.

    This is how a make-up payment bundled into a bigger transfer reaches the
    batch it belongs to: the tap that spends the first part of the credit is
    also what asks about the rest.
    """
    return f"剩 {money_str(credit['remaining'])} · 可能係："


# ---- the unpaid legs of a short-paid batch ----

def short_allocation_line(batch: dict) -> str:
    """What Telegram says after a short allocation: money is in, not all of it,
    and the legs are named on the dashboard — not here."""
    return (f"已收 {money_str(batch['received'])} · "
            f"未收 {money_str(batch['outstanding'])} · "
            "平台查完喺 dashboard 入返邊張單")


def guess_unpaid(batch: dict) -> list[list[str]]:
    """Subsets of legs whose platform amounts equal the outstanding to the cent.

    Only meaningful for a partial batch (some money received, some still owed);
    returns [] for anything else.  Each result is a sorted list of order ids;
    results are ordered smallest subset first, then lexicographically by ids,
    capped at MAX_GUESSES.  A batch of 15 legs with subsets up to size 5 is
    ~5,000 combinations — fine on request.
    """
    if batch.get("outstanding", 0) <= CENT or batch.get("state") != "partial":
        return []
    target = batch["outstanding"]
    orders = batch.get("orders") or []
    if not orders:
        return []
    items = [(o["order_id"], leg_amount(batch, o)) for o in orders]
    hits: list[list[str]] = []
    for size in range(1, min(MAX_GUESS_SIZE, len(items)) + 1):
        for combo in combinations(items, size):
            if _same(sum(a for _, a in combo), target):
                hits.append(sorted(oid for oid, _ in combo))
                if len(hits) > MAX_GUESSES:
                    break
        if len(hits) > MAX_GUESSES:
            break
    hits.sort(key=lambda h: (len(h), h))
    return hits[:MAX_GUESSES]


QUEUE_LIMIT = 20


def queue_text(pending: list[dict]) -> str:
    """The work queue: what the bank has paid that no batch accounts for yet."""
    if not pending:
        return "全部對齊。"
    total = round(sum(c["remaining"] for c in pending), 2)
    lines = [f"未對 {len(pending)} 筆 · {money_str(total)} · 最舊 {md(pending[0]['value_date'])}"]
    for c in pending[:QUEUE_LIMIT]:
        line = f"#{c['id']} · {md(c['value_date'])} · {money_str(c['amount'])}"
        if c["allocated"] > CENT:
            line += f" · 剩 {money_str(c['remaining'])}"
        lines.append(line)
    if len(pending) > QUEUE_LIMIT:
        lines.append(f"…仲有 {len(pending) - QUEUE_LIMIT} 筆")
    return "\n".join(lines)


def detail_text(credit: dict, db_path: str) -> str:
    head = f"入數 #{credit['id']} · {money_str(credit['amount'])} · {md(credit['value_date'])}"
    if credit["memo"]:
        head += f" · {credit['memo']}"
    lines = [head]
    lines += [f"已對：批次 #{a['settlement_id']} {money_str(a['amount'])}"
              for a in credit["allocations"]] or ["未對"]
    lines.append(f"剩 {money_str(credit['remaining'])}")
    if credit["archived_reason"]:
        lines.append(f"已收埋（{credit['archived_reason']}）")
    return "\n".join(lines)


def backfill_summary(total: int, exact_ids: list[int]) -> str:
    """One line for a backfill: dozens of cards would bury the chat, and the
    work queue is the place to deal with them.

    It reports what the matcher found, not what it did — a backfill allocates
    nothing, so the count is an invitation to work the queue.
    """
    found = f"{len(exact_ids)} 筆有啱數嘅 batch"
    if exact_ids:
        found += "（" + "、".join(f"#{i}" for i in exact_ids) + "）"
    return f"入咗 {total} 筆入數紀錄 · {found} · /credits 睇"
