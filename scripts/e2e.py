"""Behaviour checks for the web app, driven through a real browser.

    python scripts/e2e.py                    every check
    python scripts/e2e.py --only day.add     checks whose name begins with this
    python scripts/e2e.py --list             name the checks and stop

Playwright WebKit as an iPhone 14. Every check gets a server of its own on a
freshly seeded synthetic database (scripts/seed_demo_db.py) with both clocks
pinned to 14:00, so checks do not depend on each other or on when they run.
Each prints PASS or FAIL; the exit status is non-zero if any failed.

The shell's service worker is kept out of every check but those about it
(worker.*, and the auth.* checks that need the cached shell): WebKit does not
let a page under a worker have its requests held or stubbed, which is how the
other checks arrange what they test.

A check also fails when the page logged an error, a request failed or the
server answered with an error status, unless the check said to expect it.

A check drives the page through what is on screen only, as scripts/shots.py
does. Checks are registered with @check, in the order they run; a new view
adds its own under its own name prefix.
"""
import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import seed_demo_db  # noqa: E402
from harness import (DEVICE, LOGIN_PATH, ROOT, TIMEOUT_MS, TIMEZONE, Driver,  # noqa: E402
                     Server, copy_app, demo_now, new_context, paste_message)

CHECKS = []


def check(name: str, clock: str = "fixed", still: bool = True,
          workers: bool = False, copy: bool = False, desktop: bool = False):
    """Register a check. `clock` is "fixed" (Date frozen, timers real) or
    "installed" (the check moves time itself). `still` turns the page's
    transitions and animations off, which is what every check wants unless
    motion is what it checks.
    `workers` lets the shell's service worker in; such a check cannot hold or
    stub a request from the page and makes the server misbehave instead
    (Server.fault). `copy` serves the app from a throwaway copy the check may
    change, to stand for a deploy. `desktop` runs it in a wide window with a
    pointer that hovers and no touch, so it clicks where the others tap."""
    def register(fn):
        CHECKS.append({"name": name, "fn": fn, "clock": clock, "still": still,
                       "workers": workers, "copy": copy, "desktop": desktop})
        return fn
    return register


class Failed(AssertionError):
    pass


def body_of(req):
    """A request's body as text. A multipart body is parted by a boundary the
    browser makes up afresh for each request, so the boundary is replaced by a
    fixed word and two builds sending the same form compare equal."""
    kind = req.headers.get("content-type", "")
    if not kind.startswith("multipart/form-data"):
        return req.post_data
    boundary = kind.split("boundary=")[-1]
    return (req.post_data_buffer or b"").decode("latin-1").replace(boundary, "BOUNDARY")


# ---- settle view: what the checks read and compute ----

WEEKDAY = "一二三四五六日"      # date.weekday(): Monday is 0


def fmt(n) -> str:
    """Money as the app prints it: cents only when there are some."""
    return f"{n:.2f}" if n % 1 else f"{n:.0f}"


def md_label(d: date) -> str:
    return f"{d.month}月{d.day}日"


def md_slash(d: date) -> str:
    return f"{d.month}/{d.day}"


def span_label(a: date, z: date) -> str:
    """How a run of consecutive days is named on a sheet."""
    if a == z:
        return md_label(a)
    if (a.year, a.month) == (z.year, z.month):
        return f"{a.month}月{a.day}–{z.day}日"
    return md_label(a) + "–" + md_label(z)


def month_key(d: date) -> str:
    return d.isoformat()[:7]


def add_months(d: date, n: int) -> date:
    """The first day of the month `n` months from d's."""
    m = d.year * 12 + d.month - 1 + n
    return date(m // 12, m % 12 + 1, 1)


def week_id(d: date) -> str:
    """The id of the week row a day is drawn on: rows begin on Sunday."""
    return "wk" + (d - timedelta(days=(d.weekday() + 1) % 7)).isoformat()


def month_label(d: date, now: bool = False) -> str:
    return f"{d.year} 年 {d.month} 月" + ("今個月" if now else "")


def settle_path(d: date, platform: str = "ride") -> str:
    return f"/api/settle?month={month_key(d)}&platform={platform}"


CREDITS = "/api/credits?platform=ride"

# The week row at the top of the strip, read the way the page reads it: the
# first row whose end is below the sticky header.
TOP_WEEK_JS = """
() => {
  const header = [...document.querySelectorAll('.header')].find(e => e.getClientRects().length);
  const edge = header.getBoundingClientRect().bottom + 1;
  for (const el of document.querySelectorAll('.wkblock')) {
    const r = el.getBoundingClientRect();
    if (r.bottom > edge) return [el.id, Math.round(r.top)];
  }
  return null;
}
"""

# What is wrong with the strip's lanes, if anything: a bar or chip with no
# width, a label cut by the columns reserved for it, two marks of one lane row
# printed over each other. A strip laid out while it had no width fails this.
STRIP_JS = """
() => {
  const bad = [];
  for (const lane of document.querySelectorAll('#grid .lane')) {
    const items = [...lane.children].map(el => ({
      mark: el.matches('.slot') ? el.firstElementChild : el, r: el.getBoundingClientRect() }));
    for (const { mark, r } of items) {
      if (r.width < 8 || r.height < 8) bad.push('no size: ' + mark.textContent);
      if (!mark.matches('.spill-l') && mark.scrollWidth > Math.ceil(r.width) + 1) bad.push('cut: ' + mark.textContent);
    }
    for (let i = 0; i < items.length; i++) for (let j = i + 1; j < items.length; j++) {
      const a = items[i].r, b = items[j].r;
      if (Math.abs(a.top - b.top) < 1 && a.left < b.right - 0.5 && b.left < a.right - 0.5) {
        bad.push('overlap: ' + items[i].mark.textContent + ' / ' + items[j].mark.textContent);
      }
    }
  }
  return bad;
}
"""

WATCH_JS = """
id => {
  window.__seen = window.__seen || {};
  if (window.__seen[id]) window.__seen[id].observer.disconnect();
  const log = [];
  const observer = new MutationObserver(list => {
    for (const m of list) log.push(m.type + ' ' + (m.target.id || m.target.className || m.target.nodeName));
  });
  observer.observe(document.getElementById(id), { subtree: true, childList: true, attributes: true, characterData: true });
  window.__seen[id] = { observer, log };
}
"""

# A file dragged over the page and let go, as the events a browser would send.
# Returns, for each, whether a listener took it for itself.
DRAG_JS = """
kinds => {
  const dt = new DataTransfer();
  dt.items.add(new File(['x'], 'statement.png', { type: 'image/png' }));
  const out = {};
  for (const kind of kinds) {
    const e = new DragEvent(kind, { dataTransfer: dt, bubbles: true, cancelable: true });
    document.body.dispatchEvent(e);
    out[kind] = e.defaultPrevented;
  }
  return out;
}
"""


SHELL_KEY = "/__shell__"

# The worker as the page sees it, and every address each of its caches holds.
WORKER_JS = """
async () => {
  const reg = await navigator.serviceWorker.getRegistration();
  const held = {};
  for (const name of (await caches.keys()).sort()) {
    const keys = await (await caches.open(name)).keys();
    held[name] = keys.map(r => new URL(r.url).pathname + new URL(r.url).search).sort();
  }
  return { controller: !!navigator.serviceWorker.controller, active: !!(reg && reg.active),
           waiting: !!(reg && reg.waiting), installing: !!(reg && reg.installing), caches: held };
}
"""

# Where the showing banner and the showing view's header sit, and what a tap
# in the middle of each of the header's controls would land on.
BANNER_JS = """
() => {
  const seen = e => e.getClientRects().length > 0;
  const banners = [...document.querySelectorAll('.shell-banner')].filter(seen);
  const header = [...document.querySelectorAll('.header')].find(seen);
  const b = banners.length ? banners[0].getBoundingClientRect() : null;
  const h = header.getBoundingClientRect();
  const controls = [...header.querySelectorAll('.nav-btn, .icon-btn, .date-btn')].filter(seen);
  return {
    banners: banners.map(e => e.id),
    banner: b && [Math.round(b.top), Math.round(b.bottom), Math.round(b.left), Math.round(b.width)],
    header: [Math.round(h.top), Math.round(h.left), Math.round(h.width)],
    covered: controls.filter(c => {
      const r = c.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return !(hit && (hit === c || c.contains(hit)));
    }).length,
    wide: document.documentElement.scrollWidth > window.innerWidth,
  };
}
"""


class Session(Driver):
    """One page on one server, with everything it asked the server recorded."""

    def __init__(self, ctx, base_url: str, today: date, server: Server):
        super().__init__(ctx, base_url)
        self.today = today
        self.server = server
        self.t = seed_demo_db.targets(today)
        self.requests = []     # (method, path, resource type), as they start
        self.finished = []     # (method, path), as they finish
        self.answers = []      # (path, status, answered by the service worker)
        self.writes = []       # (method, path, body text) for every non-GET to /api/
        self.noise = []        # console errors, failed requests, error statuses
        self.allowed = []
        self.held = {}         # path -> routes, oldest first, for requests a check is holding back

    # -- recording --

    def _watch(self, page) -> None:
        super()._watch(page)

        def path_of(url: str) -> str:
            return url[len(self.base):] if url.startswith(self.base) else url

        def started(req):
            path = path_of(req.url)
            self.requests.append((req.method, path, req.resource_type))
            if req.method != "GET" and path.startswith("/api/"):
                self.writes.append((req.method, path, body_of(req)))

        def finished(req):
            self.finished.append((req.method, path_of(req.url)))

        def failed(req):
            self.noise.append(f"request failed: {req.method} {path_of(req.url)} ({req.failure})")

        def answered(res):
            self.answers.append((path_of(res.url), res.status, res.from_service_worker))
            if res.status >= 400:
                self.noise.append(f"http {res.status}: {res.request.method} {path_of(res.url)}")

        def logged(msg):
            if msg.type == "error":
                where = path_of((msg.location or {}).get("url", ""))
                self.noise.append(f"console error: {msg.text} [{where}]")

        page.on("request", started)
        page.on("requestfinished", finished)
        page.on("requestfailed", failed)
        page.on("response", answered)
        page.on("console", logged)

    def allow(self, *fragments: str) -> None:
        """Errors naming any of these are the check's own doing."""
        self.allowed.extend(fragments)

    def unexpected(self) -> list:
        out = [f"page error: {e}" for e in self.errors] + self.noise
        return [n for n in out if not any(a in n for a in self.allowed)]

    # -- asserting --

    def expect(self, ok, what: str) -> None:
        if not ok:
            raise Failed(what)

    def eq(self, got, want, what: str) -> None:
        if got != want:
            raise Failed(f"{what}: got {got!r}, want {want!r}")

    def wait(self, pred, what: str, ms: int = TIMEOUT_MS):
        """Poll until pred() is truthy; the page keeps running in between."""
        deadline = time.monotonic() + ms / 1000
        while True:
            got = pred()
            if got:
                return got
            if time.monotonic() > deadline:
                raise Failed(f"never happened: {what}")
            self.page.wait_for_timeout(40)

    def never(self, pred, what: str, ms: int = 900) -> None:
        deadline = time.monotonic() + ms / 1000
        while time.monotonic() < deadline:
            if pred():
                raise Failed(f"happened: {what}")
            self.page.wait_for_timeout(40)

    # -- the server, from outside the page --

    def api(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as res:
            return json.loads(res.read())

    def day(self, offset: int = 0) -> str:
        return (self.today + timedelta(days=offset)).isoformat()

    def orders(self, offset: int = 0) -> list:
        return self.api("GET", "/api/orders?date=" + self.day(offset))["orders"]

    def ids(self, offset: int = 0) -> list:
        return [o["order_id"] for o in self.orders(offset)]

    # -- holding requests back --

    def hold(self, *paths: str) -> None:
        """Every request for these paths waits until release() or answer(),
        each of which lets the oldest one waiting go."""
        wanted = set(paths)
        self.letting_go = False

        def handler(route, request):
            path = request.url[len(self.base):]
            if path in wanted and not self.letting_go:
                self.held.setdefault(path, []).append(route)
            else:
                route.continue_()

        self.page.route("**/api/**", handler)

    def holding(self, path: str) -> int:
        return len(self.held.get(path, []))

    def release(self, path: str) -> None:
        self.wait(lambda: self.holding(path), f"a request for {path}")
        self.held[path].pop(0).continue_()

    def answer(self, path: str, **fulfil) -> None:
        self.wait(lambda: self.holding(path), f"a request for {path}")
        self.held[path].pop(0).fulfill(**fulfil)

    def release_all(self) -> None:
        """Stop holding: what is waiting goes, and so does what comes later."""
        self.letting_go = True
        for routes in self.held.values():
            while routes:
                routes.pop(0).continue_()
        self.page.unroute_all()

    def stub(self, method, glob: str, status: int, body: str, content_type: str) -> None:
        """Answer every matching request with this instead of the server's.
        `method` is one method or several; a request by another method goes
        on to whatever was stubbed before, or to the server."""
        methods = (method,) if isinstance(method, str) else method

        def handler(route, request):
            if request.method in methods:
                route.fulfill(status=status, body=body, content_type=content_type)
            else:
                route.fallback()
        self.page.route(glob, handler)

    def refuse(self, method: str, glob: str, message: str = "測試拒絕") -> None:
        self.stub(method, glob, 400, json.dumps({"error": message}), "application/json")
        self.allow("http 400", "status of 400")

    # -- reading the day view --

    def press(self, selector: str, **kw) -> None:
        """Tap without waiting for the page to come to rest."""
        self.on(selector, **kw).first.tap()

    def keys(self, host: str, digits: str) -> None:
        for d in digits:
            self.press(f'{host} .key[data-k="{d}"]')

    def text(self, selector: str, **kw) -> str:
        return self.on(selector, **kw).first.text_content().strip()

    def count(self, selector: str, **kw) -> int:
        return self.on(selector, **kw).count()

    def rows(self) -> list:
        return self.page.eval_on_selector_all(".orders .row", "els => els.map(e => e.dataset.oid)")

    def row(self, oid: str) -> str:
        return f'.orders .row[data-oid="{oid}"]'

    def date_text(self) -> str:
        return self.text(".date-btn")

    def toast(self) -> str:
        loc = self.page.locator(".toast.show")
        return loc.first.text_content().strip() if loc.count() else ""

    def wait_toast(self, want) -> str:
        """`want` is the whole text, or a compiled pattern it must match."""
        def seen():
            t = self.toast()
            return t if (want.fullmatch(t) if hasattr(want, "fullmatch") else t == want) else None
        return self.wait(seen, f"toast {getattr(want, 'pattern', want)!r} (showing {self.toast()!r})")

    def open_day(self, path: str = "/") -> None:
        self.open(path, ".orders .row")

    def go_days(self, n: int) -> None:
        arrow = '[aria-label="後一日"]' if n > 0 else '[aria-label="前一日"]'
        for _ in range(abs(n)):
            self.tap(arrow)

    def open_order(self, oid: str) -> None:
        self.tap(self.row(oid))
        self.on(".sheet.show .field-row").first.wait_for()

    def field(self, label: str) -> str:
        """The value the open order sheet shows for an editable field."""
        return self.on(".sheet.show .field-row", has_text=label).first.locator(".fv").text_content().strip()

    def info(self) -> dict:
        """The order sheet's read-only rows, label -> text."""
        return dict(self.page.eval_on_selector_all(
            ".sheet.show .info-row",
            "els => els.map(e => [e.querySelector('.k').textContent, e.querySelector('.v').textContent])"))

    def edit(self, label: str, digits: str) -> None:
        """Change a field of the open order through the numpad."""
        self.tap(".sheet.show .field-row", has_text=label)
        self.on(".sheet.show .numpad").wait_for()
        self.keys(".sheet.show", digits)
        self.tap(".sheet.show #npOk")
        self.wait(lambda: not self.count(".sheet.show .numpad"), "the numpad to close after a save")

    def sheet_open(self) -> bool:
        return self.count(".sheet.show") > 0

    def panel_open(self) -> bool:
        return self.count(".drop.show") > 0

    def panel_title(self) -> str:
        return self.text(".drop.show .sheet-title")

    def stage(self, title: str) -> None:
        self.wait(lambda: self.panel_open() and self.panel_title() == title,
                  f"add panel stage {title!r}")

    def confirm_stage(self) -> None:
        self.tap(".drop.show .primary-btn", has_text="確認")

    def sum_rows(self) -> list:
        return self.page.eval_on_selector_all(
            ".drop.show .sum-row",
            "els => els.map(e => [e.querySelector('.k').textContent, e.querySelector('.v').textContent])")

    def last_write(self) -> tuple:
        self.expect(self.writes, "no write was sent")
        method, path, body = self.writes[-1]
        return method, path, json.loads(body)

    def scroll_y(self) -> int:
        return self.page.evaluate("() => Math.round(window.scrollY)")

    # -- reading the settle view, and both views together --

    def back(self, n: int) -> date:
        return self.today - timedelta(days=n)

    def open_settle(self) -> None:
        self.open("/settle", ".cell[data-d]")

    def go_settle(self) -> None:
        self.tap('[aria-label="埋數"]')
        self.on(".cell[data-d]").first.wait_for()

    def go_day(self) -> None:
        self.tap('[aria-label="返日程"]')
        self.on(".orders .row, .orders .empty").first.wait_for()

    def history(self, back: bool, ready: str) -> None:
        """Back or forward, which the shell answers without a document."""
        self.page.go_back() if back else self.page.go_forward()
        self.on(ready).first.wait_for()
        self.settle()

    def to_day(self) -> None:
        self.history(True, ".orders .row, .orders .empty")

    def to_settle(self) -> None:
        self.history(False, ".cell[data-d]")

    def texts(self, selector: str) -> list:
        return self.page.eval_on_selector_all(
            selector, "els => els.filter(e => e.getClientRects().length).map(e => e.textContent.trim())")

    def asked(self, since: int = 0, prefix: str = "/api/settle?") -> list:
        return [p for _, p, _ in self.requests[since:] if p.startswith(prefix)]

    def month_text(self) -> str:
        return self.text(".date-btn")

    def top_week(self) -> str:
        return self.page.evaluate(TOP_WEEK_JS)[0]

    def weeks(self) -> list:
        return self.page.eval_on_selector_all("#grid .wkblock", "els => els.map(e => e.id)")

    def strip_problems(self) -> list:
        return self.page.evaluate(STRIP_JS)

    def cell(self, back: int) -> str:
        return f'.cell[data-d="{self.back(back).isoformat()}"]'

    def bar(self, key: str) -> str:
        return f'[data-bar="{self.t["batch"][key]}"]'

    def chip(self, key: str) -> str:
        return f'[data-chip="{self.t["credit"][key]}"]'

    def marks(self, attr: str) -> dict:
        """Bars or chips on the strip: id -> (classes, labels of its pieces)."""
        got = self.page.eval_on_selector_all(
            f"#grid [data-{attr}]",
            f"els => els.map(e => [e.dataset.{attr}, e.className, e.textContent, e.getAttribute('style')])")
        out = {}
        for mid, cls, text, style in got:
            entry = out.setdefault(int(mid), {"classes": set(), "labels": [], "style": ""})
            entry["classes"].update(c for c in cls.split() if c not in ("cut-l", "cut-r", "spill-l", "lit", "dim"))
            entry["style"] += style or ""
            if text:
                entry["labels"].append(("dashed " if "makeup" in cls else "") + text)
        return out

    def lit(self) -> list:
        return sorted(set(self.page.eval_on_selector_all(
            "#grid .lit", "els => els.map(e => e.dataset.bar ? 'bar ' + e.dataset.bar : e.dataset.chip ? 'chip ' + e.dataset.chip : e.dataset.d)")))

    def title(self) -> str:
        return self.text(".sheet.show .sheet-title")

    def sub(self) -> str:
        return self.text(".sheet.show .sheet-sub")

    def open_mark(self, selector: str, ready: str = ".sheet.show .hero") -> None:
        """A bar or chip: the first tap lights its relation, the second opens it."""
        self.reach(selector)
        self.tap(selector)
        self.tap(selector)
        self.on(ready).first.wait_for()

    def open_cell(self, back: int) -> None:
        self.reach(self.cell(back))
        self.tap(self.cell(back))
        self.on(".sheet.show .orow").first.wait_for()

    def open_leg(self, oid: str) -> None:
        self.tap(f'.sheet.show .orow[data-od="{oid}"]')
        self.on(".sheet.show .field-row").first.wait_for()

    def close_sheets(self) -> None:
        self.tap(".sheet.show [data-close]")
        self.wait(lambda: not self.sheet_open() and not self.count(".scrim.show"), "the sheet and scrim to close")

    def sum_pairs(self, scope: str = ".sheet.show .sum-row") -> list:
        return self.page.eval_on_selector_all(
            scope, "els => els.map(e => [e.querySelector('.k').textContent, e.querySelector('.v').textContent])")

    def pick_statement(self) -> None:
        """Choose a file in the statement picker, as the system dialog would."""
        self.page.locator("#stmtFile").set_input_files(
            {"name": "statement.png", "mimeType": "image/png", "buffer": seed_demo_db._png()})

    def watch(self, root_id: str) -> None:
        """Start recording every change made to the DOM under a view's root."""
        self.page.evaluate(WATCH_JS, root_id)

    def changes(self, root_id: str) -> list:
        return self.page.evaluate("id => window.__seen[id].log.slice(0, 6)", root_id)

    def drag(self, *kinds: str) -> dict:
        return self.page.evaluate(DRAG_JS, list(kinds))

    def scheme(self) -> str:
        return self.page.evaluate("() => getComputedStyle(document.documentElement).colorScheme")

    def scroll_room(self) -> int:
        return self.page.evaluate("() => document.documentElement.scrollHeight - window.innerHeight")

    # -- the service worker and the shell's banners --

    def controlled(self, page=None) -> None:
        (page or self.page).wait_for_function("() => navigator.serviceWorker.controller")

    def worker(self, page=None) -> dict:
        return (page or self.page).evaluate(WORKER_JS)

    def shown_version(self, page=None):
        """The version the document on screen was built as."""
        try:
            return (page or self.page).evaluate(
                "() => document.querySelector('meta[name=asset-version]').content")
        except Exception:      # between two documents
            return None

    def banner(self, which: str, page=None) -> bool:
        return (page or self.page).locator("#banner-" + which).is_visible()

    def mark(self) -> None:
        """Leave something on the page that only a reload removes."""
        self.page.evaluate("() => { window.__mark = 1; }")

    def marked(self) -> bool:
        try:
            return self.page.evaluate("() => window.__mark === 1")
        except Exception:
            return False

    def documents(self) -> int:
        return len([r for r in self.requests if r[2] == "document"])

    def reload(self, ready: str = ".orders .row") -> None:
        self.page.reload()
        self.on(ready).first.wait_for()
        self.settle()

    def look_for_update(self, page=None) -> None:
        """What coming back to the app does: the page asks the browser to
        look for a new worker."""
        (page or self.page).evaluate(
            "() => { document.dispatchEvent(new Event('visibilitychange')); }")

    def deploy(self, **faults) -> str:
        """Replace the serving process with one serving a changed asset, as a
        deploy does, and return the new version. Needs a check with copy=True."""
        self.server.stop()
        with open(os.path.join(self.server.app_root, "static", "js", "lanes.js"), "a") as f:
            f.write("// deployed\n")
        self.server.fault(**faults)
        self.server.start()
        return self.server.version()

    def precached(self) -> list:
        """What the served worker says it stores, the shell included."""
        with urllib.request.urlopen(self.base + "/sw.js", timeout=5) as res:
            listed = re.search(r"^const ASSETS = (\[.*\]);$", res.read().decode(), re.M)
        return sorted(json.loads(listed.group(1)) + [SHELL_KEY])

    def geometry(self) -> dict:
        return self.page.evaluate(BANNER_JS)


# ---- day view: load and navigation (inventory A, K11) ----

@check("day.boot")
def day_boot(s: Session) -> None:
    s.open_day()
    s.eq(s.page.title(), "Ride Dispatch", "document title")
    s.eq(s.date_text(), s.day() + "星期" + "一二三四五六日"[s.today.weekday()] + " · 今日", "date button")
    s.eq(s.rows(), s.ids(), "rows, in the server's order")
    s.expect(s.t["order"]["cancelled"] not in s.rows(), "a cancelled order is listed")
    s.expect(("GET", "/api/orders?date=" + s.day(), "fetch") in s.requests or
             ("GET", "/api/orders?date=" + s.day(), "xhr") in s.requests,
             "today's orders were never asked for")
    box = s.on(".header").first.bounding_box()
    s.eq(round(box["y"]), 0, "header top")


@check("day.boot-requests")
def day_boot_requests(s: Session) -> None:
    s.open_day()
    docs = [r for r in s.requests if r[2] == "document"]
    s.eq(len(docs), 1, "document requests")
    paths = [p for m, p, _ in s.requests]
    s.eq(paths.count("/api/orders?date=" + s.day()), 1, "requests for today (the greeting is not a change)")
    s.eq(paths.count("/api/events"), 1, "event streams")
    # The neighbours are warmed, and only once today's own answer is in.
    done = [p for _, p in s.finished]
    for off in (-1, 1):
        near = "/api/orders?date=" + s.day(off)
        s.eq(paths.count(near), 1, f"prefetches of {near}")
        started_at = [i for i, r in enumerate(s.requests) if r[1] == near][0]
        today_started = [i for i, r in enumerate(s.requests) if r[1] == "/api/orders?date=" + s.day()][0]
        s.expect(started_at > today_started, f"{near} was asked for before today")
    s.expect(done.index("/api/orders?date=" + s.day()) < min(
        done.index("/api/orders?date=" + s.day(o)) for o in (-1, 1)),
        "a neighbour was answered before today")


@check("day.handlers-resolve")
def day_handlers_resolve(s: Session) -> None:
    """Inline handlers resolve on window; every one the shell and its modules
    emit must name a function a module published."""
    s.open_day()
    html = urllib.request.urlopen(s.base + "/").read().decode()
    sources = {"/": html}
    queue = re.findall(r'<script type="module" src="([^"]+)"', html)
    while queue:
        path = queue.pop()
        if path in sources:
            continue
        sources[path] = urllib.request.urlopen(s.base + path).read().decode()
        for spec in re.findall(r"from\s+'(\.[^']+)'", sources[path]):
            queue.append(os.path.normpath(os.path.join(os.path.dirname(path), spec)))
    s.expect(len(sources) >= 8, f"only {len(sources)} sources found")
    handlers = set()
    for path, text in sources.items():
        for attr in re.findall(r'\bon[a-z]+=\\?"([^"]*)"', text):
            call = re.match(r"(rd\.\w+\.\w+)\(", attr)
            s.expect(call, f"{path}: inline handler {attr!r} is not published under rd")
            handlers.add(call.group(1))
    s.expect(len(handlers) >= 10, f"only {len(handlers)} handlers found: {sorted(handlers)}")
    for h in sorted(handlers):
        kind = s.page.evaluate("h => { try { return typeof h.split('.').reduce((o, k) => o[k], window); }"
                               " catch (e) { return 'missing'; } }", h)
        s.eq(kind, "function", h)


@check("day.navigation")
def day_navigation(s: Session) -> None:
    s.open_day()
    s.go_days(1)
    s.expect(s.date_text().startswith(s.day(1)) and "今日" not in s.date_text(), "date button on tomorrow")
    s.eq(s.rows(), s.ids(1), "tomorrow's rows")
    s.go_days(-1)
    s.eq(s.rows(), s.ids(), "today's rows after going back")
    s.expect("今日" in s.date_text(), "date button back on today")
    s.go_days(-1)
    s.eq(s.rows(), s.ids(-1), "yesterday's rows")
    s.tap(".date-btn")
    s.eq(s.rows(), s.ids(), "today's rows after tapping the date")
    s.expect(s.date_text().startswith(s.day()), "date button after tapping the date")
    # The viewed date is not kept across a reload.
    s.go_days(1)
    s.allow("request failed: GET /api/events")     # the reload cuts the event stream
    s.page.reload()
    s.wait(lambda: s.rows() == s.ids(), "today's rows after a reload")


def to_a_day_never_seen(s: Session) -> tuple:
    """Two taps forward with tomorrow's answer held back, which leaves the view
    on a day nothing has been loaded for: the shell warms a day's neighbours
    only once that day's own answer is in. Returns the held path and the rows
    on screen."""
    near = "/api/orders?date=" + s.day(1)
    s.hold(near, "/api/orders?date=" + s.day(2))
    s.press('[aria-label="後一日"]')
    s.wait(lambda: s.holding(near), "the request for tomorrow")
    s.expect(s.date_text().startswith(s.day(1)), "the date button did not change at once")
    shown = s.rows()
    s.press('[aria-label="後一日"]')
    s.wait(lambda: s.date_text().startswith(s.day(2)), "the date button to change")
    return near, shown


@check("day.date-changes-before-the-answer")
def day_date_first(s: Session) -> None:
    s.open_day()
    near, shown = to_a_day_never_seen(s)
    far = "/api/orders?date=" + s.day(2)
    s.wait(lambda: s.holding(far), "the request for the day after tomorrow")
    s.eq(s.rows(), shown, "rows while the answer is pending")
    # One at a time, in the order they were asked.
    answered = s.finished.count(("GET", near))
    s.release(near)
    s.wait(lambda: s.finished.count(("GET", near)) > answered, "tomorrow's answer")
    s.release(far)
    s.wait(lambda: s.count(".empty"), "the empty day to be drawn")
    s.eq(s.text(".empty"), "冇訂單", "empty day text")
    s.release_all()
    s.settle()


@check("day.prefetched-day-paints-before-the-answer")
def day_prefetch_paints(s: Session) -> None:
    s.open_day()
    near = "/api/orders?date=" + s.day(1)
    s.hold(near)
    s.press('[aria-label="後一日"]')
    s.wait(lambda: s.rows() == s.ids(1), "tomorrow's rows from the store")
    s.wait(lambda: s.holding(near), "tomorrow to be asked for again")
    s.release_all()
    s.settle()
    s.eq(s.rows(), s.ids(1), "tomorrow's rows after the answer")


@check("day.late-answer-for-a-day-already-left")
def day_out_of_order(s: Session) -> None:
    s.open_day()
    there, back = "/api/orders?date=" + s.day(1), "/api/orders?date=" + s.day()
    late = s.orders(1)
    late[0]["order_id"] = "LATE-ANSWER"
    s.hold(there, back)
    s.press('[aria-label="後一日"]')
    s.wait(lambda: s.holding(there), "the request for tomorrow")
    s.press('[aria-label="前一日"]')
    s.wait(lambda: s.holding(back), "the request for today")
    s.release(back)
    # Today's answer starts the warm-up of its neighbours. Tomorrow's is held
    # as well, so that the read below is not simply overtaken by it.
    s.wait(lambda: s.holding(there) == 2, "the warm-up of tomorrow")
    # The read made while on tomorrow is answered last, with rows the store
    # has not seen, which it would paint if the view were still on that day.
    s.answer(there, status=200, content_type="application/json",
             body=json.dumps({"orders": late, "date": s.day(1)}))
    s.never(lambda: "LATE-ANSWER" in s.rows() or s.rows() != s.ids(),
            "a late answer painted a day the operator had left")
    s.expect(s.date_text().startswith(s.day()), "date button")
    s.release_all()
    s.settle()
    s.go_days(1)
    s.eq(s.rows(), s.ids(1), "tomorrow's rows on going there again")


@check("day.failed-load")
def day_failed_load(s: Session) -> None:
    s.open_day()
    s.allow("http 500", "status of 500")
    near, shown = to_a_day_never_seen(s)
    s.answer("/api/orders?date=" + s.day(2), status=500, content_type="text/html", body="<html>boom</html>")
    s.wait_toast("載入失敗")
    s.eq(s.rows(), shown, "the previous rows stay on screen")
    s.expect(s.date_text().startswith(s.day(2)), "date button")
    s.release_all()
    s.settle()


@check("day.failed-load-of-a-held-day")
def day_failed_held(s: Session) -> None:
    s.open_day()
    s.go_days(1)
    s.stub("GET", "**/api/orders?date=" + s.day(), 500, "<html>boom</html>", "text/html")
    s.allow("http 500", "status of 500")
    s.press('[aria-label="前一日"]')
    s.wait_toast("載入失敗")
    s.eq(s.rows(), s.ids(), "the day's last known rows")
    s.settle()


@check("day.expired-login-does-not-toast")
def day_auth_expired(s: Session) -> None:
    s.open_day()
    s.allow("http 401", "status of 401")
    s.open_order(s.t["order"]["dropoff"])
    s.stub("PATCH", "**/api/orders/*", 401, "<html>log in</html>", "text/html")
    s.tap(".sheet.show .field-row", has_text="價錢")
    s.keys(".sheet.show", "450")
    s.press(".sheet.show #npOk")
    s.wait(lambda: s.writes, "the write")
    s.never(s.toast, "a toast for an expired login (write)")
    s.expect(s.count(".sheet.show .numpad"), "the numpad stays up")
    s.expect(s.banner("auth"), "the banner for an expired login")
    # Below the banner, which lies over the top of the scrim.
    s.on(".scrim").first.tap(position={"x": 8, "y": 120})
    s.stub("GET", "**/api/orders?date=*", 401, "<html>log in</html>", "text/html")
    s.press('[aria-label="後一日"]')
    s.wait(lambda: s.date_text().startswith(s.day(1)), "the date to change")
    s.never(s.toast, "a toast for an expired login (held day)")
    s.press('[aria-label="後一日"]')
    s.wait(lambda: s.date_text().startswith(s.day(2)), "the date to change")
    s.never(s.toast, "a toast for an expired login (day never seen)")
    s.settle()


@check("day.timing-readout")
def day_timing(s: Session) -> None:
    ms = re.compile(r"\d+ ms")
    s.open("/?perf=1", ".orders .row")
    s.expect("perf" not in s.page.url, "?perf= stays in the address")
    s.wait_toast(ms)
    s.page.wait_for_timeout(2600)
    s.eq(s.toast(), "", "the toast after its time")
    s.press('[aria-label="後一日"]')
    s.wait_toast(ms)
    s.settle()
    # Holding the date button switches the readout; the tap that ends the hold
    # must not also send the view back to today.
    box = s.on(".date-btn").first.bounding_box()
    s.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    s.page.mouse.down()
    s.wait_toast("計時 關")
    s.page.mouse.up()
    s.settle()
    s.expect(s.date_text().startswith(s.day(1)), "the hold's release went to today")
    s.page.wait_for_timeout(2600)
    s.press('[aria-label="後一日"]')
    s.wait(lambda: s.date_text().startswith(s.day(2)), "the date to change")
    s.settle()
    s.never(s.toast, "a timing toast with the readout off", ms=500)
    s.page.mouse.down()
    s.wait_toast("計時 開")
    s.page.mouse.up()
    # ?perf=0 clears it on load.
    s.allow("request failed: GET /api/events")     # the navigation cuts the event stream
    s.page.goto(s.base + "/?perf=0")
    s.on(".orders .row").first.wait_for()
    s.settle()
    s.eq(s.page.evaluate("() => localStorage.getItem('perf')"), None, "the key after ?perf=0")


# ---- day view: chips, rows and marks (inventory A11-A12, B, C, K9) ----

@check("day.filter-chips")
def day_filter(s: Session) -> None:
    s.open_day()
    orders = s.orders()

    def plat(o):
        return {"滴滴": "didi", "Uber": "uber", "foodpanda": "foodpanda"}.get(o["service_type"], "ride")

    def money(n):
        return f"{n:.2f}" if n % 1 else f"{n:.0f}"

    def summary(shown):
        priced = [o for o in shown if o["price"]]
        total = sum(o["price"] + (o["banner_fee"] or 0) for o in priced)
        text = f"{len(shown)} 程 · ${money(total)}"
        if len(shown) > len(priced):
            text += f" · {len(shown) - len(priced)} 未入價"
        return text

    n = {k: len([o for o in orders if plat(o) == k]) for k in ("ride", "didi", "uber", "foodpanda")}
    chips = s.page.eval_on_selector_all(".chips .chip", "els => els.map(e => e.textContent)")
    s.eq(chips, [f"接送 {n['ride']}", f"滴滴 {n['didi']}", f"Uber {n['uber']}", f"熊貓 {n['foodpanda']}"], "chips")
    s.eq(s.text(".summary"), summary(orders), "summary, no filter")
    s.expect(s.count(".summary .warn"), "the unpriced count is not marked")

    s.tap(".chip", has_text="滴滴")
    didi = [o for o in orders if plat(o) == "didi"]
    s.eq(s.rows(), [o["order_id"] for o in didi], "rows under the 滴滴 filter")
    s.eq(s.text(".summary"), summary(didi), "summary under the filter")
    s.eq(s.text(".chip.on"), f"滴滴 {n['didi']}", "highlighted chip")
    s.tap(".chip", has_text="接送")
    ride = [o for o in orders if plat(o) == "ride"]
    s.eq(s.rows(), [o["order_id"] for o in ride], "rows under the 接送 filter")
    s.eq(s.text(".summary"), summary(ride), "summary under the filter")
    # NEXT and NOW are worked out over the rows showing.
    s.eq(s.count(".orders .row.next"), 1, "NEXT rows under a filter")
    s.eq(s.count(".orders .now"), 1, "NOW lines under a filter")
    s.tap(".chip", has_text="接送")
    s.eq(s.count(".chip.on"), 0, "highlighted chips after tapping the filter again")
    s.eq(s.rows(), [o["order_id"] for o in orders], "rows with the filter cleared")

    # The filter survives a change of date; a platform with nothing is dimmed.
    s.tap(".chip", has_text="滴滴")
    s.go_days(1)
    s.eq(s.text(".chip.on"), "滴滴 0", "filter after changing date")
    s.expect("zero" in s.on(".chip.on").first.get_attribute("class"), "an empty platform's chip is not dimmed")
    s.eq(s.text(".empty"), "冇滴滴訂單", "empty text under a filter")
    s.eq(s.text(".summary"), "0 程 · $0", "summary of nothing")


@check("day.rows-and-marks")
def day_rows(s: Session) -> None:
    s.open_day()
    o = s.t["order"]

    def cls(oid):
        return s.on(s.row(oid)).first.get_attribute("class").split()

    def rail(oid):
        return s.page.eval_on_selector_all(s.row(oid) + " .rail > div", "els => els.map(e => e.textContent)")

    # NEXT is the first row not yet done; rows before it that are done are dimmed.
    s.eq([r for r in s.rows() if "next" in cls(r)], [o["landed_banner"]], "NEXT row")
    s.expect("done" in cls(o["done_pickup"]) and "done" in cls(o["dropoff"]), "finished rows are not dimmed")
    s.expect("done" not in cls(o["upcoming_hotel"]), "a row still to come is dimmed")
    # The NOW line sits before the first row later than the clock.
    s.eq(s.text(".orders .now .now-t"), "14:00", "NOW time")
    after_now = s.page.evaluate(
        "() => document.querySelector('.orders .now').closest('.gap').nextElementSibling.dataset.oid")
    later = [x["order_id"] for x in s.orders() if x["row_time"] > s.day() + " 14:00:00"]
    s.eq(after_now, later[0], "the row under the NOW line")
    box = s.on(s.row(o["landed_banner"])).first.bounding_box()
    height = s.page.viewport_size["height"]
    s.expect(box["y"] >= 0 and box["y"] + box["height"] <= height, "the NEXT row is not on screen after the load")

    # A 接机 leads with its landing time and says what that time is.
    s.eq(rail(o["done_pickup"])[:2], ["09:12", "已到閘"], "rail of a pickup at the gate")
    depart = [x for x in s.orders() if x["order_id"] == o["landed_banner"]][0]["depart_hhmm"]
    s.eq(rail(o["landed_banner"]), ["13:42", "已降落", "出發 " + depart, "用車 14:27"], "rail of a landed pickup")
    s.eq(rail(o["upcoming_hotel"])[:2], ["16:55", "預計"], "rail of a pickup still in the air")
    s.eq(rail(o["dropoff"]), ["11:00"], "rail of a 送机")
    s.eq(s.text(s.row(o["landed_banner"]) + " .flt"), "UO623", "flight box")
    s.eq(s.text(s.row(o["landed_banner"]) + " .route .big"), "灣仔例子酒店", "shortened destination")
    s.eq(s.text(s.row(o["dropoff"]) + " .route .small"), "機場", "the airport end of a 送机")
    s.eq(s.page.eval_on_selector_all(s.row(o["landed_banner"]) + " .tag", "els => els.map(e => e.className + '|' + e.textContent)"),
         ["tag banner|舉牌", "tag neutral|出場 45"], "tags")
    s.eq(s.text(s.row(o["upcoming_hotel"]) + " .tag"), "出場 20", "exit tag")
    s.expect("urgent" in s.on(s.row(o["upcoming_hotel"]) + " .tag").first.get_attribute("class"), "a 20 minute exit is not urgent")
    s.eq(s.text(s.row(o["landed_banner"]) + " .price"), "$560", "gross price with the banner fee")
    s.eq(s.text(s.row(o["unpriced"]) + " .price.unset"), "未入價", "unpriced row")
    quick = [r for r in s.rows() if "quick" in cls(r)]
    s.eq(len(quick), 3, "quick rows")
    s.eq(s.text(s.row(quick[0]) + " .qplat"), "滴滴", "platform name on a quick row")
    s.eq(s.text(s.row(quick[2]) + " .qprice"), "$55.50", "cents on a quick row")
    s.eq(s.page.eval_on_selector_all(".orders .gap-label", "els => els.map(e => e.textContent)")[:2],
         ["1h 48m", "1h 10m"], "gap labels")
    # Another day has neither mark.
    s.go_days(1)
    s.eq(s.count(".orders .row.next") + s.count(".orders .now"), 0, "NEXT or NOW on another day")


@check("day.minute-tick", clock="installed")
def day_minute_tick(s: Session) -> None:
    s.open_day()
    o = s.t["order"]
    s.eq(s.text(".orders .now .now-t"), "14:00", "NOW time at the start")
    asked = len(s.requests)
    s.page.clock.fast_forward(100 * 60 * 1000)
    s.wait(lambda: s.text(".orders .now .now-t") in ("15:40", "15:41"), "the NOW line to follow the clock")
    s.eq(s.on(".orders .row.next").first.get_attribute("data-oid"), o["upcoming_hotel"], "NEXT after 100 minutes")
    s.expect("done" in s.on(s.row(o["landed_banner"])).first.get_attribute("class"), "a row that finished is not dimmed")
    s.eq(len(s.requests), asked, "requests made by the minute tick")


# ---- order sheet (inventory D) ----

@check("day.order-sheet")
def day_order_sheet(s: Session) -> None:
    s.open_day()
    oid = s.t["order"]["landed_banner"]
    s.open_order(oid)
    s.eq(s.text(".sheet.show .sheet-title"), "接機 14:20", "title")
    s.eq(s.text(".sheet.show .sheet-sub"), "#" + oid[-6:], "subtitle")
    info = s.info()
    s.eq(list(info), ["乘客", "電話", "境外", "航班", "車型", "路線", "備註", "結算"], "info rows")
    s.eq(info["航班"], "UO623 · 已降落 13:42", "flight row")
    s.eq(info["結算"], "未結算", "settlement row")
    s.eq(s.page.eval_on_selector_all(".sheet.show .info-row a", "els => els.map(e => e.getAttribute('href'))"),
         ["tel:+8613800000102", "tel:+886900000102"], "phone links")
    s.eq(s.page.eval_on_selector_all(".sheet.show .field-row .fk", "els => els.map(e => e.textContent)"),
         ["價錢", "隧道費", "停車費", "舉牌費", "時間"], "editable fields of a ride")
    s.eq(s.page.eval_on_selector_all(".sheet.show .pp-opt", "els => els.map(e => e.textContent + (e.classList.contains('on') ? '*' : ''))"),
         ["P1", "P4*", "富豪"], "pickup points")
    s.eq(s.on(".sheet.show .tg-link").first.get_attribute("href"),
         "https://t.me/agent_ride_bot?start=order_" + oid, "Telegram link")

    # Money numpad.
    s.tap(".sheet.show .field-row", has_text="價錢")
    s.eq(s.text(".sheet.show .sheet-title"), "改價錢", "numpad title")
    s.eq(s.text(".sheet.show .numpad-hint"), "而家 $520", "numpad hint")
    s.eq(s.text(".sheet.show .numpad-display"), "$0", "empty display")
    s.expect(s.on(".sheet.show #npOk").first.is_disabled(), "confirm is enabled with nothing typed")
    s.keys(".sheet.show", ".")
    s.eq(s.text(".sheet.show .numpad-display"), "$0.", "a leading dot")
    s.keys(".sheet.show", "5.12345678")
    s.eq(s.text(".sheet.show .numpad-display"), "$0.51234", "one dot, seven characters")
    s.expect(s.on(".sheet.show #npOk").first.is_enabled(), "confirm is disabled for a valid number")
    # ✕ on a stacked view goes back one level; on the detail it closes.
    s.tap(".sheet.show .sheet-x")
    s.eq(s.text(".sheet.show .sheet-title"), "接機 14:20", "back on the detail")
    # Time numpad.
    s.tap(".sheet.show .field-row", has_text="時間")
    s.eq(s.text(".sheet.show .sheet-title"), "改時間", "time numpad title")
    s.eq(s.text(".sheet.show .numpad-display"), "––:––", "empty time")
    s.eq(s.count('.sheet.show .key[data-k="."]'), 0, "a dot key on the time numpad")
    s.keys(".sheet.show", "2599")
    s.eq(s.text(".sheet.show .numpad-display"), "25:99", "typed time")
    s.expect(s.on(".sheet.show #npOk").first.is_disabled(), "confirm is enabled for 25:99")
    # The scrim closes everything, whatever is stacked.
    s.on(".scrim").first.tap(position={"x": 8, "y": 8})
    s.wait(lambda: not s.sheet_open() and not s.count(".scrim.show"), "the sheet and scrim to close")
    s.open_order(oid)
    s.tap(".sheet.show .sheet-x")
    s.wait(lambda: not s.sheet_open() and not s.count(".scrim.show"), "✕ on the detail to close the sheet")

    # A quick order has its own fields and no route.
    quick = [r for r in s.rows() if r.startswith("uber_")][0]
    s.open_order(quick)
    s.eq(s.text(".sheet.show .sheet-title"), "Uber 15:05", "quick order title")
    s.eq(s.page.eval_on_selector_all(".sheet.show .field-row .fk", "els => els.map(e => e.textContent)"),
         ["總收入", "通行費", "時間"], "editable fields of an Uber order")
    s.eq(list(s.info()), ["結算"], "info rows of a quick order")
    s.eq(s.count(".sheet.show .tg-link"), 0, "Telegram links on a quick order")
    s.eq(s.writes, [], "writes")


@check("day.edit-fields")
def day_edit_fields(s: Session) -> None:
    s.open_day()
    oid = s.t["order"]["landed_banner"]
    s.open_order(oid)
    path = "/api/orders/" + oid
    s.edit("價錢", "600")
    s.eq(s.last_write(), ("PATCH", path, {"price": 600}), "price write")
    s.eq(s.field("價錢"), "$600", "price on the sheet")
    s.eq(s.text(s.row(oid) + " .price"), "$640", "price on the row behind")
    s.edit("隧道費", "45")
    s.eq(s.last_write(), ("PATCH", path, {"tunnel_fee": 45}), "tunnel fee write")
    s.eq(s.field("隧道費"), "$45", "tunnel fee on the sheet")
    s.edit("停車費", "20.5")
    s.eq(s.last_write(), ("PATCH", path, {"parking_fee": 20.5}), "parking fee write")
    s.eq(s.field("停車費"), "$20.50", "parking fee on the sheet")
    s.edit("舉牌費", "50")
    s.eq(s.last_write(), ("PATCH", path, {"banner_fee": 50}), "banner fee write")
    s.eq(s.text(s.row(oid) + " .price"), "$650", "gross on the row behind")
    s.edit("時間", "1545")
    s.eq(s.last_write(), ("PATCH", path, {"time": "15:45"}), "time write")
    s.eq(s.text(".sheet.show .sheet-title"), "接機 15:45", "title after the time change")
    s.eq(len(s.writes), 5, "writes")
    saved = [x for x in s.orders() if x["order_id"] == oid][0]
    s.eq((saved["price"], saved["tunnel_fee"], saved["parking_fee"], saved["banner_fee"], saved["scheduled_time"]),
         (600, 45, 20.5, 50, s.day() + " 15:45:00"), "what the server holds")


@check("day.pickup-point-and-waiving-parking")
def day_pickup_point(s: Session) -> None:
    s.open_day()
    oid = s.t["order"]["landed_banner"]
    path = "/api/orders/" + oid
    s.open_order(oid)
    s.tap(".sheet.show .pp-opt", has_text="P4")
    s.eq(s.writes, [], "tapping the current point sends nothing")
    s.tap(".sheet.show .pp-opt", has_text="P1")
    s.eq(s.last_write(), ("PATCH", path, {"pickup_point": "P1"}), "pickup point write")
    s.wait(lambda: s.field("停車費") == "$35", "P1's parking charge")
    s.eq(s.text(".sheet.show .pp-opt.on"), "P1", "highlighted point")
    s.tap(".sheet.show .fr-waive")
    s.eq(s.last_write(), ("PATCH", path, {"parking_fee": 0}), "waive write")
    s.wait(lambda: s.field("停車費") == "$0", "the parking fee to clear")
    s.eq(s.count(".sheet.show .fr-waive"), 0, "the 免 pill with nothing to waive")
    s.tap(".sheet.show .pp-opt", has_text="富豪")
    s.eq(s.last_write(), ("PATCH", path, {"pickup_point": "富豪"}), "pickup point write")
    s.tap(".sheet.show .pp-opt", has_text="P4")
    s.eq(s.last_write(), ("PATCH", path, {"pickup_point": "P4"}), "pickup point write")
    s.wait(lambda: s.field("停車費") == "$32", "P4's parking charge")
    # The same waiver from inside the numpad.
    s.tap(".sheet.show .field-row", has_text="停車費")
    s.eq(s.text(".sheet.show #npQuick"), "免停車費", "waive button on the numpad")
    s.tap(".sheet.show #npQuick")
    s.eq(s.last_write(), ("PATCH", path, {"parking_fee": 0}), "waive write from the numpad")
    s.wait(lambda: not s.count(".sheet.show .numpad") and s.field("停車費") == "$0", "back on the detail, fee cleared")
    s.eq(len(s.writes), 5, "writes")


@check("day.cancel-order")
def day_cancel(s: Session) -> None:
    s.open_day()
    oid = s.t["order"]["dropoff"]
    s.open_order(oid)
    s.tap(".sheet.show .cancel-link")
    s.eq(s.text(".sheet.show .sheet-title"), "取消訂單", "confirm title")
    s.eq(s.text(".sheet.show .sheet-sub"), "#" + oid[-6:], "confirm subtitle")
    s.expect("11:00 · DEMO/DELTA" in s.text(".sheet.show .cancel-info"), "who is being cancelled")
    s.tap(".sheet.show .ghost-btn", has_text="返回")
    s.eq(s.text(".sheet.show .sheet-title"), "送機 11:00", "返回 goes back to the detail")
    s.tap(".sheet.show .cancel-link")
    s.tap(".sheet.show .sheet-x")
    s.eq(s.text(".sheet.show .sheet-title"), "送機 11:00", "✕ goes back to the detail")
    s.eq(s.writes, [], "writes before confirming")
    s.tap(".sheet.show .cancel-link")
    s.press(".sheet.show .primary-btn.danger")
    s.wait_toast("已取消 #" + oid[-6:])
    s.eq(s.last_write(), ("PATCH", "/api/orders/" + oid, {"status": "cancelled"}), "cancel write")
    s.settle()
    s.expect(not s.sheet_open() and not s.count(".scrim.show"), "the sheet stays open after a cancel")
    s.expect(oid not in s.rows(), "the cancelled order is still listed")
    s.eq(len(s.writes), 1, "writes")


@check("day.refused-edit")
def day_refused_edit(s: Session) -> None:
    s.open_day()
    oid = s.t["order"]["dropoff"]
    s.open_order(oid)
    s.refuse("PATCH", "**/api/orders/*")
    s.tap(".sheet.show .field-row", has_text="價錢")
    s.keys(".sheet.show", "450")
    s.press(".sheet.show #npOk")
    s.wait_toast("測試拒絕")
    s.eq(s.text(".sheet.show .sheet-title"), "改價錢", "the numpad stays up")
    s.eq(s.text(".sheet.show .numpad-display"), "$0", "the numpad is cleared for another try")
    s.tap(".sheet.show .sheet-x")
    s.page.wait_for_timeout(2500)
    s.tap(".sheet.show .cancel-link")
    s.press(".sheet.show .primary-btn.danger")
    s.wait_toast("測試拒絕")
    s.eq(s.text(".sheet.show .primary-btn.danger"), "確認取消", "the cancel button comes back")
    s.expect(s.on(".sheet.show .primary-btn.danger").first.is_enabled(), "the cancel button stays disabled")
    s.settle()


@check("day.batched-order-is-locked")
def day_batch_locked(s: Session) -> None:
    s.open_day()
    s.go_days(-5)
    oid = seed_demo_db._oid(503)
    s.open_order(oid)
    locked = s.page.eval_on_selector_all(".sheet.show .field-row.locked",
                                         "els => els.map(e => e.querySelector('.fk').textContent + '|' + e.querySelector('.chev').textContent)")
    s.eq(locked, ["價錢|已結算", "隧道費|已結算", "舉牌費|已結算"], "locked fields")
    s.press(".sheet.show .field-row", has_text="價錢")
    s.wait_toast("已結算嘅單要先撤銷結算")
    s.eq(s.count(".sheet.show .numpad"), 0, "a numpad for a locked field")
    s.eq(s.text(".sheet.show .cancel-note"), "已結算嘅單要先撤銷結算先取消得", "cancel note")
    s.eq(s.count(".sheet.show .cancel-link"), 0, "cancel links on a batched order")
    settled = s.today - timedelta(days=2)
    s.eq(s.info()["結算"], f"等過數 · 結算 {settled.month}/{settled.day}", "settlement row")
    s.tap(".sheet.show .field-row", has_text="停車費")
    s.eq(s.text(".sheet.show .sheet-title"), "改停車費", "parking stays editable")
    s.eq(s.writes, [], "writes")


# ---- add flow (inventory E) ----

def quick_order(s: Session, platform: str, time_digits: str, amount: str, toll) -> None:
    """Walk the quick-order stages up to the confirm stage. `toll` is the
    digits to type, None to tap the no-toll button, or False when the
    platform has no toll stage."""
    s.tap(f".drop.show .quick-type-btn.{platform}")
    s.keys(".drop.show", time_digits)
    s.confirm_stage()
    s.keys(".drop.show", amount)
    s.confirm_stage()
    if toll is None:
        s.tap(".drop.show #npQuick")
    elif toll is not False:
        s.keys(".drop.show", toll)
        s.confirm_stage()
    s.on(".drop.show #addSave").wait_for()


@check("day.add-didi")
def day_add_didi(s: Session) -> None:
    s.open_day()
    before = s.rows()
    s.tap('[aria-label="入單"]')
    s.stage("入單")
    s.eq(s.text(".drop.show .sheet-sub"), s.day() + " 星期" + "一二三四五六日"[s.today.weekday()], "panel subtitle")
    s.eq(s.page.evaluate("() => document.activeElement.className"), "paste-box", "focused element")
    s.eq(s.on(".drop.show .paste-box").first.get_attribute("placeholder"), "喺度貼訂單 message", "placeholder")
    s.eq(s.page.eval_on_selector_all(".drop.show .quick-type-btn", "els => els.map(e => e.textContent)"),
         ["滴滴", "Uber", "foodpanda"], "quick buttons")
    s.tap(".drop.show .quick-type-btn.didi")
    s.stage("滴滴 · 時間")
    s.eq(s.text(".drop.show .numpad-hint"), s.day(), "time stage hint")
    s.keys(".drop.show", "1530")
    s.confirm_stage()
    s.stage("滴滴 · 車費")
    s.eq(s.text(".drop.show .numpad-hint"), "15:30", "amount stage hint")
    s.keys(".drop.show", "128")
    s.confirm_stage()
    s.stage("滴滴 · 隧道費")
    s.eq(s.text(".drop.show #npQuick"), "冇隧道費", "no-toll button")
    s.keys(".drop.show", "25")
    s.confirm_stage()
    s.stage("滴滴 · 確認")
    weekday = "一二三四五六日"[s.today.weekday()]
    s.eq(s.sum_rows(), [["日子", f"{s.day()} 星期{weekday}"], ["時間", "15:30"], ["車費", "$128"],
                        ["隧道費", "$25"], ["淨收入", "$103"]], "confirm rows")
    s.press(".drop.show #addSave")
    s.wait_toast("滴滴 已入單")
    s.eq(s.last_write(), ("POST", "/api/orders", {"type": "didi", "date": s.day(), "time": "15:30",
                                                  "price": 128, "tunnel_fee": 25}), "create write")
    s.settle()
    s.expect(not s.panel_open() and not s.count(".scrim.show"), "the panel stays open after a save")
    new = [r for r in s.rows() if r not in before]
    s.eq(len(new), 1, "new rows")
    s.eq(s.text(s.row(new[0]) + " .rail .t"), "15:30", "the new row's time")
    s.eq(s.text(s.row(new[0]) + " .qprice"), "$128", "the new row's price")
    s.page.wait_for_timeout(400)
    s.eq(s.page.eval_on_selector(".drop", "e => e.innerHTML"), "", "the closed panel's content")
    s.eq(len(s.writes), 1, "writes")


@check("day.add-uber-clears-another-filter")
def day_add_uber(s: Session) -> None:
    s.open_day()
    s.tap(".chip", has_text="滴滴")
    s.tap('[aria-label="入單"]')
    quick_order(s, "uber", "0910", "96", "20")
    s.stage("Uber · 確認")
    s.eq(s.sum_rows()[2:], [["行程收入", "$96"], ["通行費", "$20"], ["總收入", "$116"]], "confirm rows")
    s.press(".drop.show #addSave")
    s.wait_toast("Uber 已入單")
    # Stored price is trip income plus the reimbursed toll.
    s.eq(s.last_write(), ("POST", "/api/orders", {"type": "uber", "date": s.day(), "time": "09:10",
                                                  "price": 116, "tunnel_fee": 20}), "create write")
    s.settle()
    s.eq(s.count(".chip.on"), 0, "the filter that would hide the new row")
    s.eq(s.rows(), s.ids(), "rows")


@check("day.add-without-toll")
def day_add_no_toll(s: Session) -> None:
    s.open_day()
    s.tap('[aria-label="入單"]')
    quick_order(s, "didi", "0805", "70", None)
    s.eq(s.sum_rows()[2:], [["車費", "$70"], ["隧道費", "冇"]], "confirm rows with no toll")
    s.press(".drop.show #addSave")
    s.wait_toast("滴滴 已入單")
    s.eq(s.last_write(), ("POST", "/api/orders", {"type": "didi", "date": s.day(), "time": "08:05",
                                                  "price": 70, "tunnel_fee": 0}), "create write")
    s.settle()


@check("day.add-foodpanda-on-the-viewed-date")
def day_add_foodpanda(s: Session) -> None:
    s.open_day()
    s.go_days(1)
    s.tap('[aria-label="入單"]')
    s.expect(s.text(".drop.show .sheet-sub").startswith(s.day(1)), "the panel names the viewed date")
    s.tap(".drop.show .quick-type-btn.foodpanda")
    s.stage("foodpanda · 時間")
    s.keys(".drop.show", "1200")
    s.confirm_stage()
    s.stage("foodpanda · 價錢")
    s.keys(".drop.show", "55.5")
    s.confirm_stage()
    # No toll stage for this platform.
    s.stage("foodpanda · 確認")
    s.eq([r[0] for r in s.sum_rows()], ["日子", "時間", "價錢"], "confirm rows")
    s.press(".drop.show #addSave")
    s.wait_toast("foodpanda 已入單")
    s.eq(s.last_write(), ("POST", "/api/orders", {"type": "foodpanda", "date": s.day(1), "time": "12:00",
                                                  "price": 55.5, "tunnel_fee": 0}), "create write")
    s.settle()
    s.eq(s.rows(), s.ids(1), "tomorrow's rows with the new order")
    s.eq(len(s.rows()), 3, "rows on tomorrow")


@check("day.add-back-and-close")
def day_add_back(s: Session) -> None:
    s.open_day()
    s.tap('[aria-label="入單"]')
    s.tap(".drop.show .quick-type-btn.didi")
    s.keys(".drop.show", "1530")
    s.confirm_stage()
    s.stage("滴滴 · 車費")
    s.keys(".drop.show", "9")
    # ✕ goes back one stage, which still holds what was entered on it.
    s.tap(".drop.show .sheet-x")
    s.stage("滴滴 · 時間")
    s.eq(s.text(".drop.show .numpad-display"), "15:30", "the time stage on going back to it")
    s.tap(".drop.show .sheet-x")
    s.stage("入單")
    # A stage starts empty each time it is entered.
    s.tap(".drop.show .quick-type-btn.didi")
    s.eq(s.text(".drop.show .numpad-display"), "––:––", "the time stage on re-entry")
    s.keys(".drop.show", "1530")
    s.confirm_stage()
    s.eq(s.text(".drop.show .numpad-display"), "$0", "the amount stage on re-entry")
    s.keys(".drop.show", "128")
    s.confirm_stage()
    s.keys(".drop.show", "25")
    s.confirm_stage()
    s.stage("滴滴 · 確認")
    s.tap(".drop.show .ghost-btn", has_text="返上一步")
    s.stage("滴滴 · 隧道費")
    s.tap(".drop.show .sheet-x")
    s.stage("滴滴 · 車費")
    # The scrim closes the whole panel from any stage.
    height = s.page.viewport_size["height"]
    s.on(".scrim").first.tap(position={"x": 8, "y": height - 8})
    s.wait(lambda: not s.panel_open() and not s.count(".scrim.show"), "the panel and scrim to close")
    # ✕ on the first stage closes it too.
    s.tap('[aria-label="入單"]')
    s.stage("入單")
    s.tap(".drop.show .sheet-x")
    s.wait(lambda: not s.panel_open() and not s.count(".scrim.show"), "✕ on the first stage to close the panel")
    s.eq(s.writes, [], "writes")


@check("day.add-refused")
def day_add_refused(s: Session) -> None:
    s.open_day()
    s.refuse("POST", "**/api/orders")
    s.tap('[aria-label="入單"]')
    quick_order(s, "foodpanda", "1200", "40", False)
    s.press(".drop.show #addSave")
    s.wait_toast("測試拒絕")
    s.eq(s.text(".drop.show #addSave"), "儲存", "the save button comes back")
    s.expect(s.on(".drop.show #addSave").first.is_enabled(), "the save button stays disabled")
    s.settle()


@check("day.add-panel-animates", still=False)
def day_add_animates(s: Session) -> None:
    """The panel grows between stages by an inline height that a listener
    takes off again when the transition ends, and is emptied once it has slid
    away."""
    s.open_day()
    s.tap('[aria-label="入單"]')
    s.stage("入單")
    s.page.evaluate("""() => {
      const drop = document.querySelector('.drop.show');
      window.__heights = [];
      new MutationObserver(() => window.__heights.push(drop.style.height))
        .observe(drop, { attributes: true, attributeFilter: ['style'] });
    }""")
    s.press(".drop.show .quick-type-btn.didi")
    s.stage("滴滴 · 時間")
    s.wait(lambda: any(h.endswith("px") for h in s.page.evaluate("() => window.__heights")),
           "the panel to be given a height to animate from")
    s.wait(lambda: s.page.evaluate("() => document.querySelector('.drop.show').style.height") == "",
           "the inline height to be released when the transition ends")
    s.press(".drop.show .sheet-x")
    s.stage("入單")
    s.press(".drop.show .sheet-x")
    s.wait(lambda: s.page.eval_on_selector(".drop", "e => !e.classList.contains('show') && e.innerHTML === ''"),
           "the closed panel to be emptied")
    s.wait(lambda: not s.count(".scrim.show"), "the scrim to go")


# ---- paste flow (inventory F) ----

def paste(s: Session, text: str) -> None:
    s.on(".drop.show .paste-box").first.fill(text)
    s.tap(".drop.show .primary-btn", has_text="解析")


def amended(**changes: str) -> str:
    """The synthetic message with some of its lines rewritten."""
    text = paste_message()
    for old, new in changes.items():
        if old not in text:
            raise Failed(f"the message has no {old!r}")
        text = text.replace(old, new)
    return text


PASTE_ID = "1128000000000002"       # the order number in the synthetic message
PASTE_DAY = "2026-06-27"


@check("day.paste-new-order")
def day_paste_new(s: Session) -> None:
    msg = paste_message()
    s.open_day()
    s.tap(".chip", has_text="滴滴")
    s.tap('[aria-label="入單"]')
    s.tap(".drop.show .primary-btn", has_text="解析")
    s.eq(s.writes, [], "解析 with an empty box sends nothing")
    paste(s, msg)
    s.eq(s.last_write(), ("POST", "/api/orders/parse", {"text": msg.strip()}), "parse write")
    s.stage("#000002 · 入價")
    preview = s.page.eval_on_selector_all(
        ".drop.show .paste-preview .sum-row",
        "els => els.map(e => [e.querySelector('.k').textContent, e.querySelector('.v').textContent, e.querySelector('.v').className])")
    s.eq([r[0] for r in preview], ["平台", "用車時間", "乘客", "境外", "航班", "出場", "車型", "路線", "備註", "停車費"],
         "preview rows")
    s.eq(preview[0][1], "携程 · 接機", "platform row")
    s.eq(preview[1][1], PASTE_DAY + " 12:35", "time row")
    s.eq(preview[5][1:], ["30分鐘 — 降落即刻出發", "v tight"], "exit row")
    # A suggested price: prefilled, steppers either side, keypad tucked away.
    s.eq(s.text(".drop.show .numpad-display"), "$480建議", "suggested amount")
    s.expect(s.on(".drop.show #npOk").first.is_enabled(), "confirm is disabled with a suggestion")
    s.expect("open" not in s.page.locator(".drop.show #npPad").get_attribute("class"), "the keypad is open beside a suggestion")
    s.press('.drop.show .step[data-s="10"]')
    s.eq(s.text(".drop.show .numpad-display"), "$490", "after +$10")
    # ✕ returns to the form with the message still in the box.
    s.tap(".drop.show .sheet-x")
    s.stage("入單")
    s.eq(s.on(".drop.show .paste-box").first.input_value(), msg.strip(), "the pasted text after going back")
    s.tap(".drop.show .primary-btn", has_text="解析")
    s.stage("#000002 · 入價")
    s.press('.drop.show .step[data-s="-10"]')
    s.press('.drop.show .step[data-s="-10"]')
    s.eq(s.text(".drop.show .numpad-display"), "$460", "after −$10 twice")
    s.press(".drop.show #npOk")
    s.wait_toast("已入單 #000002")
    s.eq(s.last_write(), ("POST", "/api/orders", {"type": "paste", "text": msg.strip(), "price": 460}), "create write")
    s.settle()
    # The view moves to the order's own date, and a filter that hides it goes.
    s.expect(s.date_text().startswith(PASTE_DAY), "the view did not move to the order's date")
    s.eq(s.rows(), [PASTE_ID], "rows on the order's date")
    s.eq(s.count(".chip.on"), 0, "the filter that would hide the new row")
    s.eq(s.text(s.row(PASTE_ID) + " .price"), "$460", "the new row's price")
    s.expect(not s.panel_open(), "the panel stays open after a save")
    s.eq(len(s.writes), 3, "writes")


@check("day.paste-own-price-and-skip")
def day_paste_own_price(s: Session) -> None:
    msg = paste_message()
    s.open_day()
    s.tap('[aria-label="入單"]')
    paste(s, msg)
    s.stage("#000002 · 入價")
    s.tap(".drop.show #npPadToggle")
    s.expect("open" in s.on(".drop.show #npPad").first.get_attribute("class"), "the keypad did not open")
    s.expect("gone" in s.page.locator(".drop.show #npOpt").get_attribute("class"), "自己入價 stays after opening the keypad")
    s.expect("expanded" in s.on(".drop.show #npRow").first.get_attribute("class"), "the steppers stay beside an open keypad")
    # The first key replaces the suggestion.
    s.keys(".drop.show", "5")
    s.eq(s.text(".drop.show .numpad-display"), "$5", "typing over the suggestion")
    s.keys(".drop.show", "20")
    s.press(".drop.show #npOk")
    s.wait_toast("已入單 #000002")
    s.eq(s.last_write(), ("POST", "/api/orders", {"type": "paste", "text": msg.strip(), "price": 520}), "create write")
    s.settle()

    # Saving without a price, as another order.
    other = amended(**{PASTE_ID: "1128000000000077"})
    s.tap('[aria-label="入單"]')
    paste(s, other)
    s.stage("#000077 · 入價")
    s.eq(s.text(".drop.show #npSkip"), "先唔入價，直接儲存", "skip link")
    s.press(".drop.show #npSkip")
    s.wait_toast("已入單 #000077")
    s.eq(s.last_write(), ("POST", "/api/orders", {"type": "paste", "text": other.strip()}), "create write without a price")
    s.settle()
    s.eq(s.text(s.row("1128000000000077") + " .price"), "未入價", "the unpriced row")


@check("day.paste-resent-message")
def day_paste_resent(s: Session) -> None:
    msg = paste_message()
    s.api("POST", "/api/orders", {"type": "paste", "text": msg, "price": 500})
    s.open_day()
    # The same message again: nothing to change, nothing to save.
    s.tap('[aria-label="入單"]')
    paste(s, msg)
    s.stage("貼單 · 冇變更")
    s.eq(s.text(".drop.show .dup-warn"), "#000002 同 DB 一樣，冇嘢改", "duplicate warning")
    s.eq(s.count(".drop.show #npOk") + s.count(".drop.show .numpad"), 0, "ways to save a duplicate")
    s.expect(s.count(".drop.show .paste-preview .sum-row") >= 8, "the preview under the warning")
    s.tap(".drop.show .sheet-x")
    s.stage("入單")

    # The message with a new time: an amendment, priced at what the order has.
    later = amended(**{"12:35:00": "13:05:00"})
    paste(s, later)
    s.stage("#000002 · 更新")
    changes = s.page.eval_on_selector_all(
        ".drop.show .paste-preview.changes .sum-row",
        "els => els.map(e => [e.querySelector('.k').textContent, e.querySelector('.was').textContent, e.querySelector('.v').textContent])")
    s.eq(len(changes), 1, "changed fields")
    s.expect("12:35" in changes[0][1] and "13:05" in changes[0][2], f"old and new time in {changes[0]}")
    s.eq(s.text(".drop.show .numpad-display"), "$500建議", "the price the order already has")
    s.eq(s.text(".drop.show #npSkip"), "保留 $500，直接更新", "skip link")
    s.press(".drop.show #npSkip")
    s.wait_toast("已更新 #000002")
    s.eq(s.last_write(), ("POST", "/api/orders", {"type": "paste", "text": later.strip()}), "amendment write")
    s.settle()
    s.expect(s.date_text().startswith(PASTE_DAY), "the view did not move to the order's date")

    # Another amendment, repriced on the way.
    flight = amended(**{"12:35:00": "13:05:00", "CX477": "CX479"})
    s.page.wait_for_timeout(2500)
    s.tap('[aria-label="入單"]')
    paste(s, flight)
    s.stage("#000002 · 更新")
    s.press('.drop.show .step[data-s="10"]')
    s.press(".drop.show #npOk")
    s.wait_toast("已更新 #000002")
    s.eq(s.last_write(), ("POST", "/api/orders", {"type": "paste", "text": flight.strip(), "price": 510}), "repriced amendment write")
    s.settle()
    s.eq(s.text(s.row(PASTE_ID) + " .price"), "$510", "the row's price")
    s.eq(s.text(s.row(PASTE_ID) + " .flt"), "CX479", "the row's flight")

    # Cancelled, then pasted again: the order comes back.
    s.api("PATCH", "/api/orders/" + PASTE_ID, {"status": "cancelled"})
    s.wait(lambda: s.count(".empty"), "the cancelled row to go")
    s.page.wait_for_timeout(2500)
    s.tap('[aria-label="入單"]')
    paste(s, flight)
    s.stage("#000002 · 入價")
    s.press(".drop.show #npSkip")
    s.wait_toast("已重新入單 #000002")
    s.settle()
    s.eq(s.rows(), [PASTE_ID], "rows after the re-entry")


@check("day.paste-amends-an-unpriced-or-batched-order")
def day_paste_special(s: Session) -> None:
    msg = paste_message()
    s.api("POST", "/api/orders", {"type": "paste", "text": msg})
    s.open_day()
    s.tap('[aria-label="入單"]')
    paste(s, amended(**{"12:35:00": "13:05:00"}))
    s.stage("#000002 · 更新")
    s.eq(s.text(".drop.show #npSkip"), "先唔入價，直接更新", "skip link for an order with no price")
    s.eq(s.text(".drop.show .numpad-display"), "$0", "amount for an order with no price")
    s.expect(s.on(".drop.show #npOk").first.is_disabled(), "confirm is enabled with nothing typed")
    s.expect("open" in s.on(".drop.show #npPad").first.get_attribute("class"), "the keypad is shut with no suggestion")
    s.tap(".drop.show .sheet-x")
    s.stage("入單")
    # The same order number as a leg a batch has claimed.
    paste(s, amended(**{PASTE_ID: seed_demo_db._oid(503)}))
    s.stage("貼單 · 已結算")
    s.eq(s.text(".drop.show .dup-warn"), "已結算嘅單要先撤銷結算", "locked warning")
    s.eq(s.count(".drop.show #npOk"), 0, "ways to save over a batched order")
    s.eq([w[1] for w in s.writes], ["/api/orders/parse", "/api/orders/parse"], "writes")


@check("day.paste-unrecognised")
def day_paste_unrecognised(s: Session) -> None:
    s.open_day()
    s.allow("http 400", "status of 400")
    s.tap('[aria-label="入單"]')
    s.on(".drop.show .paste-box").first.fill("hello")
    s.press(".drop.show .primary-btn")
    s.wait_toast("認唔到格式")
    s.eq(s.last_write(), ("POST", "/api/orders/parse", {"text": "hello"}), "parse write")
    s.eq(s.text(".drop.show .primary-btn"), "解析", "the parse button comes back")
    s.expect(s.on(".drop.show .primary-btn").first.is_enabled(), "the parse button stays disabled")
    s.stage("入單")
    s.settle()


# ---- live update (inventory K, D33-D34, A8) ----

@check("day.live-update")
def day_live_update(s: Session) -> None:
    s.open_day()
    oid = s.t["order"]["dropoff"]
    s.page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
    s.settle()
    at = s.scroll_y()
    s.api("PATCH", "/api/orders/" + oid, {"price": 455})
    s.wait(lambda: s.text(s.row(oid) + " .price") == "$455", "the change made elsewhere")
    s.settle()
    s.eq(s.scroll_y(), at, "scroll position after a live update")
    made = s.api("POST", "/api/orders", {"type": "didi", "date": s.day(), "time": "13:00", "price": 80})
    s.wait(lambda: made["order_id"] in s.rows(), "the order created elsewhere")
    s.api("PATCH", "/api/orders/" + oid, {"status": "cancelled"})
    s.wait(lambda: oid not in s.rows(), "the order cancelled elsewhere to go")
    s.settle()
    s.eq(len([r for r in s.requests if r[2] == "document"]), 1, "document requests")
    s.eq(s.writes, [], "writes by the page")


@check("day.live-update-with-a-sheet-open")
def day_live_sheet(s: Session) -> None:
    s.open_day()
    oid = s.t["order"]["dropoff"]
    s.open_order(oid)
    s.api("PATCH", "/api/orders/" + oid, {"tunnel_fee": 30})
    s.wait(lambda: s.field("隧道費") == "$30", "the open detail to follow the change")
    # A view stacked on the detail is left alone.
    s.tap(".sheet.show .field-row", has_text="價錢")
    s.keys(".sheet.show", "12")
    s.api("PATCH", "/api/orders/" + oid, {"parking_fee": 12})
    s.settle()
    s.page.wait_for_timeout(2500)   # the server looks for changes every two seconds
    s.settle()
    s.eq(s.text(".sheet.show .sheet-title"), "改價錢", "the stacked numpad")
    s.eq(s.text(".sheet.show .numpad-display"), "$12", "what was typed on it")
    s.tap(".sheet.show .sheet-x")
    s.eq(s.field("停車費"), "$12", "the detail once back on it")
    # The order goes away: the sheet closes.
    s.api("PATCH", "/api/orders/" + oid, {"status": "cancelled"})
    s.wait(lambda: not s.sheet_open() and not s.count(".scrim.show"), "the sheet to close when its order goes")
    s.expect(oid not in s.rows(), "the cancelled order is still listed")
    s.eq(s.writes, [], "writes by the page")


@check("day.live-update-with-the-add-panel-open")
def day_live_panel(s: Session) -> None:
    s.open_day()
    oid = s.t["order"]["dropoff"]
    s.tap('[aria-label="入單"]')
    s.on(".drop.show .paste-box").first.fill("half a message")
    s.api("PATCH", "/api/orders/" + oid, {"price": 455})
    s.wait(lambda: s.text(s.row(oid) + " .price") == "$455", "the change made elsewhere")
    s.expect(s.panel_open(), "the add panel closed on a live update")
    s.eq(s.on(".drop.show .paste-box").first.input_value(), "half a message", "the text being typed")


# A statement as the reader reports one, for checks that are about the sheet
# and the confirm rather than about reading an image.
STATEMENT_READ = {
    "token": "demo-token", "report": "DEMO 結算單\n3 程 · $1270", "credit_line": "入數 DEMO $1270 啱數",
    "confirm_label": "確認結算 + 對入數", "can_settle": True, "no_orders_offer": None,
}


# ---- settle view: header, chips, months (inventory G, H1-H9) ----

@check("settle.boot")
def settle_boot(s: Session) -> None:
    s.open_settle()
    s.eq(s.page.title(), "埋數 · Ride Dispatch", "document title")
    s.eq(s.month_text(), month_label(s.today, now=True), "month button")
    s.eq(s.page.eval_on_selector_all(
        ".header-row > *", "els => els.filter(e => e.getClientRects().length).map(e => e.getAttribute('aria-label'))"),
        ["前一個月", None, "後一個月", "讀結算圖", "返日程"], "header controls")
    s.eq(s.on('[aria-label="返日程"]').first.get_attribute("href"), "/", "the ✕ link")
    book = s.api("GET", settle_path(s.today))
    ledger = s.api("GET", CREDITS)
    waiting = [c for c in ledger["credits"] if c["state"] in ("open", "partial")]
    s.eq(s.text(".summary"),
         f"未結算 ${fmt(book['totals']['unsettled'])} · 等過數 ${fmt(book['totals']['awaiting'])}"
         f" · 入數未對 {len(waiting)} 筆 ${fmt(ledger['sums']['open'])}", "summary")
    s.expect(s.count(".summary .warn") and s.count(".summary .sum-link"), "the summary's amber figure and queue link")
    n = book["counts"]
    s.eq(s.texts(".chips .chip"), [f"接送 {n['ride']}", f"滴滴 {n['didi']}", f"Uber {n['uber']}", f"熊貓 {n['foodpanda']}"], "chips")
    s.eq(s.text(".chip.on"), f"接送 {n['ride']}", "highlighted chip")
    s.eq(s.texts(".header .wk span"), list("日一二三四五六"), "weekday heads")
    s.expect(s.text(".legend").startswith("琥珀 = 平台欠緊"), "the colour key")
    s.eq(s.on(".cell.today").first.get_attribute("data-d"), s.day(), "today's cell")
    s.eq(s.top_week(), week_id(s.today.replace(day=1)), "the row at the top of the strip")
    # The strip keeps loading until neither end is within reach of the screen.
    top, bottom, height = s.page.evaluate(
        "() => [document.querySelector('#sentTop').getBoundingClientRect().bottom,"
        " document.querySelector('#sentBot').getBoundingClientRect().top, window.innerHeight]")
    s.expect(top <= -149 and bottom >= height + 149, f"an end of the strip is still in reach: {top}, {bottom}, {height}")
    asked = s.asked()
    s.expect(settle_path(s.today) in asked, "the current month was never asked for")
    s.expect(CREDITS in s.asked(prefix="/api/credits"), "the ledger was never asked for")
    s.eq(s.strip_problems(), [], "the strip's lanes")
    # Form controls and scrollbars follow the page's colour scheme.
    s.eq(s.scheme(), "dark", "color-scheme of the document")
    s.eq(s.writes, [], "writes")


@check("settle.boot-requests")
def settle_boot_requests(s: Session) -> None:
    s.open_settle()
    s.eq(len([r for r in s.requests if r[2] == "document"]), 1, "document requests")
    paths = [p for _, p, _ in s.requests]
    s.eq(paths.count("/api/events"), 1, "event streams")
    s.eq(paths.count(CREDITS), 1, "requests for the ledger (the greeting is not a change)")
    months = s.asked()
    s.eq(len(months), len(set(months)), f"a month asked for twice: {months}")
    s.expect(not any(p.startswith("/api/orders") for p in paths), "the day view loaded behind the settle view")
    s.eq(s.rows(), [], "day rows drawn behind the settle view")


@check("settle.page-rules-by-width")
def settle_widths(s: Session) -> None:
    """The settle view's own rules on the body and on shared controls, which
    must hold for that view and for it alone."""
    def measure():
        return s.page.evaluate(
            "() => { const vis = sel => [...document.querySelectorAll(sel)].find(e => e.getClientRects().length);"
            " return [Math.round(document.body.getBoundingClientRect().width),"
            " getComputedStyle(document.body).paddingBottom,"
            " Math.round(vis('.nav-btn').getBoundingClientRect().width)]; }")

    def resize(width: int) -> None:
        s.page.set_viewport_size({"width": width, "height": 800})
        s.page.wait_for_timeout(400)      # the strip re-lays itself out 150 ms after the last resize
        s.settle()

    s.open_settle()
    s.eq(measure(), [390, "20px", 42], "settle at phone width: body width, bottom padding, round button")
    resize(1000)
    s.eq(measure(), [640, "20px", 42], "settle on a wide screen")
    s.eq(s.page.eval_on_selector(".cell[data-d]", "e => getComputedStyle(e).minHeight"), "76px", "cell height on a wide screen")
    s.eq(s.strip_problems(), [], "the strip's lanes after a resize")
    resize(340)
    s.eq(measure(), [340, "20px", 34], "settle below 361px")
    s.open_day()
    s.eq(measure(), [390, "0px", 42], "day at phone width")
    resize(1000)
    s.eq(measure(), [480, "0px", 42], "day on a wide screen")
    resize(340)
    s.eq(measure(), [340, "0px", 42], "day below 361px")
    s.eq(s.scheme(), "normal", "color-scheme of the document on the day view")


@check("settle.platform-chips")
def settle_platform(s: Session) -> None:
    s.open_settle()
    s.eq(s.page.evaluate("() => localStorage.getItem('settlePlatform')"), None, "stored platform before any choice")
    s.tap('[aria-label="前一個月"]')
    asked = len(s.requests)
    s.tap(".chip", has_text="滴滴")
    s.expect(settle_path(s.today, "didi") in s.asked(asked), "the current month of the chosen platform")
    s.expect("/api/credits?platform=didi" in s.asked(asked, "/api/credits"), "the chosen platform's ledger")
    s.expect(not any("platform=ride" in p for p in s.asked(asked, "/api/")), "a request for the platform left behind")
    s.expect(s.text(".chip.on").startswith("滴滴"), "highlighted chip")
    s.eq(s.page.evaluate("() => localStorage.getItem('settlePlatform')"), "didi", "stored platform")
    # The strip starts again on the current month.
    s.eq(s.top_week(), week_id(s.today.replace(day=1)), "the row at the top after switching")
    s.eq(s.month_text(), month_label(s.today, now=True), "month button after switching")
    book = s.api("GET", settle_path(s.today, "didi"))
    s.eq(s.text(".summary"), f"未結算 ${fmt(book['totals']['unsettled'])} · 等過數 ${fmt(book['totals']['awaiting'])}", "summary")
    s.eq(s.count("#grid [data-bar]") + s.count("#grid [data-chip]"), 0, "another platform's bars and chips")
    s.tap(s.cell(10))
    s.eq(s.sub(), "滴滴", "day sheet subtitle")
    s.eq(s.texts(".sheet.show .orow .oll"), ["滴滴"], "a quick order's label")
    s.close_sheets()
    # Tapping the chosen chip again does nothing.
    asked = len(s.requests)
    s.tap(".chip", has_text="滴滴")
    s.eq(s.asked(asked, "/api/"), [], "requests for tapping the chosen chip")
    s.allow("request failed: GET /api/events")     # a reload cuts the event stream
    s.page.reload()
    s.on(".cell[data-d]").first.wait_for()
    s.settle()
    s.expect(s.text(".chip.on").startswith("滴滴"), "the chosen platform after a reload")
    s.page.evaluate("() => localStorage.setItem('settlePlatform', 'no-such-platform')")
    s.page.reload()
    s.on(".cell[data-d]").first.wait_for()
    s.settle()
    s.expect(s.text(".chip.on").startswith("接送"), "an unknown stored platform falls back to 接送")
    s.eq(s.writes, [], "writes")


@check("settle.month-navigation")
def settle_months(s: Session) -> None:
    s.open_settle()
    cur = s.today.replace(day=1)
    prev, before = add_months(cur, -1), add_months(cur, -2)
    asked = len(s.requests)
    s.tap('[aria-label="前一個月"]')
    s.eq(s.month_text(), month_label(prev), "month button after ←")
    s.eq(s.top_week(), week_id(prev), "top row after ←")
    s.expect(settle_path(prev) not in s.asked(asked), "a month the strip holds was fetched again")
    s.tap('[aria-label="後一個月"]')
    s.eq(s.month_text(), month_label(cur, now=True), "month button after →")
    s.eq(s.top_week(), week_id(cur), "top row after →")
    s.tap('[aria-label="前一個月"]')
    s.tap('[aria-label="前一個月"]')
    s.eq(s.month_text(), month_label(before), "month button two months back")
    s.eq(s.top_week(), week_id(before), "top row two months back")
    weeks = s.weeks()
    days = [date.fromisoformat(w[2:]) for w in weeks]
    s.expect(all(b - a == timedelta(days=7) for a, b in zip(days, days[1:])), "the strip is not one unbroken run of weeks")
    # The first week of each later month carries the hairline, and nothing else does.
    starts = s.page.eval_on_selector_all("#grid .wkblock.mstart", "els => els.map(e => e.id)")
    months = sorted({month_key(d + timedelta(days=6)) for d in days})
    s.eq(starts, [week_id(date.fromisoformat(m + "-01")) for m in months[1:]], "rows that begin a month")
    # The month button goes to the current month, and does nothing once there.
    s.tap(".date-btn")
    s.eq(s.month_text(), month_label(cur, now=True), "month button after tapping it")
    s.eq(s.top_week(), week_id(cur), "top row after tapping the month button")
    at, asked = s.scroll_y(), len(s.requests)
    s.tap(".date-btn")
    s.eq((s.scroll_y(), s.asked(asked)), (at, []), "tapping the month button on the current month")
    # The header follows the scroll.
    after = add_months(cur, 1)
    # The strip grows as its end comes into reach, so the row may take more
    # than one scroll to bring to the top.
    for _ in range(4):
        s.page.evaluate("""id => {
          const header = [...document.querySelectorAll('.header')].find(e => e.getClientRects().length)
            .getBoundingClientRect().bottom;
          window.scrollBy(0, document.getElementById(id).getBoundingClientRect().top - header + 8);
        }""", week_id(after + timedelta(days=7)))
        s.settle()
    s.eq(s.top_week(), week_id(after + timedelta(days=7)), "top row after scrolling into the next month")
    s.eq(s.month_text(), month_label(after), "month button after scrolling")
    s.eq(s.weeks()[:len(weeks)], weeks, "rows already drawn changed")


@check("settle.edge-loading")
def settle_edges(s: Session) -> None:
    s.open_settle()
    boot = settle_path(s.today)

    def earliest() -> date:
        return (date.fromisoformat(s.weeks()[0][2:]) + timedelta(days=6)).replace(day=1)

    def latest() -> date:
        return date.fromisoformat(s.weeks()[-1][2:]).replace(day=1)

    first, last = earliest(), latest()
    # Near the top, with the first row a little above the line it sits on: the
    # month before arrives above it and the row does not move.
    held = s.page.evaluate("""() => {
      const header = [...document.querySelectorAll('.header')].find(e => e.getClientRects().length)
        .getBoundingClientRect().bottom;
      const row = document.querySelector('#grid .wkblock');
      window.scrollTo(0, window.scrollY + row.getBoundingClientRect().top - header + 20);
      return [row.id, Math.round(row.getBoundingClientRect().top)];
    }""")
    s.settle()
    s.expect(earliest() < first, "nothing was loaded above the strip")
    s.eq(s.page.evaluate(TOP_WEEK_JS), held, "the top row after a month arrived above it")
    # At the bottom, the month after.
    s.page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
    s.settle()
    s.expect(latest() > last, "nothing was loaded below the strip")
    # Wandering over what is held asks for nothing, and no month was asked for twice.
    s.page.evaluate("() => window.scrollTo(0, (document.documentElement.scrollHeight - window.innerHeight) / 2)")
    s.settle()
    asked = len(s.requests)
    for step in (-200, 400, -200):
        s.page.evaluate("d => window.scrollBy(0, d)", step)
        s.settle()
    s.eq(s.asked(asked), [], "requests for scrolling over months already held")
    twice = sorted({p for p in s.asked() if p != boot and s.asked().count(p) > 1})
    s.eq(twice, [], "months fetched more than once")
    days = [date.fromisoformat(w[2:]) for w in s.weeks()]
    s.expect(all(b - a == timedelta(days=7) for a, b in zip(days, days[1:])), "the strip is not one unbroken run of weeks")
    s.eq(s.strip_problems(), [], "the strip's lanes")


def doctored_ledger(s: Session, months_before_strip: int) -> date:
    """Serve the ledger with one more batch on the unmatched credit: a batch
    whose days lie that many months before the strip's first, which no
    seeded data reaches. Returns the day; the page must be loaded already."""
    first = (date.fromisoformat(s.weeks()[0][2:]) + timedelta(days=6)).replace(day=1)
    far = add_months(first, -months_before_strip).replace(day=15)
    ledger = s.api("GET", CREDITS)
    credit = [c for c in ledger["credits"] if c["id"] == s.t["credit"]["exact"]][0]
    credit["batches"].append({"id": 990, "dates": [far.isoformat()], "orders": 1, "amount": 100.0,
                              "confirmed_amount": 100.0, "outstanding": 0.0, "state": "paid", "has_image": False})
    s.stub("GET", "**/api/credits?platform=ride", 200, json.dumps(ledger), "application/json")
    s.allow("request failed: GET /api/events")     # the reload cuts the event stream
    s.page.reload()
    s.on(".cell[data-d]").first.wait_for()
    s.settle()
    return far


@check("settle.reveal-fills-the-months-between")
def settle_fill(s: Session) -> None:
    s.open_settle()
    first = (date.fromisoformat(s.weeks()[0][2:]) + timedelta(days=6)).replace(day=1)
    far = doctored_ledger(s, 3)
    s.eq((date.fromisoformat(s.weeks()[0][2:]) + timedelta(days=6)).replace(day=1), first, "the strip's first month after the reload")
    asked = len(s.requests)
    chip = s.chip("exact")
    s.reach(chip)
    s.tap(chip)
    # The far end of the relation is three months above the strip: every month
    # up to it is loaded, once, and the strip goes to the row that carries it.
    got = s.asked(asked)
    for n in (1, 2, 3):
        s.eq(got.count(settle_path(add_months(first, -n))), 1, f"requests for the month {n} before the strip")
    s.eq(s.top_week(), week_id(far), "the row the strip went to")
    s.eq(s.month_text(), month_label(far), "month button")
    days = [date.fromisoformat(w[2:]) for w in s.weeks()]
    s.expect(all(b - a == timedelta(days=7) for a, b in zip(days, days[1:])), "the strip is not one unbroken run of weeks")
    s.expect(week_id(s.today) in s.weeks(), "the months held before were thrown away")
    s.expect(f"chip {s.t['credit']['exact']}" in s.lit(), "the focus was dropped")
    s.eq(s.writes, [], "writes")


@check("settle.far-jump-refounds-the-strip")
def settle_refound(s: Session) -> None:
    s.open_settle()
    first = (date.fromisoformat(s.weeks()[0][2:]) + timedelta(days=6)).replace(day=1)
    far = doctored_ledger(s, 7)
    chip = s.chip("exact")
    s.reach(chip)
    asked = len(s.requests)
    s.tap(chip)
    got = s.asked(asked)
    s.eq(got.count(settle_path(far)), 1, "requests for the month jumped to")
    # Not the month just above the old strip: bringing the chip under the
    # finger can scroll that one into reach, which is the strip's own growth.
    between = [settle_path(add_months(first, -n)) for n in (2, 3, 4)]
    s.eq([p for p in got if p in between], [], "months between were paid for")
    s.expect(week_id(s.today) not in s.weeks(), "the old strip is still there")
    s.expect(week_id(far) in s.weeks(), "the row carrying the far end is not on the new strip")
    s.eq(s.month_text(), month_label(far), "month button")
    days = [date.fromisoformat(w[2:]) for w in s.weeks()]
    s.expect(all(b - a == timedelta(days=7) for a, b in zip(days, days[1:])), "the strip is not one unbroken run of weeks")
    # And back: the current month is as far away again.
    asked = len(s.requests)
    s.tap(".date-btn")
    s.eq(s.asked(asked).count(settle_path(s.today)), 1, "requests for the current month")
    s.expect(week_id(far) not in s.weeks(), "the far strip is still there")
    s.eq(s.month_text(), month_label(s.today, now=True), "month button back on the current month")
    s.eq(s.top_week(), week_id(s.today.replace(day=1)), "top row back on the current month")
    s.eq(s.writes, [], "writes")


@check("settle.failed-load")
def settle_failed_load(s: Session) -> None:
    s.open_settle()
    weeks, text = s.weeks(), s.month_text()
    s.stub("GET", "**/api/settle?month=*", 500, "<html>boom</html>", "text/html")
    s.allow("http 500", "status of 500")
    # Going to the strip's top asks for the month above it, which fails.
    s.page.evaluate("() => window.scrollTo(0, 0)")
    s.wait_toast("載入失敗")
    s.settle()
    s.eq(s.weeks(), weeks, "the strip after a failed month")
    s.page.wait_for_timeout(2600)
    # A reload of what is held fails the same way and leaves it as it was.
    s.api("PATCH", "/api/orders/" + seed_demo_db._oid(12), {"price": 401})
    s.wait_toast("載入失敗")
    s.settle()
    s.eq((s.weeks(), s.month_text() != ""), (weeks, text != ""), "the strip after a failed reload")
    s.expect(s.count(".cell[data-d]") > 0, "the strip was emptied")


# ---- settle view: cells, bars, chips, focus (inventory H10-H30) ----

@check("settle.cells-bars-and-chips")
def settle_marks(s: Session) -> None:
    s.open_settle()
    s.reach(s.chip("archived"))
    s.reach(s.bar("paid"))

    def amount(selector: str) -> tuple:
        el = s.on(selector + " .amt").first
        return el.get_attribute("class"), el.text_content()

    s.eq(amount(s.cell(1)), ("amt unsettled", "$940"), "a day no batch has claimed")
    s.eq(amount(s.cell(3)), ("amt unsettled", "$940"), "a day whose 舉牌 was paid ahead")
    s.eq(amount(s.cell(26)), ("amt done", "$840"), "a day wholly on a batch")
    s.eq(amount(s.cell(34)), ("amt done", "$896.55"), "a day with a fined leg, to the cent")
    s.eq(amount(s.cell(-1)), ("amt future", "$900"), "a day still to come")
    s.eq(amount(s.cell(0)), ("amt unsettled", "$1890"), "today")
    s.expect("today" in s.on(s.cell(0)).first.get_attribute("class"), "today's cell is not ringed")
    empty = s.on(".cell.none").first
    s.expect(empty.is_disabled() and empty.get_attribute("data-d") is None, "an empty day can be opened")
    firsts = [t for t in s.texts("#grid .cell .d") if "/" in t]
    s.expect(firsts and all(t.endswith("/1") for t in firsts) and f"{s.today.month}/1" in firsts,
             f"day numbers carrying a month: {firsts}")

    def arrow(point: date, start: date) -> str:
        return "→" + (f"{point.day}日" if month_key(point) == month_key(start) else md_slash(point))

    b, bars = s.t["batch"], s.marks("bar")
    s.eq((bars[b["paid"]]["classes"], bars[b["paid"]]["labels"]), ({"bar", "paid"}, ["$1376.55"]), "a collected batch")
    s.eq((bars[b["short"]]["classes"], bars[b["short"]]["labels"]), ({"bar", "partial"}, ["$2310 · 差 $380"]), "a batch paid short")
    s.expect("linear-gradient" in bars[b["short"]]["style"], "a short-paid bar is not split at what was received")
    s.eq((bars[b["awaiting"]]["classes"], bars[b["awaiting"]]["labels"]), ({"bar", "awaiting"}, ["$1270"]), "a batch awaiting money")
    s.eq(sorted(bars[b["held_back"]]["labels"]), sorted(["$1780", "dashed " + arrow(s.back(12), s.back(15))]),
         "a batch with a held-back leg")
    s.eq(sorted(bars[b["ahead"]]["labels"]), sorted(["$1425", "dashed $40" + arrow(s.back(6), s.back(3))]),
         "a batch that paid a 舉牌 ahead")
    c, chips = s.t["credit"], s.marks("chip")
    s.eq({k: (sorted(chips[c[k]]["classes"]), chips[c[k]]["labels"]) for k in c}, {
        "paid": (["cchip"], ["入$1376.55"]), "short": (["cchip", "short"], ["入$1930"]),
        "exact": (["cchip", "open"], ["入$1270"]), "partial": (["cchip", "open"], ["入$2080"]),
        "archived": (["cchip", "gone"], ["入$215.50"]), "group": (["cchip", "open"], ["入$2870"]),
    }, "chips")
    s.eq(s.strip_problems(), [], "the strip's lanes")
    s.eq(s.writes, [], "writes")


@check("settle.focus")
def settle_focus(s: Session) -> None:
    s.open_settle()
    bar, chip = s.bar("short"), s.chip("short")
    s.reach(bar)
    relation = sorted([f"bar {s.t['batch']['short']}", f"chip {s.t['credit']['short']}"] +
                      [s.back(n).isoformat() for n in (27, 26, 25)])
    s.tap(bar)
    s.eq(s.lit(), relation, "what a bar's first tap lights")
    s.expect(not s.sheet_open(), "the first tap opened a sheet")
    s.expect(s.count("#grid .dim") > 5 and not s.count("#grid .cell.none.dim"), "everything else recedes, bar the empty days")
    # The same relation, whichever end names it.
    s.tap(chip)
    s.eq(s.lit(), relation, "what the chip of the same relation lights")
    s.expect(not s.sheet_open(), "the first tap on the other end opened a sheet")
    s.tap(chip)
    s.on(".sheet.show .hero").wait_for()
    s.eq(s.title(), "入數 " + md_label(s.back(21)), "second tap on a chip opens its credit")
    s.close_sheets()
    s.eq(s.lit(), relation, "the focus after closing the sheet")
    # It survives a change made elsewhere.
    s.api("PATCH", "/api/orders/" + seed_demo_db._oid(12), {"price": 401})
    s.wait(lambda: s.text(s.cell(1) + " .amt") == "$941", "the change made elsewhere")
    s.eq(s.lit(), relation, "the focus after a live update")
    # A day opens on one tap whatever is focused.
    s.tap(s.cell(26))
    s.eq(s.title(), md_label(s.back(26)) + " 星期" + WEEKDAY[s.back(26).weekday()], "a day opened under a focus")
    s.close_sheets()
    s.tap(".legend")
    s.eq((s.lit(), s.count("#grid .dim")), ([], 0), "after tapping empty calendar")
    s.tap(bar)
    s.tap(bar)
    s.on(".sheet.show .hero").wait_for()
    s.eq(s.title(), "結算 " + span_label(s.back(27), s.back(25)), "second tap on a bar opens its batch")
    s.eq(s.writes, [], "writes")


# ---- settle view: sheets (inventory I) ----

@check("settle.sheets-stack")
def settle_stack(s: Session) -> None:
    s.open_settle()
    day = md_label(s.back(19)) + " 星期" + WEEKDAY[s.back(19).weekday()]
    batch = "結算 " + span_label(s.back(19), s.back(18))
    s.open_cell(19)
    s.eq((s.title(), s.sub()), (day, "接送"), "day sheet")
    s.eq(s.count(".sheet.show .sheet-back"), 0, "a back button on a sheet with nothing under it")
    s.tap(".sheet.show .blink")
    s.eq(s.title(), batch, "the batch opened from its day")
    s.eq(s.on(".sheet.show .sheet-back").first.get_attribute("aria-label"), "返上一層", "back button")
    s.tap(".sheet.show .sheet-back")
    s.eq(s.title(), day, "‹ goes back one level")
    s.tap(".sheet.show .blink")
    s.tap(".sheet.show [data-undo]")
    s.eq(s.title(), "撤銷結算", "third level")
    s.tap(".sheet.show .ghost-btn", has_text="返回")
    s.eq(s.title(), batch, "返回 goes back one level")
    s.tap(".sheet.show .ghost-btn", has_text="收埋")
    s.eq(s.title(), day, "收埋 goes back one level")
    # ✕ closes the whole stack, and so does the scrim.
    s.tap(".sheet.show .blink")
    s.tap(".sheet.show [data-undo]")
    s.tap(".sheet.show .sheet-x")
    s.wait(lambda: not s.sheet_open() and not s.count(".scrim.show"), "✕ to close the whole stack")
    s.open_cell(19)
    s.tap(".sheet.show .blink")
    s.on(".scrim").first.tap(position={"x": 8, "y": 8})
    s.wait(lambda: not s.sheet_open() and not s.count(".scrim.show"), "the scrim to close the whole stack")
    # 收埋 on the only sheet closes it.
    s.open_mark(s.bar("awaiting"))
    s.eq(s.count(".sheet.show .sheet-back"), 0, "a back button on a batch opened from the strip")
    s.tap(".sheet.show .ghost-btn", has_text="收埋")
    s.wait(lambda: not s.sheet_open() and not s.count(".scrim.show"), "收埋 to close the only sheet")
    s.eq(s.writes, [], "writes")


@check("settle.day-and-batch-sheets")
def settle_batch_sheets(s: Session) -> None:
    s.open_settle()
    o = seed_demo_db._oid
    credit_day = md_slash(s.back(21))
    s.open_cell(26)
    rows = s.page.eval_on_selector_all(
        ".sheet.show .orow",
        "els => els.map(e => [e.dataset.od, e.querySelector('.ot').textContent, e.querySelector('.oid').textContent,"
        " e.querySelector('.oll').textContent, e.querySelector('.oend').textContent, e.querySelector('.otag').className])")
    s.eq(rows, [
        [o(203), "11:30", "8800 0000 0000 0203", "接機 · CX488", "$460已收 " + credit_day, "otag paid"],
        [o(204), "19:15", "8800 0000 0000 0204", "單程 · 旺角樣本賓館", "$380未過數", "otag unsettled"],
    ], "legs of a day on a short-paid batch")
    s.eq(s.count(".sheet.show .orow [data-copy]"), 0, "copy targets inside rows that open an order")
    s.eq(s.texts(".sheet.show .blink-t"), [f"批次 {span_label(s.back(27), s.back(25))} · 5 程 · $2310 · 差 $380"], "batch link")
    s.close_sheets()
    # A leg whose 舉牌 another batch paid ahead, and that batch's link on its day.
    s.open_cell(3)
    s.eq(s.texts(f'.sheet.show .orow[data-od="{o(601)}"] .otag'), ["未結算", "舉牌 $40 先結"], "a leg with its 舉牌 paid ahead")
    s.eq(s.texts(".sheet.show .blink-t"),
         [f"批次 {span_label(s.back(6), s.back(5))} · 3 程 · $1425 · 等過數 · 連 {s.back(3).day}日 舉牌 $40"],
         "the link to the batch that paid it")
    s.tap(".sheet.show .blink")
    s.eq(s.title(), "結算 " + span_label(s.back(6), s.back(5)), "batch opened from the link")
    s.eq(s.sub(), f"接送 · 3 程 · 結算日 {md_slash(s.back(2))} · 連 {s.back(3).day}日 舉牌 $40", "batch subtitle")
    s.eq(s.text(".sheet.show .prop-head"), "帳項 · $40", "the batch's own lines")
    s.eq(s.sum_pairs(".sheet.show .prop-sec .sum-row"), [[f"舉牌先結 …0601 · {md_slash(s.back(3))}", "+$40"]], "the line paid ahead")
    s.close_sheets()

    # A collected batch with a statement, a fined leg and a screenshot.
    s.open_mark(s.bar("paid"))
    s.eq(s.title(), "結算 " + span_label(s.back(34), s.back(33)), "batch title")
    s.eq(s.sub(), f"接送 · 3 程 · 結算日 {md_slash(s.back(31))}", "batch subtitle")
    s.eq(s.texts(".sheet.show .hero > div"), ["平台確認", "$1376.55", "已收齊 · " + md_slash(s.back(29))], "hero")
    pairs = s.sum_pairs(".sheet.show .sum-rows .sum-row")
    s.eq(pairs, [["應收", "$1376.55"], ["入數 " + md_slash(s.back(29)), "$1376.55›"], ["結算單", "睇圖"]], "summary rows")
    link = s.on(".sheet.show .sum-row a").first
    s.eq((link.get_attribute("href"), link.get_attribute("target")), ("/api/settlements/1/image", "_blank"), "screenshot link")
    s.eq(s.texts(".sheet.show .xbtn"), ["解除"], "the button that takes money back")
    s.eq(s.text(".sheet.show .fold"), "3 程▸", "the folded order list")
    s.eq(s.count(".sheet.show .orow"), 0, "order rows while folded")
    s.tap(".sheet.show .fold")
    s.eq(s.texts(".sheet.show .oday"), [md_label(s.back(n)) + " 星期" + WEEKDAY[s.back(n).weekday()] for n in (34, 33)], "day headings")
    s.eq(s.texts(".sheet.show .orow .oa"), ["$476.55 · 判罰 −$63.45", "$420", "$480"], "order figures")
    # The fold survives a change made elsewhere.
    s.api("PATCH", "/api/orders/" + o(12), {"price": 401})
    s.wait(lambda: s.text(s.cell(1) + " .amt") == "$941", "the change made elsewhere")
    s.settle()
    s.eq(s.count(".sheet.show .orow"), 3, "order rows after a live update")
    # An order number copies whole, however it is grouped for the eye.
    s.page.evaluate("() => Object.defineProperty(navigator, 'clipboard', { configurable: true,"
                    " value: { writeText: t => { window.__copied = t; return Promise.resolve(); } } })")
    s.press(".sheet.show .orow .oid")
    s.wait_toast("已複製 " + o(111))
    s.eq(s.page.evaluate("() => window.__copied"), o(111), "what was copied")
    s.eq(s.count(".sheet.show .orow"), 3, "the sheet after a copy")
    s.eq(s.text(".sheet.show .ghost-btn.danger"), "撤銷結算", "undo button")
    s.close_sheets()

    # A batch that picked up a leg from an earlier statement: its days are not consecutive.
    s.open_mark(s.bar("held_back"))
    s.eq(s.title(), "結算 " + span_label(s.back(12), s.back(11)), "title of a batch with a held-back leg")
    s.expect(s.sub().endswith(f" · 連 {s.back(15).day}日 1 程"), f"held-back run in {s.sub()!r}")
    s.eq(s.writes, [], "writes")


@check("settle.credit-and-queue-sheets")
def settle_credit_sheets(s: Session) -> None:
    s.open_settle()
    c = s.t["credit"]
    s.tap(".sum-link")
    s.eq((s.title(), s.sub()), ("入數未對", "接送 · 3 筆 $4440"), "queue")
    s.eq(s.page.eval_on_selector_all(".sheet.show .qrow", "els => els.map(e => [e.dataset.credit, e.textContent])"), [
        [str(c["exact"]), f"{md_slash(s.back(14))} · $1270未對›"],
        [str(c["partial"]), f"{md_slash(s.back(7))} · $2080未對 · 剩 $300›"],
        [str(c["group"]), f"{md_slash(s.back(0))} · $2870未對›"],
    ], "queue rows, oldest first")
    group = "、".join([span_label(s.back(6), s.back(5)), span_label(s.back(9), s.back(8))])
    s.eq(s.page.eval_on_selector_all(".sheet.show .qprop", "els => els.map(e => e.textContent)"), [
        f"→ 批次 {span_label(s.back(19), s.back(18))} 差 $1270對",
        f"→ 2 個批次 · {group} · $2870對晒",
    ], "matches that are not in question")
    # A credit part-used: what it could still pay, and what it has paid.
    s.tap(f'.sheet.show .qrow[data-credit="{c["partial"]}"]')
    s.eq((s.title(), s.sub()), ("入數 " + md_label(s.back(7)), "接送 · DEMO PLATFORM LTD"), "credit sheet")
    s.eq(s.texts(".sheet.show .hero > div"), ["到帳", "$2080", "已對 $1780 · 剩 $300"], "hero")
    s.eq(s.text(".sheet.show .prop-head"), "可能對", "proposals heading")
    s.eq(s.texts(".sheet.show .prow .pbtn"), ["對 $300（差 $1145）", "對 $300（差 $970）", "對 $300（差 $80）"], "what each tap would do")
    s.eq(s.sum_pairs(".sheet.show .sum-rows .sum-row"), [
        [f"批次 {span_label(s.back(12), s.back(11))}", "4 程 · $1780›"], ["Ref", "DEMO-REF-0004"], ["備註", "SUPPLIERPAY"]], "rows")
    s.tap(".sheet.show .sum-row.link")
    s.eq(s.title(), "結算 " + span_label(s.back(12), s.back(11)), "the batch the credit paid")
    s.tap(".sheet.show .sheet-back")
    s.tap(".sheet.show .sheet-back")
    s.eq(s.title(), "入數未對", "back on the queue")
    s.close_sheets()
    # The group one transfer pays, offered as one row on the credit's own sheet.
    s.open_mark(s.chip("group"))
    s.eq(s.texts(".sheet.show .hero > div"), ["到帳", "$2870", "未對"], "hero of an unmatched credit")
    s.eq(s.texts(".sheet.show .prow")[0], f"2 個批次 · {group} · $2870啱數對晒", "the group row")
    s.eq(s.count(".sheet.show .prow .ptag"), 1, "啱數 tags: the group's, not its batches' own")
    s.close_sheets()
    # A credit paid into a batch that is still short, and an archived one.
    s.open_mark(s.chip("short"))
    s.eq(s.texts(".sheet.show .hero > div")[2], "已對", "hero of a matched credit")
    s.eq(s.texts(".sheet.show .sub-note"), ["批次仲差 $380"], "the batch it left short")
    s.close_sheets()
    s.open_mark(s.chip("archived"))
    s.eq(s.texts(".sheet.show .hero > div")[2], "收埋（no-orders）", "hero of an archived credit")
    s.eq(s.writes, [], "writes")


@check("settle.allocate")
def settle_allocate(s: Session) -> None:
    s.open_settle()
    b, c = s.t["batch"], s.t["credit"]
    s.tap(".sum-link")
    s.press(f'.sheet.show .pbtn[data-alloc-credit="{c["exact"]}"]')
    s.wait_toast("已對 $1270 · 批次收齊")
    s.eq(s.writes[-1], ("POST", f"/api/credits/{c['exact']}/allocate", json.dumps({"settlement_id": b["awaiting"]}, separators=(",", ":"))),
         "allocate write")
    s.settle()
    # The queue stays, without the credit that was put away.
    s.eq(s.title(), "入數未對", "the sheet after 對")
    s.eq(s.page.eval_on_selector_all(".sheet.show .qrow", "els => els.map(e => e.dataset.credit)"),
         [str(c["partial"]), str(c["group"])], "queue rows after 對")
    s.close_sheets()
    s.expect(s.text(".summary").endswith("入數未對 2 筆 $3170"), f"summary after 對: {s.text('.summary')!r}")
    s.eq(s.marks("bar")[b["awaiting"]]["classes"], {"bar", "paid"}, "the bar of the batch just paid")
    s.eq(s.marks("chip")[c["exact"]]["classes"], {"cchip"}, "the chip of the credit just matched")
    s.page.wait_for_timeout(2500)
    # From the short-paid batch's own sheet, with money that does not cover it.
    s.open_mark(s.bar("short"))
    s.eq(s.text(".sheet.show .prop-head"), "等緊補數 · 差 $380", "what the batch is waiting for")
    s.press(".sheet.show .pbtn", has_text="對 $300（差 $80）")
    s.wait_toast("已對 $300 · 仲差 $80")
    s.eq(s.writes[-1], ("POST", f"/api/credits/{c['partial']}/allocate", json.dumps({"settlement_id": b["short"]}, separators=(",", ":"))),
         "allocate write from the batch")
    s.settle()
    s.eq(s.texts(".sheet.show .hero > div")[2], f"已收 $2230（{md_slash(s.back(7))}） · 差 $80", "the batch after a part payment")
    s.eq(len(s.writes), 2, "writes")


@check("settle.allocate-all")
def settle_allocate_all(s: Session) -> None:
    s.open_settle()
    b, c = s.t["batch"], s.t["credit"]
    s.open_mark(s.chip("group"))
    s.press(".sheet.show .pbtn[data-alloc-all]")
    s.wait_toast("已對 2 個批次 · $2870 · 收齊")
    s.eq(s.writes[-1], ("POST", f"/api/credits/{c['group']}/allocate-all",
                        json.dumps({"settlement_ids": [b["ahead"], b["group"]]}, separators=(",", ":"))), "allocate-all write")
    s.settle()
    s.eq(s.texts(".sheet.show .hero > div")[2], "已對", "the credit after 對晒")
    s.eq(s.count(".sheet.show .prow"), 0, "proposals on a matched credit")
    s.eq(len(s.texts(".sheet.show .sum-row.link")), 2, "the batches it paid")
    s.close_sheets()
    bars = s.marks("bar")
    s.eq((bars[b["ahead"]]["classes"] - {"makeup"}, bars[b["group"]]["classes"]), ({"bar", "paid"}, {"bar", "paid"}), "both bars")
    s.eq(len(s.writes), 1, "writes")


@check("settle.unlink")
def settle_unlink(s: Session) -> None:
    s.open_settle()
    b, c = s.t["batch"], s.t["credit"]
    s.open_mark(s.bar("held_back"))
    s.tap(".sheet.show .xbtn")
    s.eq((s.title(), s.sub()), ("解除入數", f"{span_label(s.back(12), s.back(11))} · 入數 {md_slash(s.back(7))}"), "confirm view")
    s.eq(s.text(".sheet.show .undo-info"), "$1780 會由呢個批次拎返出嚟，批次變返差 $1780，錢返到入數度。", "what it says will happen")
    s.tap(".sheet.show .ghost-btn", has_text="返回")
    s.eq(s.title(), "結算 " + span_label(s.back(12), s.back(11)), "返回 goes back to the batch")
    s.eq(s.writes, [], "writes before confirming")
    s.tap(".sheet.show .xbtn")
    s.press(".sheet.show [data-unlinkgo]")
    s.wait_toast("已解除入數")
    s.eq(s.writes[-1][:2], ("DELETE", f"/api/settlements/{b['held_back']}/allocations/{c['partial']}"), "deallocate write")
    s.settle()
    s.eq(s.title(), "結算 " + span_label(s.back(12), s.back(11)), "back on the batch")
    s.eq(s.texts(".sheet.show .hero > div")[2], "等過數", "the batch with its money taken back")
    s.eq(s.count(".sheet.show .xbtn"), 0, "allocations left on the batch")
    s.close_sheets()
    s.expect(s.text(".summary").endswith("入數未對 3 筆 $6220"), f"summary after 解除: {s.text('.summary')!r}")
    s.eq(len(s.writes), 1, "writes")


@check("settle.undo")
def settle_undo(s: Session) -> None:
    s.open_settle()
    b = s.t["batch"]
    s.open_cell(19)
    s.tap(".sheet.show .blink")
    s.tap(".sheet.show [data-undo]")
    s.eq((s.title(), s.sub()), ("撤銷結算", f"{span_label(s.back(19), s.back(18))} · 3 程"), "confirm view")
    s.eq(s.text(".sheet.show .undo-info"), "呢 3 程會變返未結算，$1270 嘅結算紀錄會刪走。", "what it says will happen")
    s.press(".sheet.show [data-undogo]")
    s.wait_toast("已撤銷結算")
    s.eq(s.writes[-1][:2], ("DELETE", f"/api/settlements/{b['awaiting']}"), "undo write")
    s.settle()
    # Back on the day it was opened from, whose legs are loose again.
    s.eq(s.title(), md_label(s.back(19)) + " 星期" + WEEKDAY[s.back(19).weekday()], "the sheet after an undo")
    s.eq(s.texts(".sheet.show .otag"), ["未結算", "未結算"], "the day's legs")
    s.eq(s.count(".sheet.show .blink"), 0, "batch links on the day")
    s.close_sheets()
    s.expect(b["awaiting"] not in s.marks("bar"), "the undone batch still has a bar")
    s.eq(s.on(s.cell(19) + " .amt").first.get_attribute("class"), "amt unsettled", "the day's amount")
    s.eq(len(s.writes), 1, "writes")


@check("settle.unpaid-ticks")
def settle_ticks(s: Session) -> None:
    s.open_settle()
    o = seed_demo_db._oid
    s.open_mark(s.bar("short"), ".sheet.show .up-sec")
    s.eq(s.text(".sheet.show .up-head"), "邊張單未過？ · 差 $380", "heading")
    s.expect(s.text(".sheet.show .up-note").startswith("系統估：") and s.text(".sheet.show .up-note").endswith("，啱差額，已剔"),
             f"the single guess: {s.text('.sheet.show .up-note')!r}")

    def ticks() -> list:
        return s.page.eval_on_selector_all(".sheet.show [data-uptick]",
                                           "els => els.filter(e => e.querySelector('.up-chk.on')).map(e => e.dataset.uptick)")

    def foot() -> tuple:
        return s.text(".sheet.show .up-sum"), s.on(".sheet.show .up-btn").first.is_enabled()

    s.eq(s.count(".sheet.show [data-uptick]"), 5, "tick rows")
    s.eq((ticks(), foot()), ([o(204)], ("剔咗 $380 = 差額", True)), "the stored mark")
    s.eq(s.texts(f'.sheet.show [data-uptick="{o(204)}"] .oend > span'), ["$380", "未過數"], "a ticked row")
    # The box, not the middle of the row: the order number there copies itself.
    s.tap(f'.sheet.show [data-uptick="{o(204)}"] .up-chk')
    s.eq((ticks(), foot()), ([], ("剔咗 $0 ≠ 差 $380", False)), "with nothing ticked")
    s.tap(f'.sheet.show [data-uptick="{o(201)}"] .up-chk')
    s.eq((ticks(), foot()), ([o(201)], ("剔咗 $560 ≠ 差 $380", False)), "with the wrong leg ticked")
    s.expect("warn" in s.on(".sheet.show .up-sum").first.get_attribute("class"), "a wrong sum is not marked")
    # A repaint puts the ticks back to what is stored.
    s.api("PATCH", "/api/orders/" + o(12), {"price": 401})
    s.wait(lambda: ticks() == [o(204)], "the ticks to be reset by a live update")
    s.tap(f'.sheet.show [data-uptick="{o(204)}"] .up-chk')
    s.tap(f'.sheet.show [data-uptick="{o(204)}"] .up-chk')
    s.press(".sheet.show .up-btn")
    s.wait_toast("已記低")
    s.eq(s.writes[-1], ("POST", f"/api/settlements/{s.t['batch']['short']}/unpaid",
                        json.dumps({"order_ids": [o(204)]}, separators=(",", ":"))), "unpaid write")
    s.settle()
    s.eq(len(s.writes), 1, "writes")


@check("settle.refused-writes")
def settle_refused(s: Session) -> None:
    s.open_settle()
    for glob, method, message in (("**/api/credits/*/allocate", "POST", "拒絕對"),
                                  ("**/api/credits/*/allocate-all", "POST", "拒絕對晒"),
                                  ("**/api/settlements/*/allocations/*", "DELETE", "拒絕解除"),
                                  ("**/api/settlements/*", "DELETE", "拒絕撤銷"),
                                  ("**/api/settlements/*/unpaid", "POST", "拒絕記低")):
        s.refuse(method, glob, message)
    s.tap(".sum-link")
    s.press(".sheet.show .qprop .pbtn", has_text="對")
    s.wait_toast("拒絕對")
    s.press(".sheet.show .pbtn[data-alloc-all]")
    s.wait_toast("拒絕對晒")
    s.eq(s.count(".sheet.show .qrow"), 3, "queue rows after two refusals")
    s.close_sheets()
    s.open_mark(s.bar("held_back"))
    s.tap(".sheet.show .xbtn")
    s.press(".sheet.show [data-unlinkgo]")
    s.wait_toast("拒絕解除")
    s.eq(s.title(), "解除入數", "the confirm view after a refusal")
    s.tap(".sheet.show .sheet-back")
    s.tap(".sheet.show [data-undo]")
    s.press(".sheet.show [data-undogo]")
    s.wait_toast("拒絕撤銷")
    s.eq(s.title(), "撤銷結算", "the confirm view after a refusal")
    s.close_sheets()
    s.open_mark(s.bar("short"), ".sheet.show .up-sec")
    s.press(".sheet.show .up-btn")
    s.wait_toast("拒絕記低")
    s.eq(len(s.writes), 5, "writes")
    s.settle()


@check("settle.order-sheet")
def settle_order_sheet(s: Session) -> None:
    s.open_settle()
    o = seed_demo_db._oid
    s.page.evaluate("() => Object.defineProperty(navigator, 'clipboard', { configurable: true,"
                    " value: { writeText: t => { window.__copied = t; return Promise.resolve(); } } })")
    # A leg of a batch: it waits for the order, then shows it with what a batch locks.
    s.open_cell(26)
    path = "/api/orders/" + o(203)
    s.hold(path)
    s.press(f'.sheet.show .orow[data-od="{o(203)}"]')
    s.wait(lambda: s.holding(path), "the order to be asked for")
    s.eq((s.title(), s.text(".sheet.show .empty")), ("單 …0203", "讀緊…"), "the sheet while the order is on its way")
    s.release_all()
    s.on(".sheet.show .field-row").first.wait_for()
    s.settle()
    day = s.back(26)
    s.eq((s.title(), s.sub()), ("接機 11:30", f"#000203 · {md_label(day)} 星期{WEEKDAY[day.weekday()]}"), "order sheet")
    info = s.info()
    s.eq(list(info), ["單號", "乘客", "航班", "車型", "路線", "結算"], "info rows")
    s.eq(info["單號"], "8800 0000 0000 0203", "the whole number, grouped")
    s.eq(info["結算"], f"批次 {span_label(s.back(27), s.back(25))} · 差 $380 ›", "settlement row")
    s.eq(s.page.eval_on_selector_all(".sheet.show .field-row.locked", "els => els.map(e => e.querySelector('.fk').textContent + '|' + e.querySelector('.chev').textContent)"),
         ["價錢|已結算", "隧道費|已結算", "舉牌費|已結算"], "fields a batch locks")
    s.press(".sheet.show .field-row", has_text="價錢")
    s.wait_toast("已結算嘅單要先撤銷結算")
    s.eq(s.count(".sheet.show .numpad"), 0, "a numpad for a locked field")
    s.eq(s.text(".sheet.show .cancel-note"), "已結算嘅單要先撤銷結算先取消得", "cancel note")
    s.page.wait_for_timeout(2500)
    s.press(".sheet.show .info-row .oid")
    s.wait_toast("已複製 " + o(203))
    s.eq(s.page.evaluate("() => window.__copied"), o(203), "what was copied")
    # A numpad stacked on it: ‹ goes back one level, and a save returns to the order.
    s.tap(".sheet.show .field-row", has_text="停車費")
    s.eq((s.title(), s.count(".sheet.show .sheet-back")), ("改停車費", 1), "numpad on the stack")
    s.tap(".sheet.show .sheet-back")
    s.eq(s.title(), "接機 11:30", "‹ on the numpad")
    s.edit("停車費", "20")
    s.eq(s.last_write(), ("PATCH", path, {"parking_fee": 20}), "parking fee write")
    s.eq((s.title(), s.field("停車費")), ("接機 11:30", "$20"), "the order after the save")
    # Its batch opens on top of it.
    s.tap(".sheet.show .info-link")
    s.eq(s.title(), "結算 " + span_label(s.back(27), s.back(25)), "the batch opened from the order")
    s.tap(".sheet.show .sheet-back")
    s.eq(s.title(), "接機 11:30", "back on the order")
    # ✕ closes the whole stack here.
    s.tap(".sheet.show .field-row", has_text="時間")
    s.tap(".sheet.show .sheet-x")
    s.wait(lambda: not s.sheet_open() and not s.count(".scrim.show"), "✕ to close the whole stack")

    # A leg whose 舉牌 was paid ahead names the batch that paid it.
    s.open_cell(3)
    s.open_leg(o(601))
    info = s.info()
    s.eq((info["淨收"], info["結算"], info["舉牌"]),
         ("$565", "未結算", f"$40 先結 · 批次 {span_label(s.back(6), s.back(5))} · 等過數 ›"), "rows of a leg with its 舉牌 paid ahead")
    s.close_sheets()

    # A loose leg: priced from here, and the strip behind follows.
    s.open_cell(1)
    s.open_leg(o(11))
    s.edit("價錢", "600")
    s.eq(s.last_write(), ("PATCH", "/api/orders/" + o(11), {"price": 600}), "price write")
    s.eq(s.field("價錢"), "$600", "price on the sheet")
    s.eq(s.text(s.cell(1) + " .amt"), "$1040", "the day's amount behind the sheet")
    s.tap(".sheet.show .sheet-back")
    # Cancelled from here: back to the day, which no longer lists it.
    s.open_leg(o(12))
    s.tap(".sheet.show .cancel-link")
    s.press(".sheet.show .primary-btn.danger")
    s.wait_toast("已取消 #000012")
    s.eq(s.last_write(), ("PATCH", "/api/orders/" + o(12), {"status": "cancelled"}), "cancel write")
    s.settle()
    s.eq(s.title(), md_label(s.back(1)) + " 星期" + WEEKDAY[s.back(1).weekday()], "the sheet after a cancel")
    s.eq(s.page.eval_on_selector_all(".sheet.show .orow", "els => els.map(e => e.dataset.od)"), [o(11)], "the day's legs")
    s.eq(len(s.writes), 3, "writes")


@check("settle.order-that-cannot-be-read")
def settle_order_error(s: Session) -> None:
    s.open_settle()
    o = seed_demo_db._oid
    s.open_cell(1)
    s.stub("GET", "**/api/orders/" + o(12), 404, json.dumps({"error": "搵唔到單"}), "application/json")
    s.allow("http 404", "status of 404")
    s.tap(f'.sheet.show .orow[data-od="{o(12)}"]')
    s.eq((s.title(), s.text(".sheet.show .order-err")), ("單 …0012", "搵唔到單"), "an order the server does not have")
    s.eq(s.count(".sheet.show .field-row"), 0, "fields of an order that could not be read")
    s.tap(".sheet.show .sheet-back")
    s.eq(s.count(".sheet.show .orow"), 2, "back on the day")
    s.never(s.toast, "a toast for an order that could not be read", ms=300)


# ---- settle view: statement intake (inventory J) ----

@check("settle.statement-read")
def settle_statement_read(s: Session) -> None:
    s.open_settle()
    s.allow("http 400", "status of 400")
    path = "/api/statements/read"
    button = s.on('[aria-label="讀結算圖"]').first
    s.eq(button.text_content(), "圖", "the button at rest")
    # By the picker. The image is a blank pixel, so the reader finds nothing
    # in it; that it answered at all is the file having arrived.
    s.hold(path)
    s.pick_statement()
    s.wait(lambda: s.holding(path), "the read to be sent")
    s.eq((button.text_content(), button.is_disabled()), ("⋯", True), "the button while reading")
    s.release_all()
    s.wait_toast(re.compile(r"讀唔到張圖.*— 再上載一次"))
    s.eq((button.text_content(), button.is_disabled()), ("圖", False), "the button after reading")
    s.expect(not s.sheet_open(), "a sheet for a statement that could not be read")
    method, sent, body = s.writes[-1]
    s.eq((method, sent), ("POST", path), "read write")
    s.expect('name="file"; filename="statement.png"' in body and "Content-Type: image/png" in body, f"multipart body: {body!r}")
    # The same file again still triggers a read.
    s.page.wait_for_timeout(2500)
    s.pick_statement()
    s.wait_toast(re.compile(r"讀唔到張圖.*— 再上載一次"))
    s.eq(len(s.writes), 2, "writes after picking the same file twice")
    s.page.wait_for_timeout(2500)

    # By dropping a file on the page.
    s.eq(s.drag("dragenter"), {"dragenter": True}, "a file dragged in is taken")
    s.eq(s.text(".drop.show"), "放低張結算圖", "the drop overlay")
    s.page.evaluate("() => document.body.dispatchEvent(new DragEvent('dragleave', { bubbles: true }))")
    s.eq(s.count(".drop.show"), 0, "the overlay after the drag left")
    s.eq(s.page.evaluate("""() => {
      const dt = new DataTransfer();
      dt.setData('text/plain', 'not a file');
      document.body.dispatchEvent(new DragEvent('dragenter', { dataTransfer: dt, bubbles: true, cancelable: true }));
      return document.querySelectorAll('.drop.show').length;
    }"""), 0, "the overlay for a drag that carries no file")
    s.eq(s.drag("dragenter", "dragover", "drop"), {"dragenter": True, "dragover": True, "drop": True}, "a dropped file is taken")
    s.wait(lambda: len(s.writes) == 3, "the dropped file to be read")
    s.eq(s.writes[-1][:2], ("POST", path), "read write for a dropped file")
    s.eq(s.count(".drop.show"), 0, "the overlay after the drop")
    s.wait(lambda: s.toast(), "the reader's answer")
    s.settle()


@check("settle.statement-confirm")
def settle_statement_confirm(s: Session) -> None:
    s.open_settle()
    s.allow("http 410", "status of 410")
    s.stub("POST", "**/api/statements/read", 200, json.dumps(STATEMENT_READ), "application/json")
    s.pick_statement()
    s.on(".sheet.show .stmt-report").wait_for()
    s.eq((s.title(), s.sub()), ("結算單", "接送"), "statement sheet")
    s.eq(s.text(".sheet.show .stmt-report"), STATEMENT_READ["report"], "the report, as the server sent it")
    s.eq(s.text(".sheet.show .stmt-credit"), STATEMENT_READ["credit_line"], "credit line")
    s.eq(s.texts(".sheet.show .primary-btn, .sheet.show .ghost-btn"), ["確認結算 + 對入數", "唔確認"], "buttons")
    # 唔確認 writes nothing.
    s.tap(".sheet.show .ghost-btn")
    s.wait(lambda: not s.sheet_open(), "the sheet to close")
    s.eq(len(s.writes), 1, "writes after declining")
    # The server no longer holds this read: its refusal stays on the sheet.
    s.pick_statement()
    s.on(".sheet.show .stmt-report").wait_for()
    s.press(".sheet.show [data-stmtgo]")
    s.wait(lambda: s.count(".sheet.show .stmt-err"), "the refusal")
    s.eq(s.writes[-1], ("POST", "/api/statements/confirm", json.dumps({"token": "demo-token"}, separators=(",", ":"))), "confirm write")
    s.settle()
    s.eq(s.text(".sheet.show .stmt-err"), "已過期，再上載一次", "the server's refusal")
    s.eq(s.texts(".sheet.show .primary-btn, .sheet.show .ghost-btn"), ["收埋"], "buttons after a refusal")
    s.close_sheets()
    # Confirmed: the sheet says so and leads to the batch.
    batch = s.t["batch"]["awaiting"]
    s.stub("POST", "**/api/statements/confirm", 200,
           json.dumps({"settlement_id": batch, "text": "DEMO 已結算 3 程 $1270"}), "application/json")
    s.pick_statement()
    s.on(".sheet.show .stmt-report").wait_for()
    s.press(".sheet.show [data-stmtgo]")
    s.wait(lambda: s.title() == "已結算", "the sheet to say it is settled")
    s.settle()
    s.eq(s.text(".sheet.show .stmt-report"), "DEMO 已結算 3 程 $1270", "the server's text")
    s.eq(s.texts(".sheet.show .ghost-btn"), ["睇批次", "收埋"], "buttons after settling")
    s.tap(".sheet.show .ghost-btn", has_text="睇批次")
    s.eq(s.title(), "結算 " + span_label(s.back(19), s.back(18)), "the batch opened from the statement")
    s.eq([w[1] for w in s.writes], ["/api/statements/read", "/api/statements/read", "/api/statements/confirm",
                                    "/api/statements/read", "/api/statements/confirm"], "writes")


# ---- settle view: live update, timing (inventory K) ----

@check("settle.live-update")
def settle_live(s: Session) -> None:
    s.open_settle()
    b, c = s.t["batch"], s.t["credit"]
    s.reach(s.bar("awaiting"))
    at, top = s.scroll_y(), s.top_week()
    s.api("POST", f"/api/credits/{c['exact']}/allocate", {"settlement_id": b["awaiting"]})
    s.wait(lambda: s.marks("bar")[b["awaiting"]]["classes"] == {"bar", "paid"}, "the bar to follow a change made elsewhere")
    s.settle()
    s.eq((s.scroll_y(), s.top_week()), (at, top), "the strip's position after a live update")
    s.expect(s.text(".summary").endswith("入數未對 2 筆 $3170"), "the summary after a live update")
    # An open sheet follows too.
    s.open_mark(s.bar("awaiting"))
    s.eq(s.texts(".sheet.show .hero > div")[2], "已收齊 · " + md_slash(s.back(14)), "the batch, collected")
    s.api("DELETE", f"/api/settlements/{b['awaiting']}/allocations/{c['exact']}")
    s.wait(lambda: s.texts(".sheet.show .hero > div")[2] == "等過數", "the open sheet to follow a change made elsewhere")
    # A batch that goes away takes its sheets with it and leaves what was under them.
    s.close_sheets()
    s.open_cell(19)
    s.tap(".sheet.show .blink")
    s.tap(".sheet.show [data-undo]")
    s.eq(s.title(), "撤銷結算", "three sheets deep")
    s.api("DELETE", f"/api/settlements/{b['awaiting']}")
    s.wait(lambda: s.title() == md_label(s.back(19)) + " 星期" + WEEKDAY[s.back(19).weekday()],
           "the stack to fall back to the day")
    s.eq(s.count(".sheet.show .blink"), 0, "links to a batch that is gone")
    s.close_sheets()
    # The same with nothing under it: the sheet closes.
    s.open_mark(s.bar("group"))
    s.api("DELETE", f"/api/settlements/{b['group']}")
    s.wait(lambda: not s.sheet_open() and not s.count(".scrim.show"), "the sheet of a batch that is gone to close")
    s.eq(s.writes, [], "writes by the page")


@check("settle.live-update-with-an-order-open")
def settle_live_order(s: Session) -> None:
    s.open_settle()
    o = seed_demo_db._oid
    s.open_cell(1)
    s.open_leg(o(12))
    s.api("PATCH", "/api/orders/" + o(12), {"tunnel_fee": 30})
    s.wait(lambda: s.field("隧道費") == "$30", "the open order to follow a change made elsewhere")
    # A numpad stacked on it is repainted, as any top sheet is, and stays a numpad.
    s.tap(".sheet.show .field-row", has_text="價錢")
    s.api("PATCH", "/api/orders/" + o(12), {"parking_fee": 12})
    s.settle()
    s.page.wait_for_timeout(2500)   # the server looks for changes every two seconds
    s.settle()
    s.eq(s.title(), "改價錢", "the stacked numpad")
    s.tap(".sheet.show .sheet-back")
    s.eq(s.field("停車費"), "$12", "the order once back on it")
    s.eq(s.writes, [], "writes by the page")


@check("settle.timing-readout")
def settle_timing(s: Session) -> None:
    ms = re.compile(r"\d+ ms")
    # The switch is taken on this address as on the day view's.
    s.open("/settle?perf=1", ".cell[data-d]")
    s.eq(s.page.url, s.base + "/settle", "the address once ?perf= is taken")
    s.wait_toast(ms)
    s.page.wait_for_timeout(2600)
    # Only the first paint after the document loaded says so.
    s.api("PATCH", "/api/orders/" + seed_demo_db._oid(12), {"price": 401})
    s.wait(lambda: s.text(s.cell(1) + " .amt") == "$941", "the change made elsewhere")
    s.never(s.toast, "a timing toast for a live update", ms=500)
    s.allow("request failed: GET /api/events")     # the navigation cuts the event stream
    s.page.goto(s.base + "/settle?perf=0")
    s.on(".cell[data-d]").first.wait_for()
    s.settle()
    s.eq((s.page.url, s.page.evaluate("() => localStorage.getItem('perf')")), (s.base + "/settle", None),
         "the address and the key after ?perf=0")
    s.never(s.toast, "a timing toast with the readout off", ms=300)


@check("settle.expired-login-does-not-toast")
def settle_auth_expired(s: Session) -> None:
    s.open_settle()
    s.allow("http 401", "status of 401")
    o = seed_demo_db._oid
    quiet = 500

    def expired(method, glob: str) -> None:
        s.stub(method, glob, 401, "<html>log in</html>", "text/html")

    expired(("POST", "PATCH", "DELETE"), "**/api/**")
    s.tap(".sum-link")
    sent = len(s.writes)
    s.press(".sheet.show .qprop .pbtn", has_text="對")
    s.wait(lambda: len(s.writes) > sent, "the allocate write")
    s.never(s.toast, "a toast for an expired login (對)", ms=quiet)
    s.press(".sheet.show .pbtn[data-alloc-all]")
    s.wait(lambda: len(s.writes) > sent + 1, "the allocate-all write")
    s.never(s.toast, "a toast for an expired login (對晒)", ms=quiet)
    s.close_sheets()
    s.open_mark(s.bar("held_back"))
    s.tap(".sheet.show .xbtn")
    s.press(".sheet.show [data-unlinkgo]")
    s.wait(lambda: len(s.writes) > sent + 2, "the deallocate write")
    s.never(s.toast, "a toast for an expired login (解除)", ms=quiet)
    s.tap(".sheet.show .sheet-back")
    s.tap(".sheet.show [data-undo]")
    s.press(".sheet.show [data-undogo]")
    s.wait(lambda: len(s.writes) > sent + 3, "the undo write")
    s.never(s.toast, "a toast for an expired login (撤銷)", ms=quiet)
    s.close_sheets()
    s.open_mark(s.bar("short"), ".sheet.show .up-sec")
    s.press(".sheet.show .up-btn")
    s.wait(lambda: len(s.writes) > sent + 4, "the unpaid write")
    s.never(s.toast, "a toast for an expired login (記低)", ms=quiet)
    s.close_sheets()
    s.open_cell(1)
    s.open_leg(o(11))
    s.tap(".sheet.show .field-row", has_text="停車費")
    s.keys(".sheet.show", "20")
    s.press(".sheet.show #npOk")
    s.wait(lambda: len(s.writes) > sent + 5, "the order write")
    s.never(s.toast, "a toast for an expired login (order edit)", ms=quiet)
    s.expect(s.count(".sheet.show .numpad"), "the numpad stays up")
    s.close_sheets()
    s.pick_statement()
    s.wait(lambda: len(s.writes) > sent + 6, "the statement read")
    s.never(s.toast, "a toast for an expired login (statement read)", ms=quiet)
    s.eq(s.text('[aria-label="讀結算圖"]'), "圖", "the read button after a read that did not happen")
    # A confirm that never reached the server leaves the statement as it was.
    s.stub("POST", "**/api/statements/read", 200, json.dumps(STATEMENT_READ), "application/json")
    s.pick_statement()
    s.on(".sheet.show .stmt-report").wait_for()
    s.press(".sheet.show [data-stmtgo]")
    s.wait(lambda: len(s.writes) > sent + 8, "the confirm write")
    s.never(s.toast, "a toast for an expired login (statement confirm)", ms=quiet)
    s.eq(s.count(".sheet.show .stmt-err"), 0, "an error on the statement sheet")
    s.expect(s.on(".sheet.show [data-stmtgo]").first.is_enabled(), "the confirm button is left disabled")
    s.close_sheets()
    # Reads: a month the strip does not hold, then everything it does.
    expired("GET", "**/api/settle?month=*")
    expired("GET", "**/api/credits?platform=*")
    asked = len(s.requests)
    s.page.evaluate("() => window.scrollTo(0, 0)")
    s.wait(lambda: s.asked(asked), "the month above the strip to be asked for")
    s.never(s.toast, "a toast for an expired login (a month)", ms=quiet)
    # A change on the server asks for nothing more: the banner has said why.
    s.expect(s.banner("auth"), "the banner for an expired login")
    asked = len(s.requests)
    s.api("PATCH", "/api/orders/" + o(12), {"price": 401})
    s.never(lambda: s.requests[asked:], "a request for a change while the login is expired", ms=3500)
    s.never(s.toast, "a toast for an expired login (a change)", ms=quiet)
    s.settle()


# ---- the two views in one document (inventory A9, G3, L4, L10; plan review focus 3 and 5) ----

@check("views.switch-without-a-document-request")
def views_switch(s: Session) -> None:
    s.open_day()
    asked = len(s.requests)
    s.go_settle()
    s.settle()
    s.expect(s.page.url.endswith("/settle"), "address after $")
    s.eq(s.page.title(), "埋數 · Ride Dispatch", "title after $")
    s.eq(s.page.evaluate("() => document.body.dataset.view"), "settle", "the view the body names")
    s.eq(s.count(".orders .row"), 0, "day rows showing on the settle view")
    s.eq(s.scheme(), "dark", "color-scheme on the settle view")
    middle = len(s.requests)
    s.go_day()
    s.expect(s.page.url.endswith("/") and not s.page.url.endswith("/settle"), "address after ✕")
    s.eq(s.page.title(), "Ride Dispatch", "title after ✕")
    s.eq(s.count(".cell"), 0, "settle cells showing on the day view")
    s.eq(s.rows(), s.ids(), "day rows after coming back")
    s.eq(s.scheme(), "normal", "color-scheme on the day view")
    new = s.requests[asked:]
    s.eq([r for r in new if r[2] == "document" or r[1] in ("/", "/settle")], [], "document requests for switching")
    s.eq([r for r in new if not r[1].startswith("/api/")], [], "requests other than for data")
    s.eq([p for _, p, _ in new].count("/api/events"), 0, "new event streams")
    # Each view asks for its own data when it is shown, and only for that.
    s.expect(all(p.startswith(("/api/settle?", "/api/credits?")) for _, p, _ in s.requests[asked:middle]),
             f"requests on showing settle: {s.requests[asked:middle]}")
    s.expect(all(p.startswith("/api/orders?") for _, p, _ in s.requests[middle:]),
             f"requests on showing the day view: {s.requests[middle:]}")
    s.eq(s.writes, [], "writes")


@check("views.reload-and-history")
def views_history(s: Session) -> None:
    s.allow("request failed: GET /api/events")     # a reload cuts the event stream
    s.open_settle()
    s.page.reload()
    s.on(".cell[data-d]").first.wait_for()
    s.settle()
    s.expect(s.page.url.endswith("/settle") and s.count(".orders .row") == 0, "a reload on /settle left the settle view")
    s.go_day()
    s.page.reload()
    s.on(".orders .row").first.wait_for()
    s.settle()
    s.expect(not s.page.url.endswith("/settle") and s.count(".cell") == 0, "a reload on / left the day view")
    docs = len([r for r in s.requests if r[2] == "document"])
    s.go_settle()
    s.to_day()
    s.expect(not s.page.url.endswith("/settle") and s.rows() == s.ids(), "back did not return to the day view")
    s.eq(s.page.title(), "Ride Dispatch", "title after back")
    s.to_settle()
    s.expect(s.page.url.endswith("/settle") and s.count(".cell[data-d]") > 0, "forward did not return to the settle view")
    s.eq(s.page.title(), "埋數 · Ride Dispatch", "title after forward")
    s.eq(len([r for r in s.requests if r[2] == "document"]), docs, "document requests for back and forward")
    # Each view keeps an on-screen way to the other.
    s.eq(s.count('[aria-label="返日程"]'), 1, "the way back to the day view")
    s.to_day()
    s.eq(s.count('[aria-label="埋數"]'), 1, "the way to the settle view")


@check("views.each-view-keeps-its-scroll")
def views_scroll(s: Session) -> None:
    s.open_day()
    s.expect(s.scroll_room() > 40, "the day list does not scroll on this screen")
    s.page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
    s.settle()
    day_at = s.scroll_y()
    s.go_settle()
    s.tap('[aria-label="前一個月"]')
    s.tap('[aria-label="前一個月"]')
    s.page.evaluate("() => window.scrollBy(0, 37)")
    s.settle()
    at, top, weeks, text = s.scroll_y(), s.page.evaluate(TOP_WEEK_JS), s.weeks(), s.month_text()
    held = sorted(set(s.asked()))
    s.expect(at != day_at and at > 0, "the two views happen to be scrolled alike")
    s.go_day()
    s.eq(s.scroll_y(), day_at, "the day view's scroll after coming back")
    asked = len(s.requests)
    s.go_settle()
    s.eq((s.scroll_y(), s.page.evaluate(TOP_WEEK_JS)), (at, top), "the settle view's scroll and top row after coming back")
    s.eq((s.weeks(), s.month_text()), (weeks, text), "the rows the strip holds, and the month it names")
    # Shown again, it reloads what it holds, once, and reaches for nothing more.
    s.eq(sorted(s.asked(asked)), held, "months asked for on coming back")
    s.eq(s.asked(asked, "/api/credits"), [CREDITS], "ledger requests on coming back")
    s.eq(s.strip_problems(), [], "the strip's lanes")
    # Back and forward restore them the same way.
    s.to_day()
    s.eq(s.scroll_y(), day_at, "the day view's scroll after back")
    asked = len(s.requests)
    s.to_settle()
    s.eq((s.scroll_y(), s.page.evaluate(TOP_WEEK_JS)), (at, top), "the settle view's scroll and top row after forward")
    s.eq(sorted(s.asked(asked)), held, "months asked for after forward")


@check("views.hidden-settle-waits-until-shown")
def views_hidden_settle(s: Session) -> None:
    s.open_day()
    s.go_settle()
    b, c = s.t["batch"], s.t["credit"]
    s.reach(s.bar("awaiting"))
    row = week_id(s.back(19))
    s.go_day()
    s.watch("view-settle")
    asked = len(s.requests)
    s.api("POST", f"/api/credits/{c['exact']}/allocate", {"settlement_id": b["awaiting"]})
    s.wait(lambda: s.asked(asked, "/api/orders?"), "the day view to hear of the change", ms=6000)
    s.settle()
    s.page.wait_for_timeout(600)
    s.eq(s.asked(asked) + s.asked(asked, "/api/credits"), [], "requests made for the hidden settle view")
    s.eq(s.changes("view-settle"), [], "changes to the hidden settle view")
    s.go_settle()
    s.eq(s.marks("bar")[b["awaiting"]]["classes"], {"bar", "paid"}, "the batch paid while the view was hidden")
    s.eq(s.marks("chip")[c["exact"]]["classes"], {"cchip"}, "the credit matched while the view was hidden")
    s.eq(s.strip_problems(), [], "the strip's lanes")
    box = s.on(s.bar("awaiting")).first.bounding_box()
    s.expect(box["width"] > 40 and box["height"] > 10, f"the bar has no size: {box}")
    # The row is laid out exactly as a page that never left would lay it out.
    drawn = s.page.eval_on_selector("#" + row, "e => e.innerHTML")
    s.allow("request failed: GET /api/events")     # the reload cuts the event stream
    s.page.reload()
    s.on(".cell[data-d]").first.wait_for()
    s.settle()
    s.reach("#" + row)
    s.eq(s.page.eval_on_selector("#" + row, "e => e.innerHTML"), drawn, "the row, drawn on return and drawn afresh")


@check("views.late-settle-answers-are-not-drawn-while-hidden")
def views_late_settle(s: Session) -> None:
    s.open_day()
    s.page.evaluate("() => window.scrollTo(0, 60)")
    s.settle()
    day_at = s.scroll_y()
    s.go_settle()
    b, c = s.t["batch"], s.t["credit"]
    s.reach(s.bar("awaiting"))
    held = sorted(set(s.asked()))
    # A reload in flight when the operator leaves.
    s.hold(*held, CREDITS)
    s.api("POST", f"/api/credits/{c['exact']}/allocate", {"settlement_id": b["awaiting"]})
    s.wait(lambda: s.holding(CREDITS) and all(s.holding(p) for p in held), "the reload", ms=6000)
    s.press('[aria-label="返日程"]')
    s.on(".orders .row").first.wait_for()
    s.watch("view-settle")
    done = len(s.finished)
    s.release_all()
    s.wait(lambda: len(s.finished) >= done + len(held) + 1, "the held answers to land")
    s.settle()
    s.page.wait_for_timeout(300)
    s.eq(s.changes("view-settle"), [], "changes to the hidden settle view when its reload landed")
    s.eq(s.scroll_y(), day_at, "the day view's scroll")
    s.never(s.toast, "a toast", ms=200)
    s.go_settle()
    s.eq(s.marks("bar")[b["awaiting"]]["classes"], {"bar", "paid"}, "the change, once the view is shown")

    # A month in flight when the operator leaves.
    first = (date.fromisoformat(s.weeks()[0][2:]) + timedelta(days=6)).replace(day=1)
    above = settle_path(add_months(first, -1))
    s.hold(above)
    s.page.evaluate("() => window.scrollTo(0, 0)")
    s.wait(lambda: s.holding(above), "the month above the strip to be asked for")
    weeks = s.weeks()
    s.press('[aria-label="返日程"]')
    s.on(".orders .row").first.wait_for()
    s.watch("view-settle")
    done = len(s.finished)
    s.release_all()
    s.wait(lambda: len(s.finished) > done, "the held month to land")
    s.settle()
    s.page.wait_for_timeout(300)
    s.eq(s.changes("view-settle"), [], "changes to the hidden settle view when a month landed")
    s.eq(s.scroll_y(), day_at, "the day view's scroll")
    # The month is held, and drawn by the load that showing the view makes.
    s.go_settle()
    s.expect(len(s.weeks()) > len(weeks) and s.weeks()[-len(weeks):] == weeks, "the month that landed while hidden is not on the strip")
    days = [date.fromisoformat(w[2:]) for w in s.weeks()]
    s.expect(all(z - a == timedelta(days=7) for a, z in zip(days, days[1:])), "the strip is not one unbroken run of weeks")
    s.eq(s.strip_problems(), [], "the strip's lanes")
    s.eq(s.writes, [], "writes by the page")


@check("views.late-day-answers-are-not-drawn-while-hidden")
def views_late_day(s: Session) -> None:
    s.open_day()
    s.go_settle()
    s.to_day()
    # A save in flight when the operator leaves, with the numpad still up.
    oid = s.t["order"]["dropoff"]
    path = "/api/orders/" + oid
    s.open_order(oid)
    s.tap(".sheet.show .field-row", has_text="價錢")
    s.keys(".sheet.show", "450")
    s.hold(path)
    s.press(".sheet.show #npOk")
    s.wait(lambda: s.holding(path), "the save")
    s.page.go_forward()
    s.on(".cell[data-d]").first.wait_for()
    # The settle view has a sheet of its own open, which the day view's save
    # must leave alone.
    s.press(s.cell(1))
    s.on(".sheet.show .orow").first.wait_for()
    title = s.title()
    s.watch("view-day")
    done = len(s.finished)
    s.release_all()
    s.wait(lambda: ("PATCH", path) in s.finished[done:], "the save to land")
    s.settle()
    s.page.wait_for_timeout(300)
    s.eq(s.changes("view-day"), [], "changes to the hidden day view when its save landed")
    s.eq((s.title(), s.count(".sheet.show .orow")), (title, 2), "the settle view's own sheet")
    s.close_sheets()
    s.to_day()
    s.wait(lambda: s.count(".sheet.show .field-row") and s.field("價錢") == "$450", "the saved order, once the view is shown")
    s.eq(s.title(), "送機 11:00", "the day view's sheet, back on the order")
    s.eq(s.last_write(), ("PATCH", path, {"price": 450}), "the save")
    s.tap(".sheet.show .sheet-x")

    # A day in flight when the operator leaves.
    near, shown = to_a_day_never_seen(s)
    far = "/api/orders?date=" + s.day(2)
    s.wait(lambda: s.holding(far), "the request for the day after tomorrow")
    s.press('[aria-label="埋數"]')
    s.on(".cell[data-d]").first.wait_for()
    s.watch("view-day")
    done = len(s.finished)
    s.release_all()
    s.wait(lambda: ("GET", far) in s.finished[done:] and ("GET", near) in s.finished[done:], "the held answers to land")
    s.settle()
    s.page.wait_for_timeout(300)
    s.eq(s.changes("view-day"), [], "changes to the hidden day view when its day landed")
    s.go_day()
    s.eq((s.rows(), s.count(".orders .empty")), ([], 1), "the day, once the view is shown")
    s.expect(s.date_text().startswith(s.day(2)), "the date the view was left on")
    s.eq(len(s.writes), 1, "writes")


@check("views.order-sheet-follows-the-showing-view")
def views_order_host(s: Session) -> None:
    s.open_day()
    s.go_settle()
    s.to_day()
    o = seed_demo_db._oid
    mine, theirs = s.t["order"]["dropoff"], o(11)
    # The day view's order, with a numpad on it, left open.
    s.open_order(mine)
    s.tap(".sheet.show .field-row", has_text="價錢")
    s.keys(".sheet.show", "45")
    s.to_settle()
    s.eq((s.count(".sheet.show"), s.count(".scrim.show")), (0, 0), "the day view's sheet and scrim on the settle view")
    # The settle view's order: its own rows, its own head, its own order.
    s.open_cell(1)
    s.open_leg(theirs)
    s.eq(s.title(), "接機 10:15", "the settle view's order")
    s.eq(list(s.info())[0], "單號", "the settle view's own rows")
    s.tap(".sheet.show .field-row", has_text="停車費")
    s.eq(s.count(".sheet.show .sheet-back"), 1, "the settle view's back button on the numpad")
    s.tap(".sheet.show .sheet-back")
    s.eq(s.title(), "接機 10:15", "‹ went back one level")
    s.edit("停車費", "20")
    s.eq(s.last_write(), ("PATCH", "/api/orders/" + theirs, {"parking_fee": 20}), "the settle view's save")
    s.tap(".sheet.show .field-row", has_text="時間")
    s.tap(".sheet.show .sheet-x")
    s.wait(lambda: not s.sheet_open() and not s.count(".scrim.show"), "✕ on the settle view to close the whole stack")
    # Back on the day view: its numpad as it was left, and ✕ one level at a time.
    s.to_day()
    s.eq((s.title(), s.text(".sheet.show .numpad-display")), ("改價錢", "$45"), "the day view's numpad as it was left")
    s.eq(s.count(".sheet.show .sheet-back"), 0, "a back button on the day view's sheet")
    s.keys(".sheet.show", "0")
    s.tap(".sheet.show #npOk")
    s.wait(lambda: not s.count(".sheet.show .numpad"), "the numpad to close after a save")
    s.eq(s.last_write(), ("PATCH", "/api/orders/" + mine, {"price": 450}), "the day view's save")
    s.eq((s.title(), s.field("價錢")), ("送機 11:00", "$450"), "the day view's order after the save")
    s.eq(list(s.info())[-1], "結算", "the day view's own rows")
    s.tap(".sheet.show .field-row", has_text="時間")
    s.tap(".sheet.show .sheet-x")
    s.eq(s.title(), "送機 11:00", "✕ on the day view goes back one level")
    s.tap(".sheet.show .sheet-x")
    s.wait(lambda: not s.sheet_open() and not s.count(".scrim.show"), "✕ on the detail to close the sheet")
    # And once more the other way.
    s.to_settle()
    s.open_cell(1)
    s.open_leg(theirs)
    s.eq((list(s.info())[0], s.field("停車費")), ("單號", "$20"), "the settle view's order again")
    s.eq(len(s.writes), 2, "writes")


@check("views.late-order-answers-go-to-the-view-that-asked")
def views_late_order(s: Session) -> None:
    s.open_day()
    s.go_settle()
    s.to_day()
    o = seed_demo_db._oid
    mine, theirs = s.t["order"]["landed_banner"], o(12)
    path = "/api/orders/" + theirs
    s.open_order(mine)
    s.to_settle()
    # An order asked for on the settle view and answered after it was left.
    s.open_cell(1)
    s.hold(path)
    s.press(f'.sheet.show .orow[data-od="{theirs}"]')
    s.wait(lambda: s.holding(path), "the order to be asked for")
    s.page.go_back()
    s.on(".orders .row").first.wait_for()
    s.watch("view-settle")
    done = len(s.finished)
    s.release_all()
    s.wait(lambda: ("GET", path) in s.finished[done:], "the order to arrive")
    s.page.wait_for_timeout(300)
    s.eq(s.changes("view-settle"), [], "changes to the hidden settle view when its order arrived")
    s.eq((s.title(), s.count(".sheet.show .field-row")), ("接機 14:20", 5), "the day view's own sheet")
    s.to_settle()
    s.on(".sheet.show .field-row").first.wait_for()
    s.eq((s.title(), s.info()["單號"]), ("送機 16:30", "8800 0000 0000 0012"), "the order, once the view is shown")
    # A cancel answered after the view was left is the settle view's to act on.
    s.tap(".sheet.show .cancel-link")
    s.hold(path)
    s.press(".sheet.show .primary-btn.danger")
    s.wait(lambda: s.holding(path), "the cancel")
    s.page.go_back()
    s.on(".orders .row").first.wait_for()
    s.page.wait_for_timeout(500)      # the day view's own load on being shown
    asked = len(s.requests)
    s.release_all()
    s.wait_toast("已取消 #000012")
    s.settle()
    s.eq((s.title(), s.count(".sheet.show .field-row")), ("接機 14:20", 5), "the day view's own sheet after the cancel landed")
    # The settle view would reload after its write, and does not while hidden.
    s.eq(s.asked(asked) + s.asked(asked, "/api/credits"), [], "requests made for the hidden settle view")
    s.to_settle()
    s.wait(lambda: s.count(".sheet.show .orow") == 1, "the day sheet without the cancelled leg")
    s.eq(s.title(), md_label(s.back(1)) + " 星期" + WEEKDAY[s.back(1).weekday()], "the settle view's sheet after the cancel")
    s.eq(s.last_write(), ("PATCH", path, {"status": "cancelled"}), "the cancel")


@check("views.statement-read-answered-on-the-day-view")
def views_statement(s: Session) -> None:
    s.open_day()
    s.go_settle()
    path = "/api/statements/read"
    s.hold(path)
    s.pick_statement()
    s.wait(lambda: s.holding(path), "the read to be sent")
    s.press('[aria-label="返日程"]')
    s.on(".orders .row").first.wait_for()
    s.answer(path, status=200, content_type="application/json", body=json.dumps(STATEMENT_READ))
    s.wait(lambda: ("POST", path) in s.finished, "the read to be answered")
    s.page.wait_for_timeout(300)
    s.eq((s.count(".sheet.show"), s.count(".scrim.show")), (0, 0), "a sheet or scrim over the day view")
    s.release_all()
    s.open_order(s.t["order"]["dropoff"])
    s.tap(".sheet.show .sheet-x")
    # The statement is there when the operator comes back for it.
    s.go_settle()
    s.on(".sheet.show .stmt-report").wait_for()
    s.eq((s.title(), s.text(".sheet.show .stmt-report")), ("結算單", STATEMENT_READ["report"]), "the statement, once the view is shown")
    s.eq((s.text('[aria-label="讀結算圖"]'), s.on('[aria-label="讀結算圖"]').first.is_disabled()), ("圖", False), "the read button")
    s.expect(s.on(".sheet.show [data-stmtgo]").first.is_enabled(), "the statement cannot be confirmed")
    s.eq(len(s.writes), 1, "writes")


@check("views.sheets-do-not-leak")
def views_sheets(s: Session) -> None:
    s.open_day()
    s.go_settle()
    s.open_mark(s.bar("awaiting"))
    batch = s.title()
    s.to_day()
    s.eq((s.count(".sheet.show"), s.count(".scrim.show"), s.count(".drop.show")), (0, 0, 0), "settle's sheet, scrim or overlay on the day view")
    # The day view scrolls and takes taps.
    s.page.evaluate("() => window.scrollTo(0, 50)")
    s.eq(s.scroll_y(), 50, "the day view's scroll under a sheet left open on the other view")
    s.open_order(s.t["order"]["dropoff"])
    s.eq(s.title(), "送機 11:00", "the day view's sheet")
    s.to_settle()
    s.eq((s.count(".sheet.show"), s.count(".scrim.show"), s.title()), (1, 1, batch), "the settle view's sheet, as it was left")
    s.close_sheets()
    s.tap(s.cell(1))
    s.on(".sheet.show .orow").first.wait_for()
    s.close_sheets()
    s.to_day()
    s.eq((s.count(".sheet.show"), s.count(".scrim.show"), s.title()), (1, 1, "送機 11:00"), "the day view's sheet, as it was left")
    # The add panel is the day view's alone.
    s.tap(".sheet.show .sheet-x")
    s.tap('[aria-label="入單"]')
    s.stage("入單")
    s.page.go_forward()
    s.on(".cell[data-d]").first.wait_for()
    s.settle()
    s.eq((s.count(".drop.show"), s.count(".scrim.show"), s.count(".paste-box")), (0, 0, 0), "the add panel on the settle view")
    s.eq(s.writes, [], "writes")


@check("views.settle-listeners-stand-down-on-the-day-view")
def views_listeners(s: Session) -> None:
    s.open_day()
    s.go_settle()
    s.go_day()
    s.watch("view-settle")
    # A file dragged over the day view is not the settle view's to take.
    s.eq(s.drag("dragenter", "dragover", "drop"), {"dragenter": False, "dragover": False, "drop": False},
         "drag events taken on the day view")
    s.eq(s.page.eval_on_selector("#settle-drop", "e => e.className"), "drop", "the settle view's drop overlay")
    # A resize and a scroll do not lay the hidden strip out.
    s.page.set_viewport_size({"width": 430, "height": 700})
    s.page.evaluate("() => window.scrollTo(0, 40)")
    s.page.wait_for_timeout(500)
    s.settle()
    s.eq(s.changes("view-settle"), [], "changes to the hidden settle view")
    s.eq(s.writes, [], "writes")
    # Shown again, the strip is laid out for the new width.
    s.go_settle()
    s.eq(s.strip_problems(), [], "the strip's lanes at the new width")
    s.eq(s.drag("dragenter"), {"dragenter": True}, "a file dragged over the settle view")
    s.eq(s.count(".drop.show"), 1, "the drop overlay on the settle view")


@check("views.switch-timing")
def views_timing(s: Session) -> None:
    ms = re.compile(r"\d+ ms")
    s.open("/?perf=1", ".orders .row")
    s.wait_toast(ms)
    s.page.wait_for_timeout(2600)
    # To settle: from the tap to the paint of what the server answered.
    s.hold(CREDITS)
    s.press('[aria-label="埋數"]')
    s.wait(lambda: s.holding(CREDITS), "the settle view's load")
    s.page.wait_for_timeout(700)
    s.eq(s.toast(), "", "a readout before the data is painted")
    s.release_all()
    took = int(s.wait_toast(ms).split()[0])
    s.expect(700 <= took < 5000, f"day to settle reported {took} ms with the answer held for 700")
    s.settle()
    s.page.wait_for_timeout(2600)
    # A month scrolled in, or a change, is not a navigation.
    s.tap('[aria-label="前一個月"]')
    s.never(s.toast, "a readout for scrolling the strip", ms=400)
    # Back to the day view, which paints what it holds at once.
    s.press('[aria-label="返日程"]')
    took = int(s.wait_toast(ms).split()[0])
    s.expect(took < 500, f"settle to day reported {took} ms")
    s.settle()
    s.page.wait_for_timeout(2600)
    # Back and forward are not taps: nothing to time from.
    s.page.go_back()
    s.on(".cell[data-d]").first.wait_for()
    s.settle()
    s.never(s.toast, "a readout for back", ms=500)


# ---- the service worker (plan review focus 1) ----
#
# These run with the worker let in. A page it controls cannot have its requests
# held or stubbed, so what the server was asked is read from the server's own
# log, and the server is made to misbehave instead (Server.fault).

LOST_SERVER = ("request failed", "Failed to load resource", "Could not connect", "http 503",
               "status of 503", "network connection was lost")
# A request the browser gave up on, because the page was reloaded under it or
# because it was sent to another origin's login, is reported in these words.
CUT_OFF = ("access control checks", "Access-Control-Allow-Origin")


@check("worker.first-install-takes-control-without-a-reload", workers=True)
def worker_first_install(s: Session) -> None:
    s.open_day()
    s.mark()
    s.controlled()
    s.page.wait_for_timeout(600)
    s.expect(s.marked(), "the page was reloaded when the first worker took control")
    s.eq(s.documents(), 1, "document requests")
    s.expect(not s.banner("update") and not s.banner("auth"), "a banner is showing")
    g = s.geometry()
    s.eq((g["banners"], g["header"][0]), ([], 0), "banners taking room while hidden")
    w = s.worker()
    s.eq((w["active"], w["waiting"], w["installing"]), (True, False, False), "the registration")
    # Whole: the document and every asset it can ask for, and nothing else.
    listed = s.precached()
    s.eq(w["caches"], {"shell-" + s.server.version(): listed}, "what the worker holds")
    wanted = {p for m, p, kind in s.requests if kind in ("script", "stylesheet")}
    s.expect(wanted and wanted <= set(listed), f"the page asked for assets the worker does not hold: {wanted - set(listed)}")


@check("worker.shell-from-the-cache-data-from-the-server", workers=True)
def worker_offline_shell(s: Session) -> None:
    s.allow(*LOST_SERVER)
    s.open_day()
    s.controlled()
    # The server refuses the document, the assets and the worker script: a
    # launch must not need any of them.
    s.server.fault(no_shell=True)
    asked, answered = len(s.server.asked()), len(s.answers)
    s.reload()
    s.eq(s.rows(), s.ids(), "rows after a launch the server gave no document to")
    since = s.server.asked()[asked:]
    s.eq([a for a in since if a[1].split("?")[0] in ("/", "/settle") or a[1].startswith("/assets/")], [],
         "requests to the server for the document or an asset")
    s.expect(("GET", "/api/orders?date=" + s.day(), 200) in since, "today's orders were not asked of the server")
    answers = s.answers[answered:]
    shell = [w for p, st, w in answers if p == "/" or p.startswith("/assets/")]
    s.expect(len(shell) > 10 and all(shell), "the document or an asset did not come from the worker")
    s.eq([p for p, st, w in answers if p.startswith("/api/") and w], [], "data answered by the worker")
    # Ordinary use under the worker: a change made elsewhere, a write, the other view.
    oid = s.t["order"]["dropoff"]
    s.api("PATCH", "/api/orders/" + oid, {"price": 455})
    s.wait(lambda: s.text(s.row(oid) + " .price") == "$455", "the change made elsewhere")
    s.open_order(oid)
    s.edit("價錢", "460")
    s.eq(s.last_write(), ("PATCH", "/api/orders/" + oid, {"price": 460}), "the write")
    s.expect(("PATCH", "/api/orders/" + oid, 200) in s.server.asked(), "the write never reached the server")
    s.on(".scrim").first.tap(position={"x": 8, "y": 8})
    s.go_settle()
    s.expect(("GET", CREDITS, 200) in s.server.asked(), "the settle view's data was not asked of the server")
    s.go_day()
    held = [k for keys in s.worker()["caches"].values() for k in keys]
    s.eq([k for k in held if not (k.startswith("/assets/") or k == SHELL_KEY)], [], "stored by the worker beside the shell")
    s.eq([p for p, st, w in s.answers if p.startswith("/api/") and w], [], "data answered by the worker")
    # No server at all: the shell still paints, the data is asked for and
    # fails, and nothing stands in for it.
    s.server.stop()
    tried = len(s.requests)
    s.page.reload()
    s.wait_toast("載入失敗")
    s.expect(s.date_text().startswith(s.day()), "the date button without a server")
    s.eq(s.count('[aria-label="入單"]'), 1, "the header's controls without a server")
    s.eq(s.rows(), [], "rows shown with no server to give them")
    s.expect(("GET", "/api/orders?date=" + s.day(), "fetch") in s.requests[tried:], "today's orders were not asked for")


@check("worker.stale-version-address-is-refused", workers=True)
def worker_stale_address(s: Session) -> None:
    s.allow("http 404", "status of 404")
    s.open_day()
    s.controlled()
    v = s.server.version()
    fetch = "p => fetch(p).then(r => r.status)"
    s.eq(s.page.evaluate(fetch, "/assets/000000000000/js/main.js"), 404, "an asset under a version that is not the server's")
    s.eq(s.page.evaluate(fetch, f"/assets/{v}/js/main.js"), 200, "the same asset under the server's version")
    s.wait(lambda: ("GET", "/assets/000000000000/js/main.js", 404) in s.server.asked(),
           "an address the worker does not hold to be passed to the server")


def refused_install(s: Session, document: str, **fault) -> None:
    """Load with the server's document unfit to be stored, then with it fit."""
    s.allow(*LOST_SERVER)
    s.server.fault(**fault)
    s.open_day()
    s.wait(lambda: len([a for a in s.server.asked() if a == ("GET", document, 200)]) >= 2,
           "the worker's own request for the document")
    s.never(lambda: s.worker()["controller"] or s.worker()["active"], "a worker took charge of a version it could not store whole", ms=1500)
    s.eq([k for keys in s.worker()["caches"].values() for k in keys if k == SHELL_KEY], [], "a document stored")
    s.eq(s.rows(), s.ids(), "the app, from the network")
    s.expect(not s.banner("update"), "an update is offered")
    # The next load tries again.
    s.server.fault()
    s.reload()
    s.controlled()
    s.eq(s.worker()["caches"], {"shell-" + s.server.version(): s.precached()}, "what the worker holds")


@check("worker.install-refused-for-a-document-of-another-version", workers=True)
def worker_refuses_version(s: Session) -> None:
    refused_install(s, "/", doc_version="000000000000")


@check("worker.install-refused-for-a-redirected-document", workers=True)
def worker_refuses_redirect(s: Session) -> None:
    refused_install(s, "/?redirected=1", doc_redirect=True)


@check("worker.update-waits-for-the-tap", workers=True, copy=True)
def worker_update(s: Session) -> None:
    s.allow(*LOST_SERVER)
    s.open_day()
    s.controlled()
    v1 = s.shown_version()
    s.open_order(s.t["order"]["dropoff"])
    s.mark()
    v2 = s.deploy()
    s.expect(v2 != v1, "the deploy did not change the version")
    s.look_for_update()
    s.wait(lambda: s.banner("update"), "the update banner")
    s.never(lambda: not s.marked(), "a reload nobody asked for", ms=1500)
    s.eq(s.shown_version(), v1, "the version on screen before the tap")
    s.expect(s.sheet_open(), "the open sheet was lost")
    w = s.worker()
    s.eq((w["active"], w["waiting"]), (True, True), "the new worker, waiting")
    s.eq(w["caches"].get("shell-" + v1), [a.replace(v2, v1) for a in s.precached()], "the cache of the version in use")
    s.eq(s.documents(), 1, "document requests before the tap")
    s.eq(s.text("#banner-update"), "有新版本 · 撳呢度更新", "the banner")
    # The banner is reachable over the scrim of the open sheet.
    since, asked = len(s.requests), len(s.server.asked())
    s.page.locator("#banner-update").tap()
    s.wait(lambda: s.shown_version() == v2, "the new version after the tap")
    s.on(".orders .row").first.wait_for()
    s.settle()
    s.expect(not s.marked() and s.documents() == 2, "the tap did not reload the page exactly once")
    s.expect(not s.banner("update"), "the banner after the update")
    s.eq([r for r in s.requests[since:] if f"/assets/{v1}/" in r[1]], [], "requests for the old version's assets")
    s.eq([a for a in s.server.asked()[asked:] if f"/assets/{v1}/" in a[1]], [], "the old version's assets asked of the server")
    w = s.worker()
    s.eq((w["waiting"], w["caches"]), (False, {"shell-" + v2: s.precached()}), "the worker after the update")
    s.eq(s.rows(), s.ids(), "rows on the new version")


@check("worker.waiting-version-is-taken-at-launch", workers=True, copy=True)
def worker_update_at_boot(s: Session) -> None:
    s.allow(*LOST_SERVER, *CUT_OFF)
    s.open_day()
    s.controlled()
    v1 = s.shown_version()
    v2 = s.deploy()
    s.look_for_update()
    s.wait(lambda: s.banner("update"), "the update banner")
    # Not tapped. The next launch has nothing open to lose, and takes it.
    s.page.reload()
    s.wait(lambda: s.shown_version() == v2, "the waiting version at the next launch")
    s.on(".orders .row").first.wait_for()
    s.settle()
    s.expect(not s.banner("update"), "the banner after the update")
    s.eq(sorted(s.worker()["caches"]), ["shell-" + v2], "caches")
    s.expect(v1 != v2 and s.rows() == s.ids(), "rows on the new version")


@check("worker.another-window-is-offered-the-reload", workers=True, copy=True)
def worker_two_windows(s: Session) -> None:
    s.allow(*LOST_SERVER)
    s.open_day()
    s.controlled()
    first = s.page
    s.open_day()
    s.controlled()
    v1 = s.shown_version()
    s.open_order(s.t["order"]["dropoff"])
    s.mark()
    v2 = s.deploy()
    s.look_for_update(first)
    s.wait(lambda: s.banner("update", first), "the update banner in the first window")
    first.locator("#banner-update").tap()
    s.wait(lambda: s.shown_version(first) == v2, "the first window on the new version")
    # The second window's worker has been replaced under it. It is told, and
    # keeps what it has open until its own banner is tapped.
    s.wait(lambda: s.banner("update"), "the update banner in the second window")
    s.never(lambda: not s.marked(), "the second window reloaded on its own", ms=1500)
    s.expect(s.sheet_open() and s.shown_version() == v1, "the second window lost what it had open")
    s.page.locator("#banner-update").tap()
    s.wait(lambda: s.shown_version() == v2, "the second window on the new version")
    s.on(".orders .row").first.wait_for()
    s.settle()
    s.expect(not s.banner("update"), "the banner after the update")


@check("worker.deploy-during-install-leaves-the-old-version-in-charge", workers=True, copy=True)
def worker_redeployed(s: Session) -> None:
    s.allow(*LOST_SERVER, "http 404", "status of 404")
    s.open_day()
    s.controlled()
    v1 = s.shown_version()
    held = s.precached()
    # The worker script of one version, then the server of the next: every
    # asset address the script names is gone.
    v2 = s.deploy(no_assets=True)
    s.look_for_update()
    s.wait(lambda: any(p.startswith(f"/assets/{v2}/") and st == 404 for m, p, st in s.server.asked()),
           "the install to ask for its assets")
    s.never(lambda: s.banner("update"), "an update is offered though its install failed", ms=1500)
    w = s.worker()
    s.eq((w["active"], w["waiting"], w["installing"]), (True, False, False), "the registration")
    s.eq(w["caches"].get("shell-" + v1), held, "the cache of the version in charge")
    s.eq(w["caches"].get("shell-" + v2, []), [], "stored of the version that failed")
    # It still launches from what it holds.
    s.server.fault(no_shell=True)
    s.reload()
    s.eq((s.shown_version(), s.rows()), (v1, s.ids()), "the next launch")
    # The next look finds the server whole and offers the version.
    s.server.fault()
    s.look_for_update()
    s.wait(lambda: s.banner("update"), "the update banner once the install can finish")
    s.eq(s.worker()["waiting"], True, "the new worker, waiting")


# ---- an expired login (plan review focus 2) ----

@check("auth.banner-instead-of-a-toast")
def auth_banner(s: Session) -> None:
    s.allow("http 401", "status of 401")
    s.open_settle()
    g = s.geometry()
    s.eq((g["banners"], g["header"][0]), ([], 0), "banners taking room while hidden")
    s.go_day()
    oid = s.t["order"]["dropoff"]
    for glob in ("**/api/orders?date=*", "**/api/ping", "**/api/settle?*", "**/api/credits?*"):
        s.stub("GET", glob, 401, "<html>log in</html>", "text/html")
    # The stream still runs: a change on the server makes the day view ask, and
    # that request is what finds the login gone.
    asked = len(s.requests)
    s.api("PATCH", "/api/orders/" + oid, {"price": 455})
    s.wait(lambda: s.banner("auth"), "the banner", ms=6000)
    s.eq(s.text("#banner-auth"), "登入過期 · 撳呢度重新登入", "the banner")
    s.never(s.toast, "a toast for an expired login")
    s.expect(not s.banner("update"), "the update banner")
    s.eq(len([r for r in s.requests[asked:] if r[1].startswith("/api/orders?")]), 1, "requests that found the login gone")
    # From here a change on the server asks nothing: there is nothing to fetch.
    asked = len(s.requests)
    s.api("PATCH", "/api/orders/" + oid, {"price": 456})
    s.never(lambda: s.requests[asked:], "a request for a change while the login is expired", ms=3500)
    # The banner takes the top of the screen and the header sits under it,
    # every control still in reach; so too with the page scrolled.
    width = s.page.viewport_size["width"]
    for where in ("day", "settle", "settle, scrolled"):
        if where == "settle":
            s.go_settle()
        if where.endswith("scrolled"):
            s.page.evaluate("() => window.scrollBy(0, 400)")
            s.settle()
        g = s.geometry()
        s.eq(g["banners"], ["banner-auth"], f"banners showing ({where})")
        s.eq(g["banner"], [0, 44, 0, width], f"the banner's box ({where})")
        s.eq(g["header"], [44, 0, width], f"the header's box ({where})")
        s.eq((g["covered"], g["wide"]), (0, False), f"header controls covered, page wider than the screen ({where})")
    s.never(s.toast, "a toast for an expired login (the settle view)", ms=300)
    # With a sheet open the banner is still in reach, over the scrim.
    s.go_day()
    s.open_order(oid)
    hit = s.page.evaluate("() => document.elementFromPoint(innerWidth / 2, 22).id")
    s.eq(hit, "banner-auth", "what a tap on the banner lands on with a sheet open")
    # The update banner gives way to it.
    s.page.evaluate("() => { document.getElementById('banner-update').hidden = false; }")
    s.eq(s.geometry()["banners"], ["banner-auth"], "banners showing with an update waiting too")
    s.settle()


@check("auth.expired-at-launch-with-the-shell-from-the-cache", workers=True)
def auth_launch(s: Session) -> None:
    s.allow(*LOST_SERVER, *CUT_OFF)
    s.open_settle()
    s.controlled()
    # The proxy now answers everything with its login. The document is the
    # worker's, so the launch paints; the data is what shows the login gone.
    s.server.fault(expired=True)
    answered = len(s.answers)
    s.page.reload()
    s.wait(lambda: s.banner("auth"), "the banner")
    s.expect(s.page.url == s.base + "/settle" and s.count('[aria-label="讀結算圖"]') == 1,
             "the settle view did not paint from the cache")
    s.expect(any(w for p, st, w in s.answers[answered:] if p == "/settle"), "the document did not come from the worker")
    s.never(s.toast, "a toast for an expired login")
    s.eq(s.count(".cell[data-d]"), 0, "data shown with the login expired")
    # The tap is a navigation the worker passes on, so the proxy can answer it.
    asked = len(s.server.asked())
    s.page.locator("#banner-auth").tap()
    s.page.wait_for_url("**" + LOGIN_PATH + "?**")
    def logins() -> list:
        return [st for m, p, st in s.server.asked()[asked:] if p.startswith("/settle?login=")]

    s.wait(logins, "the navigation the banner made to reach the server")
    s.eq(logins(), [302], "the navigation the banner made, as the server saw it")
    # Logged in again: the proxy sends the browser back to what it asked for.
    s.server.fault()
    asked = len(s.server.asked())
    s.page.locator("#back").tap()
    s.on(".cell[data-d]").first.wait_for()
    s.settle()
    s.eq(s.page.url, s.base + "/settle", "the address after the login")
    s.expect(not s.banner("auth"), "the banner after the login")
    s.eq(logins(), [200], "the return, as the server saw it")
    s.expect(("GET", CREDITS, 200) in s.server.asked()[asked:], "the settle view's data after the login")
    s.expect(s.worker()["controller"], "the worker after the login")
    # And the session works: a change on the server reaches the page.
    asked = len(s.server.asked())
    s.api("PATCH", "/api/orders/" + seed_demo_db._oid(12), {"price": 401})
    s.wait(lambda: ("GET", CREDITS, 200) in s.server.asked()[asked:], "the reload for a change", ms=6000)
    s.settle()


@check("auth.dropped-stream-finds-the-expired-login", workers=True)
def auth_stream(s: Session) -> None:
    s.allow(*LOST_SERVER, *CUT_OFF)
    s.open_day()
    s.controlled()
    shown = s.rows()
    tried = len(s.requests)

    def made(path: str) -> int:
        return len([r for r in s.requests[tried:] if r[1] == path])

    # The line drops. Every failure of the stream asks for a ping, and the
    # pings are spaced however often it fails.
    s.server.stop()
    s.wait(lambda: made("/api/events") >= 3, "the stream to try three times", ms=30_000)
    s.expect(1 <= made("/api/ping") < made("/api/events"),
             f"{made('/api/ping')} pings for {made('/api/events')} tries of the stream")
    s.expect(not s.banner("auth"), "a dropped line is taken for an expired login")
    # When the line is back the login is gone. Nothing on the page asks for
    # data; the stream failing to reconnect is the only sign.
    s.server.fault(expired=True)
    s.server.start()
    s.wait(lambda: s.banner("auth"), "the banner", ms=20_000)
    s.never(s.toast, "a toast for an expired login")
    s.eq(s.rows(), shown, "rows")
    # The page has its answer and asks no more.
    pings = made("/api/ping")
    s.never(lambda: made("/api/ping") > pings, "a ping once the login is known to be gone", ms=7000)


@check("auth.expired-login-found-when-the-line-comes-straight-back", workers=True)
def auth_stream_quick(s: Session) -> None:
    s.allow(*LOST_SERVER, *CUT_OFF)
    s.open_day()
    s.controlled()
    # The stream's failure against the login comes seconds after its failure
    # for the dropped line, inside the gap that spaces the pings, and in WebKit
    # it is the last there will be: the stream does not try again after it.
    s.server.stop()
    s.server.fault(expired=True)
    s.server.start()
    s.wait(lambda: s.banner("auth"), "the banner", ms=20_000)
    s.never(s.toast, "a toast for an expired login")


# ---- the stream in a document that is never reloaded ----
#
# The server is made to misbehave rather than the page's requests held: an
# EventSource is the browser's own, and what it does with a refusal is the
# thing being checked.

HIDE_JS = """
state => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}
"""


def streams(s: Session) -> int:
    """How many event streams the page has had opened for it."""
    return len([a for a in s.answers if a[0] == "/api/events" and a[1] == 200])


def refused(s: Session) -> int:
    """How many times the server has refused the event stream. The browser
    reports a refused stream as a request it gave up, with no answer."""
    return len([a for a in s.server.asked() if a[1:] == ("/api/events", 502)])


def live_again(s: Session, opened: int) -> None:
    """Wait for a stream opened since `opened` were counted, and for the page
    to have caught up on its greeting."""
    s.wait(lambda: streams(s) > opened, "the stream to be opened again", ms=45_000)
    s.settle()


@check("stream.reopened-after-the-gateway-refused-it")
def stream_bad_gateway(s: Session) -> None:
    s.allow(*LOST_SERVER, "http 502", "status of 502", "request failed: GET /api/events")
    s.open_day()
    oid = s.t["order"]["dropoff"]
    opened, shown = streams(s), s.rows()
    # A deploy: the server goes, and until it is back the tunnel answers for it.
    s.server.stop()
    s.server.fault(bad_gateway=True)
    s.server.start()
    # A refusal ends an EventSource; one refusal after another is the page
    # opening a new one each time.
    s.wait(lambda: refused(s) >= 3, "the stream to be tried again after being refused", ms=30_000)
    s.expect(not s.banner("auth"), "a refusing gateway is taken for an expired login")
    s.never(s.toast, "a toast for a stream that is down", ms=300)
    s.eq(s.rows(), shown, "rows while the stream is down")
    # The server is back. What changed while the stream was down arrives with
    # the new stream's greeting, and what changes afterwards arrives live.
    s.server.fault()
    s.api("PATCH", "/api/orders/" + oid, {"price": 454})
    live_again(s, opened)
    s.wait(lambda: s.text(s.row(oid) + " .price") == "$454", "the change made while the stream was down")
    s.api("PATCH", "/api/orders/" + oid, {"price": 455})
    s.wait(lambda: s.text(s.row(oid) + " .price") == "$455", "a change made after the stream came back")
    s.eq(s.documents(), 1, "document requests")
    s.eq(s.writes, [], "writes by the page")


@check("stream.reopened-after-the-server-was-away")
def stream_server_away(s: Session) -> None:
    s.allow(*LOST_SERVER)
    s.open_settle()
    opened, tried = streams(s), len(s.requests)
    s.server.stop()
    s.wait(lambda: len([r for r in s.requests[tried:] if r[1] == "/api/events"]) >= 2,
           "the stream to be tried while the server is away", ms=30_000)
    s.expect(not s.banner("auth"), "a server that is away is taken for an expired login")
    s.server.start()
    live_again(s, opened)
    s.api("PATCH", "/api/orders/" + seed_demo_db._oid(12), {"price": 401})
    s.wait(lambda: s.text(s.cell(1) + " .amt") == "$941", "a change made after the server came back")
    s.eq(s.documents(), 1, "document requests")
    s.eq(s.writes, [], "writes by the page")


@check("stream.coming-back-to-the-app-refreshes-the-showing-view")
def stream_visible(s: Session) -> None:
    s.open_day()
    today = "/api/orders?date=" + s.day()

    def since(asked: int, prefix: str = "/api/") -> list:
        return [p for _, p, _ in s.requests[asked:] if p.startswith(prefix)]

    # Going away asks nothing; coming back asks for what is showing, once.
    asked = len(s.requests)
    s.page.evaluate(HIDE_JS, "hidden")
    s.never(lambda: since(asked), "a request on being hidden", ms=500)
    s.page.evaluate(HIDE_JS, "visible")
    s.wait(lambda: today in since(asked), "the day view to refresh on coming back")
    s.settle()
    s.eq(since(asked).count(today), 1, "requests for the day showing")
    s.eq([p for p in since(asked) if not p.startswith("/api/orders?date=")], [], "requests for anything else")
    s.eq(s.rows(), s.ids(), "rows")
    # The event alone, with the document never hidden, is not a return.
    asked = len(s.requests)
    s.page.evaluate("() => { document.dispatchEvent(new Event('visibilitychange')); }")
    s.never(lambda: since(asked), "a refresh for a document that was never hidden", ms=700)
    # The settle view, when that is the one showing, and only it.
    s.go_settle()
    asked = len(s.requests)
    s.page.evaluate(HIDE_JS, "hidden")
    s.page.evaluate(HIDE_JS, "visible")
    s.wait(lambda: CREDITS in since(asked), "the settle view to refresh on coming back")
    s.settle()
    s.eq(since(asked).count(CREDITS), 1, "requests for the ledger")
    s.eq(since(asked, "/api/orders"), [], "requests for the hidden day view")
    s.eq(since(asked).count("/api/events"), 0, "new event streams")
    s.eq(s.strip_problems(), [], "the strip's lanes")
    s.eq(s.writes, [], "writes")


@check("stream.nothing-reopens-or-refreshes-with-the-login-expired")
def stream_expired(s: Session) -> None:
    s.allow(*LOST_SERVER, *CUT_OFF)
    s.open_day()
    shown = s.rows()
    # The line drops and comes back with the login gone.
    s.server.stop()
    s.server.fault(expired=True)
    s.server.start()
    s.wait(lambda: s.banner("auth"), "the banner", ms=30_000)
    # Longer than any wait a stream closed before the banner could be serving.
    asked = len(s.requests)
    s.never(lambda: s.requests[asked:], "a request once the login is known to be gone", ms=9000)
    s.page.evaluate(HIDE_JS, "hidden")
    s.page.evaluate(HIDE_JS, "visible")
    s.never(lambda: s.requests[asked:], "a request on coming back with the login expired", ms=1500)
    s.never(s.toast, "a toast for an expired login", ms=200)
    s.eq(s.rows(), shown, "rows")


# ---- inventory items no check above reaches ----

# Where the page is scrolled to, and where it would be with this row's middle
# at the middle of the screen (as far as the document can scroll).
CENTRED_JS = """
el => {
  const r = el.getBoundingClientRect();
  const most = document.documentElement.scrollHeight - window.innerHeight;
  const want = window.scrollY + r.top + r.height / 2 - window.innerHeight / 2;
  return [Math.round(window.scrollY), Math.round(Math.max(0, Math.min(most, want)))];
}
"""

# Every style rule in force, nested ones included, as [selector, declarations,
# the media query around it].
RULES_JS = """
() => {
  const out = [];
  const walk = (rules, media) => {
    for (const r of rules) {
      if (r.selectorText !== undefined) out.push([r.selectorText, r.style.cssText, media]);
      if (r.cssRules && r.cssRules.length) walk(r.cssRules, r.media ? r.media.mediaText : media);
    }
  };
  for (const sheet of document.styleSheets) walk(sheet.cssRules, '');
  return out;
}
"""


@check("inventory.day-scroll")
def inventory_day_scroll(s: Session) -> None:
    """A7, A8, A14, C16."""
    s.open_day()
    nxt = ".orders .row.next"

    def centred(what: str) -> None:
        at, want = s.on(nxt).first.evaluate(CENTRED_JS)
        s.expect(abs(at - want) <= 1, f"the NEXT row is not at the middle of the screen {what}: at {at}, want {want}")

    s.expect(s.on(nxt).first.evaluate(CENTRED_JS)[1] > 0, "the NEXT row is in the middle without any scrolling")
    centred("after the load")
    s.page.evaluate("() => window.scrollTo(0, 0)")
    s.tap(".date-btn")
    centred("after tapping the date")
    s.go_days(1)
    s.go_days(-1)
    centred("after coming back to today")
    # The header stays at the top while the list scrolls under it.
    s.page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
    s.settle()
    s.expect(s.scroll_y() > 0, "the list does not scroll")
    s.eq(round(s.on(".header").first.bounding_box()["y"]), 0, "header top with the list scrolled")
    # A filter tap and a saved edit do not move the list.
    s.page.evaluate("() => window.scrollTo(0, 0)")
    s.tap(".chip", has_text="接送")
    s.eq(s.scroll_y(), 0, "scroll after a filter tap")
    s.tap(".chip", has_text="接送")
    s.eq(s.scroll_y(), 0, "scroll after clearing the filter")
    s.open_order(s.t["order"]["done_pickup"])
    s.edit("價錢", "481")
    s.tap(".sheet.show .sheet-x")
    s.eq(s.scroll_y(), 0, "scroll after a saved edit")
    # A new row is brought into view.
    s.tap('[aria-label="入單"]')
    quick_order(s, "foodpanda", "2350", "40", False)
    before = s.rows()
    s.press(".drop.show #addSave")
    s.wait_toast("foodpanda 已入單")
    s.settle()
    new = [r for r in s.rows() if r not in before]
    s.eq((len(new), s.rows()[-1] == new[0] if new else None), (1, True), "the new row, last in the list")
    box = s.on(s.row(new[0])).first.bounding_box()
    s.expect(s.scroll_y() > 0 and box["y"] >= 0 and box["y"] + box["height"] <= s.page.viewport_size["height"],
             f"the new row was not scrolled into view: {box}")


@check("inventory.now-line-after-the-last-row", clock="installed")
def inventory_now_last(s: Session) -> None:
    """C12, A8: with every row's time passed the NOW line closes the list."""
    s.open_day()
    s.page.evaluate("() => window.scrollTo(0, 0)")
    s.page.clock.fast_forward(9 * 60 * 60 * 1000 + 30 * 60 * 1000)
    s.wait(lambda: s.text(".orders .now .now-t").startswith("23:3"), "the NOW line to follow the clock")
    last = s.page.evaluate("() => { const g = document.querySelector('.orders').lastElementChild;"
                           " return [g.className, !!g.querySelector('.now')]; }")
    s.eq(last, ["gap", True], "what closes the list")
    s.eq(s.count(".orders .now"), 1, "NOW lines")
    s.eq(s.count(".orders .row.next"), 0, "NEXT rows with every order done")
    s.eq(s.scroll_y(), 0, "scroll after the minute re-render")


@check("inventory.day-rows-and-sheet")
def inventory_day_rows(s: Session) -> None:
    """B2, B5, C9, D12, D13, D14, D15, D23, L8."""
    s.open_day()
    o = s.t["order"]
    # The filter outlives a change made elsewhere.
    s.tap(".chip", has_text="接送")
    s.api("PATCH", "/api/orders/" + o["dropoff"], {"price": 455})
    s.wait(lambda: s.text(s.row(o["dropoff"]) + " .price") == "$455", "the change made elsewhere")
    s.expect(s.text(".chip.on").startswith("接送"), "the filter after a live update")
    s.tap(".chip", has_text="接送")
    # Quick orders have their own fields; an unpriced order says so in amber.
    fields = "els => els.map(e => e.textContent)"
    s.open_order([r for r in s.rows() if r.startswith("didi_")][0])
    s.eq(s.page.eval_on_selector_all(".sheet.show .field-row .fk", fields), ["車費", "隧道費", "時間"], "fields of a 滴滴 order")
    s.tap(".sheet.show .sheet-x")
    s.open_order(o["unpriced"])
    s.eq(s.text(".sheet.show .field-row .fv.unset"), "未入價", "the price of an unpriced order")
    s.tap(".sheet.show .sheet-x")
    s.open_order([r for r in s.rows() if r.startswith("foodpanda_")][0])
    s.eq(s.page.eval_on_selector_all(".sheet.show .field-row .fk", fields), ["價錢", "時間"], "fields of a foodpanda order")
    # Behind an open sheet the page is covered, not locked: it scrolls, and a
    # tap where a row lies reaches the scrim and no row.
    s.page.evaluate("() => window.scrollTo(0, 30)")
    s.eq(s.scroll_y(), 30, "scroll behind an open sheet")
    x, y = s.page.evaluate("""() => {
      const top = [...document.querySelectorAll('.sheet.show')].find(e => e.getClientRects().length)
        .getBoundingClientRect().top;
      const head = [...document.querySelectorAll('.header')].find(e => e.getClientRects().length)
        .getBoundingClientRect().bottom;
      for (const row of document.querySelectorAll('.orders .row')) {
        const r = row.getBoundingClientRect();
        const y = r.top + r.height / 2;
        if (y > head + 4 && y < top - 4) return [r.left + r.width / 2, y];
      }
      return [0, 0];
    }""")
    s.expect(y > 0, "no row lies clear of both the header and the sheet")
    hit = s.page.evaluate("([x, y]) => document.elementFromPoint(x, y).className", [x, y])
    s.eq(hit, "scrim show", "what a tap on a row behind the sheet lands on")
    s.page.touchscreen.tap(x, y)
    s.wait(lambda: not s.sheet_open() and not s.count(".scrim.show"), "the scrim to close the sheet")
    # A zero chip is dimmed and still takes a tap.
    s.go_days(1)
    s.tap(".chip.zero", has_text="滴滴")
    s.eq((s.text(".chip.on"), s.text(".empty")), ("滴滴 0", "冇滴滴訂單"), "a tapped zero chip")
    # A reload forgets the filter.
    s.allow("request failed: GET /api/events")     # the reload cuts the event stream
    s.page.reload()
    s.on(".orders .row").first.wait_for()
    s.settle()
    s.eq(s.count(".chip.on"), 0, "the filter after a reload")
    # A fined, collected order: the fine on the row and on the sheet.
    s.go_days(-34)
    fined = seed_demo_db._oid(111)
    s.eq(s.text(s.row(fined) + " .price"), "$540−$63.45", "gross and fine on the row")
    s.eq(s.text(s.row(fined) + " .price .pen"), "−$63.45", "the fine's badge")
    s.open_order(fined)
    info = s.info()
    paid = s.back(29)
    s.eq((info["判罰"], info["結算"]), ("−$63.45（淨收 $476.55）", f"已收 {paid.month}/{paid.day}"), "fine and settlement rows")
    s.tap(".sheet.show .sheet-x")
    # A car park that is none of the three shows as a fourth, fixed pill.
    orders = s.orders(-34)
    [x for x in orders if x["order_id"] == fined][0]["pickup_point"] = "示範停車場"
    s.stub("GET", "**/api/orders?date=" + s.day(-34), 200,
           json.dumps({"orders": orders, "date": s.day(-34)}), "application/json")
    s.go_days(-1)
    s.go_days(1)
    s.open_order(fined)
    s.eq(s.page.eval_on_selector_all(
        ".sheet.show .pp-opt", "els => els.map(e => e.tagName + ' ' + e.textContent + (e.classList.contains('on') ? '*' : ''))"),
        ["BUTTON P1", "BUTTON P4", "BUTTON 富豪", "SPAN 示範停車場*"], "pickup points")
    s.eq(s.writes, [], "writes")


@check("inventory.writes-in-flight")
def inventory_in_flight(s: Session) -> None:
    """D20, D28, E9, F2, F7, F11, F13."""
    s.open_day()
    s.allow("http 400", "status of 400")
    refusal = dict(status=400, content_type="application/json", body=json.dumps({"error": "測試拒絕"}))
    oid = s.t["order"]["dropoff"]
    one = "/api/orders/" + oid
    s.hold(one, "/api/orders", "/api/orders/parse")

    def button(selector: str) -> tuple:
        el = s.on(selector).first
        return el.text_content().strip(), el.is_disabled()

    # A save from the numpad.
    s.open_order(oid)
    s.tap(".sheet.show .field-row", has_text="價錢")
    s.keys(".sheet.show", "450")
    s.press(".sheet.show #npOk")
    s.wait(lambda: s.holding(one), "the save")
    s.eq(button(".sheet.show #npOk")[1], True, "確認 disabled while its request is in flight")
    s.answer(one, **refusal)
    s.wait_toast("測試拒絕")
    s.tap(".sheet.show .sheet-x")
    # A cancel.
    s.tap(".sheet.show .cancel-link")
    s.press(".sheet.show .primary-btn.danger")
    s.wait(lambda: s.holding(one), "the cancel")
    s.eq(button(".sheet.show .primary-btn.danger"), ("取消緊…", True), "the cancel button in flight")
    s.answer(one, **refusal)
    s.wait(lambda: button(".sheet.show .primary-btn.danger") == ("確認取消", False), "the cancel button to come back")
    s.tap(".sheet.show .sheet-x")
    s.tap(".sheet.show .sheet-x")
    # A quick order's save.
    s.tap('[aria-label="入單"]')
    quick_order(s, "foodpanda", "1200", "40", False)
    s.press(".drop.show #addSave")
    s.wait(lambda: s.holding("/api/orders"), "the save")
    s.eq(button(".drop.show #addSave"), ("儲存緊…", True), "the save button in flight")
    s.answer("/api/orders", **refusal)
    s.wait(lambda: button(".drop.show #addSave") == ("儲存", False), "the save button to come back")
    height = s.page.viewport_size["height"]
    s.on(".scrim").first.tap(position={"x": 8, "y": height - 8})
    s.wait(lambda: not s.panel_open(), "the panel to close")
    # Parsing a pasted message.
    s.page.wait_for_timeout(2500)
    s.tap('[aria-label="入單"]')
    s.on(".drop.show .paste-box").first.fill(paste_message())
    s.press(".drop.show .primary-btn", has_text="解析")
    s.wait(lambda: s.holding("/api/orders/parse"), "the parse")
    s.eq(button(".drop.show #parseBtn"), ("解析緊…", True), "the parse button in flight")
    s.release("/api/orders/parse")
    s.stage("#000002 · 入價")
    # Saving it without a price, refused: the link comes back.
    s.press(".drop.show #npSkip")
    s.wait(lambda: s.holding("/api/orders"), "the save without a price")
    s.eq(button(".drop.show #npSkip")[1], True, "the skip link in flight")
    s.answer("/api/orders", **refusal)
    s.wait_toast("測試拒絕")
    s.wait(lambda: button(".drop.show #npSkip") == ("先唔入價，直接儲存", False), "the skip link to come back")
    # Saving it at a price of its own, refused: back to the suggestion.
    s.page.wait_for_timeout(2500)
    s.press('.drop.show .step[data-s="10"]')
    s.eq(s.text(".drop.show .numpad-display"), "$490", "after +$10")
    s.press(".drop.show #npOk")
    s.answer("/api/orders", **refusal)
    s.wait_toast("測試拒絕")
    s.wait(lambda: s.text(".drop.show .numpad-display") == "$480建議", "the amount to go back to the suggestion")
    # The steppers stop at nothing.
    for _ in range(50):
        s.press('.drop.show .step[data-s="-10"]')
    s.eq(s.text(".drop.show .numpad-display"), "$0", "the amount after more −$10 than it holds")
    s.release_all()
    s.settle()


@check("inventory.settle-details")
def inventory_settle(s: Session) -> None:
    """G11, H10, H28, I5, I16, I18, I23, J5."""
    s.open_settle()
    b, c = s.t["batch"], s.t["credit"]
    # The weekday heads stay with the header while the strip scrolls.
    s.page.evaluate("() => window.scrollBy(0, 300)")
    s.settle()
    s.eq(round(s.on(".header").first.bounding_box()["y"]), 0, "header top with the strip scrolled")
    wk, head = s.on(".header .wk").first.bounding_box(), s.on(".header").first.bounding_box()
    s.expect(wk["y"] >= 0 and wk["y"] + wk["height"] <= head["y"] + head["height"] + 1, "the weekday heads left the header")
    # A batch confirmed for less than the system expected says by how much.
    s.open_mark(s.bar("awaiting"))
    s.eq(s.sum_pairs(".sheet.show .sum-rows .sum-row"), [["應收", "$1290"], ["差額", "−$20"]], "expected and difference")
    s.close_sheets()
    # A tap on an empty day falls through to the calendar and clears a focus.
    bar = s.bar("short")
    s.reach(bar)
    s.tap(bar)
    s.expect(s.lit(), "nothing was lit by the first tap")
    x, y = s.page.evaluate("""() => {
      const head = [...document.querySelectorAll('.header')].find(e => e.getClientRects().length)
        .getBoundingClientRect().bottom;
      for (const cell of document.querySelectorAll('#grid .cell.none')) {
        const r = cell.getBoundingClientRect();
        if (r.top > head && r.bottom < window.innerHeight) return [r.left + r.width / 2, r.top + r.height / 2];
      }
      return [0, 0];
    }""")
    s.expect(y > 0, "no empty day on screen")
    s.page.touchscreen.tap(x, y)
    s.wait(lambda: s.lit() == [] and s.count("#grid .dim") == 0, "a tap on an empty day to clear the focus")
    s.expect(not s.sheet_open(), "an empty day opened a sheet")
    # The make-up payment arrives: the batch says which leg it was for.
    s.api("POST", f"/api/credits/{c['exact']}/allocate", {"settlement_id": b["short"]})
    s.wait(lambda: s.marks("bar")[b["short"]]["classes"] == {"bar", "paid"}, "the batch to be collected")
    s.settle()
    s.open_mark(bar)
    notes = s.texts(".sheet.show .sub-note")
    s.eq(notes, ["其餘 4 程", "補 …0204"], "which legs each transfer was for")
    s.tap(".sheet.show .fold")
    made_up = s.back(14)
    s.expect(any(f"補收 {md_slash(made_up)}" in t for t in s.texts(".sheet.show .orow")), "the held-back leg in the order list")
    s.close_sheets()
    s.open_cell(26)
    s.eq(s.texts(f'.sheet.show .orow[data-od="{seed_demo_db._oid(204)}"] .otag'), [f"補收 {md_slash(made_up)}"], "the leg on its day")
    s.close_sheets()
    # A focus is dropped when what it names is gone.
    group = s.bar("group")
    s.reach(group)
    s.tap(group)
    s.expect(f"bar {b['group']}" in s.lit(), "the focus on a batch")
    s.api("DELETE", f"/api/settlements/{b['group']}")
    s.wait(lambda: s.lit() == [] and s.count("#grid .dim") == 0, "the focus on a batch that is gone to be dropped")
    # A read that never reaches the server.
    s.allow("request failed: POST /api/statements/read", "Failed to load resource")
    s.page.route("**/api/statements/read", lambda route: route.abort())
    s.pick_statement()
    s.wait_toast("讀唔到張圖")
    s.eq(s.text('[aria-label="讀結算圖"]'), "圖", "the read button after a failed read")
    s.settle()


@check("inventory.hover-lights-and-a-click-opens", desktop=True)
def inventory_hover(s: Session) -> None:
    """H27, H31: a pointer that can hover makes the first ask by resting."""
    s.open_settle()
    bar = s.bar("short")
    for _ in range(8):
        if s.count(bar):
            break
        s.on('[aria-label="前一個月"]').first.click()
        s.settle()
    s.eq(round(s.page.evaluate("() => document.body.getBoundingClientRect().width")), 640, "the column on a wide screen")
    s.on(bar).first.hover()
    s.wait(lambda: f"bar {s.t['batch']['short']}" in s.lit(), "a resting pointer to light the relation")
    s.expect(not s.sheet_open(), "resting on a bar opened a sheet")
    s.on(".legend").first.hover()
    s.wait(lambda: s.lit() == [] and s.count("#grid .dim") == 0, "the focus to clear when the pointer moves off")
    s.on(bar).first.click()
    s.on(".sheet.show .hero").wait_for()
    s.eq(s.title(), "結算 " + span_label(s.back(27), s.back(25)), "the sheet one click opened")
    s.eq(s.writes, [], "writes")


@check("inventory.styles", still=False)
def inventory_styles(s: Session) -> None:
    """C17, D1, E2, F6, H29, K10, L5, L6, L7: what the stylesheets promise and
    no tap can show."""
    def promised(pressed_parts: tuple, safe_parts: tuple, calm_parts: tuple) -> None:
        rules = s.page.evaluate(RULES_JS)

        def need(parts: tuple, found: list, what: str) -> None:
            for part in parts:
                s.expect(any(part in sel for sel in found), f"{part}: {what}: {found}")

        need(pressed_parts, sorted({sel for sel, _, _ in rules if ":active" in sel}), "no pressed state")
        need(safe_parts, sorted({sel for sel, text, _ in rules if "safe-area-inset" in text}), "no safe-area inset")
        need(calm_parts, sorted({sel for sel, _, m in rules if "prefers-reduced-motion" in m}), "still moves under reduced motion")

    s.open_settle()
    promised((".bar:active", ".cell:not(.none):active", ".orow.tap:active", ".pbtn:active", ".nav-btn:active", ".field-row:active"),
             ("body", ".header", ".sheet", ".toast"), (".sheet", ".scrim", ".np-pad"))
    s.open_day()
    promised((".row:active", ".field-row:active", ".key:active", ".nav-btn:active"),
             ("body", ".header", ".sheet", ".toast", ".drop"),
             (".sheet", ".scrim", ".drop", ".paste-preview .sum-row", ".np-pad"))
    # With motion on, the sheet and panel slide; with it reduced they do not.
    def moving() -> list:
        return s.page.evaluate("() => ['.sheet', '.scrim', '.drop'].map(q => {"
                               " const e = [...document.querySelectorAll(q)].find(x => x.closest('#view-day') || !document.getElementById('view-day'));"
                               " return parseFloat(getComputedStyle(e || document.querySelector(q)).transitionDuration) > 0; })")
    s.eq(moving(), [True, True, True], "sheet, scrim and panel transitions")
    s.page.emulate_media(reduced_motion="reduce")
    s.eq(moving(), [False, False, False], "sheet, scrim and panel transitions under reduced motion")
    s.page.emulate_media(reduced_motion="no-preference")
    # The toast never takes a tap; the sheet stops at 88% of the screen.
    toast = s.page.eval_on_selector(".toast", "e => { const c = getComputedStyle(e); return [c.pointerEvents, c.position]; }")
    s.eq(toast, ["none", "fixed"], "the toast")
    s.open_order(s.t["order"]["landed_banner"])
    height = s.page.viewport_size["height"]
    s.eq(s.page.evaluate("() => { const e = [...document.querySelectorAll('.sheet.show')].find(x => x.getClientRects().length);"
                         " return [Math.round(parseFloat(getComputedStyle(e).maxHeight)), getComputedStyle(e).overflowY]; }"),
         [round(height * 0.88), "auto"], "the sheet's height limit")
    s.eq(s.count(".sheet.show .grab"), 1, "grab bars")
    s.tap(".sheet.show .sheet-x")
    # One text input in the whole app, at 16px.
    s.tap('[aria-label="入單"]')
    s.stage("入單")
    s.eq(s.page.evaluate("() => [...document.querySelectorAll('textarea, input:not([type=file])')].map(e => e.className)"),
         ["paste-box"], "text inputs")
    s.eq(s.page.eval_on_selector(".paste-box", "e => getComputedStyle(e).fontSize"), "16px", "the paste box's text size")
    s.on(".drop.show .paste-box").first.fill(paste_message())
    s.press(".drop.show .primary-btn", has_text="解析")
    s.on(".drop.show .paste-preview").wait_for()
    s.eq(s.page.eval_on_selector(".drop.show .paste-preview", "e => getComputedStyle(e).overflowY"), "auto", "the preview scrolls inside itself")
    s.eq(s.page.eval_on_selector(".drop.show", "e => getComputedStyle(e).overflowY"), "hidden", "the panel does not scroll")
    s.settle()

# ---- running ----

def run(playwright, browser, chk: dict, today: date):
    """Run one check on a server of its own. Returns the problem, or None."""
    with contextlib.ExitStack() as stack:
        root = ROOT
        if chk["copy"]:
            root = copy_app(stack.enter_context(tempfile.TemporaryDirectory(prefix="ride-app-")))
        server = Server(today, root)
        url = stack.enter_context(server)
        if chk["clock"] == "installed":
            ctx = browser.new_context(**playwright.devices[DEVICE], color_scheme="dark",
                                      timezone_id=TIMEZONE, locale="zh-HK",
                                      service_workers="block")
            ctx.clock.install(time=demo_now(today))
        else:
            ctx = new_context(playwright, browser, "dark", today, still=chk["still"],
                              workers=chk["workers"], desktop=chk["desktop"])
        s = Session(ctx, url, today, server)
        problem = None
        try:
            chk["fn"](s)
            bad = s.unexpected()
            if bad:
                problem = "; ".join(bad[:4])
        except Exception as e:      # a check that cannot finish has failed
            problem = f"{type(e).__name__}: {e}".splitlines()[0]
            bad = s.unexpected()
            if bad:
                problem += " | also: " + "; ".join(bad[:3])
        finally:
            # A request a failed check left waiting must not outlive its page.
            if s.page:
                s.page.unroute_all(behavior="ignoreErrors")
            ctx.close()
        return problem


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="", metavar="PREFIX", help="checks whose name begins with this")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today(),
                    help="the day the data and both clocks are built around (default: today)")
    ap.add_argument("--list", action="store_true", help="name the checks and stop")
    args = ap.parse_args()

    wanted = [c for c in CHECKS if c["name"].startswith(args.only)]
    if args.list:
        for c in wanted:
            print(c["name"])
        return
    if not wanted:
        raise SystemExit(f"no check begins with {args.only!r}")

    from playwright.sync_api import sync_playwright
    failed = total = 0

    def report(ok: bool, label: str, problem: str = "") -> None:
        nonlocal failed, total
        total += 1
        failed += not ok
        print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else "\n      " + problem), flush=True)

    with sync_playwright() as p:
        browser = p.webkit.launch()
        for chk in wanted:
            problem = run(p, browser, chk, args.today)
            report(problem is None, chk["name"], problem or "")
        browser.close()
    print(f"{total} checks, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
