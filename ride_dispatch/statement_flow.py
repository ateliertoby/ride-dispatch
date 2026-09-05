"""What a platform statement means, and what confirming one does.

Two frontends read statements — the Telegram bot and the settle page — and
both do the same three things: reconcile the screenshot against the orders,
show the operator what it adds up to, and write the batch when he agrees.
Everything between the reader and each frontend's own card lives here, so a
statement cannot mean one thing in the chat and another in the browser, and
the text they print comes from one source rather than two that drift.

Nothing here talks to a frontend.  `prepare` reads and writes nothing;
`confirm` writes and asks nothing.  Both hand back text fragments the caller
arranges into whatever its own card looks like.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime

from . import credits
from . import statement
from .db import (allocate, create_settlement, get_credit, image_extension,
                 settlement_candidates, statements_dir, unallocated_credits)

# Statements are the ride platform's; the quick platforms settle per order and
# never produce one, and settlement_candidates only returns ride orders.
PLATFORM = "ride"

# The offer under a statement whose money is in the ledger but whose legs never
# reached the system: there is no batch to create, only a credit to take out of
# the queue.
NO_ORDERS_ARCHIVE_LABEL = "收埋入數（單未入系統）"


@dataclass
class Prepared:
    """A statement read and reconciled, with nothing written yet.

    `credit_id` is the credit a confirm would also allocate; `confirm_label`
    is empty exactly when no batch can come out of this statement, which is
    the same thing as `can_settle` being false.
    """
    rec: statement.Reconciliation
    stmt_json: dict
    dates: list[str]
    total: float
    credit_id: int | None
    short_pair: tuple[float, float] | None
    report: str
    credit_line: str
    confirm_label: str
    no_orders_credit: dict | None

    @property
    def can_settle(self) -> bool:
        return self.rec.can_settle


@dataclass
class Confirmed:
    """A batch written, and what the operator is told about it.

    The lines are kept apart rather than joined because the bot prints its
    credit-offer buttons between the allocation line and the short one.
    """
    settlement_id: int
    batch: dict | None
    credit_id: int | None
    settled_reply: str
    allocation_line: str
    short_line: str

    @property
    def text(self) -> str:
        return "\n".join(part for part in
                         (self.settled_reply, self.allocation_line, self.short_line) if part)


def _rows(rec: statement.Reconciliation) -> int:
    return sum(len(d.rows) for d in rec.days)


def prepare(db_path: str, stmt: statement.Statement, now: datetime) -> Prepared:
    """Reconcile a statement against the orders and the ledger, writing nothing."""
    dates = statement.dates_of(stmt)
    orders = settlement_candidates(db_path, dates, now)
    rec = statement.reconcile(stmt, orders, now)
    stmt_json = statement.corrected_json(stmt, rec)
    total = rec.confirmed or 0.0
    common = dict(rec=rec, stmt_json=stmt_json, dates=dates, total=total,
                  report=statement.format_report(rec))
    # The ledger is asked before anything is written: a batch has to trace back
    # to a statement, so the statement is where the operator is told which
    # credit it accounts for, and one confirm records both.
    m = credits.propose_statement(db_path, PLATFORM, total, stmt_json)
    matched = get_credit(db_path, m.exact[0]) if m.reason == "exact" else None
    # Exact beats short beats candidates: money that covers the statement
    # answers it, money that does not is the short-payment case, and anything
    # else is only a suggestion.
    short = m.short[0] if matched is None and m.short else None
    if not rec.can_settle:
        # No batch can come out of this statement.  A credit that agrees with
        # its total is money for legs the system never had, so the only offer
        # is taking that credit out of the queue.
        no_orders = matched if matched and not rec.settle_ids else None
        return Prepared(
            **common, credit_id=None, short_pair=None,
            credit_line=credits.no_orders_text(no_orders, _rows(rec)) if no_orders else "",
            confirm_label="", no_orders_credit=no_orders,
        )
    short_pair = None
    if matched:
        credit_line = credits.statement_match_text(matched)
    elif short:
        # The platform pays a statement short because its own system failed to
        # submit some of the legs; the confirm records the money that did
        # arrive and the batch stays owed the rest.
        credit_line = credits.statement_short_text(short, total)
        short_pair = (short["remaining"], round(total - short["remaining"], 2))
    else:
        offered = credits.offer(m, unallocated_credits(db_path, PLATFORM))
        credit_line = credits.statement_offer_text(offered) if offered else credits.NO_CREDIT_YET
    chosen = matched or short
    return Prepared(
        **common, credit_id=chosen["id"] if chosen else None, short_pair=short_pair,
        credit_line=credit_line,
        confirm_label=statement.confirm_label(rec, credit=chosen is not None, short=short_pair),
        no_orders_credit=None,
    )


def confirm(db_path: str, prepared: Prepared, image: bytes | None,
            now: datetime) -> Confirmed:
    """Write the batch the statement describes, and the credit it named.

    ValueError from create_settlement propagates: it names an order the batch
    cannot hold, and each frontend words that refusal in its own voice.
    """
    settlement_id = create_settlement(
        db_path, PLATFORM, prepared.rec.settle_ids, prepared.rec.confirmed or 0.0,
        now.strftime("%Y-%m-%d"), statement=prepared.stmt_json, image=image,
        penalties=statement.penalties_of(prepared.rec),
    )
    credit_id = prepared.credit_id
    batch = None
    allocation_line = ""
    # The card named this credit before the batch existed, so one confirm
    # records both.  The credit can have been spent in between, in which case
    # the batch still stands and the credit degrades to a note.
    if credit_id is not None:
        try:
            batch = allocate(db_path, credit_id, settlement_id)
            allocation_line = credits.allocation_line(batch, [])
        except ValueError as e:
            allocation_line = f"對唔到入數：{e}"
            credit_id = None
    return Confirmed(
        settlement_id=settlement_id, batch=batch, credit_id=credit_id,
        settled_reply=statement.settled_reply(settlement_id, prepared.rec, prepared.dates),
        allocation_line=allocation_line,
        short_line=(credits.short_allocation_line(batch)
                    if batch is not None and batch["state"] == "partial" else ""),
    )


def unreadable_text(stmt: statement.Statement) -> str:
    """Why the reader came back with nothing, in the operator's words.

    Each frontend appends its own advice about sending the original file.
    """
    return "讀唔到張圖（" + "；".join(stmt.warnings or ["冇日期 / 訂單行"]) + "）"


def keep_unread_image(db_path: str, stem: str, data: bytes) -> None:
    """Keep a screenshot the reader could not read.

    The operator's copy scrolls away in a chat or a photo roll, and a reader
    bug can only be reproduced from the exact bytes.  Nothing here may cost
    the operator their reply, so a failure to write is logged and swallowed.
    """
    try:
        d = os.path.join(statements_dir(db_path), "failed")
        os.makedirs(d, exist_ok=True)
        # The stem comes from Telegram or from a browser upload and is about to
        # be part of a path.
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", (stem or "")[:12])
        name = f"{datetime.now():%Y%m%d-%H%M%S}-{safe}.{image_extension(data)}"
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
    except Exception:
        logging.getLogger("statement").exception("could not keep unreadable statement image")
