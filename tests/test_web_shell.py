import os
import shutil
import tempfile

import pytest

from ride_dispatch import web
from ride_dispatch.db import init_db


@pytest.fixture
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    monkeypatch.setattr(web, "DB_PATH", path)
    monkeypatch.setattr(web, "SHELL", True)
    web.app.config["TESTING"] = True
    with web.app.test_client() as c:
        yield c
    os.unlink(path)


@pytest.fixture
def static_copy(tmp_path, monkeypatch):
    """The app pointed at a throwaway copy of static/, so a test can change
    the assets without writing into the working tree."""
    copy = tmp_path / "static"
    shutil.copytree(web.app.static_folder, copy)
    monkeypatch.setattr(web.app, "static_folder", str(copy))
    return copy


def test_both_routes_serve_the_same_shell(client):
    day, settle = client.get("/"), client.get("/settle")
    assert day.status_code == settle.status_code == 200
    assert day.get_data() == settle.get_data()
    assert day.headers["X-Asset-Version"] == web.asset_version()
    assert day.headers["Cache-Control"] == "no-cache"
    assert f'content="{web.asset_version()}"' in day.get_data(as_text=True)


def test_an_asset_is_served_under_the_current_version_only(client):
    v = web.asset_version()
    ok = client.get(f"/assets/{v}/manifest.webmanifest")
    assert ok.status_code == 200
    assert ok.mimetype == "application/manifest+json"
    assert ok.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert client.get("/assets/000000000000/manifest.webmanifest").status_code == 404
    assert client.get(f"/assets/{v}/icons/icon-192.png").mimetype == "image/png"
    assert client.get(f"/assets/{v}/no-such-file.js").status_code == 404


def test_an_asset_path_cannot_leave_the_static_folder(client):
    v = web.asset_version()
    # The first two name files that exist one level above static/. The last
    # decodes to an absolute path, which routing answers with a redirect to
    # the same path made relative, so the redirect is followed.
    for escape in ("../templates/dashboard.html", "../ride_dispatch/web.py",
                   "icons/../../templates/dashboard.html",
                   "%2e%2e/templates/dashboard.html", "..%2ftemplates%2fdashboard.html",
                   "%2fetc%2fhosts"):
        res = client.get(f"/assets/{v}/{escape}", follow_redirects=True)
        assert res.status_code == 404, escape


def test_the_version_follows_the_assets(client, static_copy):
    before = web.asset_version()
    assert before == web.asset_version()
    probe = static_copy / "probe.txt"
    probe.write_text("x")
    added = web.asset_version()
    assert added != before
    probe.write_text("y")
    assert web.asset_version() not in (before, added)
    probe.unlink()
    assert web.asset_version() == before


def test_the_version_is_computed_once_outside_tests(client, static_copy, monkeypatch):
    monkeypatch.setitem(web.app.config, "TESTING", False)
    monkeypatch.setattr(web, "_asset_version_cache", None)
    first = web.asset_version()
    (static_copy / "probe.txt").write_text("x")
    assert web.asset_version() == first


def test_api_answers_are_never_cacheable(client):
    for path in ("/api/orders?date=2026-07-01", "/api/settle?month=2026-07&platform=ride",
                 "/api/credits?platform=ride", "/api/ping",
                 "/api/orders/no-such-order", "/api/no-such-route"):
        assert client.get(path).headers["Cache-Control"] == "no-store", path
    res = client.post("/api/orders", json={"type": "didi", "date": "2026-07-01",
                                           "time": "14:30", "price": 250})
    assert res.status_code == 201 and res.headers["Cache-Control"] == "no-store"


def test_a_statement_image_is_never_cacheable(client):
    from datetime import datetime
    from ride_dispatch.db import create_settlement, save_quick_order
    save_quick_order(web.DB_PATH, "T2-0001", "滴滴", "2026-07-01 14:30:00", 200.0, 0.0, source="滴滴")
    sid = create_settlement(web.DB_PATH, "didi", ["T2-0001"], 200.0, "2026-07-02",
                            now=datetime(2026, 7, 2, 9, 0), image=b"\xff\xd8x")
    res = client.get(f"/api/settlements/{sid}/image")
    assert res.status_code == 200 and res.data == b"\xff\xd8x"
    assert res.headers["Cache-Control"] == "no-store"


def test_the_event_stream_keeps_its_own_cache_header(client):
    res = client.get("/api/events", buffered=False)
    try:
        assert res.headers["Cache-Control"] == "no-cache"
    finally:
        res.close()


def test_pages_and_assets_are_not_marked_no_store(client, monkeypatch):
    monkeypatch.setattr(web, "SHELL", False)
    assert "no-store" not in client.get("/").headers.get("Cache-Control", "")
    assert "no-store" not in client.get("/manifest.webmanifest").headers.get("Cache-Control", "")


def test_ping_names_the_version(client):
    assert client.get("/api/ping").get_json() == {"ok": True, "version": web.asset_version()}


def test_the_old_pages_still_answer_without_the_flag(client, monkeypatch):
    monkeypatch.setattr(web, "SHELL", False)
    day = client.get("/")
    assert "function detailView(" in day.get_data(as_text=True)
    assert "X-Asset-Version" not in day.headers
    assert "埋數" in client.get("/settle").get_data(as_text=True)
