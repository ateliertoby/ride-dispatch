"""Bank credits (入數): feed ingestion, the credit ↔ batch matcher, and the
wrappers that perform links.

A batch is paid when, and only when, a bank credit is linked to it.  The
matcher is pure so both directions — a credit arriving from the feed, a batch
being created from a statement — share one set of rules and one set of tests.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import combinations

from .db import (awaiting_batches, get_credit, get_settlement, insert_credit,
                 link_credit, unallocated_credits)
from .service import PLATFORMS
from .statement import awaiting_label, date_span_label, money_str

logger = logging.getLogger("credits")

# The platform pays a statement two days after its settle date, and holds a
# weekend's statements for the Monday transfer: a week covers every observed
# gap without reaching back into the batches before it.
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

def anchor(batch: dict) -> str:
    """The date the platform paid a batch against.

    Its statement's latest settle date when it has one, else its latest service
    date: a held-back order's service date is already stale by the time the
    statement carrying it is paid.
    """
    stmt = batch.get("statement") or {}
    settle_dates = [r.get("settle_date") for day in stmt.get("days", [])
                    for r in day.get("rows", []) if r.get("settle_date")]
    if settle_dates:
        return max(settle_dates)
    return max(o["scheduled_time"][:10] for o in batch["orders"])


def in_window(anchor_date: str, value_date: str) -> bool:
    v = datetime.strptime(value_date, "%Y-%m-%d")
    a = datetime.strptime(anchor_date, "%Y-%m-%d")
    return v - timedelta(days=WINDOW_DAYS) <= a <= v


@dataclass
class Match:
    linked: list[int] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    reason: str = "none"


def _candidates(batches: list[dict], remaining: float) -> list[dict]:
    """Batches worth offering: only those a link would not overshoot, newest first."""
    fit = [b for b in batches if b["confirmed_amount"] <= remaining + 0.005]
    fit.sort(key=lambda b: (anchor(b), b["id"]), reverse=True)
    return fit[:MAX_CANDIDATES]


def match_credit(credit: dict, awaiting: list[dict]) -> Match:
    """What this credit pays for, or what to offer the operator.

    A near miss is never a link: an amount that does not agree to the cent is a
    question about a fee or a held-back order, and it gets a card.
    """
    remaining = credit["remaining"]
    pool = [b for b in awaiting if b["platform"] == credit["platform"]]
    exact = [b for b in pool if _same(b["confirmed_amount"], remaining)]
    if len(exact) > 1:
        # Same figure twice is common (a batch is often one day's work): the
        # window is what tells this month's batch from the identical old one.
        windowed = [b for b in exact if in_window(anchor(b), credit["value_date"])]
        exact = windowed or exact
    if len(exact) == 1:
        return Match(linked=[exact[0]["id"]], reason="exact")
    if len(exact) > 1:
        return Match(candidates=_candidates(exact, remaining), reason="ambiguous")
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
        return Match(linked=sorted(b["id"] for b in hits[0]), reason="subset")
    if hits:
        union = {b["id"]: b for combo in hits for b in combo}
        return Match(candidates=_candidates(list(union.values()), remaining), reason="ambiguous")
    return Match(candidates=_candidates(pool, remaining), reason="none")


def match_batch(batch: dict, unallocated: list[dict]) -> Match:
    """Which credit paid this batch, or which credits to offer.

    The mirror of match_credit with one rule fewer: a batch is paid by one
    credit, so there is nothing to combine.
    """
    amount = batch["confirmed_amount"]
    pool = [c for c in unallocated if c["platform"] == batch["platform"]]
    exact = [c for c in pool if _same(c["remaining"], amount)]
    if len(exact) > 1:
        windowed = [c for c in exact if in_window(anchor(batch), c["value_date"])]
        exact = windowed or exact
    if len(exact) == 1:
        return Match(linked=[exact[0]["id"]], reason="exact")
    fit = [c for c in pool if c["remaining"] >= amount - 0.005]
    fit.sort(key=lambda c: (c["value_date"], c["id"]), reverse=True)
    return Match(candidates=fit[:MAX_CANDIDATES], reason="ambiguous" if exact else "none")


def resolve_credit(db_path: str, credit_id: int) -> Match:
    """Match a credit against the batches waiting for money, linking if resolved."""
    credit = get_credit(db_path, credit_id)
    if credit is None or credit["archived_reason"] or credit["remaining"] <= 0.005:
        return Match()
    m = match_credit(credit, awaiting_batches(db_path, credit["platform"]))
    for settlement_id in m.linked:
        link_credit(db_path, credit_id, settlement_id)
    return m


def resolve_batch(db_path: str, settlement_id: int) -> Match:
    """Match a new batch against the credits already in the ledger, linking if resolved."""
    batch = get_settlement(db_path, settlement_id)
    if batch is None or batch["bank_credit_id"] is not None:
        return Match()
    m = match_batch(batch, unallocated_credits(db_path, batch["platform"]))
    for credit_id in m.linked:
        link_credit(db_path, credit_id, settlement_id)
    return m


def offer(m: Match, awaiting: list[dict]) -> list[dict]:
    """The batches a card should offer, given a match and the pool it came from.

    A resolved match carries its batches in `linked` and leaves `candidates`
    empty, so a card built from `candidates` alone would claim nothing fits
    while a batch matches the credit to the cent — which is what a hand-made
    partial link leaves behind.
    """
    if m.linked:
        by_id = {b["id"]: b for b in awaiting}
        return [by_id[i] for i in m.linked if i in by_id]
    return m.candidates


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


def credit_card_text(credit: dict, candidates: list[dict]) -> str:
    if credit["remaining"] <= 0.005:
        return credit_head(credit)
    second = "等緊過數：" if candidates else "冇 batch 啱銀碼"
    return "\n".join([credit_head(credit), second, "send 結算圖入嚟都會自動對"])


def settled_link_line(credit: dict) -> str:
    """What a just-settled batch says about the credit it was born linked to."""
    return f"已對 {md(credit['value_date'])} 入數 {money_str(credit['amount'])}"


def linked_line(credit: dict, linked_ids: list[int], db_path: str) -> str:
    """What the heartbeat says when a credit found its batches on its own."""
    batches = [b for b in (get_settlement(db_path, i) for i in linked_ids) if b]
    head = f"入數 {money_str(credit['amount'])} · {md(credit['value_date'])}"
    if len(batches) == 1:
        b = batches[0]
        dates = sorted({o["scheduled_time"][:10] for o in b["orders"]})
        return f"{head} 已對 批次 #{b['id']} · {date_span_label(dates)} · {len(b['orders'])} 程"
    return "\n".join([f"{head} 已對 {len(batches)} 個批次"] + [_batch_line(b) for b in batches])


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


def backfill_summary(total: int, auto_ids: list[int]) -> str:
    """One line for a backfill: dozens of cards would bury the chat, and the
    work queue is the place to deal with them."""
    auto = f"自動對到 {len(auto_ids)} 筆"
    if auto_ids:
        auto += "（" + "、".join(f"#{i}" for i in auto_ids) + "）"
    return f"入咗 {total} 筆入數紀錄 · {auto} · 未對 {total - len(auto_ids)} 筆 · /credits 睇"
