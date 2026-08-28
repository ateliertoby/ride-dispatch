import os
import tempfile

import pytest

import sqlite3

from ride_dispatch.db import (
    init_db, open_parking_session, get_open_parking_session, get_parking_session,
    update_parking_session, close_parking_session, mark_parking_observed,
    recent_parking_sessions, free_parking_entries_since,
)


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    os.unlink(path)


def _open(db_path, pv_nr=212022, entry="2026-08-23 18:48", order_id=None):
    return open_parking_session(db_path, pv_nr=pv_nr, plate="YY3953", location="P4O",
                                location_name="Car Park 4", entry_time=entry, order_id=order_id)


def test_open_and_get_open_session(db_path):
    assert get_open_parking_session(db_path) is None
    sid = _open(db_path)
    row = get_open_parking_session(db_path)
    assert row["id"] == sid and row["pv_nr"] == 212022 and row["exit_time"] is None
    assert row["paid"] == 0 and row["free"] is None and row["auto_link_sent"] == 0


def test_pv_nr_is_unique(db_path):
    _open(db_path)
    with pytest.raises(Exception):
        _open(db_path)


def test_update_allowed_fields_only(db_path):
    sid = _open(db_path)
    update_parking_session(db_path, sid, paid=1, paid_amount=32.0, scheduled_exit="2026-08-23 19:48",
                           payment_ref="PPR9WOO9V", link_sent_at="2026-08-23 18:59", auto_link_sent=1,
                           order_id="O1")
    row = get_parking_session(db_path, sid)
    assert row["paid"] == 1 and row["paid_amount"] == 32.0 and row["payment_ref"] == "PPR9WOO9V"
    assert row["order_id"] == "O1" and row["auto_link_sent"] == 1
    with pytest.raises(ValueError):
        update_parking_session(db_path, sid, entry_time="x")


def test_update_stores_the_latest_hkia_reading(db_path):
    sid = _open(db_path)
    assert get_parking_session(db_path, sid)["last_park_minutes"] is None
    update_parking_session(db_path, sid, last_seen_at="2026-08-23 19:20:22",
                           last_park_minutes=32, last_fee=0)
    update_parking_session(db_path, sid, last_seen_at="2026-08-23 19:21:22",
                           last_park_minutes=33, last_fee=32.0)
    row = get_parking_session(db_path, sid)
    assert row["last_seen_at"] == "2026-08-23 19:21:22"
    assert row["last_park_minutes"] == 33 and row["last_fee"] == 32.0


def test_close_session(db_path):
    sid = _open(db_path)
    close_parking_session(db_path, sid, exit_time="2026-08-23 19:36", free=0,
                          gone_at="2026-08-23 19:37:22")
    assert get_open_parking_session(db_path) is None
    row = get_parking_session(db_path, sid)
    assert row["exit_time"] == "2026-08-23 19:36" and row["free"] == 0
    assert row["gone_at"] == "2026-08-23 19:37:22" and row["observed"] is None


def test_observed_overrides_the_free_column_both_ways(db_path):
    sid = _open(db_path)
    close_parking_session(db_path, sid, exit_time="2026-08-23 19:36", free=0)
    assert mark_parking_observed(db_path, sid, "free") is True
    row = get_parking_session(db_path, sid)
    assert row["observed"] == "free" and row["free"] == 1
    # The allowance query has to see the correction.
    assert free_parking_entries_since(db_path, "2026-08-23 00:00") == ["2026-08-23 18:48"]
    # A second, different verdict replaces the first.
    assert mark_parking_observed(db_path, sid, "gate") is True
    row = get_parking_session(db_path, sid)
    assert row["observed"] == "gate" and row["free"] == 0
    assert free_parking_entries_since(db_path, "2026-08-23 00:00") == []
    assert mark_parking_observed(db_path, 9999, "free") is False


def test_init_db_adds_the_reading_columns_to_an_old_database(db_path):
    # A database created before the readings were kept: drop the table and
    # rebuild it at the old shape, then let init_db migrate it in place.
    old = """
        CREATE TABLE parking_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pv_nr INTEGER UNIQUE, plate TEXT, location TEXT, location_name TEXT,
            entry_time TEXT, exit_time TEXT, paid INTEGER DEFAULT 0, paid_amount REAL,
            scheduled_exit TEXT, payment_ref TEXT, link_sent_at TEXT,
            auto_link_sent INTEGER DEFAULT 0, free INTEGER, order_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE parking_sessions")
    conn.execute(old)
    conn.execute("INSERT INTO parking_sessions (pv_nr, entry_time, exit_time, free) "
                 "VALUES (7, '2026-08-23 18:48', '2026-08-23 19:36', 0)")
    conn.commit()
    conn.close()

    init_db(db_path)

    row = recent_parking_sessions(db_path, 1)[0]
    assert row["pv_nr"] == 7 and row["free"] == 0          # the old row survives
    for col in ("last_seen_at", "last_park_minutes", "last_fee", "gone_at", "observed"):
        assert row[col] is None
    init_db(db_path)                                        # migration repeats safely


def test_recent_and_free_queries(db_path):
    a = _open(db_path, pv_nr=1, entry="2026-08-22 16:35")
    b = _open(db_path, pv_nr=2, entry="2026-08-23 13:36")
    c = _open(db_path, pv_nr=3, entry="2026-08-23 18:48")
    close_parking_session(db_path, a, exit_time="2026-08-22 16:50", free=1)
    close_parking_session(db_path, b, exit_time="2026-08-23 13:50", free=1)
    close_parking_session(db_path, c, exit_time="2026-08-23 19:36", free=0)
    recent = recent_parking_sessions(db_path, limit=2)
    assert [r["pv_nr"] for r in recent] == [3, 2]
    assert free_parking_entries_since(db_path, "2026-08-22 19:00") == ["2026-08-23 13:36"]
    assert free_parking_entries_since(db_path, "2026-08-24 00:00") == []
