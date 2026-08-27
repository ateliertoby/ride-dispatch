"""Bank credits (入數): feed ingestion and the credit ↔ batch matcher.

A batch is paid when, and only when, a bank credit is linked to it, and the
only thing that links is a tap: everything here proposes.  Nothing in this
module writes to settlements, so a matcher that grows a new rule cannot start
moving money on its own.

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

from .db import (awaiting_batches, get_credit, get_settlement, insert_credit,
                 unallocated_credits)
from .service import PLATFORMS
from .statement import awaiting_label, money_str

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

_REQUIRED = ("ref", "platform", "amount", "value_date")

_feed_seen: dict[str, tuple[int, int]] = {}
_feed_missing_logged: set[str] = set()


def _same(a: float, b: float) -> bool:
    return abs(a - b) < 0.005


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
    one combination that sums to the credit.  Nothing acts on it without a tap.
    """
    exact: list[int] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    reason: str = "none"


def _by_anchor(batches: list[dict]) -> list[dict]:
    return sorted(batches, key=lambda b: (anchor(b) or "", b["id"]), reverse=True)


def _candidates(batches: list[dict], remaining: float) -> list[dict]:
    """Batches worth offering: only those a link would not overshoot, newest first."""
    return _by_anchor([b for b in batches
                       if b["confirmed_amount"] <= remaining + 0.005])[:MAX_CANDIDATES]


def _lead_with(leading: list[dict], rest: list[dict]) -> list[dict]:
    """`leading` first, then whatever `rest` adds, deduped and capped.

    Used to float the right amount on the wrong date to the top of a card: it
    is not evidence enough to link, but it is the likeliest tap.
    """
    seen = {x["id"] for x in leading}
    return (leading + [x for x in rest if x["id"] not in seen])[:MAX_CANDIDATES]


def match_credit(credit: dict, awaiting: list[dict]) -> Match:
    """What this credit could pay for, best proposal first.

    A near miss is never proposed as the answer: an amount that does not agree
    to the cent is a question about a fee or a held-back order, so it lands
    among the alternatives instead.
    """
    remaining = credit["remaining"]
    pool = [b for b in awaiting if b["platform"] == credit["platform"]]
    fits = _candidates(pool, remaining)
    same_amount = [b for b in pool if _same(b["confirmed_amount"], remaining)]
    exact = _by_anchor([b for b in same_amount if in_window(anchor(b), credit["value_date"])])
    if exact:
        return Match(exact=[b["id"] for b in exact], candidates=fits,
                     reason="exact" if len(exact) == 1 else "ambiguous")
    # A Monday transfer covers a weekend's statements, so a combination that
    # sums to the credit is a real payment rather than a coincidence — but only
    # when it is the sole combination that does.
    windowed = [b for b in pool if in_window(anchor(b), credit["value_date"])]
    hits = []
    for size in range(2, min(MAX_SUBSET, len(windowed)) + 1):
        for combo in combinations(windowed, size):
            if _same(sum(b["confirmed_amount"] for b in combo), remaining):
                hits.append(combo)
    if len(hits) == 1:
        return Match(exact=sorted(b["id"] for b in hits[0]), candidates=fits, reason="subset")
    if hits:
        union = {b["id"]: b for combo in hits for b in combo}
        return Match(candidates=_lead_with(_candidates(list(union.values()), remaining), fits),
                     reason="ambiguous")
    # Nothing agrees.  A batch of exactly this amount on the wrong date leads
    # the card: it is the one the operator most likely wants, and letting them
    # say so is the whole difference between offering and assuming.
    return Match(candidates=_lead_with(_by_anchor(same_amount), fits), reason="none")


def _by_value_date(credits: list[dict]) -> list[dict]:
    return sorted(credits, key=lambda c: (c["value_date"], c["id"]), reverse=True)


def match_batch(batch: dict, unallocated: list[dict]) -> Match:
    """Which credit could have paid this batch.

    The mirror of match_credit with one rule fewer: a batch is paid by one
    credit, so there is nothing to combine.
    """
    amount = batch["confirmed_amount"]
    pool = [c for c in unallocated if c["platform"] == batch["platform"]]
    fits = _by_value_date([c for c in pool
                           if c["remaining"] >= amount - 0.005])[:MAX_CANDIDATES]
    same_amount = _by_value_date([c for c in pool if _same(c["remaining"], amount)])
    exact = [c for c in same_amount if in_window(anchor(batch), c["value_date"])]
    if exact:
        return Match(exact=[c["id"] for c in exact], candidates=fits,
                     reason="exact" if len(exact) == 1 else "ambiguous")
    return Match(candidates=_lead_with(same_amount, fits), reason="none")


def propose_credit(db_path: str, credit_id: int) -> Match:
    """What the batches waiting for money say about this credit.  Never writes."""
    credit = get_credit(db_path, credit_id)
    if credit is None or credit["archived_reason"] or credit["remaining"] <= 0.005:
        return Match()
    return match_credit(credit, awaiting_batches(db_path, credit["platform"]))


def propose_batch(db_path: str, settlement_id: int) -> Match:
    """What the ledger says about a batch that is waiting for money.  Never writes."""
    batch = get_settlement(db_path, settlement_id)
    if batch is None or batch["bank_credit_id"] is not None:
        return Match()
    return match_batch(batch, unallocated_credits(db_path, batch["platform"]))


def propose_statement(db_path: str, platform: str, confirmed_amount: float,
                      statement_json: dict) -> Match:
    """What the ledger says about a statement whose batch does not exist yet.

    The card has to name the credit before anything is written, so the
    statement stands in for the batch it is about to become: its settle dates
    are the anchor and its total is the amount.  A statement whose total could
    not be read has nothing to match on and matches nothing.
    """
    if confirmed_amount <= 0.005:
        return Match()
    batch = {"id": 0, "platform": platform, "confirmed_amount": confirmed_amount,
             "statement": statement_json, "orders": []}
    return match_batch(batch, unallocated_credits(db_path, platform))


def offer(m: Match, pool: list[dict]) -> list[dict]:
    """The proposals a card shows: what agrees to the cent first, then the rest.

    Both halves are buttons the operator may tap; leading with `exact` says
    which one the matcher believes without acting on the belief.
    """
    by_id = {x["id"]: x for x in pool}
    return _lead_with([by_id[i] for i in m.exact if i in by_id], m.candidates)


# ---- text for the bot ----

# The bank's value date, short: the year is never in question and the operator
# reads these beside the platform's own MM-DD figures.
def md(date: str) -> str:
    return f"{date[5:7]}-{date[8:10]}"


def _batch_line(batch: dict) -> str:
    return f"批次 {awaiting_label(batch)}"


def credit_head(credit: dict) -> str:
    head = f"入數 {money_str(credit['amount'])} · {md(credit['value_date'])}"
    if credit["remaining"] <= 0.005:
        return head + " · 已全部對齊"
    if credit["linked"] > 0.005:
        return head + f" · 已對 {money_str(credit['linked'])} · 剩 {money_str(credit['remaining'])}"
    return head + " · 未對"


def credit_label(credit: dict) -> str:
    """A credit as a button: what landed, when, and what is left of it."""
    label = f"入數 {money_str(credit['amount'])} · {md(credit['value_date'])}"
    if credit["linked"] > 0.005:
        label += f" · 剩 {money_str(credit['remaining'])}"
    return label


def credit_card_text(credit: dict, m: Match, offered: list[dict]) -> str:
    """The card for one credit: what the matcher believes, and what it offers.

    A believed match still needs the tap, so the head asks rather than states.
    """
    if credit["remaining"] <= 0.005:
        return credit_head(credit)
    if m.exact and m.reason in ("exact", "subset"):
        head = f"入數 {money_str(credit['amount'])} · {md(credit['value_date'])}"
        if credit["linked"] > 0.005:
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


def settled_link_line(credit: dict) -> str:
    """What a just-settled batch says about the credit it was born linked to."""
    return f"已對 {md(credit['value_date'])} 入數 {money_str(credit['amount'])}"


QUEUE_LIMIT = 20


def queue_text(pending: list[dict]) -> str:
    """The work queue: what the bank has paid that no batch accounts for yet."""
    if not pending:
        return "全部對齊。"
    total = round(sum(c["remaining"] for c in pending), 2)
    lines = [f"未對 {len(pending)} 筆 · {money_str(total)} · 最舊 {md(pending[0]['value_date'])}"]
    for c in pending[:QUEUE_LIMIT]:
        line = f"#{c['id']} · {md(c['value_date'])} · {money_str(c['amount'])}"
        if c["linked"] > 0.005:
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
    batches = [b for b in (get_settlement(db_path, i) for i in credit["settlement_ids"]) if b]
    lines += [f"已對：{_batch_line(b)}" for b in batches] or ["未對"]
    lines.append(f"剩 {money_str(credit['remaining'])}")
    if credit["archived_reason"]:
        lines.append(f"已收埋（{credit['archived_reason']}）")
    return "\n".join(lines)


def backfill_summary(total: int, exact_ids: list[int]) -> str:
    """One line for a backfill: dozens of cards would bury the chat, and the
    work queue is the place to deal with them.

    It reports what the matcher found, not what it did — a backfill links
    nothing, so the count is an invitation to work the queue.
    """
    found = f"{len(exact_ids)} 筆有啱數嘅 batch"
    if exact_ids:
        found += "（" + "、".join(f"#{i}" for i in exact_ids) + "）"
    return f"入咗 {total} 筆入數紀錄 · {found} · /credits 睇"
