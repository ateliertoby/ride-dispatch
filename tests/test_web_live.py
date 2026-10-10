"""The web app behind a real socket. The test client hands a response straight
to the test and has no connection to lose, so what the server does when a
client leaves, and what a stream answers before its first byte, are only seen
here."""
import os
import socket
import tempfile
import threading
import time

import pytest
from werkzeug.serving import make_server

import ride_dispatch.web as web
from ride_dispatch.db import init_db


@pytest.fixture
def live(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    monkeypatch.setattr(web, "DB_PATH", path)
    # The app's own cadence, scaled so a quiet stream writes within the test.
    monkeypatch.setattr(web, "STREAM_TICK", 0.02)
    monkeypatch.setattr(web, "STREAM_KEEPALIVE", 0.04)
    server = make_server("127.0.0.1", 0, web.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        os.unlink(path)


def _open_stream(port):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"GET /api/events HTTP/1.1\r\nHost: localhost\r\n\r\n")
    return s


def _read_until(s, marker):
    data = b""
    while marker not in data:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _wait_for(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_a_stream_whose_client_left_gives_back_what_it_held(live, monkeypatch):
    held = []

    class Counted(web._Watch):
        def __init__(self):
            super().__init__()
            held.append(self)

        def close(self):
            super().close()
            held.remove(self)

    monkeypatch.setattr(web, "_Watch", Counted)
    before = threading.active_count()
    streams = [_open_stream(live) for _ in range(5)]
    for s in streams:
        assert b"data: connected" in _read_until(s, b"data: connected")
    assert _wait_for(lambda: len(held) == 5)
    assert threading.active_count() == before + 5

    for s in streams:
        s.close()
    # Nothing in the database changes: the stream's own writes are all that
    # can find the clients gone.
    assert _wait_for(lambda: not held), f"{len(held)} streams still hold a connection"
    assert _wait_for(lambda: threading.active_count() == before)


def test_a_stream_that_cannot_read_the_database_fails_before_it_greets(live, monkeypatch):
    """The greeting is what tells the page its stream is up, and resets how
    long it waits before trying again. Sent ahead of a failure, it turns the
    page's growing delay into a retry every two seconds."""
    monkeypatch.setattr(web, "DB_PATH", os.path.join(tempfile.gettempdir(), "no-such-dir", "x.db"))
    s = _open_stream(live)
    try:
        answer = _read_until(s, b"\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 500"), answer[:40]
        assert b"data: connected" not in answer + _read_until(s, b"data: connected")
    finally:
        s.close()
