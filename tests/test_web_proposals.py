"""The proposals the ledger and the month carry, and what one request costs.

Both endpoints answer for every waiting credit or every batch still owed money
in one payload.  Each answer has to be the one the per-row path gives, and the
number of reads behind a payload must not follow the number of rows in it.
"""
import os
import sqlite3
import sys
from datetime import date

import pytest

import ride_dispatch.web as web
from ride_dispatch import credits, db

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import seed_demo_db  # noqa: E402

TODAY = date(2026, 10, 15)


@pytest.fixture
def client(monkeypatch):
    web.app.config["TESTING"] = True
    with web.app.test_client() as c:
        yield c


def seeded(monkeypatch, tmp_path, name: str, backlog: bool) -> str:
    path = str(tmp_path / name)
    seed_demo_db.seed(path, TODAY, backlog=backlog)
    monkeypatch.setattr(web, "DB_PATH", path)
    return path


def connections(monkeypatch, request_once) -> int:
    """How many times one request opens the database."""
    real = sqlite3.connect
    opened = []

    def connect(*args, **kwargs):
        opened.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", connect)
    try:
        request_once()
    finally:
        monkeypatch.setattr(db.sqlite3, "connect", real)
    return len(opened)


def ledger(client) -> list[dict]:
    r = client.get("/api/credits?platform=ride")
    assert r.status_code == 200
    return r.get_json()["credits"]


def test_every_waiting_credit_is_offered_what_its_own_proposal_offers(client, monkeypatch, tmp_path):
    path = seeded(monkeypatch, tmp_path, "backlog.db", backlog=True)
    rows = ledger(client)
    waiting = [c for c in rows if c["state"] in ("open", "partial")]
    assert len(waiting) > seed_demo_db.BACKLOG
    for c in waiting:
        m = credits.propose_credit(path, c["id"])
        offered = credits.offer(m, db.open_batches(path, "ride"))
        assert [(p["id"], p["exact"], p["outstanding"]) for p in c["proposals"]] == \
            [(b["id"], b["id"] in m.exact, b["outstanding"]) for b in offered]
        group = m.reason == "subset" and len(m.exact) > 1
        assert (c["combo"] or {}).get("ids") == (list(m.exact) if group else None)
    for c in rows:
        if c not in waiting:
            assert (c["proposals"], c["combo"]) == ([], None)
    # The seed holds each kind of answer, so the comparison above is not one
    # of empty lists: a single batch, a whole group, and nothing at all.
    by_id = {c["id"]: c for c in rows}
    assert [p["exact"] for p in by_id[seed_demo_db.CREDIT["exact"]]["proposals"]][:1] == [True]
    assert any(c["combo"] for c in waiting)
    assert any(not c["proposals"] for c in waiting)


def test_the_ledger_costs_the_same_reads_however_many_credits_wait(client, monkeypatch, tmp_path):
    seeded(monkeypatch, tmp_path, "plain.db", backlog=False)
    few = sum(c["state"] in ("open", "partial") for c in ledger(client))
    plain = connections(monkeypatch, lambda: ledger(client))
    seeded(monkeypatch, tmp_path, "backlog.db", backlog=True)
    many = sum(c["state"] in ("open", "partial") for c in ledger(client))
    assert few > 0 and many == few + seed_demo_db.BACKLOG
    # The ledger, then the open batches once.
    assert connections(monkeypatch, lambda: ledger(client)) == plain == 2


def test_open_batches_are_the_batches_still_owed_money_whole(monkeypatch, tmp_path):
    path = seeded(monkeypatch, tmp_path, "plain.db", backlog=False)
    conn = sqlite3.connect(path)
    every = [r[0] for r in conn.execute("SELECT id FROM settlements WHERE platform = 'ride' ORDER BY id")]
    conn.close()
    whole = [db.get_settlement(path, sid) for sid in every]
    owed = [b for b in whole if b["outstanding"] > db.CENT]
    assert 0 < len(owed) < len(whole)
    assert db.open_batches(path, "ride") == owed
