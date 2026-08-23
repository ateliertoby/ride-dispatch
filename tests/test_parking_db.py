import os
import tempfile

import pytest

from ride_dispatch.db import (
    init_db, open_parking_session, get_open_parking_session, get_parking_session,
    update_parking_session, close_parking_session, recent_parking_sessions,
    free_parking_entries_since,
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


def test_close_session(db_path):
    sid = _open(db_path)
    close_parking_session(db_path, sid, exit_time="2026-08-23 19:36", free=0)
    assert get_open_parking_session(db_path) is None
    row = get_parking_session(db_path, sid)
    assert row["exit_time"] == "2026-08-23 19:36" and row["free"] == 0


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
