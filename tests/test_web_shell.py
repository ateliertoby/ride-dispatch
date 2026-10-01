import json
import os
import posixpath
import re
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


def test_a_font_is_served_as_a_font_and_kept(client):
    v = web.asset_version()
    for name in ("B612Mono-Regular.woff2", "B612Mono-Bold.woff2"):
        res = client.get(f"/assets/{v}/fonts/{name}")
        assert res.status_code == 200, name
        assert res.mimetype == "font/woff2", name
        assert res.headers["Cache-Control"] == "public, max-age=31536000, immutable", name
        assert res.get_data()[:4] == b"wOF2", name
    # The licence the fonts are distributed under travels with them.
    assert os.path.isfile(os.path.join(web.app.static_folder, "fonts", "OFL.txt"))


def _asset_urls(html: str) -> list[str]:
    return re.findall(r'(?:href|src)="(/assets/[^"]+)"', html)


def test_the_shell_links_its_styles_and_module_by_versioned_address(client):
    v = web.asset_version()
    html = client.get("/").get_data(as_text=True)
    urls = _asset_urls(html)
    assert urls == [f"/assets/{v}/fonts/B612Mono-Regular.woff2",
                    f"/assets/{v}/fonts/B612Mono-Bold.woff2",
                    f"/assets/{v}/css/base.css", f"/assets/{v}/css/order-sheet.css",
                    f"/assets/{v}/css/day.css", f"/assets/{v}/css/settle.css",
                    f"/assets/{v}/js/main.js"]
    assert f'<script type="module" src="/assets/{v}/js/main.js"></script>' in html
    for url in urls:
        res = client.get(url)
        assert res.status_code == 200, url
        kind = {".css": "text/css", ".js": "text/javascript", ".woff2": "font/woff2"}
        assert res.mimetype == kind[os.path.splitext(url)[1]], url


def test_the_shell_preloads_the_fonts_the_stylesheet_names(client):
    v = web.asset_version()
    html = client.get("/").get_data(as_text=True)
    css = client.get(f"/assets/{v}/css/base.css").get_data(as_text=True)
    named = re.findall(r'url\("\.\./(fonts/[^"]+\.woff2)"\)', css)
    assert sorted(named) == ["fonts/B612Mono-Bold.woff2", "fonts/B612Mono-Regular.woff2"]
    for path in named:
        # Without crossorigin the preloaded copy is not the one the stylesheet
        # uses, and the file is fetched twice.
        assert (f'<link rel="preload" as="font" type="font/woff2" '
                f'href="/assets/{v}/{path}" crossorigin>') in html, path


def test_every_module_the_shell_imports_is_served_as_javascript(client):
    """A module's imports are relative, so they resolve under the same
    versioned address; one served with another type stops the whole app."""
    v = web.asset_version()
    seen, queue = set(), ["js/main.js"]
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        res = client.get(f"/assets/{v}/{path}")
        assert res.status_code == 200, path
        assert res.mimetype == "text/javascript", path
        for spec in re.findall(r"""from\s+'(\.[^']+)'""", res.get_data(as_text=True)):
            queue.append(posixpath.normpath(posixpath.join(posixpath.dirname(path), spec)))
    assert {"js/main.js", "js/router.js", "js/day/index.js", "js/settle/index.js",
            "js/order-sheet.js", "js/shared.js", "js/api.js", "js/store.js", "js/stream.js",
            "js/dates.js", "js/lanes.js"} <= seen


def test_asset_types_do_not_follow_the_systems_table(client, monkeypatch):
    import mimetypes
    monkeypatch.setattr(mimetypes, "guess_type", lambda *a, **k: ("text/plain", None))
    v = web.asset_version()
    assert client.get(f"/assets/{v}/js/api.js").mimetype == "text/javascript"
    assert client.get(f"/assets/{v}/css/base.css").mimetype == "text/css"
    assert client.get(f"/assets/{v}/manifest.webmanifest").mimetype == "application/manifest+json"
    assert client.get(f"/assets/{v}/fonts/B612Mono-Regular.woff2").mimetype == "font/woff2"


def test_the_shell_holds_both_views_and_one_toast(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="view-day" class="view view-day" hidden' in html
    assert 'id="view-settle" class="view view-settle" hidden' in html
    assert html.count('id="toast"') == 1
    # Inline handlers resolve on window; the modules publish theirs under rd.
    # The settle view's markup has none: its controls are found by listeners.
    handlers = re.findall(r'onclick="([^"]+)"', html)
    assert handlers and all(h.startswith("rd.day.") for h in handlers)
    # Each view switches to the other through the router, not the server.
    assert '<a class="icon-btn" href="/settle" data-nav aria-label="埋數">' in html
    assert '<a class="icon-btn" href="/" data-nav aria-label="返日程">' in html


def test_no_id_appears_twice_in_the_shell(client):
    """Both views are in one document: an id one of them shares with the
    other would make each find the other's element."""
    html = client.get("/").get_data(as_text=True)
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids)), sorted(i for i in set(ids) if ids.count(i) > 1)
    day, settle = html.split('<div id="view-settle"')
    assert all(i in day for i in ('id="day-tabs"', 'id="day-foot"', 'id="day-scrim"',
                                  'id="day-sheet"', 'id="day-drop"'))
    assert all(i in settle for i in ('id="settle-tabs"', 'id="settle-foot"',
                                     'id="settle-scrim"', 'id="settle-sheet"',
                                     'id="settle-drop"'))


def test_an_asset_path_cannot_leave_the_static_folder(client):
    v = web.asset_version()
    # The first two name files that exist one level above static/. The last
    # decodes to an absolute path, which routing answers with a redirect to
    # the same path made relative, so the redirect is followed.
    for escape in ("../templates/app.html", "../ride_dispatch/web.py",
                   "icons/../../templates/app.html",
                   "%2e%2e/templates/app.html", "..%2ftemplates%2fapp.html",
                   "%2fetc%2fhosts"):
        res = client.get(f"/assets/{v}/{escape}", follow_redirects=True)
        assert res.status_code == 404, escape
    # The files named are really there to be reached, or the refusals above
    # would prove nothing.
    above = os.path.dirname(web.app.static_folder.rstrip(os.sep))
    assert os.path.isfile(os.path.join(above, "templates", "app.html"))
    assert os.path.isfile(os.path.join(above, "ride_dispatch", "web.py"))


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


def test_the_worker_is_served_from_the_root_and_never_cached(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.mimetype == "text/javascript"
    assert r.headers["Cache-Control"] == "no-cache"
    body = r.get_data(as_text=True)
    v = web.asset_version()
    assert f'"{v}"' in body
    assert f"/assets/{v}/js/main.js" in body
    assert f"/assets/{v}/css/base.css" in body


def test_the_worker_lists_every_script_stylesheet_and_font(client):
    body = client.get("/sw.js").get_data(as_text=True)
    v = web.asset_version()
    listed = 0
    for sub, kinds in (("js", (".js",)), ("css", (".css",)), ("fonts", (".woff2",))):
        base = os.path.join(web.app.static_folder, sub)
        for root, _d, files in os.walk(base):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), web.app.static_folder)
                if name.endswith(kinds):
                    assert f'"/assets/{v}/{rel}"' in body, rel
                    listed += 1
                else:
                    assert rel == os.path.join("fonts", "OFL.txt"), rel
                    assert f"/assets/{v}/{rel}" not in body
    assert f'"/assets/{v}/fonts/B612Mono-Regular.woff2"' in body
    assert f'"/assets/{v}/fonts/B612Mono-Bold.woff2"' in body
    # Everything the document links and every module those import is among them.
    for url in _asset_urls(client.get("/").get_data(as_text=True)):
        assert f'"{url}"' in body, url
    assert listed


def test_the_worker_never_names_the_api(client):
    """No code path in the worker can match or store a data request."""
    body = client.get("/sw.js").get_data(as_text=True)
    v = web.asset_version()
    listed = [line for line in body.splitlines() if line.startswith("const ASSETS = ")]
    assert len(listed) == 1
    # What it stores by name is asset addresses only (one of them a script
    # called api.js), and nothing else in it mentions the API at all.
    urls = json.loads(listed[0][len("const ASSETS = "):].rstrip(";"))
    assert urls and all(u.startswith(f"/assets/{v}/") for u in urls)
    assert "api" not in body.replace(listed[0], "").lower()
    # It answers two kinds of request and no other: an asset address, and a
    # navigation to one of the shell's own paths.
    assert body.count("respondWith(") == 2
    assert "url.pathname.startsWith('/assets/')" in body
    assert "const SHELL_PATHS = ['/', '/settle'];" in body
    assert "req.mode === 'navigate' && SHELL_PATHS.includes(url.pathname)" in body


def test_the_worker_changes_with_the_assets(client, static_copy):
    """The browser looks for a new version by comparing the worker's bytes."""
    before = client.get("/sw.js").get_data()
    (static_copy / "js" / "probe.js").write_text("// x\n")
    after = client.get("/sw.js").get_data(as_text=True)
    assert after.encode() != before
    assert f"/assets/{web.asset_version()}/js/probe.js" in after


def test_the_shell_holds_two_hidden_banners(client):
    html = client.get("/").get_data(as_text=True)
    assert '<button class="shell-banner" id="banner-auth" hidden>' in html
    assert '<button class="shell-banner" id="banner-update" hidden>' in html
    assert html.index('id="banner-auth"') < html.index('id="view-day"')


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


def test_pages_and_assets_are_not_marked_no_store(client):
    assert "no-store" not in client.get("/").headers.get("Cache-Control", "")
    assert "no-store" not in client.get("/manifest.webmanifest").headers.get("Cache-Control", "")


def test_ping_names_the_version(client):
    assert client.get("/api/ping").get_json() == {"ok": True, "version": web.asset_version()}


def test_the_document_carries_no_script_or_style_of_its_own(client):
    """Everything the app runs is an asset under the versioned address, which
    is what lets the worker hold one version whole. Script or style written
    into the document would be outside it."""
    for path in ("/", "/settle"):
        html = client.get(path).get_data(as_text=True)
        assert re.findall(r"<script\b[^>]*>", html) == [
            f'<script type="module" src="/assets/{web.asset_version()}/js/main.js">'], path
        assert "<style" not in html, path
        assert "function " not in html, path



def test_the_order_sheet_names_each_pickup_point_at_the_servers_charge():
    """The sheet prints each meeting point's first-hour charge under its name
    from a table of its own, the twin of ingest.PICKUP_POINTS, which is what
    the server writes when a point is chosen. The two must not drift."""
    from ride_dispatch.ingest import PICKUP_POINTS
    with open(os.path.join(web.app.static_folder, "js", "order-sheet.js"), encoding="utf-8") as f:
        table = re.search(r"^const PICKUP_POINTS = \{(.*)\};$", f.read(), re.M).group(1)
    twin = {name: float(fee) for name, fee in re.findall(r"'([^']+)':\s*([\d.]+)", table)}
    assert twin == PICKUP_POINTS
    assert list(twin) == list(PICKUP_POINTS)
