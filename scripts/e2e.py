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
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import seed_demo_db  # noqa: E402
from harness import (DEVICE, LOGIN_PATH, LONG_AMOUNT, ROOT, TIMEOUT_MS, TIMEZONE, Driver,  # noqa: E402
                     Server, copy_app, demo_now, new_context, paste_message, stress_strip)

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
    """What the settle view's month button says: year and month as one
    figure, and that it is the current month when it is."""
    return f"{d.year}·{d.month:02d}" + ("今個月" if now else "")


def date_head(day: str, today: str = "") -> str:
    """What the day view's date button says for a day: month and day as one
    figure, then the weekday; the year only when it is not `today`'s."""
    d = date.fromisoformat(day)
    return (f"{d.month:02d}·{d.day:02d}星期{WEEKDAY[d.weekday()]}"
            + (" · 今日" if day == today else "")
            + (str(d.year) if today and d.year != date.fromisoformat(today).year else ""))


def settle_path(d: date, platform: str = "ride") -> str:
    return f"/api/settle?month={month_key(d)}&platform={platform}"


CREDITS = "/api/credits?platform=ride"

# A day each seeded batch covers, in days before today: where its sheet is
# reached from.
BATCH_DAY = {"paid": 34, "short": 26, "awaiting": 19, "held_back": 12, "ahead": 6, "group": 8}


def day_runs(days: list, month: str) -> str:
    """How the settle view words the days a statement covers: runs of
    consecutive days, bare day numbers while every day is in the month shown
    and each run carrying its month once any is not."""
    days = sorted(set(days))
    runs = []
    for d in days:
        if runs and d - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(d)
        else:
            runs.append([d])
    inside = all(month_key(d) == month for d in days)

    def word(run: list) -> str:
        a, z = run[0], run[-1]
        if inside:
            return (str(a.day) if a == z else f"{a.day}–{z.day}") + "日"
        if a == z:
            return md_slash(a)
        return md_slash(a) + "–" + (str(z.day) if month_key(a) == month_key(z) else md_slash(z))
    return "、".join(word(r) for r in runs)

# The month's total keys, in the order they stand: the payload's name for
# each figure and the key's label.
TOTAL_KEYS = (("fare", "本月車費"), ("received", "已收"), ("awaiting", "等過數"), ("unsettled", "未結算"))


def key_texts(totals) -> list:
    """What the four total keys say for one month's `month_totals`: label and
    figure run together, the figure with $, thousands comma and cents. A
    month with no totals to state shows a dash in each."""
    return [label + ("—" if totals is None else f"${totals[name]:,.2f}") for name, label in TOTAL_KEYS]


# How each total key is drawn, in order: what the checks compare against the
# palette and against each other.
KEYS_JS = """
() => [...document.querySelectorAll('#settle-lens .lkey')].map(key => {
  const cs = e => getComputedStyle(e);
  const v = key.querySelector('.v'), ct = key.querySelector('.ct'), mk = key.querySelector('.mk');
  const box = key.getBoundingClientRect(), fig = v.getBoundingClientRect();
  return {
    lens: key.dataset.lens, tag: key.tagName, pressed: key.getAttribute('aria-pressed'),
    height: box.height, ground: cs(key).backgroundColor,
    ink: cs(v).color, face: cs(v).fontFamily, size: parseFloat(cs(v).fontSize),
    cents: ct ? parseFloat(cs(ct).fontSize) / parseFloat(cs(v).fontSize) : null,
    rule: [mk.getBoundingClientRect().height, cs(mk).backgroundColor],
    lined: [key, ...key.querySelectorAll('*')].some(e => cs(e).textDecorationLine !== 'none'),
    figTop: Math.round(fig.top), figHeight: fig.height,
    inside: fig.left >= box.left - 0.5 && fig.right <= box.right - parseFloat(cs(key).paddingRight) + 0.5,
  };
})
"""

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

# What is wrong with the strip's cells, if anything: a week that is not seven
# days, anything drawn in a week besides its days, a day with no size, a day
# holding work that does not show exactly one money figure and one state mark,
# a figure wider than its column or written with a currency sign or a
# thousands mark, and anything in the calendar that is underlined.
STRIP_JS = """
() => {
  const bad = [];
  const cal = [...document.querySelectorAll('.cal')].find(e => e.getClientRects().length);
  if (!cal) return ['no calendar on screen'];
  for (const row of cal.querySelectorAll('#grid .wkblock')) {
    if (row.querySelectorAll('.cell').length !== 7) bad.push('not seven days: ' + row.id);
    if (row.children.length !== 1 || row.querySelector('.lane, [data-bar], [data-chip]')) bad.push('more than days in ' + row.id);
  }
  for (const cell of cal.querySelectorAll('#grid .cell')) {
    const box = cell.getBoundingClientRect(), name = cell.dataset.d || 'an empty day';
    if (box.width < 8 || box.height < 8) bad.push('no size: ' + name);
    const figures = cell.querySelectorAll('.amt');
    if (figures.length !== (cell.dataset.d ? 1 : 0)) bad.push(figures.length + ' money figures: ' + name);
    if (cell.querySelectorAll('.mk').length !== (cell.dataset.d ? 1 : 0)) bad.push('state marks: ' + name);
    const rest = [...cell.children].filter(e => !e.matches('.d, .amt')).map(e => e.textContent).join('').trim();
    if (rest) bad.push('text beside the date and the figure: ' + name + ' ' + rest);
    for (const f of figures) {
      const r = f.getBoundingClientRect();
      if (r.left < box.left - 0.5 || r.right > box.right + 0.5) bad.push('figure wider than its column: ' + name + ' ' + f.textContent);
      if (!/^−?\\d+(\\.\\d\\d)?$/.test(f.textContent)) bad.push('not a bare figure: ' + name + ' ' + f.textContent);
    }
  }
  for (const e of cal.querySelectorAll('*')) {
    if (getComputedStyle(e).textDecorationLine !== 'none') bad.push('underlined: ' + (e.className || e.tagName));
  }
  return bad;
}
"""

# How one day's cell is drawn: the ink, weight and size of its figure, the
# cents against the dollars, its date, and the rule under the figure with the
# length of a short-paid rule's first part.
CELL_JS = """
e => {
  const cs = x => getComputedStyle(x);
  const a = e.querySelector('.amt'), m = e.querySelector('.mk'), ct = e.querySelector('.ct'), d = e.querySelector('.d');
  const r = m.getBoundingClientRect();
  return {
    ink: cs(a).color, weight: cs(a).fontWeight, size: parseFloat(cs(a).fontSize),
    cents: ct ? parseFloat(cs(ct).fontSize) / parseFloat(cs(a).fontSize) : null,
    date: [cs(d).color, cs(d).fontSize],
    rule: [r.width, r.height, cs(m).backgroundColor, cs(m).backgroundImage],
    got: parseFloat(m.style.getPropertyValue('--got')),
    ground: cs(e).backgroundColor, faded: [e, a, d, m].some(x => cs(x).opacity !== '1'),
  };
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

    def foot(self) -> list:
        """The day view's foot, cell by cell: label and figure run together."""
        return self.texts(".foot .foot-in > div")

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

    def totals(self) -> list:
        """The settle view's foot, cell by cell: label and figure run together."""
        return self.texts(".foot .foot-in > *")

    def keys_text(self) -> list:
        """The month's total keys, key by key: label and figure run together."""
        return self.texts("#settle-lens .lkey")

    def pressed(self) -> list:
        """The lens of every total key that says it is the chosen one."""
        return self.page.eval_on_selector_all(
            '#settle-lens .lkey[aria-pressed="true"]', "els => els.map(e => e.dataset.lens)")

    def header_height(self) -> float:
        return self.page.evaluate(
            "() => [...document.querySelectorAll('.header')].find(e => e.getClientRects().length)"
            ".getBoundingClientRect().height")

    def top_week(self) -> str:
        return self.page.evaluate(TOP_WEEK_JS)[0]

    def weeks(self) -> list:
        return self.page.eval_on_selector_all("#grid .wkblock", "els => els.map(e => e.id)")

    def strip_problems(self) -> list:
        return self.page.evaluate(STRIP_JS)

    def resize(self, width: int) -> None:
        self.page.set_viewport_size({"width": width, "height": 800})
        self.page.wait_for_timeout(400)
        self.settle()

    def colour(self, selector: str, prop: str = "color") -> str:
        return self.on(selector).first.evaluate(f"e => getComputedStyle(e)['{prop}']")

    def token(self, name: str) -> str:
        """A palette token as the browser reports a colour."""
        return self.page.evaluate(
            "n => { const e = document.createElement('i'); e.style.color = 'var(' + n + ')';"
            " document.body.appendChild(e); const c = getComputedStyle(e).color; e.remove(); return c; }", name)

    def cell(self, back: int) -> str:
        return f'.cell[data-d="{self.back(back).isoformat()}"]'

    def cell_state(self, back: int) -> tuple:
        """A day's cell: the state it is drawn in, and its figure."""
        cls, text = self.page.eval_on_selector(
            self.cell(back), "e => [e.className, e.querySelector('.amt').textContent]")
        states = [c[3:] for c in cls.split() if c.startswith("st-")]
        return (states[0] if len(states) == 1 else states, text)

    def look(self, back: int) -> dict:
        return self.on(self.cell(back)).first.evaluate(CELL_JS)

    def lit(self) -> list:
        return sorted(self.page.eval_on_selector_all("#grid .cell.lit", "els => els.map(e => e.dataset.d)"))

    def view_month(self) -> str:
        """The month the header names, as 'YYYY-MM'."""
        text = self.month_text()
        return text[:4] + "-" + text[5:7]

    def to_month(self, d: date) -> None:
        """Page the header to the month `d` is in, by its arrows."""
        want = month_key(d)
        for _ in range(12):
            at = self.view_month()
            if at == want:
                return
            self.tap('[aria-label="前一個月"]' if at > want else '[aria-label="後一個月"]')
        raise Failed(f"the header never named {want}")

    def list_rows(self) -> list:
        """The statement list, row by row, as it is drawn."""
        return self.page.evaluate(LIST_JS)

    def title(self) -> str:
        return self.text(".sheet.show .sheet-title")

    def sub(self) -> str:
        return self.text(".sheet.show .sheet-sub")

    def open_batch(self, key: str, ready: str = ".sheet.show .hero") -> None:
        """A batch's sheet, from a day it covers: the day's sheet, then its
        link to the batch."""
        self.open_cell(BATCH_DAY[key])
        self.tap(f'.sheet.show .blink[data-bl="{self.t["batch"][key]}"]')
        self.on(ready).first.wait_for()

    def focus_on(self, key: str, ready: str = ".sheet.show .hero") -> None:
        """Light a batch's days from its sheet."""
        self.open_batch(key, ready)
        self.tap(".sheet.show [data-focus]")
        self.wait(lambda: not self.sheet_open(), "the sheet to close on the way to the calendar")

    def open_credit(self, key: str) -> None:
        """An unmatched credit's sheet, through the queue in the foot."""
        self.tap(".foot [data-credits]")
        self.tap(f'.sheet.show .qrow[data-credit="{self.t["credit"][key]}"]')
        self.on(".sheet.show .hero").first.wait_for()

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
        with open(os.path.join(self.server.app_root, "static", "js", "dates.js"), "a") as f:
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
    s.eq(s.date_text(), date_head(s.day(), s.day()), "date button")
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
    s.expect(s.date_text().startswith(date_head(s.day(1))) and "今日" not in s.date_text(), "date button on tomorrow")
    s.eq(s.rows(), s.ids(1), "tomorrow's rows")
    s.go_days(-1)
    s.eq(s.rows(), s.ids(), "today's rows after going back")
    s.expect("今日" in s.date_text(), "date button back on today")
    s.go_days(-1)
    s.eq(s.rows(), s.ids(-1), "yesterday's rows")
    s.tap(".date-btn")
    s.eq(s.rows(), s.ids(), "today's rows after tapping the date")
    s.expect(s.date_text().startswith(date_head(s.day())), "date button after tapping the date")
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
    s.expect(s.date_text().startswith(date_head(s.day(1))), "the date button did not change at once")
    shown = s.rows()
    s.press('[aria-label="後一日"]')
    s.wait(lambda: s.date_text().startswith(date_head(s.day(2))), "the date button to change")
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
    s.expect(s.date_text().startswith(date_head(s.day())), "date button")
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
    s.expect(s.date_text().startswith(date_head(s.day(2))), "date button")
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
    s.wait(lambda: s.date_text().startswith(date_head(s.day(1))), "the date to change")
    s.never(s.toast, "a toast for an expired login (held day)")
    s.press('[aria-label="後一日"]')
    s.wait(lambda: s.date_text().startswith(date_head(s.day(2))), "the date to change")
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
    s.expect(s.date_text().startswith(date_head(s.day(1))), "the hold's release went to today")
    s.page.wait_for_timeout(2600)
    s.press('[aria-label="後一日"]')
    s.wait(lambda: s.date_text().startswith(date_head(s.day(2))), "the date to change")
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


@check("day.timing-readout-after-a-failed-load")
def day_timing_failed(s: Session) -> None:
    """A date tap whose load fails must not be what the next paint is timed
    from: that paint was asked for by nothing the operator did."""
    ms = re.compile(r"\d+ ms")
    s.allow("http 500", "status of 500")
    far = "/api/orders?date=" + s.day(2)
    s.open("/?perf=1", ".orders .row")
    s.wait_toast(ms)
    s.stub("GET", "**" + far, 500, "<html>boom</html>", "text/html")
    s.go_days(1)
    s.page.wait_for_timeout(2600)
    # A day nothing is held for, and its load fails.
    s.press('[aria-label="後一日"]')
    s.wait_toast("載入失敗")
    s.page.wait_for_timeout(2600)
    # The day is painted later, by a change made elsewhere.
    s.page.unroute("**" + far)
    answered = len(s.answers)
    s.api("PATCH", "/api/orders/" + s.t["order"]["dropoff"], {"price": 455})
    s.wait(lambda: (far, 200, False) in s.answers[answered:], "the day to be loaded by the change")
    s.wait(lambda: s.count(".orders .empty") or s.rows() == s.ids(2), "the day to be painted")
    s.never(lambda: ms.fullmatch(s.toast()), "a readout timed from the tap whose load failed", ms=600)
    s.settle()


# ---- day view: tabs, foot, rows and marks (inventory A11-A12, B, C, K9) ----

@check("day.filter-tabs")
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
        cells = [f"程數{len(shown)}"]
        if len(shown) > len(priced):
            cells.append(f"未入價{len(shown) - len(priced)}")
        return cells + [f"當日車費${money(total)}"]

    n = {k: len([o for o in orders if plat(o) == k]) for k in ("ride", "didi", "uber", "foodpanda")}
    every = f"全部{len(orders)}"
    tabs = s.page.eval_on_selector_all(".tabs .tab", "els => els.map(e => e.textContent)")
    s.eq(tabs, [every, f"接送{n['ride']}", f"滴滴{n['didi']}", f"Uber{n['uber']}", f"熊貓{n['foodpanda']}"], "tabs")
    s.eq(s.texts(".tab.on"), [every], "highlighted tab with no filter")
    s.eq(s.foot(), summary(orders), "foot, no filter")
    s.expect(s.count(".foot .warn"), "the unpriced count is not marked")

    s.tap(".tab", has_text="滴滴")
    didi = [o for o in orders if plat(o) == "didi"]
    s.eq(s.rows(), [o["order_id"] for o in didi], "rows under the 滴滴 filter")
    s.eq(s.foot(), summary(didi), "foot under the filter")
    s.eq(s.count(".foot .warn"), 0, "an unpriced count with every row showing priced")
    s.eq(s.texts(".tab.on"), [f"滴滴{n['didi']}"], "highlighted tab")
    s.tap(".tab", has_text="接送")
    ride = [o for o in orders if plat(o) == "ride"]
    s.eq(s.rows(), [o["order_id"] for o in ride], "rows under the 接送 filter")
    s.eq(s.foot(), summary(ride), "foot under the filter")
    # NEXT and NOW are worked out over the rows showing.
    s.eq(s.count(".orders .row.next"), 1, "NEXT rows under a filter")
    s.eq(s.count(".orders .now"), 1, "NOW lines under a filter")
    s.tap(".tab", has_text="接送")
    s.eq(s.texts(".tab.on"), [every], "highlighted tabs after tapping the filter again")
    s.eq(s.rows(), [o["order_id"] for o in orders], "rows with the filter cleared")
    s.eq(s.foot(), summary(orders), "foot with the filter cleared")
    # 全部 clears whatever filter is on.
    s.tap(".tab", has_text="Uber")
    s.eq(len(s.rows()), n["uber"], "rows under the Uber filter")
    s.tap(".tab", has_text="全部")
    s.eq((s.texts(".tab.on"), s.rows()), ([every], [o["order_id"] for o in orders]), "after tapping 全部")

    # The filter survives a change of date; a platform with nothing is dimmed.
    s.tap(".tab", has_text="滴滴")
    s.go_days(1)
    s.eq(s.text(".tab.on"), "滴滴0", "filter after changing date")
    s.expect("zero" in s.on(".tab.on").first.get_attribute("class"), "an empty platform's tab is not dimmed")
    s.eq(s.text(".empty"), "冇滴滴訂單", "empty text under a filter")
    s.eq(s.foot(), ["程數0", "當日車費$0"], "foot of nothing")
    # The foot is the bottom of the screen, and the last row scrolls clear of it.
    s.tap(".tab", has_text="全部")
    s.go_days(-1)
    s.page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
    s.settle()
    foot = s.on(".foot").first.bounding_box()
    last = s.on(s.row(s.rows()[-1])).first.bounding_box()
    s.eq(round(foot["y"] + foot["height"]), s.page.viewport_size["height"], "the foot's bottom edge")
    s.expect(last["y"] + last["height"] <= foot["y"], f"the last row is under the foot: {last} / {foot}")


@check("day.rows-and-marks")
def day_rows(s: Session) -> None:
    s.open_day()
    o = s.t["order"]

    def cls(oid):
        return s.on(s.row(oid)).first.get_attribute("class").split()

    def rail(oid):
        """The row's time, then its second line up to the marks."""
        return s.page.eval_on_selector_all(
            s.row(oid) + " .time, " + s.row(oid) + " .meta > .st, " + s.row(oid) + " .meta > .num",
            "els => els.map(e => e.textContent)")

    # NEXT is the first row not yet done; rows before it that are done are dimmed.
    s.eq([r for r in s.rows() if "next" in cls(r)], [o["landed_banner"]], "NEXT row")
    s.expect("done" in cls(o["done_pickup"]) and "done" in cls(o["dropoff"]), "finished rows are not dimmed")
    s.expect("done" not in cls(o["upcoming_hotel"]), "a row still to come is dimmed")
    # The NOW line sits before the first row later than the clock.
    s.eq(s.text(".orders .now .now-t"), "14:00", "NOW time")
    after_now = s.page.evaluate(
        "() => document.querySelector('.orders .now').nextElementSibling.dataset.oid")
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
    s.eq(s.page.eval_on_selector_all(".orders .row .st", "els => els.map(e => e.className)"),
         ["st gate", "st landed", "st est"], "status blocks, by state")
    s.eq(s.text(s.row(o["landed_banner"]) + " .code"), "UO623", "flight number")
    s.eq(s.text(s.row(o["landed_banner"]) + " .place"), "灣仔例子酒店", "shortened destination")
    # A 送机 says so where a flight number would stand, and its place is
    # where the passenger is collected.
    s.eq((s.text(s.row(o["dropoff"]) + " .code"), s.text(s.row(o["dropoff"]) + " .place")),
         ("送機", "旺角樣本賓館"), "code and place of a 送机")
    s.eq((s.text(s.row(o["unpriced"]) + " .code"), s.text(s.row(o["unpriced"]) + " .place")),
         ("單程", "尖沙咀示範酒店 →旺角樣本賓館"), "code and place of a 單程")
    s.eq(s.page.eval_on_selector_all(s.row(o["landed_banner"]) + " .mk", "els => els.map(e => e.className + '|' + e.textContent)"),
         ["mk sign|舉牌", "mk neutral|出場 45"], "marks")
    s.eq(s.text(s.row(o["upcoming_hotel"]) + " .mk"), "出場 20", "exit mark")
    s.expect("urgent" in s.on(s.row(o["upcoming_hotel"]) + " .mk").first.get_attribute("class"), "a 20 minute exit is not urgent")
    s.eq(s.text(s.row(o["landed_banner"]) + " .price"), "$560", "gross price with the banner fee")
    s.eq(s.text(s.row(o["unpriced"]) + " .price.unset"), "未入價", "unpriced row")
    quick = [r for r in s.rows() if "quick" in cls(r)]
    s.eq(len(quick), 3, "quick rows")
    s.eq((s.text(s.row(quick[0]) + " .code"), s.text(s.row(quick[0]) + " .place")), ("DIDI", "滴滴"),
         "platform on a quick row")
    s.eq(s.text(s.row(quick[2]) + " .price"), "$55.50", "cents on a quick row")
    s.eq(s.count(s.row(quick[0]) + " .meta"), 0, "a second line on a quick row")
    # The wait since the row before, under the time of the row it leads to;
    # under half an hour it says nothing.
    gaps = dict(s.page.eval_on_selector_all(
        ".orders .row", "els => els.map(e => [e.dataset.oid, (e.querySelector('.gap') || {}).textContent || ''])"))
    listed = s.rows()
    s.eq([gaps[r] for r in listed[:3]], ["", "+1h48", "+1h10"], "gap figures")
    times = [x["row_time"][11:16] for x in s.orders()]
    for prev, this, oid in zip(times, times[1:], listed[1:]):
        mins = (int(this[:2]) - int(prev[:2])) * 60 + int(this[3:]) - int(prev[3:])
        if mins < 30:
            want = ""
        elif mins < 60:
            want = f"+{mins}m"
        else:
            want = f"+{mins // 60}h" + (f"{mins % 60:02d}" if mins % 60 else "")
        s.eq(gaps[oid], want, f"gap figure before {this}")
    s.eq(s.page.evaluate("() => { const g = document.querySelector('.orders .row .gap'), t = g.parentElement.querySelector('.time');"
                         " const a = g.getBoundingClientRect(), b = t.getBoundingClientRect();"
                         " return [Math.round(a.left) === Math.round(b.left), a.top >= b.bottom - 1]; }"),
         [True, True], "the gap figure sits under its row's time")
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


def repaint(s: Session, n: int) -> None:
    """Make the day view ask again and paint, as a change on the server does:
    a price nothing else looks at is changed, and the row shows it."""
    oid = s.t["order"]["dropoff"]
    s.api("PATCH", "/api/orders/" + oid, {"price": 400 + n})
    s.wait(lambda: s.text(s.row(oid) + " .price") == f"${400 + n}", "the repaint")
    s.settle()


def serve_orders(s: Session, change) -> None:
    """From now on today's orders reach the page as `change` leaves them."""
    def handler(route, request):
        orders = s.orders()
        change({o["order_id"]: o for o in orders})
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"orders": orders, "date": s.day()}))
    s.page.unroute("**/api/orders?date=" + s.day())
    s.page.route("**/api/orders?date=" + s.day(), handler)


@check("day.row-cells-of-less-common-orders")
def day_row_cells(s: Session) -> None:
    """What the seeded day has none of: each must still be on the row."""
    s.open_day()
    o = s.t["order"]
    long_name = "將軍澳示範國際會議展覽中心酒店式服務住宅南翼"

    def change(by):
        a = by[o["upcoming_hotel"]]         # a 接机 with no flight number, 舉牌, a tight exit
        a.update(flight_number="", pickup="深圳灣示範口岸(示範)", banner_fee=40,
                 passenger_exit_minutes=30, exit_urgency="tight")
        b = by[o["dropoff"]]                # a 送机 to a named terminal, fined, with cents
        b.update(dropoff="香港國際機場T2(示範)", price=1520.5, penalty_fee=97.38, pickup=long_name + "(示範道1號)")
        c = by[o["unpriced"]]               # a 送机 that does not end at the airport
        c.update(service_type="送机", dropoff="示範口岸(示範)")
        d = by[o["done_pickup"]]            # another service
        d.update(service_type="接站", pickup="香港西九龍站(示範)", flight_number="")

    serve_orders(s, change)
    s.api("PATCH", "/api/orders/" + o["landed_banner"], {"tunnel_fee": 1})    # any change: the view asks again
    s.wait(lambda: s.count(".orders .code.org"), "the repaint")
    s.settle()

    def cell(oid, part):
        return s.text(s.row(oid) + " " + part)

    s.eq((cell(o["upcoming_hotel"], ".code.org"), cell(o["upcoming_hotel"], ".place")),
         ("深圳灣示範口岸", "沙田範例廣場"), "a 接机 with no flight number: its origin, small, and the drop-off")
    s.eq(s.page.eval_on_selector_all(s.row(o["upcoming_hotel"]) + " .mk", "els => els.map(e => e.className + '|' + e.textContent)"),
         ["mk sign|舉牌", "mk tight|出場 30"], "marks of a tight exit with a 舉牌")
    s.eq(cell(o["upcoming_hotel"], ".price"), "$490", "gross with the 舉牌")
    s.eq((cell(o["dropoff"], ".code"), cell(o["dropoff"], ".place")), ("送機 T2", long_name), "a 送机 to a terminal")
    s.eq((cell(o["dropoff"], ".price"), cell(o["dropoff"], ".price .pen")), ("$1520.50−$97.38", "−$97.38"),
         "a fare with cents and the fine under it")
    s.eq((cell(o["unpriced"], ".code"), cell(o["unpriced"], ".place"), cell(o["unpriced"], ".place .end.sub")),
         ("送機", "尖沙咀示範酒店 →示範口岸", "→示範口岸"), "a 送机 that ends somewhere else")
    s.eq((cell(o["done_pickup"], ".code"), cell(o["done_pickup"], ".place")),
         ("接站", "香港西九龍站 →尖沙咀示範酒店"), "another service: its name and both ends")
    s.eq(s.count(s.row(o["done_pickup"]) + " .st"), 0, "a status block on a row that is not a 接机")
    for width in LAYOUT_WIDTHS:
        s.page.set_viewport_size({"width": width, "height": 800})
        s.settle()
        s.eq(s.page.evaluate(ROWS_JS), [], f"layout at {width} wide")


# The widths the day view is laid out for: the narrowest, the two phone widths
# its columns change between, and the widest.
LAYOUT_WIDTHS = (320, 340, 390, 480)

# What is wrong with the rows as drawn, if anything. A row is a box that holds
# all of its cells and touches no other row; a cell is on the screen and its
# text is not cut; the first three columns start on the same x in every row
# and under the column head; the lines of a place stay clear of the code
# before them and of the fare.
ROWS_JS = """
() => {
  const out = [];
  if (document.documentElement.scrollWidth > innerWidth) out.push('page wider than the screen');
  const rows = [...document.querySelectorAll('.orders .row')];
  const name = row => row.querySelector('.time').textContent + ' ' + row.dataset.oid;
  const hit = (a, b) => a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
  const boxes = rows.map(row => row.getBoundingClientRect());
  for (let i = 0; i < rows.length; i++) for (let j = i + 1; j < rows.length; j++) {
    if (hit(boxes[i], boxes[j])) out.push('rows overlap: ' + name(rows[i]) + ' / ' + name(rows[j]));
  }
  rows.forEach((row, i) => {
    const r = boxes[i];
    for (const el of row.querySelectorAll('.time, .code, .place, .place .end, .price, .price .pen, .meta, .meta > *, .gap')) {
      const b = el.getBoundingClientRect();
      if (b.right > innerWidth + 0.5 || b.left < -0.5) out.push('off screen: ' + el.textContent);
      if (b.top < r.top - 0.5 || b.bottom > r.bottom + 0.5 || b.left < r.left - 0.5 || b.right > r.right + 0.5) {
        out.push('outside its row (' + name(row) + '): ' + el.className + ' ' + el.textContent);
      }
      if (el.scrollWidth > el.clientWidth + 1 && el.clientWidth) out.push('cut: ' + el.textContent);
    }
    const [time, code, place, price] = ['.time', '.code', '.place', '.price'].map(q => row.querySelector(q));
    const range = document.createRange();
    range.selectNodeContents(place);
    const lines = [...range.getClientRects()];
    const others = [time, code, price, ...row.querySelectorAll('.gap, .meta')].map(e => [e.className, e.getBoundingClientRect()]);
    // A line's box is as tall as the face's ascent and descent, which is more
    // than the line it sits on: its middle is what must stay clear.
    for (const line of lines) for (const [cls, box] of others) {
      const mid = { left: line.left, right: line.right, top: line.top + line.height / 4, bottom: line.bottom - line.height / 4 };
      if (hit(mid, box)) out.push('the place runs into ' + cls + ' in ' + name(row));
    }
    if (Math.abs(time.getBoundingClientRect().top - r.top - parseFloat(getComputedStyle(row).paddingTop)) > 0.5) {
      out.push('empty space above the first line of ' + name(row));
    }
    const t = time.getBoundingClientRect(), c = code.getBoundingClientRect(), f = price.getBoundingClientRect();
    if (c.left < t.right - 0.5) out.push('time and code overlap in ' + name(row));
    if (f.left < c.right - 0.5) out.push('code and fare overlap in ' + name(row));
    if (Math.abs(f.right - (r.right - parseFloat(getComputedStyle(row).paddingRight))) > 0.5) out.push('fare off the right edge in ' + name(row));
  });
  const lefts = q => new Set(rows.map(row => Math.round(row.querySelector(q).getBoundingClientRect().left)));
  for (const q of ['.time', '.code', '.place']) if (lefts(q).size !== 1) out.push('ragged column ' + q);
  const head = [...document.querySelectorAll('.cols span')].map(e => Math.round(e.getBoundingClientRect().left));
  const first = ['.time', '.code', '.place'].map(q => Math.round(rows[0].querySelector(q).getBoundingClientRect().left));
  if (String(head.slice(0, 3)) !== String(first)) out.push('column head ' + head + ' against rows ' + first);
  return out;
}
"""

LONG_PLACE = "將軍澳示範國際會議展覽中心酒店式服務住宅南翼"
LONG_PLACE_2 = "港珠澳大橋香港口岸旅檢大樓示範出口"


@check("day.rows-hold-long-places")
def day_long_places(s: Session) -> None:
    """A place that wraps to several lines, in every kind of row that shows
    one, with and without the wait before it and with and without a 判罰 under
    the fare: each row grows to hold it and none is drawn over another."""
    s.open_day()
    o = s.t["order"]
    base = {x["order_id"]: x for x in s.orders()}
    kinds = {
        "送机": dict(base[o["dropoff"]], pickup=LONG_PLACE + "(示範道1號)"),
        "单程接送": dict(base[o["unpriced"]], pickup=LONG_PLACE + "(示範道1號)", dropoff=LONG_PLACE_2 + "(示範)", price=400),
        "接机": dict(base[o["done_pickup"]], dropoff=LONG_PLACE + "(示範道1號)"),
    }
    made, minute = [], 15 * 60
    for kind, template in kinds.items():
        for gap in (False, True):
            for fined in (False, True):
                minute += 45 if gap else 10
                hhmm = f"{minute // 60:02d}:{minute % 60:02d}"
                row = dict(template, order_id=f"99{len(made):014d}", penalty_fee=97.38 if fined else None,
                           scheduled_time=f"{s.day()} {hhmm}:00", row_time=f"{s.day()} {hhmm}:00")
                if kind == "接机":
                    row.update(flight_status="est", flight_eta=hhmm, flight_gate=None, flight_scheduled=hhmm)
                made.append((row, gap, fined))
    # A five-figure fare with cents, where the fare column is at its widest.
    made[-1][0].update(price=12480.5)

    def handler(route, request):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"orders": [m[0] for m in made], "date": s.day()}))
    s.page.route("**/api/orders?date=" + s.day(), handler)
    s.api("PATCH", "/api/orders/" + o["landed_banner"], {"tunnel_fee": 1})    # any change: the view asks again
    s.wait(lambda: s.rows() == [m[0]["order_id"] for m in made], "the repaint")
    s.settle()
    for row, gap, fined in made:
        q = s.row(row["order_id"])
        s.eq((s.count(q + " .gap"), s.count(q + " .price .pen")), (int(gap), int(fined)),
             f"wait and fine on {row['service_type']} {row['scheduled_time'][11:16]}")
        s.expect(LONG_PLACE in s.text(q + " .place"), f"the whole name on {row['service_type']}: {s.text(q + ' .place')}")
    for width in LAYOUT_WIDTHS:
        s.page.set_viewport_size({"width": width, "height": 800})
        s.settle()
        s.eq(s.page.evaluate(ROWS_JS), [], f"layout at {width} wide")
        lines = s.page.evaluate("""() => [...document.querySelectorAll('.orders .row .place')].map(e => {
          const r = document.createRange(); r.selectNodeContents(e);
          return new Set([...r.getClientRects()].map(b => Math.round(b.top))).size; })""")
        s.expect(min(lines) >= 2, f"no place wrapped at {width} wide: {lines}")


def open_held(s: Session, path: str) -> None:
    """Open the app with the first request for `path` kept waiting."""
    s.page = s.ctx.new_page()
    s.page.set_default_timeout(TIMEOUT_MS)
    s._watch(s.page)
    s.hold(path)
    s.page.goto(s.base + "/")
    s.wait(lambda: s.holding(path), "the first request for the day")


@check("day.placeholder-rows-on-a-cold-first-paint-only")
def day_placeholders(s: Session) -> None:
    today = "/api/orders?date=" + s.day()
    open_held(s, today)
    s.eq((s.count(".orders .ph-row"), s.rows(), s.count(".empty")), (5, [], 0), "the list before the first answer")
    s.expect(s.date_text().startswith(date_head(s.day())), "the date button before the first answer")
    box = s.page.evaluate("() => { const r = document.querySelector('.orders .ph-row'), h = document.querySelector('.cols');"
                          " return [...r.children].slice(0, 3).map((e, i) =>"
                          " Math.round(e.getBoundingClientRect().left) === Math.round(h.children[i].getBoundingClientRect().left)); }")
    s.eq(box, [True, True, True], "placeholder blocks stand on the rows' columns")
    s.eq(s.page.evaluate("() => [...document.querySelectorAll('.ph-row, .ph-row *')].some(e => getComputedStyle(e).animationName !== 'none')"),
         False, "a placeholder that moves")
    s.release(today)
    s.wait(lambda: s.rows() == s.ids(), "the rows")
    s.eq(s.count(".ph-row"), 0, "placeholders once the rows are in")
    s.release_all()
    s.settle()
    # A day never seen, reached later, keeps the rows on screen instead.
    near, shown = to_a_day_never_seen(s)
    s.wait(lambda: s.holding("/api/orders?date=" + s.day(2)), "the request for the day after tomorrow")
    s.eq((s.count(".ph-row"), s.rows()), (0, shown), "the list while a later day is pending")
    s.release_all()
    s.settle()
    s.eq(s.count(".ph-row"), 0, "placeholders on an empty day")
    # Nor does a day the store holds, nor coming back from the other view.
    s.go_days(-2)
    s.go_settle()
    s.hold(today)
    s.press('[aria-label="返日程"]')
    s.wait(lambda: s.holding(today), "the request on coming back")
    s.eq((s.count(".ph-row"), s.rows()), (0, s.ids()), "the list on coming back, before the answer")
    s.release_all()
    s.settle()


@check("day.placeholder-rows-give-way-when-the-first-load-fails")
def day_placeholders_fail(s: Session) -> None:
    s.allow("http 500", "status of 500")
    today = "/api/orders?date=" + s.day()
    open_held(s, today)
    s.eq(s.count(".orders .ph-row"), 5, "placeholders before the first answer")
    s.answer(today, status=500, content_type="text/html", body="<html>boom</html>")
    s.wait_toast("載入失敗")
    s.eq((s.count(".ph-row"), s.text(".empty"), s.foot()), (0, "冇訂單", ["程數0", "當日車費$0"]), "the list after the failure")
    s.release_all()
    # The next load that works draws the rows, with no placeholders before it.
    s.hold(today)
    s.press(".date-btn")
    s.wait(lambda: s.holding(today), "the request on tapping the date")
    s.eq(s.count(".ph-row"), 0, "placeholders once the list has been drawn")
    s.release_all()
    s.wait(lambda: s.rows() == s.ids(), "the rows")
    s.settle()


@check("day.status-block-turns-over-when-it-changes", still=False)
def day_turn_over(s: Session) -> None:
    s.open_day()
    oid = s.t["order"]["upcoming_hotel"]
    block = s.row(oid) + " .st"

    def turning() -> list:
        """Every status block that is turning: its row and the animation it runs."""
        return s.page.eval_on_selector_all(
            ".orders .row .st.turn", "els => els.map(e => [e.closest('.row').dataset.oid, getComputedStyle(e).animationName])")

    s.eq((s.text(block), turning()), ("預計", []), "the first paint of the day")
    repaint(s, 1)
    s.eq(turning(), [], "a repaint with no status changed")
    s.tap(".tab", has_text="接送")
    s.tap(".tab", has_text="全部")
    s.eq(turning(), [], "a filter put on and taken off")
    # The feed moves the flight on: that block turns, once, and no other.
    state = {"status": "landed", "gate": None}

    def change(by):
        by[oid].update(flight_status=state["status"], flight_gate=state["gate"])

    serve_orders(s, change)
    repaint(s, 2)
    s.eq((s.text(block), turning()), ("已降落", [[oid, "st-turn"]]), "the block whose word changed")
    s.eq(s.page.eval_on_selector(block, "e => [getComputedStyle(e).animationDuration, getComputedStyle(e).animationIterationCount]"),
         ["0.5s", "1"], "the turn")
    repaint(s, 3)
    s.eq((s.text(block), turning()), ("已降落", []), "the paint after the turn")
    # Leaving the day and coming back is not a change.
    s.go_days(1)
    s.go_days(-1)
    s.eq((s.text(block), turning()), ("已降落", []), "after another day and back")
    # With motion reduced the word changes and nothing moves.
    s.page.emulate_media(reduced_motion="reduce")
    state.update(status="gate", gate="16:58")
    repaint(s, 4)
    s.eq(s.text(block), "已到閘", "the word under reduced motion")
    s.eq([name for _, name in turning()], ["none"], "the animation under reduced motion")
    s.eq(s.page.evaluate("() => [...document.querySelectorAll('#view-day *')].filter(e => getComputedStyle(e).animationName !== 'none').length"),
         0, "anything in the day view animating under reduced motion")
    # The paste preview's rows are held still as well.
    s.tap('[aria-label="入單"]')
    s.on(".drop.show .paste-box").first.fill(paste_message())
    s.press(".drop.show .primary-btn", has_text="解析")
    s.on(".drop.show .paste-preview .sum-row").first.wait_for()
    s.eq(s.page.eval_on_selector_all(".drop.show .paste-preview .sum-row", "els => [...new Set(els.map(e => getComputedStyle(e).animationName))]"),
         ["none"], "the preview rows' animation under reduced motion")
    s.page.emulate_media(reduced_motion="no-preference")
    s.eq(s.page.eval_on_selector_all(".drop.show .paste-preview .sum-row", "els => [...new Set(els.map(e => getComputedStyle(e).animationName))]"),
         ["row-in"], "the preview rows' animation with motion allowed")
    s.settle()


@check("day.date-button-formats")
def day_date_formats(s: Session) -> None:
    s.open_day()
    parts = "() => { const b = document.querySelector('.date-btn'); return ['.d', '.w', '.w b', '.y'].map(q => (b.querySelector(q) || {}).textContent || ''); }"
    wd = lambda d: "星期" + WEEKDAY[d.weekday()]
    t = s.today
    s.eq(s.page.evaluate(parts), [f"{t.month:02d}·{t.day:02d}", wd(t) + " · 今日", "今日", ""], "today")
    s.eq(s.page.eval_on_selector(".date-btn .d", "e => getComputedStyle(e).fontFamily.split(',')[0].replace(/\"/g, '')"),
         "B612 Mono", "the figure's face")
    s.go_days(1)
    n = t + timedelta(days=1)
    if n.year == t.year:
        s.eq(s.page.evaluate(parts), [f"{n.month:02d}·{n.day:02d}", wd(n), "", ""], "another day of this year")
    # The last day of the year, then the first of the next: the year appears,
    # small, once it is not this one.
    last = date(t.year, 12, 31)
    s.page.clock.set_fixed_time(datetime.combine(last, demo_now(t).timetz()))
    s.tap(".date-btn")
    s.eq(s.page.evaluate(parts), ["12·31", wd(last) + " · 今日", "今日", ""], "the last day of the year, as today")
    s.go_days(1)
    first = date(t.year + 1, 1, 1)
    s.eq(s.page.evaluate(parts), ["01·01", wd(first) + str(first.year), "", str(first.year)], "a day in another year")
    s.eq(s.date_text(), date_head(first.isoformat(), last.isoformat()), "the button's whole text")
    small, big = s.page.evaluate("() => ['.y', '.d'].map(q => parseFloat(getComputedStyle(document.querySelector('.date-btn ' + q)).fontSize))")
    s.expect(small < big / 2, f"the year is not small: {small} against {big}")
    s.go_days(-1)
    s.eq(s.page.evaluate(parts)[3], "", "the year, back in this one")


# ---- order sheet (inventory D) ----

@check("day.order-sheet")
def day_order_sheet(s: Session) -> None:
    s.open_day()
    oid = s.t["order"]["landed_banner"]
    s.open_order(oid)
    s.eq(s.text(".sheet.show .sheet-title"), "UO623 14:20", "title")
    s.eq(s.text(".sheet.show .sheet-sub"), "#" + oid[-6:], "subtitle")
    info = s.info()
    s.eq(list(info), ["乘客", "電話", "境外", "航班", "車型", "路線", "備註", "結算"], "info rows")
    s.eq(info["航班"], "UO623 · 已降落 13:42", "flight row")
    s.eq(info["結算"], "未結算", "settlement row")
    s.eq(s.page.eval_on_selector_all(".sheet.show .info-row a", "els => els.map(e => e.getAttribute('href'))"),
         ["tel:+8613800000102", "tel:+886900000102"], "phone links")
    s.eq(s.page.eval_on_selector_all(".sheet.show .field-row .fk", "els => els.map(e => e.textContent)"),
         ["價錢", "隧道費", "停車費", "舉牌費", "時間"], "editable fields of a ride")
    s.eq(s.page.eval_on_selector_all(".sheet.show .pp-opt", "els => els.map(e => e.querySelector('.pp-n').textContent + (e.classList.contains('on') ? '*' : ''))"),
         ["P1", "P4*", "富豪"], "pickup points")
    s.eq(s.page.eval_on_selector_all(".sheet.show .pp-opt small", "els => els.map(e => e.textContent)"),
         ["$35", "$32", "$0"], "each point's first-hour charge")
    widths = s.page.eval_on_selector_all(".sheet.show .pp-opt", "els => els.map(e => Math.round(e.getBoundingClientRect().width))")
    s.eq(len(set(widths)), 1, f"the points are equal segments: {widths}")
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
    s.eq(s.text(".sheet.show .sheet-title"), "UO623 14:20", "back on the detail")
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
    s.eq(s.text(".sheet.show .sheet-title"), "UO623 15:45", "title after the time change")
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
    s.eq(s.text(".sheet.show .pp-opt.on .pp-n"), "P1", "highlighted point")
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
    s.eq(s.text(".sheet.show .cancel-info .num"), "11:00", "the time is set as a figure")
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
    # A locked field is text with its figure and the reason, not a control
    # made to look disabled; the ones that can still be edited are buttons.
    shape = s.page.eval_on_selector_all(
        ".sheet.show .field-row",
        "els => els.map(e => [e.querySelector('.fk').textContent, e.tagName, e.querySelector('.fv').textContent, getComputedStyle(e).opacity])")
    s.eq(shape, [["價錢", "DIV", "$475", "1"], ["隧道費", "DIV", "$0", "1"], ["停車費", "BUTTON", "$32", "1"],
                 ["舉牌費", "DIV", "$0", "1"], ["時間", "BUTTON", "12:20", "1"]], "fields of a batched order")
    s.eq(s.count(".sheet.show button.field-row.locked, .sheet.show .field-row.locked:disabled"), 0, "locked fields that are controls")
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
    s.eq(s.text(s.row(new[0]) + " .time"), "15:30", "the new row's time")
    s.eq(s.text(s.row(new[0]) + " .price"), "$128", "the new row's price")
    s.page.wait_for_timeout(400)
    s.eq(s.page.eval_on_selector(".drop", "e => e.innerHTML"), "", "the closed panel's content")
    s.eq(len(s.writes), 1, "writes")


@check("day.add-uber-clears-another-filter")
def day_add_uber(s: Session) -> None:
    s.open_day()
    s.tap(".tab", has_text="滴滴")
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
    s.expect(s.text(".tab.on").startswith("全部"), "the filter that would hide the new row")
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


@check("day.add-back-keeps-the-pasted-text")
def day_add_back_text(s: Session) -> None:
    """E14: a quick order begun over a half-pasted message does not lose it."""
    s.open_day()
    s.tap('[aria-label="入單"]')
    text = "half a message\n  second line "
    s.on(".drop.show .paste-box").first.fill(text)
    s.tap(".drop.show .quick-type-btn.didi")
    s.stage("滴滴 · 時間")
    s.tap(".drop.show .sheet-x")
    s.stage("入單")
    s.eq(s.on(".drop.show .paste-box").first.input_value(), text, "the box after backing out of the first quick stage")
    # From deeper in, one stage at a time.
    s.tap(".drop.show .quick-type-btn.uber")
    s.keys(".drop.show", "0910")
    s.confirm_stage()
    s.stage("Uber · 行程收入")
    s.tap(".drop.show .sheet-x")
    s.tap(".drop.show .sheet-x")
    s.stage("入單")
    s.eq(s.on(".drop.show .paste-box").first.input_value(), text, "the box after backing out of the second")
    # An empty box stays empty, and closing the panel forgets the text.
    s.tap(".drop.show .sheet-x")
    s.wait(lambda: not s.panel_open(), "the panel to close")
    s.page.wait_for_timeout(450)
    s.tap('[aria-label="入單"]')
    s.eq(s.on(".drop.show .paste-box").first.input_value(), "", "the box of a panel opened afresh")
    s.tap(".drop.show .quick-type-btn.foodpanda")
    s.tap(".drop.show .sheet-x")
    s.stage("入單")
    s.eq(s.on(".drop.show .paste-box").first.input_value(), "", "an empty box after backing out")
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
    s.tap(".tab", has_text="滴滴")
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
    s.expect(s.date_text().startswith(date_head(PASTE_DAY)), "the view did not move to the order's date")
    s.eq(s.rows(), [PASTE_ID], "rows on the order's date")
    s.expect(s.text(".tab.on").startswith("全部"), "the filter that would hide the new row")
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
    s.expect(s.date_text().startswith(date_head(PASTE_DAY)), "the view did not move to the order's date")

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
    s.eq(s.text(s.row(PASTE_ID) + " .code"), "CX479", "the row's flight")

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
        ".header-row > .date-btn, .header-row .keys > *",
        "els => els.filter(e => e.getClientRects().length).map(e => e.getAttribute('aria-label'))"),
        [None, "前一個月", "後一個月", "讀結算圖", "返日程"], "header controls")
    s.eq(s.on('[aria-label="返日程"]').first.get_attribute("href"), "/", "the ✕ link")
    book = s.api("GET", settle_path(s.today))
    ledger = s.api("GET", CREDITS)
    waiting = [c for c in ledger["credits"] if c["state"] in ("open", "partial")]
    s.eq(s.totals(),
         [f"未結算${fmt(book['totals']['unsettled'])}", f"等過數${fmt(book['totals']['awaiting'])}",
          f"入數未對 {len(waiting)} 筆${fmt(ledger['sums']['open'])}"], "totals")
    s.expect(s.count(".foot .warn") and s.count(".foot [data-credits]"), "the totals' amber figure and queue link")
    n = book["counts"]
    s.eq(s.texts(".tabs .tab"), [f"接送{n['ride']}", f"滴滴{n['didi']}", f"Uber{n['uber']}", f"熊貓{n['foodpanda']}"], "tabs")
    s.eq(s.text(".tab.on"), f"接送{n['ride']}", "highlighted tab")
    s.eq(s.texts(".header .wk span"), list("日一二三四五六"), "weekday heads")
    # The total keys are the calendar's legend: the page carries no other.
    s.eq(s.count(".legend") + s.count(".cal .sw"), 0, "a colour key beside the total keys")
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
    s.eq(s.strip_problems(), [], "the strip's cells")
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
        s.page.wait_for_timeout(400)
        s.settle()

    s.open_settle()
    s.eq(measure(), [390, "0px", 40], "settle at phone width: body width, bottom padding, header key")
    resize(1000)
    s.eq(measure(), [640, "0px", 40], "settle on a wide screen")
    s.eq(s.page.eval_on_selector(".cell[data-d]", "e => getComputedStyle(e).minHeight"), "76px", "cell height on a wide screen")
    s.eq(s.strip_problems(), [], "the strip's cells after a resize")
    resize(340)
    s.eq(measure(), [340, "0px", 34], "settle below 361px")
    s.open_day()
    s.eq(measure(), [390, "0px", 40], "day at phone width")
    resize(1000)
    s.eq(measure(), [480, "0px", 40], "day on a wide screen")
    resize(340)
    s.eq(measure(), [340, "0px", 40], "day below 361px")
    s.eq(s.scheme(), "normal", "color-scheme of the document on the day view")


@check("settle.platform-chips")
def settle_platform(s: Session) -> None:
    s.open_settle()
    s.eq(s.page.evaluate("() => localStorage.getItem('settlePlatform')"), None, "stored platform before any choice")
    s.tap('[aria-label="前一個月"]')
    asked = len(s.requests)
    s.tap(".tab", has_text="滴滴")
    s.expect(settle_path(s.today, "didi") in s.asked(asked), "the current month of the chosen platform")
    s.expect("/api/credits?platform=didi" in s.asked(asked, "/api/credits"), "the chosen platform's ledger")
    s.expect(not any("platform=ride" in p for p in s.asked(asked, "/api/")), "a request for the platform left behind")
    s.expect(s.text(".tab.on").startswith("滴滴"), "highlighted chip")
    s.eq(s.page.evaluate("() => localStorage.getItem('settlePlatform')"), "didi", "stored platform")
    # The strip starts again on the current month.
    s.eq(s.top_week(), week_id(s.today.replace(day=1)), "the row at the top after switching")
    s.eq(s.month_text(), month_label(s.today, now=True), "month button after switching")
    book = s.api("GET", settle_path(s.today, "didi"))
    s.eq(s.totals(), [f"未結算${fmt(book['totals']['unsettled'])}", f"等過數${fmt(book['totals']['awaiting'])}"], "totals")
    s.eq(s.cell_state(10), ("unsettled", "88"), "a day of the chosen platform")
    s.tap(s.cell(10))
    s.eq(s.sub(), "滴滴", "day sheet subtitle")
    s.eq(s.texts(".sheet.show .orow .oll"), ["滴滴"], "a quick order's label")
    s.close_sheets()
    # Tapping the chosen chip again does nothing.
    asked = len(s.requests)
    s.tap(".tab", has_text="滴滴")
    s.eq(s.asked(asked, "/api/"), [], "requests for tapping the chosen chip")
    s.allow("request failed: GET /api/events")     # a reload cuts the event stream
    s.page.reload()
    s.on(".cell[data-d]").first.wait_for()
    s.settle()
    s.expect(s.text(".tab.on").startswith("滴滴"), "the chosen platform after a reload")
    s.page.evaluate("() => localStorage.setItem('settlePlatform', 'no-such-platform')")
    s.page.reload()
    s.on(".cell[data-d]").first.wait_for()
    s.settle()
    s.expect(s.text(".tab.on").startswith("接送"), "an unknown stored platform falls back to 接送")
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
    s.eq(s.strip_problems(), [], "the strip's cells")


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


# The row of the doctored batch on its credit's sheet: the way to a batch
# whose month the strip does not hold.
FAR_BATCH = '.sheet.show [data-bl="990"]'


@check("settle.a-batch-outside-the-strip-fills-the-months-between")
def settle_fill(s: Session) -> None:
    s.open_settle()
    first = (date.fromisoformat(s.weeks()[0][2:]) + timedelta(days=6)).replace(day=1)
    far = doctored_ledger(s, 3)
    s.eq((date.fromisoformat(s.weeks()[0][2:]) + timedelta(days=6)).replace(day=1), first, "the strip's first month after the reload")
    s.open_credit("exact")
    asked = len(s.requests)
    s.tap(FAR_BATCH)
    # The batch's month is three months above the strip: every month up to
    # it is loaded, once, and the strip goes to that month.
    got = s.asked(asked)
    for n in (1, 2, 3):
        s.eq(got.count(settle_path(add_months(first, -n))), 1, f"requests for the month {n} before the strip")
    s.eq(s.top_week(), week_id(far.replace(day=1)), "the row the strip went to")
    s.eq(s.month_text(), month_label(far), "month button")
    days = [date.fromisoformat(w[2:]) for w in s.weeks()]
    s.expect(all(b - a == timedelta(days=7) for a, b in zip(days, days[1:])), "the strip is not one unbroken run of weeks")
    s.expect(week_id(s.today) in s.weeks(), "the months held before were thrown away")
    s.eq(s.strip_problems(), [], "the strip's cells")
    s.eq(s.writes, [], "writes")


@check("settle.far-jump-refounds-the-strip")
def settle_refound(s: Session) -> None:
    s.open_settle()
    first = (date.fromisoformat(s.weeks()[0][2:]) + timedelta(days=6)).replace(day=1)
    far = doctored_ledger(s, 7)
    s.open_credit("exact")
    asked = len(s.requests)
    s.tap(FAR_BATCH)
    got = s.asked(asked)
    s.eq(got.count(settle_path(far)), 1, "requests for the month jumped to")
    # Not the three after the month jumped to, which the new strip grows into
    # to fill the screen.
    between = [settle_path(add_months(first, -n)) for n in (2, 3)]
    s.eq([p for p in got if p in between], [], "months between were paid for")
    s.expect(week_id(s.today) not in s.weeks(), "the old strip is still there")
    s.expect(week_id(far) in s.weeks(), "the row carrying the far day is not on the new strip")
    s.eq(s.month_text(), month_label(far), "month button")
    days = [date.fromisoformat(w[2:]) for w in s.weeks()]
    s.expect(all(b - a == timedelta(days=7) for a, b in zip(days, days[1:])), "the strip is not one unbroken run of weeks")
    # And back: the current month is as far away again.
    s.close_sheets()
    asked = len(s.requests)
    s.tap(".date-btn")
    s.eq(s.asked(asked).count(settle_path(s.today)), 1, "requests for the current month")
    s.expect(week_id(far) not in s.weeks(), "the far strip is still there")
    s.eq(s.month_text(), month_label(s.today, now=True), "month button back on the current month")
    s.eq(s.top_week(), week_id(s.today.replace(day=1)), "top row back on the current month")
    s.eq(s.writes, [], "writes")


@check("settle.far-jump-rests-on-the-month-asked-for")
def settle_refound_rest(s: Session) -> None:
    """A batch a jump away refounds the strip on its month; the strip then
    has to come to rest on that month's first row, and stay there while it
    grows around it."""
    s.open_settle()
    far = doctored_ledger(s, 7)
    s.open_credit("exact")
    s.tap(FAR_BATCH)
    s.expect(week_id(s.today) not in s.weeks(), "the strip was not refounded")
    s.eq(s.top_week(), week_id(far.replace(day=1)), "the row at the top of the strip after the jump")
    s.eq(s.month_text(), month_label(far), "month button")
    s.eq(s.writes, [], "writes")


@check("settle.a-run-of-months-is-stored-whole-or-not-at-all")
def settle_fill_fails(s: Session) -> None:
    """Three months are asked for at once and the middle one fails: a strip
    holding the two that answered would draw the third's days as days with
    no work on them."""
    s.open_settle()
    first = (date.fromisoformat(s.weeks()[0][2:]) + timedelta(days=6)).replace(day=1)
    far = doctored_ledger(s, 3)
    s.eq((date.fromisoformat(s.weeks()[0][2:]) + timedelta(days=6)).replace(day=1), first, "the strip's first month after the reload")
    run = [settle_path(add_months(first, -n)) for n in (1, 2, 3)]
    s.allow("http 500", "status of 500")
    s.stub("GET", "**" + run[1], 500, "<html>boom</html>", "text/html")
    s.open_credit("exact")
    asked = len(s.requests)
    s.press(FAR_BATCH)
    s.wait_toast("載入失敗")
    s.settle()
    s.eq(s.asked(asked).count(run[2]), 1, "requests for the month that answered beyond the one that failed")
    # The month next to the strip may be there, brought in by the strip's own
    # growth, which asks for it alone; nothing beyond the month that failed is.
    earliest = (date.fromisoformat(s.weeks()[0][2:]) + timedelta(days=6)).replace(day=1)
    s.expect(earliest >= add_months(first, -1) and week_id(far) not in s.weeks(),
             f"the strip reaches past a month that failed: it begins at {earliest}")
    # Nothing of the run was kept: asked for again, the month that had
    # answered is fetched again.
    s.page.wait_for_timeout(2500)
    s.page.unroute("**" + run[1])
    asked = len(s.requests)
    s.tap(FAR_BATCH)
    s.eq([s.asked(asked).count(p) for p in run[1:]], [1, 1], "requests for the run, asked for again")
    s.eq(s.top_week(), week_id(far.replace(day=1)), "the row the strip went to")
    days = [date.fromisoformat(w[2:]) for w in s.weeks()]
    s.expect(all(b - a == timedelta(days=7) for a, b in zip(days, days[1:])), "the strip is not one unbroken run of weeks")
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


CLEAR = "rgba(0, 0, 0, 0)"


def rewrite_settle(s: Session, change) -> None:
    """From now on every answer from /api/settle reaches the page as `change`
    leaves it: change(body, path) rewrites the answer in place."""
    def handler(route):
        body = route.fetch().json()
        change(body, route.request.url[len(s.base):])
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    s.ctx.route("**/api/settle?*", handler)


# ---- settle view: cells and focus (inventory H10-H30) ----

# The two palettes and the two phone widths a layout check is made at.
SCHEMES = ("dark", "light")
PHONE_WIDTHS = (390, 340)


def cells_hold(s: Session, what: str) -> None:
    s.eq(s.strip_problems(), [], f"the strip's cells {what}")
    s.expect(not s.page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth"), f"the page scrolls sideways {what}")


def fare_of(orders: list) -> str:
    """What a day's cell prints for these orders: the whole day's fare."""
    from ride_dispatch.service import expected_of
    return fmt(round(sum(expected_of(o) for o in orders), 2))


@check("settle.cells")
def settle_cells(s: Session) -> None:
    """A day says one thing: what the whole day earned, and by the rule under
    that figure where the money has got to."""
    s.open_settle()
    s.reach(s.cell(38))
    want = {38: ("unsettled", "450"), 1: ("unsettled", "940"), 3: ("unsettled", "980"), 0: ("unsettled", "1890"),
            27: ("short", "960"), 26: ("short", "840"), 25: ("short", "510"),
            19: ("awaiting", "900"), 18: ("awaiting", "390"), 6: ("awaiting", "910"),
            34: ("received", "896.55"), 33: ("received", "480"), 12: ("received", "570"),
            -1: ("future", "900")}
    s.eq({n: s.cell_state(n) for n in want}, want, "the state and the figure of each seeded day")
    # Today's whole fare counts the orders still to be driven; the server's
    # payload is where the figure comes from.
    book = s.api("GET", settle_path(s.today))
    s.eq(s.cell_state(0)[1], fare_of([o for o in book["orders"] if o["scheduled_time"].startswith(s.day())]), "today's figure against the payload")
    for scheme in SCHEMES:
        s.page.emulate_media(color_scheme=scheme)
        text, text3, text4, amber, green, text2 = (s.token(n) for n in ("--text", "--text-3", "--text-4", "--amber", "--green", "--text-2"))
        owed, short, waiting, done, ahead = (s.look(n) for n in (1, 26, 19, 34, -1))
        s.eq((owed["ink"], owed["weight"], owed["rule"][:3]), (amber, "700", [24, 3, amber]), f"a day no statement has claimed ({scheme})")
        s.eq((short["ink"], short["weight"], short["rule"][:2]), (text, "700", [24, 3]), f"a day on a statement paid short ({scheme})")
        s.expect(green in short["rule"][3] and amber in short["rule"][3], f"the short rule is not green then amber ({scheme}): {short['rule'][3]}")
        # 3px of the 24 is the break between the two parts.
        s.expect(short["got"] >= 6 and 24 - 3 - short["got"] >= 6, f"a part of the short rule under 6px: {short['got']}")
        s.eq((waiting["ink"], waiting["weight"], waiting["rule"][:3]), (text, "700", [24, 1, text2]), f"a day awaiting its transfer ({scheme})")
        for name, got in (("collected", done), ("still to come", ahead)):
            s.eq((got["ink"], got["weight"], got["rule"][2:]), (text3, "400", [CLEAR, "none"]), f"a day {name} ({scheme})")
        s.eq((owed["date"], round(done["cents"], 2), {x["size"] for x in (owed, short, waiting, done, ahead)}),
             ([text3, "10px"], 0.7, {13}), f"the date, the cents and the figures' size ({scheme})")
        s.eq(s.colour(".cell.none .d"), text4, f"the date of an empty day ({scheme})")
        s.expect(not any(x["faded"] for x in (owed, short, waiting, done, ahead)), f"a day is faded by opacity ({scheme})")
        for width in PHONE_WIDTHS:
            s.resize(width)
            cells_hold(s, f"at {width} ({scheme})")
        s.resize(390)
    s.page.emulate_media(color_scheme="dark")
    s.expect("today" in s.on(s.cell(0)).first.get_attribute("class"), "today's cell is not marked")
    # Today's number is an inverse block: the ink as its ground, the ground as its ink.
    block = s.cell(0) + " .d"
    s.eq((s.text(block), s.colour(block, "backgroundColor"), s.colour(block)),
         (md_slash(s.today) if s.today.day == 1 else str(s.today.day), s.token("--text"), s.token("--bg")),
         "today's day number")
    s.eq(s.colour(s.cell(1) + " .d", "backgroundColor"), "rgba(0, 0, 0, 0)", "another day's number has a ground")
    empty = s.on(".cell.none").first
    s.expect(empty.is_disabled() and empty.get_attribute("data-d") is None, "an empty day can be opened")
    firsts = [t for t in s.texts("#grid .cell .d") if "/" in t]
    s.expect(firsts and all(t.endswith("/1") for t in firsts) and f"{s.today.month}/1" in firsts,
             f"day numbers carrying a month: {firsts}")
    # One tap on a day opens its sheet.
    s.open_cell(26)
    s.eq(s.title(), md_label(s.back(26)) + " 星期" + WEEKDAY[s.back(26).weekday()], "the sheet a day opens")
    s.eq(s.writes, [], "writes")


@check("settle.a-day-shows-its-whole-fare-and-only-driven-orders-decide-its-state")
def settle_day_fare(s: Session) -> None:
    """A day with one leg on a statement and one on none is amber and still
    shows the whole day; an order still to be driven later today is in the
    day's figure and does not make the day amber."""
    o = seed_demo_db._oid
    kept = {}

    def change(body: dict, path: str) -> None:
        # One of the two legs of a day on a statement awaiting its transfer
        # is taken off the statement.
        for b in body["settlements"]:
            if b["id"] == s.t["batch"]["awaiting"]:
                b["orders"] = [x for x in b["orders"] if x["order_id"] != o(302)]
        # Today keeps only the orders not yet driven.
        today = [x for x in body["orders"] if x["scheduled_time"].startswith(s.day())]
        body["orders"] = [x for x in body["orders"] if x not in today or x["scheduled_time"] >= body["now"]]
        if today:
            kept["today"] = [x for x in today if x["scheduled_time"] >= body["now"]]

    whole = s.api("GET", settle_path(s.back(19)))
    mixed = fare_of([x for x in whole["orders"] if x["scheduled_time"].startswith(s.back(19).isoformat())])
    rewrite_settle(s, change)
    s.open_settle()
    s.reach(s.cell(19))
    s.eq(mixed, "900", "the seeded day's whole fare")
    s.eq(s.cell_state(19), ("unsettled", mixed), "a day partly on a statement")
    s.eq(s.cell_state(18), ("awaiting", "390"), "the statement's other day")
    s.expect(len(kept["today"]) >= 2, "the seed has no order later today")
    s.eq(s.cell_state(0), ("future", fare_of(kept["today"])), "today with nothing driven yet")
    today = s.look(0)
    s.eq((today["ink"], today["weight"], today["rule"][2:]), (s.token("--text-3"), "400", [CLEAR, "none"]),
         "today's figure and rule with nothing driven yet")
    cells_hold(s, "with the days rewritten")
    s.eq(s.writes, [], "writes")


# Whether a lit day can be read where it is: below the header, above the foot.
LIT_ON_SCREEN_JS = """
() => {
  const seen = q => [...document.querySelectorAll(q)].find(e => e.getClientRects().length);
  const top = seen('.header').getBoundingClientRect().bottom, bottom = seen('.foot').getBoundingClientRect().top;
  return [...document.querySelectorAll('#grid .cell.lit')].some(e => {
    const r = e.getBoundingClientRect();
    return r.top >= top - 1 && r.bottom <= bottom + 1;
  });
}
"""


def empty_day(s: Session) -> tuple:
    """The middle of an empty day that is on screen, clear of the header and
    the foot."""
    x, y = s.page.evaluate("""() => {
      const seen = q => [...document.querySelectorAll(q)].find(e => e.getClientRects().length);
      const head = seen('.header').getBoundingClientRect().bottom, foot = seen('.foot').getBoundingClientRect().top;
      for (const cell of document.querySelectorAll('#grid .cell.none')) {
        const r = cell.getBoundingClientRect();
        if (r.top > head && r.bottom < foot) return [r.left + r.width / 2, r.top + r.height / 2];
      }
      return [0, 0];
    }""")
    s.expect(y > 0, "no empty day on screen")
    return x, y


@check("settle.focus")
def settle_focus(s: Session) -> None:
    """A statement's days are lit from its sheet, the foot names it, and the
    rest of the calendar recedes by colour until the focus is put down."""
    s.open_settle()
    days = [s.back(n) for n in (27, 26, 25)]
    relation = sorted(d.isoformat() for d in days)
    usual = s.totals()
    s.eq(len(usual), 3, "the foot's usual cells")
    # From a list: the statement's row opens its sheet, and the sheet's key
    # leads back to the calendar.
    s.tap('.lkey[data-lens="received"]')
    s.to_month(s.back(26))
    s.tap(f'#settle-list .brow[data-bl="{s.t["batch"]["short"]}"]')
    s.on(".sheet.show .up-sec").first.wait_for()
    key = s.on(".sheet.show [data-focus]").first.evaluate(
        "e => { const c = getComputedStyle(e), r = e.getBoundingClientRect();"
        " return [e.textContent, r.height >= 44, c.backgroundColor, c.borderTopWidth, c.textDecorationLine]; }")
    s.eq(key, ["喺月曆睇", True, CLEAR, "1px", "none"], "the key that leads to the calendar")
    s.tap(".sheet.show [data-focus]")
    s.wait(lambda: not s.sheet_open() and not s.count(".scrim.show"), "the sheet to close")
    s.eq(s.pressed(), ["fare"], "the lens after going to the calendar")
    s.eq((s.count(".cal"), s.count("#settle-list")), (1, 0), "the calendar in the list's place")
    s.eq(s.lit(), relation, "the days lit")
    s.expect(s.page.evaluate(LIT_ON_SCREEN_JS), "none of the statement's days is on screen")
    held = s.count("#grid .cell[data-d]")
    s.eq((s.count("#grid .cell[data-d].dim"), s.count("#grid .cell.none.dim")), (held - 3, 0), "every other day recedes, bar the empty ones")

    def line() -> str:
        return (f"{md_slash(s.back(23))} 結算 · {day_runs(days, s.view_month())} · 5 程 · $2,310.00 · 差 $380✕")

    s.eq(s.totals(), [line()], "the foot while a statement is lit")
    close = s.on(".foot [data-unfocus]").first.bounding_box()
    s.expect(close["width"] >= 44 and close["height"] >= 44, f"the key that clears the focus is under 44px: {close}")
    s.expect("B612 Mono" in s.colour(".foot .fline .num", "fontFamily"), "the line's figures are not in the figure face")
    s.eq(s.colour(".foot .fline .bs"), s.token("--amber"), "what is still owed, in the line")
    # Receding is by colour: nothing is faded, and a receded figure gives up
    # its weight and its state colour.
    for scheme in SCHEMES:
        s.page.emulate_media(color_scheme=scheme)
        lit, owed, waiting, done = (s.look(n) for n in (26, 1, 19, 34))
        s.expect(not any(x["faded"] for x in (lit, owed, waiting, done)), f"a day is faded by opacity ({scheme})")
        s.eq((lit["ground"], lit["ink"], lit["weight"]), (s.token("--surface"), s.token("--text"), "700"), f"a lit day ({scheme})")
        s.eq((owed["ink"], owed["weight"], owed["rule"][2]), (s.token("--text-2"), "400", s.token("--line")), f"a receded day with open money ({scheme})")
        s.eq((waiting["ink"], waiting["weight"], waiting["rule"][2]), (s.token("--text-3"), "400", s.token("--line")), f"a receded day awaiting its transfer ({scheme})")
        s.eq((done["ink"], done["ground"]), (s.token("--text-3"), CLEAR), f"a receded record ({scheme})")
        for width in PHONE_WIDTHS:
            s.resize(width)
            cells_hold(s, f"with a focus down at {width} ({scheme})")
            foot = s.page.evaluate("""() => {
              const f = [...document.querySelectorAll('.foot-in')].find(e => e.getClientRects().length);
              const box = f.getBoundingClientRect();
              return [...f.querySelectorAll('.pt, .fx')].every(e => { const r = e.getBoundingClientRect();
                return r.left >= box.left - 0.5 && r.right <= box.right + 0.5 && r.height < 46; });
            }""")
            s.expect(foot, f"a part of the focus line is cut or wrapped inside itself at {width}")
        s.resize(390)
    s.page.emulate_media(color_scheme="dark")
    s.reach(s.cell(27))
    # It survives a change made elsewhere.
    s.api("PATCH", "/api/orders/" + seed_demo_db._oid(12), {"price": 401})
    s.wait(lambda: s.cell_state(1)[1] == "941", "the change made elsewhere")
    s.eq((s.lit(), s.totals()), (relation, [line()]), "the focus after a live update")
    # A day opens on one tap whatever is focused, and the focus is still there after.
    s.to_top(s.cell(26))
    s.tap(s.cell(1))
    s.eq(s.title(), md_label(s.back(1)) + " 星期" + WEEKDAY[s.back(1).weekday()], "a day opened under a focus")
    s.close_sheets()
    s.eq(s.lit(), relation, "the focus after closing the sheet")
    # ✕ puts it down, and the foot is its usual self again.
    s.tap(".foot [data-unfocus]")
    s.eq((s.lit(), s.count("#grid .dim"), s.count(".foot .fline")), ([], 0, 0), "after ✕")
    s.eq([t[:3] for t in s.totals()], [t[:3] for t in usual], "the foot after ✕")
    # So does a tap on empty calendar.
    s.focus_on("short", ".sheet.show .up-sec")
    s.eq(s.lit(), relation, "the days lit a second time")
    s.page.touchscreen.tap(*empty_day(s))
    s.wait(lambda: s.lit() == [] and s.count("#grid .dim") == 0 and s.count(".foot .fline") == 0, "a tap on empty calendar to clear the focus")
    s.expect(not s.sheet_open(), "an empty day opened a sheet")
    s.eq(s.writes, [], "writes")


@check("settle.focus-is-revealed-when-its-days-are-off-screen")
def settle_focus_reveal(s: Session) -> None:
    """A batch reached through a credit, with the strip somewhere else: the
    strip goes to the statement's days."""
    s.open_settle()
    days = [s.back(n) for n in (15, 12, 11)]
    s.reach(s.cell(15))
    s.tap(".date-btn")
    s.tap('[aria-label="後一個月"]')
    s.tap('[aria-label="後一個月"]')
    s.open_credit("partial")
    s.tap(".sheet.show .sum-row.link")
    s.eq(s.title(), "結算 " + span_label(s.back(12), s.back(11)), "the batch the credit paid")
    s.tap(".sheet.show [data-focus]")
    s.wait(lambda: not s.sheet_open(), "the sheet to close")
    s.eq(s.lit(), sorted(d.isoformat() for d in days), "the days lit")
    s.expect(s.page.evaluate(LIT_ON_SCREEN_JS), "none of the statement's days was brought on screen")
    s.eq(s.totals(), [f"{md_slash(s.back(9))} 結算 · {day_runs(days, s.view_month())} · 4 程 · $1,780.00 · 已收 {md_slash(s.back(7))}✕"],
         "the foot's line for a collected statement")
    s.eq(s.colour(".foot .fline .bs"), s.token("--green"), "collected, in the line")
    # Already on screen: the strip stays where it is.
    at = s.scroll_y()
    s.focus_on("held_back")
    s.eq(s.scroll_y(), at, "the strip moved although the days were on screen")
    s.eq(s.writes, [], "writes")


# The widths the strip is laid out for: the narrowest phone, the usual one,
# the widest column a phone gets, and the desktop rule.
STRIP_WIDTHS = (340, 390, 480, 1000)


@check("settle.cell-figures-hold-at-every-width")
def settle_figures(s: Session) -> None:
    """A day's figure is set in the figure face, exact to the cent, and never
    wider than its column."""
    s.open_settle()
    s.reach(s.cell(38))
    s.expect(s.page.evaluate("() => document.fonts.check('700 13px \"B612 Mono\"') && document.fonts.check('400 13px \"B612 Mono\"')"),
             "the figure face is not in")
    for sel in ("#grid .cell .amt", "#grid .cell .d"):
        s.expect("B612 Mono" in s.colour(sel, "fontFamily"), f"{sel} is not set in the figure face")
    s.expect(s.count("#grid .amt .ct .p"), "punctuation in a figure is not pulled in")
    for width in STRIP_WIDTHS:
        s.resize(width)
        cells_hold(s, f"at {width}")
    s.eq(round(s.page.evaluate("() => document.body.getBoundingClientRect().width")), 640, "the column at the desktop rule")
    # The shrink is the last resort: at the usual phone width a seeded figure
    # with cents keeps the size the others are set in.
    s.resize(390)
    s.eq(s.look(34)["size"], 13, "a three-digit figure with cents at 390")
    s.eq(s.writes, [], "writes")


@check("settle.cells-under-stress")
def settle_cells_stress(s: Session) -> None:
    """A five-digit fare with cents in a cell and seven-figure totals in the
    foot: the figure is set smaller, whole, inside its column, and the foot
    stays on its one line."""
    stress_strip(s.ctx, s.today)
    s.open_settle()
    s.reach(s.cell(27))
    s.eq(s.cell_state(2), ("unsettled", "12345.67"), "a five-digit fare with cents")
    long = fmt(LONG_AMOUNT)
    for scheme in SCHEMES:
        s.page.emulate_media(color_scheme=scheme)
        for width in PHONE_WIDTHS:
            s.resize(width)
            cells_hold(s, f"under stress at {width} ({scheme})")
            big = s.look(2)
            s.expect(6 <= big["size"] < 13 and round(big["cents"], 2) == 0.7, f"the long figure at {width}: {big}")
    s.page.emulate_media(color_scheme="dark")
    for width in STRIP_WIDTHS:
        s.resize(width)
        cells_hold(s, f"under stress at {width}")
        # The foot's three figures stay whole on its one line.
        foot = s.page.evaluate("""() => {
          const f = [...document.querySelectorAll('.foot-in')].find(e => e.getClientRects().length);
          const box = f.getBoundingClientRect();
          const cells = [...f.querySelectorAll('.v')].map(e => e.getBoundingClientRect());
          return { tops: [...new Set(cells.map(r => Math.round(r.top)))].length,
                   inside: cells.every(r => r.left >= box.left - 0.5 && r.right <= box.right + 0.5),
                   apart: cells.every((r, i) => !i || r.left >= cells[i - 1].right) };
        }""")
        s.eq(foot, {"tops": 1, "inside": True, "apart": True}, f"the foot under stress at {width}")
    s.eq(s.totals(), [f"未結算${long}", "等過數$1048576.50", f"入數未對 8 筆${long}"], "totals under stress")
    s.eq(s.writes, [], "writes")


@check("settle.foot")
def settle_foot(s: Session) -> None:
    s.open_settle()
    book, ledger = s.api("GET", settle_path(s.today)), s.api("GET", CREDITS)
    waiting = [c for c in ledger["credits"] if c["state"] in ("open", "partial")]
    s.eq(s.totals(), [f"未結算${fmt(book['totals']['unsettled'])}", f"等過數${fmt(book['totals']['awaiting'])}",
                      f"入數未對 {len(waiting)} 筆${fmt(ledger['sums']['open'])}"], "totals against the server's")
    s.eq(s.texts(".foot .k"), ["未結算", "等過數", f"入數未對 {len(waiting)} 筆"], "labels")
    # Amber is what the platform still owes, blue the bank money not matched.
    s.eq([s.colour(".foot .warn .v"), s.colour(".foot .queue .v")], [s.token("--amber"), s.token("--blue")], "the figures' colours")
    s.expect("B612 Mono" in s.colour(".foot .v", "fontFamily"), "the figures are not in the figure face")
    foot = s.page.evaluate("""() => {
      const seen = q => [...document.querySelectorAll(q)].find(e => e.getClientRects().length);
      const f = seen('.foot'), q = seen('.foot [data-credits]'), cs = getComputedStyle(f);
      return { fixed: cs.position, bottom: Math.round(window.innerHeight - f.getBoundingClientRect().bottom),
               inView: !!f.closest('#view-settle'), tag: q.tagName,
               tall: q.getBoundingClientRect().height >= 44, line: getComputedStyle(q.querySelector('.v')).textDecorationLine,
               height: f.getBoundingClientRect().height,
               room: parseFloat(getComputedStyle(seen('.cal')).paddingBottom) };
    }""")
    s.eq({k: foot[k] for k in ("fixed", "bottom", "inView", "tag", "tall", "line")},
         {"fixed": "fixed", "bottom": 0, "inView": True, "tag": "BUTTON", "tall": True, "line": "underline"}, "the foot")
    s.expect(foot["room"] >= foot["height"], f"the strip's end does not clear the foot: {foot}")
    # The strip's true end clears the foot, and reaching it still loads on.
    weeks = len(s.weeks())
    s.page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
    s.settle()
    s.expect(len(s.weeks()) > weeks, "nothing was loaded at the strip's end")
    # The way into the queue.
    s.tap(".foot [data-credits]")
    s.eq((s.title(), s.sub()), ("入數未對", f"接送 · {len(waiting)} 筆 ${fmt(ledger['sums']['open'])}"), "the queue from the foot")
    s.close_sheets()
    # Nothing unmatched: no way in. Another platform has no credits at all.
    s.tap(".tab", has_text="滴滴")
    s.eq((len(s.totals()), s.count(".foot [data-credits]")), (2, 0), "the foot with nothing unmatched")
    # The foot belongs to the view: the day view shows its own.
    s.go_day()
    s.eq((s.count(".foot [data-credits]"), s.count(".foot .foot-in")), (0, 1), "feet on the day view")
    s.eq(s.writes, [], "writes")


@check("settle.month-figure")
def settle_month_figure(s: Session) -> None:
    def head() -> list:
        return s.page.evaluate("""() => {
          const seen = q => [...document.querySelectorAll(q)].find(e => e.getClientRects().length);
          const d = seen('.date-btn .d');
          return [d.textContent, seen('.date-btn .w').textContent.trim(), d.querySelectorAll('.p').length,
                  Math.round(seen('.header').getBoundingClientRect().height)];
        }""")

    s.open_settle()
    now = f"{s.today.year}·{s.today.month:02d}"
    figure, word, pulled, height = head()
    s.eq((figure, word, pulled), (now, "今個月", 1), "the current month")
    s.expect("B612 Mono" in s.colour(".date-btn .d", "fontFamily"), "the month is not in the figure face")
    # Back into the year before: always the year and a two-digit month.
    for _ in range(s.today.month):
        s.tap('[aria-label="前一個月"]')
    s.eq(head(), [f"{s.today.year - 1}·12", "", 1, height], "a month in another year, and the header's height")
    s.tap(".date-btn")
    s.eq(head(), [now, "今個月", 1, height], "back on the current month")
    # Where the words drop under the figure, they keep their line when empty.
    s.resize(340)
    s.tap(".date-btn")
    narrow = head()
    s.eq(narrow[:3], [now, "今個月", 1], "the current month on a narrow screen")
    s.tap('[aria-label="前一個月"]')
    prev = add_months(s.today, -1)
    s.eq(head(), [f"{prev.year}·{prev.month:02d}", "", 1, narrow[3]], "another month on a narrow screen, and the header's height")
    s.eq(s.writes, [], "writes")


# ---- settle view: the month's total keys and the lens ----


def keys_drawn(s: Session, totals: dict, what: str) -> None:
    """The four keys against one month's totals: the figures, and the ink
    and rule each state is drawn in. A key with no money in its state recedes
    and has no rule."""
    s.eq(s.keys_text(), key_texts(totals), f"the keys' figures, {what}")
    text, text2, amber = s.token("--text"), s.token("--text-2"), s.token("--amber")
    want = {
        "fare": (text, CLEAR),
        "received": (text2, CLEAR),
        "awaiting": (text, text2) if totals["awaiting"] > 0 else (text2, CLEAR),
        "unsettled": (amber, amber) if totals["unsettled"] > 0 else (text2, CLEAR),
    }
    heights = {"awaiting": 1, "unsettled": 3}
    for key in s.page.evaluate(KEYS_JS):
        ink, rule = want[key["lens"]]
        s.eq((key["ink"], key["rule"][1]), (ink, rule), f"ink and rule of {key['lens']}, {what}")
        if rule != CLEAR:
            s.eq(key["rule"][0], heights[key["lens"]], f"thickness of the rule under {key['lens']}, {what}")


@check("settle.total-keys")
def settle_total_keys(s: Session) -> None:
    s.open_settle()
    cur = s.today.replace(day=1)
    totals = s.api("GET", settle_path(cur))["month_totals"]
    keys_drawn(s, totals, "the current month")
    keys = s.page.evaluate(KEYS_JS)
    s.eq([k["lens"] for k in keys], [name for name, _ in TOTAL_KEYS], "the keys' order")
    s.eq(s.texts("#settle-lens .lkey .k"), [label for _, label in TOTAL_KEYS], "the keys' labels")
    s.eq({k["tag"] for k in keys}, {"BUTTON"}, "the keys are buttons")
    s.expect(all(k["height"] >= 44 for k in keys), f"a key under 44px: {[k['height'] for k in keys]}")
    s.expect(all("B612 Mono" in k["face"] for k in keys), "a figure is not in the figure face")
    s.eq({round(k["cents"], 2) for k in keys}, {0.7}, "the cents against the dollars")
    s.expect(not any(k["lined"] for k in keys), "something in a key is underlined")
    # The row rides in the sticky header, between the tabs and the column head.
    s.eq(s.page.evaluate("() => { const h = document.querySelector('#view-settle .header');"
                         " return [getComputedStyle(h).position, [...h.children].map(e => e.className.split(' ')[0])]; }"),
         ["sticky", ["header-row", "tabs", "lens", "cols", "cols"]], "what the header holds")
    # Two column heads, one for each form of the body, and one showing.
    s.eq(s.page.evaluate("() => [...document.querySelectorAll('#view-settle .header .cols')]"
                         ".map(e => [e.className, e.getClientRects().length > 0])"),
         [["cols wk", True], ["cols lhead", False]], "the column heads under the keys")
    # Two kinds of control: a tab is chosen by a rule under it, a key by its ground.
    s.eq((s.colour(".tab.on", "borderBottomWidth"), s.colour(".tab.on", "borderBottomColor")),
         ("2px", s.token("--text")), "the chosen tab's rule")
    s.eq((s.colour(".lkey.on", "borderBottomWidth"), s.colour(".lkey.on", "backgroundColor"),
          s.colour(".header .wk", "backgroundColor")),
         ("0px", s.token("--surface"), s.token("--surface")), "the chosen key's ground, and the column head's")
    s.eq([k["ground"] for k in keys[1:]], [CLEAR] * 3, "the grounds of the keys not chosen")
    # The month before has other money in other states, drawn by the same rule.
    s.tap('[aria-label="前一個月"]')
    keys_drawn(s, s.api("GET", settle_path(add_months(cur, -1)))["month_totals"], "the month before")
    s.eq(s.writes, [], "writes")


@check("settle.total-keys-follow-the-month-and-the-platform")
def settle_total_keys_follow(s: Session) -> None:
    s.open_settle()
    cur = s.today.replace(day=1)
    prev, after = add_months(cur, -1), add_months(cur, 1)

    def totals(d: date, platform: str = "ride") -> dict:
        return s.api("GET", settle_path(d, platform))["month_totals"]

    s.expect(key_texts(totals(prev)) != key_texts(totals(cur)), "the seed gives two months the same totals")
    s.tap('[aria-label="前一個月"]')
    s.eq((s.month_text(), s.keys_text()), (month_label(prev), key_texts(totals(prev))), "the keys after ←")
    s.tap('[aria-label="後一個月"]')
    s.eq((s.month_text(), s.keys_text()), (month_label(cur, now=True), key_texts(totals(cur))), "the keys after →")
    # The keys follow the scroll, as the month button does. The strip grows as
    # its end comes into reach, so the row may take more than one scroll.
    for _ in range(4):
        s.page.evaluate("""id => {
          const header = [...document.querySelectorAll('.header')].find(e => e.getClientRects().length)
            .getBoundingClientRect().bottom;
          window.scrollBy(0, document.getElementById(id).getBoundingClientRect().top - header + 8);
        }""", week_id(after + timedelta(days=7)))
        s.settle()
    s.eq((s.month_text(), s.keys_text()), (month_label(after), key_texts(totals(after))), "the keys after scrolling into the next month")
    s.tap(".date-btn")
    s.eq((s.month_text(), s.keys_text()), (month_label(cur, now=True), key_texts(totals(cur))), "the keys after the month button")
    # Another platform's month is another set of figures; the chosen key stays.
    s.tap('.lkey[data-lens="awaiting"]')
    s.tap(".tab", has_text="滴滴")
    s.expect(key_texts(totals(cur, "didi")) != key_texts(totals(cur)), "the seed gives two platforms the same totals")
    s.eq(s.keys_text(), key_texts(totals(cur, "didi")), "the keys after changing platform")
    s.eq(s.pressed(), ["awaiting"], "the chosen key after changing platform")
    s.tap('[aria-label="前一個月"]')
    s.eq(s.keys_text(), key_texts(totals(prev, "didi")), "the other platform's keys after ←")
    s.eq(s.pressed(), ["awaiting"], "the chosen key after changing month")
    s.eq(s.writes, [], "writes")


@check("settle.lens")
def settle_lens(s: Session) -> None:
    s.open_day()
    s.go_settle()
    s.eq(s.pressed(), ["fare"], "the chosen key on arrival")
    figures, asked = s.keys_text(), len(s.requests)
    for name, _ in TOTAL_KEYS[1:]:
        s.tap(f'.lkey[data-lens="{name}"]')
        s.eq(s.pressed(), [name], f"the chosen key after tapping {name}")
        s.eq(s.page.eval_on_selector_all("#settle-lens .lkey.on", "els => els.map(e => e.dataset.lens)"),
             [name], f"the key drawn as chosen after tapping {name}")
    s.eq(s.keys_text(), figures, "the figures after choosing keys")
    s.eq(s.asked(asked, "/api/"), [], "requests made by choosing a key")
    # The lens is where the operator was looking, not a setting: leaving the
    # view and coming back starts from the month's fare again.
    s.go_day()
    s.go_settle()
    s.eq(s.pressed(), ["fare"], "the chosen key on coming back")
    s.tap('.lkey[data-lens="unsettled"]')
    s.to_day()
    s.to_settle()
    s.eq(s.pressed(), ["fare"], "the chosen key on coming back through history")
    s.eq(s.writes, [], "writes")


# ---- settle view: the 未結算 lens ----

# Every day holding work, as the strip draws it now: its classes, its figure
# and the length the figure is sized by.
STRIP_SNAP_JS = """
() => Object.fromEntries([...document.querySelectorAll('#grid .cell[data-d]')].map(e => {
  const a = e.querySelector('.amt');
  return [e.dataset.d, [e.className, a.textContent, a.style.getPropertyValue('--n')]];
}))
"""


def cents(text: str) -> int:
    """A money figure as printed, in cents: a cell's bare figure or a key's
    with its $ and commas."""
    from decimal import Decimal
    return int(Decimal(text.replace("$", "").replace(",", "").replace("−", "-")) * 100)


def loose_days(body: dict) -> dict:
    """One month's answer from /api/settle -> {day: cents} for the days
    holding money no statement has claimed, counted as the page is meant to:
    orders already driven that are on no batch, each at what is owed for it."""
    from ride_dispatch.service import owed_of
    batched = {o["order_id"] for b in body["settlements"] for o in b["orders"]}
    out = {}
    for o in body["orders"]:
        if o["order_id"] in batched or o["scheduled_time"] >= body["now"]:
            continue
        day = o["scheduled_time"][:10]
        out[day] = out.get(day, 0) + round(owed_of(o) * 100)
    return {d: c for d, c in out.items() if c > 0}


def lens_strip(s: Session) -> dict:
    """The strip under a lens: day -> (lit, receded, printing its part, figure)."""
    return {d: ("lit" in cls.split(), "dim" in cls.split(), "part" in cls.split(), text)
            for d, (cls, text, _) in s.page.evaluate(STRIP_SNAP_JS).items()}


@check("settle.unsettled-lens")
def settle_unsettled_lens(s: Session) -> None:
    """Under 未結算 the days holding unclaimed money are lit and print that
    part; the lit figures of a month add up to the key to the cent; every
    other day recedes by colour and keeps its whole fare; choosing another
    key puts everything back."""
    from ride_dispatch.service import expected_of
    mixed_day, mixed_month = s.back(19), settle_path(s.back(19))
    moved = seed_demo_db._oid(302)

    def change(body: dict, path: str) -> None:
        # One of the two legs of a day on a statement awaiting its transfer
        # is taken off the statement, which makes the day a mixed one. The
        # month's totals are the server's, so the leg's fare is moved between
        # them the way the server would have counted it.
        fare = sum(expected_of(o) for o in body["orders"] if o["order_id"] == moved)
        for b in body["settlements"]:
            if b["id"] == s.t["batch"]["awaiting"]:
                b["orders"] = [x for x in b["orders"] if x["order_id"] != moved]
        if path == mixed_month and body.get("month_totals"):
            t = body["month_totals"]
            t.update(unsettled=round(t["unsettled"] + fare, 2), awaiting=round(t["awaiting"] - fare, 2))

    def served(d: date) -> dict:
        body = s.api("GET", settle_path(d))
        change(body, settle_path(d))
        return body

    def adds_up(what: str) -> None:
        """The lit figures of the month the header names against its key."""
        month = s.view_month()
        body = served(date.fromisoformat(month + "-01"))
        want = loose_days(body)
        strip = {d: v for d, v in lens_strip(s).items() if d.startswith(month)}
        lit = {d: cents(v[3]) for d, v in strip.items() if v[0]}
        key = cents(s.text('.lkey[data-lens="unsettled"] .v'))
        s.eq(lit, want, f"the days lit in {month} and what each prints, {what}")
        s.eq(sum(lit.values()), key, f"the lit figures of {month} against the 未結算 key, in cents, {what}")
        s.eq(key, round(body["month_totals"]["unsettled"] * 100), f"the key against the server's figure, {what}")
        s.expect(lit, f"no day is lit in {month}, {what}")
        s.expect(all(d <= s.day() for d in lit), f"a day still to come is lit, {what}: {sorted(lit)}")
        s.eq([d for d, v in strip.items() if v[0] == v[1] or v[2] != v[0]], [],
             f"days of {month} neither lit nor receded, or printing the wrong amount, {what}")

    rewrite_settle(s, change)
    s.open_settle()
    s.reach(s.cell(38))
    s.reach(s.cell(19))
    before = s.page.evaluate(STRIP_SNAP_JS)
    s.eq((s.cell_state(19), s.cell_state(18)), (("unsettled", "900"), ("awaiting", "390")),
         "the mixed day and its statement's other day under 本月車費")
    asked = len(s.requests)
    s.tap('.lkey[data-lens="unsettled"]')
    s.eq(s.pressed(), ["unsettled"], "the chosen key")
    s.eq(s.asked(asked, "/api/"), [], "requests made by choosing the lens")
    # The mixed day prints the part no statement has claimed, the 390 leg of
    # its 900; the day beside it, wholly on the statement, keeps its whole
    # fare (which happens to be 390 as well) and recedes.
    s.eq((s.cell_state(19), s.cell_state(18)), (("unsettled", "390"), ("awaiting", "390")),
         "the mixed day and its statement's other day under 未結算")
    adds_up("with the mixed day in view")
    strip = lens_strip(s)
    s.eq((strip[mixed_day.isoformat()][:3], strip[s.back(18).isoformat()][:3], strip[s.back(34).isoformat()][:3]),
         ((True, False, True), (False, True, False), (False, True, False)), "lit and receded: mixed, awaiting, collected")
    # A day still to be driven is counted nowhere, so it is never lit.
    s.eq(strip[s.day(1)], (False, True, False, "900"), "a day still to come under the lens")
    for scheme in SCHEMES:
        s.page.emulate_media(color_scheme=scheme)
        lit, waiting, done, ahead = (s.look(n) for n in (19, 18, 34, -1))
        s.expect(not any(x["faded"] for x in (lit, waiting, done, ahead)), f"a day is faded by opacity under the lens ({scheme})")
        s.eq((lit["ground"], lit["ink"], lit["weight"], lit["rule"][:3]),
             (s.token("--surface"), s.token("--amber"), "700", [24, 3, s.token("--amber")]), f"a lit day ({scheme})")
        s.eq((waiting["ink"], waiting["weight"], waiting["rule"][2], waiting["ground"]),
             (s.token("--text-3"), "400", s.token("--line"), CLEAR), f"a receded day awaiting its transfer ({scheme})")
        for name, got in (("collected", done), ("still to come", ahead)):
            s.eq((got["ink"], got["weight"], got["ground"]), (s.token("--text-3"), "400", CLEAR), f"a receded day {name} ({scheme})")
        for width in PHONE_WIDTHS:
            s.resize(width)
            cells_hold(s, f"under the lens at {width} ({scheme})")
        s.resize(390)
    s.page.emulate_media(color_scheme="dark")
    # The month button goes to the current month, which adds up on its own.
    s.tap(".date-btn")
    adds_up("on the current month")
    # A change made elsewhere repaints the strip with the lens still applied.
    s.api("PATCH", "/api/orders/" + seed_demo_db._oid(12), {"price": 401})
    s.wait(lambda: s.cell_state(1)[1] == "941", "the change made elsewhere, drawn under the lens")
    adds_up("after a live update")
    s.api("PATCH", "/api/orders/" + seed_demo_db._oid(12), {"price": 400})
    s.wait(lambda: s.cell_state(1)[1] == "940", "the change undone")
    # Another platform's strip is built from months that all arrive after the
    # lens was chosen, and so is this platform's on coming back to it.
    s.tap(".tab", has_text="滴滴")
    s.eq(s.pressed(), ["unsettled"], "the chosen key after changing platform")
    s.eq([d for d, v in lens_strip(s).items() if v[0] == v[1] or v[2] != v[0]], [], "another platform's days neither lit nor receded")
    s.tap(".tab", has_text="接送")
    s.reach(s.cell(38))
    s.eq(s.cell_state(38), ("unsettled", "450"), "a day of a month loaded under the lens")
    s.reach(s.cell(19))
    s.eq(s.cell_state(19), ("unsettled", "390"), "the mixed day after its month arrived under the lens")
    adds_up("on months that arrived after the lens was chosen")
    # Back on the whole fare, every figure and class is as it was.
    s.tap('.lkey[data-lens="fare"]')
    after = s.page.evaluate(STRIP_SNAP_JS)
    s.eq({d: after.get(d) for d in before}, before, "the strip after going back to 本月車費")
    s.eq((s.lit(), s.count("#grid .dim"), s.count("#grid .part")), ([], 0, 0), "lit, receded or part figures left behind")
    s.eq([w for w in s.writes if w[0] != "PATCH"], [], "writes other than the check's own")


@check("settle.unsettled-lens-and-focus-do-not-fight")
def settle_unsettled_lens_focus(s: Session) -> None:
    """The strip lights one set of days at a time: choosing 未結算 puts a
    focus down, and a focus put down from a sheet takes the lens back to the
    whole fare."""
    s.open_settle()
    relation = sorted(s.back(n).isoformat() for n in (27, 26, 25))
    s.focus_on("short", ".sheet.show .up-sec")
    s.eq((s.lit(), s.count(".foot .fline")), (relation, 1), "the focus and its line")
    usual = 3
    s.tap('.lkey[data-lens="unsettled"]')
    s.eq((s.pressed(), s.count(".foot .fline"), len(s.totals())), (["unsettled"], 0, usual), "the key and the foot after choosing 未結算 over a focus")
    strip = lens_strip(s)
    s.expect(not any(strip[d][0] for d in relation), "the statement's days are still lit under the lens")
    s.eq([d for d, v in strip.items() if v[0] == v[1] or v[2] != v[0]], [], "days neither lit nor receded under the lens")
    s.eq(s.cell_state(1), ("unsettled", "940"), "a day lit by the lens")
    s.expect(strip[s.back(1).isoformat()][0], "a day with unclaimed money is not lit")
    # A day still opens on one tap under the lens, and from its statement's
    # sheet the focus wins.
    s.focus_on("short", ".sheet.show .up-sec")
    s.eq((s.pressed(), s.lit(), s.count("#grid .part"), s.count(".foot .fline")), (["fare"], relation, 0, 1),
         "the key, the days lit and the figures after a focus over the lens")
    s.eq(s.cell_state(26), ("short", "840"), "a focused day's figure")
    # A tap on empty calendar under the lens leaves the lens as it is.
    s.tap(".foot [data-unfocus]")
    s.tap('.lkey[data-lens="unsettled"]')
    s.page.touchscreen.tap(*empty_day(s))
    s.settle()
    s.eq((s.pressed(), s.cell_state(1)), (["unsettled"], ("unsettled", "940")), "the lens after a tap on empty calendar")
    s.expect(lens_strip(s)[s.back(1).isoformat()][0], "the lens's days went out on a tap on empty calendar")
    s.eq(s.writes, [], "writes")


@check("settle.unsettled-lens-under-stress")
def settle_unsettled_lens_stress(s: Session) -> None:
    """A five-digit unclaimed figure with cents, lit: set smaller, whole,
    inside its column at both phone widths."""
    stress_strip(s.ctx, s.today)
    s.open_settle()
    s.reach(s.cell(27))
    s.tap('.lkey[data-lens="unsettled"]')
    s.eq((s.cell_state(2), lens_strip(s)[s.back(2).isoformat()][:3]), (("unsettled", "12345.67"), (True, False, True)),
         "a five-digit unclaimed figure with cents, lit")
    for width in PHONE_WIDTHS:
        s.resize(width)
        cells_hold(s, f"under the lens and stress at {width}")
        big = s.look(2)
        s.expect(6 <= big["size"] < 13 and round(big["cents"], 2) == 0.7, f"the long lit figure at {width}: {big}")
    s.eq(s.writes, [], "writes")


# ---- settle view: the statement lists ----

# Every row of the list on screen: what it says, part by part, and what a
# layout check needs of it.
LIST_JS = """
() => [...document.querySelectorAll('#settle-list .brow')].map(r => {
  const t = sel => [...r.querySelectorAll(sel)].map(e => e.textContent);
  const cs = e => getComputedStyle(e);
  const box = r.getBoundingClientRect();
  const name = r.querySelector('.bt'), fig = r.querySelector('.ba'), tag = r.querySelector('.btag');
  return {
    id: r.dataset.bl ? +r.dataset.bl : null, tag: r.tagName, name: name.textContent, sub: t('.bsub'),
    amount: fig ? fig.textContent : '', month: t('.bmon')[0] || '', gap: t('.bgap')[0] || '', tags: t('.btag'),
    mark: t('.bc')[0], receded: r.classList.contains('rec'), height: box.height,
    ink: [cs(name).color, cs(name).fontWeight], figure: fig ? [cs(fig).color, cs(fig).fontWeight] : null,
    tagInk: tag ? cs(tag).color : null, gapInk: r.querySelector('.bgap') ? cs(r.querySelector('.bgap')).color : null,
    lined: [r, ...r.querySelectorAll('*')].some(e => cs(e).textDecorationLine !== 'none'),
    inside: [...r.querySelectorAll('.pt, .bt, .ba, .bmon, .bgap, .btag, .bc')].every(e => {
      const b = e.getBoundingClientRect();
      return b.left >= box.left - 0.5 && b.right <= box.right + 0.5;
    }),
  };
})
"""


def money2(n: float) -> str:
    """Money as the list prints it: $, thousands comma, cents always."""
    return ("−" if n < 0 else "") + f"${abs(n):,.2f}"


def batch_days(b: dict) -> list:
    """The days a statement covers: its legs', and those of the 舉牌 lines it
    paid ahead of a held-back trip."""
    days = {o["scheduled_time"][:10] for o in b["orders"]}
    days |= {a["date"] for a in b["adjustments"] if a.get("ahead")}
    return sorted(date.fromisoformat(d) for d in days)


def month_part(b: dict, month: str) -> int:
    """What one month's totals count of a statement, in cents: each of its
    legs scheduled in the month at owed_of, plus each 舉牌 line it paid ahead
    that is dated in the month."""
    from ride_dispatch.service import owed_of
    legs = sum(round(owed_of(o) * 100) for o in b["orders"] if o["scheduled_time"][:7] == month)
    return legs + sum(round(a["amount"] * 100) for a in b["adjustments"] if a.get("ahead") and a["date"][:7] == month)


def fare_gap(b: dict) -> int:
    """A statement's confirmed figure less its legs (owed_of) and its own
    lines, in cents."""
    from ride_dispatch.service import owed_of
    held = sum(round(owed_of(o) * 100) for o in b["orders"]) + sum(round(a["amount"] * 100) for a in b["adjustments"])
    return round(b["confirmed_amount"] * 100) - held


def listed(batches: list, lens: str, month: str) -> list:
    """The statements a list shows for a month, in the order it shows them."""
    states = ("awaiting",) if lens == "awaiting" else ("paid", "partial")
    rows = [b for b in batches if b["state"] in states and any(month_key(d) == month for d in batch_days(b))]
    if lens == "awaiting":
        # Longest wait first; a statement with no date last. The dates are
        # ISO strings, so the earliest date is the longest wait.
        return sorted(rows, key=lambda b: (b["settled_on"] is None, b["settled_on"] or "", b["id"]))
    rows.sort(key=lambda b: (b["settled_on"] or "", b["id"]), reverse=True)
    return sorted(rows, key=lambda b: b["state"] != "partial")


def row_text(b: dict, lens: str, month: str, batches: list, today: date) -> dict:
    """What a statement's row says, part by part."""
    same = sorted(x["id"] for x in batches if x["settled_on"] == b["settled_on"])
    name = (md_slash(date.fromisoformat(b["settled_on"])) + " " if b["settled_on"] else "") + "結算"
    name += f" ({same.index(b['id']) + 1})" if same.index(b["id"]) else ""
    days = batch_days(b)
    sub = [f"{day_runs(days, month)} · {len(b['orders'])} 程"]
    if lens == "awaiting":
        amount = b["confirmed_amount"]
        tags = [f"等咗 {max(0, (today - date.fromisoformat(b['settled_on'])).days)} 日"] if b["settled_on"] else []
    else:
        amount = b["received"]
        if b["state"] == "partial":
            sub.append("應收 " + money2(b["confirmed_amount"]))
        sub += [f"入數 {md_slash(date.fromisoformat(a['value_date']))} · {money2(a['amount'])}" for a in b["allocations"]]
        tags = ["仲差 " + money2(b["outstanding"])] if b["state"] == "partial" else ["已收齊"]
    gap = fare_gap(b)
    return {
        "id": b["id"], "name": name, "sub": sub, "amount": money2(amount), "tags": tags,
        "month": "其中本月 " + money2(month_part(b, month) / 100) if any(month_key(d) != month for d in days) else "",
        "gap": "同車費差 " + money2(gap / 100) if gap else "",
        "receded": b["state"] == "paid",
    }


LIST_HEAD = {"awaiting": "未過數", "received": "已過數"}
LIST_EMPTY = {"awaiting": "今個月冇等過數嘅結算單", "received": "今個月未有入數"}


def statements(rows: list) -> list:
    """The rows of the list that are statements, as row_text words them."""
    keep = ("id", "name", "sub", "amount", "tags", "month", "gap", "receded")
    return [{k: r[k] for k in keep} for r in rows if r["id"] is not None]


def list_matches(s: Session, lens: str, books: dict, batches: list, what: str) -> list:
    """The list on screen against the payload of the month the header names:
    its head, its rows in order, and its wording when it has none. Returns
    the statements it should hold."""
    month = s.view_month()
    want = listed(batches, lens, month)
    s.eq(statements(s.list_rows()), [row_text(b, lens, month, batches, s.today) for b in want],
         f"the {lens} list of {month}, {what}")
    s.eq(s.texts(".header .lhead span"), [f"結算單 {len(want)} 張", str(len(want)), LIST_HEAD[lens]],
         f"the head of the {lens} list of {month}, {what}")
    s.eq(s.texts("#settle-list .empty"), [] if want else [LIST_EMPTY[lens]], f"the wording of an empty {lens} list of {month}, {what}")
    s.eq(s.keys_text(), key_texts(books[month]["month_totals"]), f"the keys over the {lens} list of {month}, {what}")
    return want


@check("settle.statement-lists")
def settle_lists(s: Session) -> None:
    """等過數 and 已收 open a list of statements in the strip's place. For
    each of the three months the seed reaches, the rows are the statements
    the payload holds for that month, in order, and what the month's total
    counts of them adds up to the key over the list."""
    cur = s.today.replace(day=1)
    months = [cur, add_months(cur, -1), add_months(cur, -2)]
    books = {month_key(m): s.api("GET", settle_path(m)) for m in months}
    batches = list({b["id"]: b for body in books.values() for b in body["settlements"]}.values())
    b, c = s.t["batch"], s.t["credit"]
    s.open_settle()
    seen = {"awaiting": [], "received": []}
    for lens in ("awaiting", "received"):
        s.tap(f'.lkey[data-lens="{lens}"]')
        s.eq(s.pressed(), [lens], "the chosen key")
        s.eq((s.count(".cal"), s.count(".header .wk"), s.count("#settle-list"), s.count(".header .lhead")),
             (0, 0, 1, 1), f"the strip and its head give way to the {lens} list and its head")
        s.eq((s.colour(".header .lhead", "backgroundColor"), s.colour(".lkey.on", "backgroundColor")),
             (s.token("--surface"), s.token("--surface")), "the list's head on the chosen key's ground")
        for m in months:
            s.to_month(m)
            month = month_key(m)
            s.eq(s.month_text(), month_label(m, now=m == cur), f"the month button under the {lens} list")
            want = list_matches(s, lens, books, batches, "paged to by the arrows")
            seen[lens] += [x["id"] for x in want]
            # What is summed, in cents: for every statement row on screen, the
            # batch it opens is taken from the payload and counted at
            # month_part(batch, month), which is owed_of over its legs
            # scheduled in the month plus the 舉牌 lines it paid ahead that
            # are dated in the month. Under 等過數 that sum is the key. Under
            # 已收 the rows also hold what a statement paid short is still
            # owed, which the server counts as short and not as received, so
            # the month's month_totals.short comes off the sum first.
            totals = books[month]["month_totals"]
            parts = sum(month_part(x, month) for x in want)
            short = round(totals["short"] * 100) if lens == "received" else 0
            key = cents(s.text(f'.lkey[data-lens="{lens}"] .v'))
            s.eq(parts - short, key, f"the rows' parts of {month} against the {lens} key, in cents")
            s.eq(key, round(totals[lens] * 100), f"the {lens} key of {month} against the server's figure")
        s.tap(".date-btn")
        s.eq(s.view_month(), month_key(cur), "the month button takes a list to the current month")
        list_matches(s, lens, books, batches, "reached by the month button")
    # Every seeded statement was listed, each under the key its state belongs
    # to: the one paid short is with the money that came, not with the waiting.
    s.eq((sorted(set(seen["awaiting"])), sorted(set(seen["received"]))),
         (sorted([b["awaiting"], b["ahead"], b["group"]]), sorted([b["paid"], b["short"], b["held_back"]])),
         "the seeded statements under each key")

    # The month of the statement paid short, under 已收: it leads, it is not
    # receded, its figure is what arrived, and what is still owed is in amber.
    s.tap('.lkey[data-lens="received"]')
    s.to_month(s.back(26))
    rows = s.list_rows()
    first = rows[0]
    s.eq((first["id"], first["receded"], first["amount"], first["tags"], first["sub"][1:]),
         (b["short"], False, "$1,930.00", ["仲差 $380.00"], ["應收 $2,310.00", f"入數 {md_slash(s.back(21))} · $1,930.00"]),
         "the statement paid short, leading the 已收 list")
    for scheme in SCHEMES:
        s.page.emulate_media(color_scheme=scheme)
        text, text2, amber, green = (s.token(n) for n in ("--text", "--text-2", "--amber", "--green"))
        rows = s.list_rows()
        s.eq((rows[0]["ink"], rows[0]["figure"], rows[0]["tagInk"]), ([text, "700"], [text, "700"], amber),
             f"a statement paid short ({scheme})")
        done = [r for r in rows if r["receded"] and r["id"] is not None]
        s.expect(done, "no collected statement in the month of the one paid short")
        s.eq({(tuple(r["ink"]), tuple(r["figure"]), r["tagInk"]) for r in done}, {((text2, "400"), (text2, "400"), green)},
             f"a collected statement recedes by weight and ink ({scheme})")
    s.page.emulate_media(color_scheme="dark")
    s.tap('.lkey[data-lens="awaiting"]')
    s.expect(b["short"] not in [r["id"] for r in s.list_rows()], "the statement paid short is under 等過數")

    # The month of the statement confirmed for 20 less than its fares: it says
    # so, in the ink of a note. The statement beside it carries a 舉牌 line of
    # its own on top of its legs and agrees with the book, so it says nothing;
    # the two confirmed on one date are told apart by a number.
    s.to_month(s.back(19))
    by = {r["id"]: r for r in s.list_rows()}
    s.eq((by[b["awaiting"]]["amount"], by[b["awaiting"]]["gap"], by[b["awaiting"]]["gapInk"]),
         ("$1,270.00", "同車費差 −$20.00", s.token("--text-2")), "a statement confirmed for less than its fares")
    s.to_month(s.back(6))
    by = {r["id"]: r for r in s.list_rows()}
    s.eq((by[b["ahead"]]["amount"], by[b["ahead"]]["gap"]), ("$1,425.00", ""), "a statement that is its legs and its own line")
    s.to_month(s.back(8))
    by = {r["id"]: r for r in s.list_rows()}
    s.eq((by[b["group"]]["name"], by[b["group"]]["gap"]), (f"{md_slash(s.back(2))} 結算 (2)", ""),
         "the second statement confirmed on one date")

    s.eq(s.writes, [], "writes")


@check("settle.statement-list-rows-hold-at-phone-widths")
def settle_list_layout(s: Session) -> None:
    """Both lists, in both palettes, at both phone widths: every row a
    button a thumb can take, ending in its mark, nothing underlined, nothing
    past the row's edge and the page not scrolling sideways."""
    s.open_settle()
    for lens, back in (("awaiting", 19), ("received", 26)):
        s.tap(f'.lkey[data-lens="{lens}"]')
        s.to_month(s.back(back))
        for scheme in SCHEMES:
            s.page.emulate_media(color_scheme=scheme)
            for width in PHONE_WIDTHS:
                s.resize(width)
                rows = s.list_rows()
                where = f"in the {lens} list at {width} ({scheme})"
                s.expect(rows, f"no rows to judge {where}")
                s.eq(({r["tag"] for r in rows}, {r["mark"] for r in rows}), ({"BUTTON"}, {"›"}), f"what a row is and ends in {where}")
                s.expect(all(r["height"] >= 44 for r in rows), f"a row under 44px {where}: {[r['height'] for r in rows]}")
                s.expect(not any(r["lined"] for r in rows), f"something underlined {where}")
                s.expect(all(r["inside"] for r in rows), f"a part of a row runs past the row {where}")
                s.expect(not s.page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth"),
                         f"the page scrolls sideways {where}")
                s.expect(not s.page.evaluate("() => [...document.querySelectorAll('.header .lhead, .header .lhead *')]"
                                             ".some(e => getComputedStyle(e).textDecorationLine !== 'none')"),
                         f"the list's head is underlined {where}")
            s.resize(390)
        s.page.emulate_media(color_scheme="dark")
    s.eq(s.writes, [], "writes")


@check("settle.a-statement-across-two-months-says-its-part-of-each")
def settle_list_straddle(s: Session) -> None:
    """A collected statement with one leg in the month before its others:
    listed in both months, its days written with their months, and in each
    its part of that month."""
    kept = seed_demo_db._oid(402)
    moved = seed_demo_db._oid(401)
    # The month the statement's other legs begin in, and a day three days
    # before that month.
    home = s.back(12).replace(day=1)
    early = home - timedelta(days=3)

    def change(body: dict, path: str) -> None:
        for x in body["settlements"]:
            if x["id"] == s.t["batch"]["held_back"]:
                for o in x["orders"]:
                    if o["order_id"] == moved:
                        o["scheduled_time"] = early.isoformat() + o["scheduled_time"][10:]

    def served(d: date) -> dict:
        body = s.api("GET", settle_path(d))
        change(body, settle_path(d))
        return body

    rewrite_settle(s, change)
    s.open_settle()
    books = {month_key(m): served(m) for m in (home, early, add_months(home, 1), s.today)}
    batches = list({x["id"]: x for body in books.values() for x in body["settlements"]}.values())
    batch = [x for x in batches if x["id"] == s.t["batch"]["held_back"]][0]
    s.expect(kept in [o["order_id"] for o in batch["orders"]], "the seeded statement lost a leg")
    s.tap('.lkey[data-lens="received"]')
    for m in (home, early):
        s.to_month(m)
        month = month_key(m)
        row = [r for r in s.list_rows() if r["id"] == batch["id"]]
        s.expect(row, f"the statement is not listed in {month}")
        want = row_text(batch, "received", month, batches, s.today)
        s.eq(statements(row), [want], f"the statement's row in {month}")
        s.expect(want["month"].startswith("其中本月 $") and "/" in want["sub"][0], f"the check expected no part and bare days: {want}")
        s.eq(cents(row[0]["month"].replace("其中本月 ", "")), month_part(batch, month), f"its part of {month}, in cents")
    s.eq(month_part(batch, month_key(early)), 44000, "the part of the month holding the one leg moved there")
    s.eq(s.writes, [], "writes")


@check("settle.a-statement-without-its-date-waits-last")
def settle_list_undated(s: Session) -> None:
    """A statement stored without its date has waited an unknown time: its
    row says nothing of a wait and stands after every row that does."""
    # A month holding two waiting statements or more, and of those the one
    # that has waited longest, which its date would have put first. The
    # seeded statements lie close enough together that some month always
    # holds two.
    cur = s.today.replace(day=1)
    held = {}
    for m in (cur, add_months(cur, -1), add_months(cur, -2)):
        held[m] = listed(s.api("GET", settle_path(m))["settlements"], "awaiting", month_key(m))
    month = [m for m in held if len(held[m]) >= 2][0]
    undated = held[month][0]["id"]

    def change(body: dict, path: str) -> None:
        for x in body["settlements"]:
            if x["id"] == undated:
                x["settled_on"] = None

    rewrite_settle(s, change)
    s.open_settle()
    s.tap('.lkey[data-lens="awaiting"]')
    s.to_month(month)
    rows = s.list_rows()
    s.eq(len(rows), len(held[month]), "the waiting statements of the month")
    s.eq((rows[-1]["id"], rows[-1]["name"], rows[-1]["tags"]), (undated, "結算", []), "the undated statement")
    s.expect(all(len(r["tags"]) == 1 and r["tags"][0].startswith("等咗 ") for r in rows[:-1]), f"the dated ones say their wait: {rows[:-1]}")
    waits = [int(r["tags"][0].split()[1]) for r in rows[:-1]]
    s.eq(waits, sorted(waits, reverse=True), "longest wait first")
    s.eq(s.writes, [], "writes")


@check("settle.the-list-follows-month-platform-and-live-changes")
def settle_list_follows(s: Session) -> None:
    """Under a list the month is changed by the arrows and the month button
    alone; the strip keeps its months and its place and is back where it was,
    or on the month the list was paged to; platform and live changes repaint
    the list with the lens kept."""
    cur = s.today.replace(day=1)
    prev = add_months(cur, -1)
    b, c = s.t["batch"], s.t["credit"]
    s.open_settle()
    # A place in the strip that is not a month's first row.
    s.reach(s.cell(26))
    s.to_top(s.cell(26))
    at, top, month, weeks = s.scroll_y(), s.top_week(), s.view_month(), s.weeks()
    asked = len(s.requests)
    s.tap('.lkey[data-lens="awaiting"]')
    s.eq((s.count(".cal"), s.view_month(), s.scroll_y()), (0, month, 0), "the list opens on the month the strip showed, from its top")
    # The hidden strip no longer names the month: scrolling the page moves nothing.
    s.page.evaluate("() => window.scrollBy(0, 400)")
    s.never(lambda: s.view_month() != month, "the month changed under a list by scrolling", 500)
    s.tap('.lkey[data-lens="received"]')
    s.tap('.lkey[data-lens="fare"]')
    s.eq((s.count(".cal"), s.count("#settle-list"), s.top_week(), s.scroll_y(), s.view_month(), s.weeks()),
         (1, 0, top, at, month, weeks), "the strip back where it was left")
    s.eq(s.asked(asked, "/api/"), [], "requests made by going to a list and back")
    # Paged to another month under the list, the strip comes back on that month.
    s.tap('.lkey[data-lens="received"]')
    other = add_months(date.fromisoformat(month + "-01"), 1 if month != month_key(cur) else -1)
    s.to_month(other)
    s.eq(s.pressed(), ["received"], "the chosen key after paging")
    s.tap('.lkey[data-lens="unsettled"]')
    s.eq((s.count(".cal"), s.top_week(), s.view_month()), (1, week_id(other), month_key(other)),
         "the strip on the month the list was paged to")
    # A month the strip does not hold is loaded before the list is drawn for it.
    s.tap('.lkey[data-lens="received"]')
    far = date.fromisoformat(s.weeks()[0][2:]) + timedelta(days=6)
    far = add_months(far.replace(day=1), -1)
    asked = len(s.requests)
    s.to_month(far)
    s.expect(settle_path(far) in s.asked(asked), "the month paged to was never asked for")
    body = s.api("GET", settle_path(far))
    want = [x["id"] for x in listed(body["settlements"], "received", month_key(far))]
    s.eq((s.keys_text(), [r["id"] for r in s.list_rows() if r["id"] is not None], s.texts("#settle-list .empty")),
         (key_texts(body["month_totals"]), want, [] if want else ["今個月未有入數"]), "the list of a month loaded under it")
    # A focus is put down by choosing a list, and its line leaves the foot.
    s.tap('.lkey[data-lens="fare"]')
    s.eq((s.top_week(), s.view_month()), (week_id(far), month_key(far)), "the strip on a month loaded under the list")
    s.reach(s.cell(26))
    s.to_top(s.cell(26))
    s.focus_on("short", ".sheet.show .up-sec")
    s.eq((len(s.lit()), s.count(".foot .fline")), (3, 1), "a focus and its line")
    s.tap('.lkey[data-lens="awaiting"]')
    s.eq(s.count(".foot .fline"), 0, "the focus line under a list")
    s.tap('.lkey[data-lens="fare"]')
    s.eq((s.lit(), s.count("#grid .dim")), ([], 0), "the focus after a list was chosen")
    # A change made elsewhere repaints the list: the waiting statement is
    # paid, and moves from one list to the other.
    s.tap('.lkey[data-lens="awaiting"]')
    s.to_month(s.back(19))
    s.expect(b["awaiting"] in [r["id"] for r in s.list_rows()], "the waiting statement is not listed")
    s.api("POST", f"/api/credits/{c['exact']}/allocate", {"settlement_id": b["awaiting"]})
    s.wait(lambda: b["awaiting"] not in [r["id"] for r in s.list_rows()], "the list to follow a change made elsewhere")
    s.eq((s.pressed(), s.count(".cal")), (["awaiting"], 0), "the lens after a live update")
    s.eq(s.keys_text(), key_texts(s.api("GET", settle_path(s.back(19)))["month_totals"]), "the keys after a live update")
    s.tap('.lkey[data-lens="received"]')
    paid = [r for r in s.list_rows() if r["id"] == b["awaiting"]]
    s.eq((paid[0]["tags"], paid[0]["sub"][-1]), (["已收齊"], f"入數 {md_slash(s.back(14))} · $1,270.00"), "the statement, collected, under 已收")
    # Another platform: the lens stays, the list is that platform's and names
    # the current month; with no statements it says so in words.
    s.tap(".tab", has_text="滴滴")
    s.eq((s.pressed(), s.count(".cal"), s.count("#settle-list"), s.month_text()),
         (["received"], 0, 1, month_label(cur, now=True)), "the lens and the month after changing platform")
    s.eq((s.list_rows(), s.texts("#settle-list .empty"), s.texts(".header .lhead span")[0]),
         ([], ["今個月未有入數"], "結算單 0 張"), "another platform's list")
    s.eq(s.keys_text(), key_texts(s.api("GET", settle_path(cur, "didi"))["month_totals"]), "another platform's keys")
    s.tap('.lkey[data-lens="awaiting"]')
    s.eq(s.texts("#settle-list .empty"), ["今個月冇等過數嘅結算單"], "another platform's waiting list")
    s.tap('[aria-label="前一個月"]')
    s.eq((s.month_text(), s.pressed()), (month_label(prev), ["awaiting"]), "paging another platform's list")
    s.tap('.lkey[data-lens="fare"]')
    s.eq((s.top_week(), s.month_text()), (week_id(prev), month_label(prev)), "another platform's strip on the month paged to")
    s.eq(s.writes, [], "writes by the page")


@check("settle.total-keys-without-a-figure")
def settle_total_keys_unknown(s: Session) -> None:
    """A month not loaded yet and a month whose totals the server withheld
    both show a dash in every key, never a zero, and the header keeps its
    height."""
    cur = s.today.replace(day=1)
    prev = add_months(cur, -1)

    def withhold(body: dict, path: str) -> None:
        if path == settle_path(prev):
            body["month_totals"] = None

    rewrite_settle(s, withhold)
    dashes = key_texts(None)
    # Before the first answer: the keys are there, each holding a dash.
    s.page = s.ctx.new_page()
    s.page.set_default_timeout(TIMEOUT_MS)
    s._watch(s.page)
    s.hold(settle_path(cur))
    s.page.goto(s.base + "/settle")
    s.wait(lambda: s.holding(settle_path(cur)), "the first request for the month")
    s.eq(s.keys_text(), dashes, "the keys before the month has loaded")
    s.eq(s.pressed(), ["fare"], "the chosen key before the month has loaded")
    height = s.header_height()
    s.release_all()
    s.on(".cell[data-d]").first.wait_for()
    s.settle()
    s.eq(s.keys_text(), key_texts(s.api("GET", settle_path(cur))["month_totals"]), "the keys once the month has loaded")
    s.eq(s.header_height(), height, "the header's height, loaded against not loaded")
    # A month whose split the server could not state exactly.
    s.tap('[aria-label="前一個月"]')
    s.eq((s.month_text(), s.keys_text()), (month_label(prev), dashes), "the keys of a month with no totals")
    s.eq(s.header_height(), height, "the header's height on a month with no totals")
    keys = s.page.evaluate(KEYS_JS)
    s.eq({k["rule"][1] for k in keys}, {CLEAR}, "state rules under a dash")
    s.expect(s.token("--amber") not in [k["ink"] for k in keys], "a dash in amber")
    # The keys can still be chosen.
    s.tap('.lkey[data-lens="unsettled"]')
    s.eq(s.pressed(), ["unsettled"], "the chosen key on a month with no totals")
    s.tap('[aria-label="後一個月"]')
    s.eq(s.keys_text(), key_texts(s.api("GET", settle_path(cur))["month_totals"]), "the keys back on a month with totals")
    s.eq(s.writes, [], "writes")


@check("settle.total-keys-hold-on-a-narrow-screen")
def settle_total_keys_narrow(s: Session) -> None:
    """Five-digit figures with cents in all four keys: they are set smaller
    together and stay whole on one line, and the header is no wider than the
    screen."""
    big = {name: 19103.5 for name, _ in TOTAL_KEYS}

    def swell(body: dict, path: str) -> None:
        if body.get("month_totals") is not None:
            body["month_totals"].update(big)

    rewrite_settle(s, swell)
    s.open_settle()
    for width in (390, 340):
        s.resize(width)
        s.eq(s.keys_text(), key_texts(big), f"the figures at {width}")
        keys = s.page.evaluate(KEYS_JS)
        wide = s.page.evaluate("""() => {
          const seen = q => [...document.querySelectorAll(q)].find(e => e.getClientRects().length);
          const h = seen('.header'), l = seen('.lens');
          const last = l.lastElementChild.getBoundingClientRect();
          return [h.scrollWidth > h.clientWidth, l.scrollWidth > l.clientWidth,
                  document.documentElement.scrollWidth > window.innerWidth, last.right > window.innerWidth + 0.5];
        }""")
        s.eq(wide, [False, False, False, False], f"header, key row, document or last key wider than the screen at {width}")
        s.expect(all(k["inside"] for k in keys), f"a figure runs out of its key at {width}")
        s.eq(len({k["figTop"] for k in keys}), 1, f"lines the figures stand on at {width}")
        s.eq({k["figHeight"] for k in keys}, {20}, f"a figure wrapped at {width}")
        s.eq(len({k["size"] for k in keys}), 1, f"sizes the figures are set in at {width}")
        s.expect(all(k["height"] >= 44 for k in keys), f"a key under 44px at {width}")
    s.expect(keys[0]["size"] < 15, f"the figures were not set smaller at 340: {keys[0]['size']}")
    s.eq(s.writes, [], "writes")


@check("settle.upload-key-is-the-primary-key")
def settle_upload_key(s: Session) -> None:
    def key(selector: str) -> list:
        return [s.colour(selector, prop) for prop in ("backgroundColor", "color", "borderTopColor")]

    s.open_settle()
    solid = [s.token("--text"), s.token("--bg"), s.token("--text")]
    s.eq(key("#stmtBtn"), solid, "the upload key: ground, ink, edge")
    s.eq(s.text("#stmtBtn"), "圖", "the upload key's face")
    s.eq(s.count(".header .stmt-btn"), 1, "solid keys in the settle header")
    s.eq(key('[aria-label="前一個月"]')[0], CLEAR, "the ground of a key beside it")
    # The same reversal the day board gives its own primary key.
    s.go_day()
    s.eq(key(".add-btn"), solid, "the day board's + key")
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
    s.tap(".foot [data-credits]")
    s.eq(s.count(".sheet.show .sheet-back"), 0, "a back button on the queue opened from the foot")
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
    s.open_batch("paid")
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
    s.eq(s.texts(".sheet.show .orow .oend"), ["$476.55判罰 −$63.45", "$420", "$480"], "order figures")
    # The fold survives a change made elsewhere.
    s.api("PATCH", "/api/orders/" + o(12), {"price": 401})
    s.wait(lambda: s.cell_state(1)[1] == "941", "the change made elsewhere")
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
    s.open_batch("held_back")
    s.eq(s.title(), "結算 " + span_label(s.back(12), s.back(11)), "title of a batch with a held-back leg")
    s.expect(s.sub().endswith(f" · 連 {s.back(15).day}日 1 程"), f"held-back run in {s.sub()!r}")
    s.eq(s.writes, [], "writes")


@check("settle.credit-and-queue-sheets")
def settle_credit_sheets(s: Session) -> None:
    s.open_settle()
    c = s.t["credit"]
    s.tap(".foot [data-credits]")
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
    s.open_credit("group")
    s.eq(s.texts(".sheet.show .hero > div"), ["到帳", "$2870", "未對"], "hero of an unmatched credit")
    s.eq(s.texts(".sheet.show .prow")[0], f"2 個批次 · {group} · $2870啱數對晒", "the group row")
    s.eq(s.count(".sheet.show .prow .ptag"), 1, "啱數 tags: the group's, not its batches' own")
    s.close_sheets()
    # A credit paid into a batch that is still short, reached from that batch.
    s.open_batch("short", ".sheet.show .up-sec")
    s.tap(".sheet.show .alloc [data-credit]")
    s.eq(s.title(), "入數 " + md_label(s.back(21)), "the credit opened from the batch it paid")
    s.eq(s.texts(".sheet.show .hero > div")[2], "已對", "hero of a matched credit")
    s.eq(s.texts(".sheet.show .sub-note"), ["批次仲差 $380"], "the batch it left short")
    s.eq(s.writes, [], "writes")


@check("settle.allocate")
def settle_allocate(s: Session) -> None:
    s.open_settle()
    b, c = s.t["batch"], s.t["credit"]
    s.tap(".foot [data-credits]")
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
    s.eq(s.totals()[-1], "入數未對 2 筆$3170", "totals after 對")
    s.reach(s.cell(19))
    s.eq((s.cell_state(19), s.cell_state(18)), (("received", "900"), ("received", "390")), "the days of the batch just paid")
    s.page.wait_for_timeout(2500)
    # From the short-paid batch's own sheet, with money that does not cover it.
    s.open_batch("short")
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
    s.open_credit("group")
    s.press(".sheet.show .pbtn[data-alloc-all]")
    s.wait_toast("已對 2 個批次 · $2870 · 收齊")
    s.eq(s.writes[-1], ("POST", f"/api/credits/{c['group']}/allocate-all",
                        json.dumps({"settlement_ids": [b["ahead"], b["group"]]}, separators=(",", ":"))), "allocate-all write")
    s.settle()
    s.eq(s.texts(".sheet.show .hero > div")[2], "已對", "the credit after 對晒")
    s.eq(s.count(".sheet.show .prow"), 0, "proposals on a matched credit")
    s.eq(len(s.texts(".sheet.show .sum-row.link")), 2, "the batches it paid")
    s.close_sheets()
    s.reach(s.cell(9))
    s.eq([s.cell_state(n)[0] for n in (9, 8, 6, 5)], ["received"] * 4, "the days of both batches")
    s.eq(len(s.writes), 1, "writes")


@check("settle.unlink")
def settle_unlink(s: Session) -> None:
    s.open_settle()
    b, c = s.t["batch"], s.t["credit"]
    s.open_batch("held_back")
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
    s.eq(s.totals()[-1], "入數未對 3 筆$6220", "totals after 解除")
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
    s.eq((s.cell_state(19), s.cell_state(18)), (("unsettled", "900"), ("unsettled", "390")), "the days of the undone batch")
    s.eq(len(s.writes), 1, "writes")


@check("settle.unpaid-ticks")
def settle_ticks(s: Session) -> None:
    s.open_settle()
    o = seed_demo_db._oid
    s.open_batch("short", ".sheet.show .up-sec")
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
    s.tap(".foot [data-credits]")
    s.press(".sheet.show .qprop .pbtn", has_text="對")
    s.wait_toast("拒絕對")
    s.press(".sheet.show .pbtn[data-alloc-all]")
    s.wait_toast("拒絕對晒")
    s.eq(s.count(".sheet.show .qrow"), 3, "queue rows after two refusals")
    s.close_sheets()
    s.open_batch("held_back")
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
    s.open_batch("short", ".sheet.show .up-sec")
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
    s.eq((s.title(), s.sub()), ("CX488 11:30", f"#000203 · {md_label(day)} 星期{WEEKDAY[day.weekday()]}"), "order sheet")
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
    s.eq(s.title(), "CX488 11:30", "‹ on the numpad")
    s.edit("停車費", "20")
    s.eq(s.last_write(), ("PATCH", path, {"parking_fee": 20}), "parking fee write")
    s.eq((s.title(), s.field("停車費")), ("CX488 11:30", "$20"), "the order after the save")
    # Its batch opens on top of it.
    s.tap(".sheet.show .info-link")
    s.eq(s.title(), "結算 " + span_label(s.back(27), s.back(25)), "the batch opened from the order")
    s.tap(".sheet.show .sheet-back")
    s.eq(s.title(), "CX488 11:30", "back on the order")
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
    s.eq(s.cell_state(1)[1], "1040", "the day's figure behind the sheet")
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


@check("settle.failed-order-refetch-is-shown")
def settle_order_refetch(s: Session) -> None:
    """An open order is fetched again with every reload. When that fetch
    fails the sheet must say so: the order it would otherwise go on showing
    is the one from before the change that caused the reload."""
    s.open_settle()
    o = seed_demo_db._oid
    s.open_cell(1)
    s.open_leg(o(12))
    one = "**/api/orders/" + o(12)
    s.allow("request failed: GET /api/orders/" + o(12), "Failed to load resource")
    s.page.route(one, lambda route: route.abort())
    s.api("PATCH", "/api/orders/" + o(11), {"price": 401})
    s.wait(lambda: s.cell_state(1)[1] == "841", "the change made elsewhere")
    s.wait(lambda: s.count(".sheet.show .order-err"), "the failed re-fetch to be shown in the sheet")
    s.eq((s.title(), s.text(".sheet.show .order-err")), ("單 …0012", "讀唔到"), "the sheet of an order that could not be read again")
    s.eq(s.count(".sheet.show .field-row"), 0, "fields of the order as it was before the reload")
    # The next reload reads it again.
    s.page.unroute(one)
    s.api("PATCH", "/api/orders/" + o(11), {"price": 402})
    s.wait(lambda: s.count(".sheet.show .field-row"), "the order to come back with the next reload")
    s.eq(s.count(".sheet.show .order-err"), 0, "the error once the order was read")
    s.eq(s.writes, [], "writes by the page")
    s.settle()


# ---- settle view: how the sheets are drawn ----

# The showing sheet, measured: its panel, what lies over its last control,
# anything in it that is cut instead of wrapped, and any field iOS would zoom
# the page for.
SHEET_JS = """
() => {
  const seen = e => e.getClientRects().length > 0;
  const sheet = [...document.querySelectorAll('.sheet.show')].find(seen);
  const cs = getComputedStyle(sheet), box = sheet.getBoundingClientRect();
  sheet.scrollTop = sheet.scrollHeight;
  const last = [...sheet.querySelectorAll('button, a')].filter(seen).pop();
  const lr = last.getBoundingClientRect();
  const hit = document.elementFromPoint(lr.left + lr.width / 2, lr.top + lr.height / 2);
  const cut = [...sheet.querySelectorAll('*')].filter(seen).filter(e => {
    const c = getComputedStyle(e);
    return c.textOverflow === 'ellipsis' || (c.overflowX !== 'visible' && e !== sheet && e.scrollWidth > e.clientWidth + 1);
  }).map(e => e.className);
  const wide = [...sheet.querySelectorAll('*')].filter(seen).filter(e => {
    const r = e.getBoundingClientRect();
    return r.left < box.left - 0.5 || r.right > box.right + 0.5;
  }).map(e => e.className || e.tagName);
  sheet.scrollTop = 0;
  return {
    radius: cs.borderTopLeftRadius, edge: cs.borderTopWidth, grab: sheet.querySelectorAll('.grab').length,
    scrolls: cs.overflowY, bottom: Math.round(window.innerHeight - box.bottom),
    tall: box.height <= window.innerHeight * 0.88 + 1,
    lastHit: !!hit && (hit === last || last.contains(hit)),
    overFoot: (() => { const f = [...document.querySelectorAll('.foot')].find(seen); if (!f) return true;
      const r = f.getBoundingClientRect(); const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return !!top && !f.contains(top); })(),
    cut, wide, sideways: document.documentElement.scrollWidth > window.innerWidth,
    small: [...document.querySelectorAll('#view-settle textarea, #view-settle select, #view-settle input:not([type=file])')]
      .filter(e => parseFloat(getComputedStyle(e).fontSize) < 16).map(e => e.className || e.id || e.tagName),
  };
}
"""

SHEET_WANT = {"radius": "0px", "edge": "1px", "grab": 0, "scrolls": "auto", "bottom": 0, "tall": True,
              "lastHit": True, "overFoot": True, "cut": [], "wide": [], "sideways": False, "small": []}


def every_settle_sheet(s: Session, look) -> None:
    """Open each kind of sheet the settle view has, one after another, and
    call look(name) with it showing."""
    o = seed_demo_db._oid
    s.open_cell(26)
    look("day")
    s.open_leg(o(203))
    look("order")
    s.tap(".sheet.show .field-row", has_text="停車費")
    look("numpad")
    s.close_sheets()
    s.open_cell(1)
    s.open_leg(o(12))
    s.tap(".sheet.show .cancel-link")
    look("cancel confirm")
    s.close_sheets()
    s.open_batch("short", ".sheet.show .up-sec")
    look("batch paid short")
    s.close_sheets()
    s.open_batch("held_back")
    s.tap(".sheet.show .fold")
    look("batch with its list open")
    s.tap(".sheet.show .xbtn")
    look("解除入數")
    s.tap(".sheet.show .sheet-back")
    s.tap(".sheet.show [data-undo]")
    look("undo")
    s.close_sheets()
    s.open_batch("ahead")
    look("batch with its own lines")
    s.close_sheets()
    s.open_credit("partial")
    look("credit")
    s.close_sheets()
    s.tap(".foot [data-credits]")
    look("queue")
    s.close_sheets()
    s.stub("POST", "**/api/statements/read", 200, json.dumps(STATEMENT_READ), "application/json")
    s.pick_statement()
    s.on(".sheet.show .stmt-report").wait_for()
    look("statement")
    s.close_sheets()
    s.page.unroute("**/api/statements/read")


@check("settle.sheets-are-flat-panels-at-every-width")
def settle_sheet_panels(s: Session) -> None:
    """Each sheet is the flat panel the order's sheet is: a hairline at the
    top, no corner and no grab bar, scrolling inside itself, lying over the
    foot with its last control in reach, nothing in it cut short or wider
    than it, and no field under 16px. At the narrow phone, the usual one and
    the desktop rule."""
    s.open_settle()
    s.reach(s.cell(34))
    for width in (390, 340, 1000):
        if width != 390:
            s.resize(width)
        every_settle_sheet(s, lambda name: s.eq(s.page.evaluate(SHEET_JS), SHEET_WANT, f"the {name} sheet at {width}"))
    s.eq(len(s.writes), 3, "writes (the three statement reads)")


@check("settle.order-rows-share-one-grid")
def settle_row_grid(s: Session) -> None:
    """Time, number and figure start on the same lines down a list, in the
    day sheet, a batch's list and the tick list; the tick list has a square
    box before the time and is otherwise the same row."""
    rows_js = """() => {
      const seen = e => e.getClientRects().length > 0;
      const sheet = [...document.querySelectorAll('.sheet.show')].find(seen);
      const edge = sheet.getBoundingClientRect().left + parseFloat(getComputedStyle(sheet).paddingLeft);
      const x = (e, side) => Math.round((e.getBoundingClientRect()[side] - edge) * 2) / 2;
      const mono = e => getComputedStyle(e).fontFamily.includes('B612 Mono');
      const rows = [...sheet.querySelectorAll('.orow')];
      const set = f => [...new Set(rows.map(f))];
      const box = rows[0].querySelector('.up-chk');
      const b = box && box.getBoundingClientRect();
      return {
        n: rows.length,
        box: box ? [Math.round(b.width), Math.round(b.height), x(box, 'left')] : null,
        boxes: sheet.querySelectorAll('.orow .up-chk').length,
        time: set(r => x(r.querySelector('.ot'), 'left')),
        number: set(r => x(r.querySelector('.ol'), 'left')),
        end: set(r => Math.round(sheet.getBoundingClientRect().right - r.querySelector('.oend').getBoundingClientRect().right)),
        mono: rows.every(r => mono(r.querySelector('.ot .num')) && mono(r.querySelector('.oid')) &&
                              [...r.querySelectorAll('.oa .num')].every(mono)),
        words: rows.every(r => !mono(r.querySelector('.oll'))),
        whole: rows.every(r => r.querySelector('.oid').textContent.replace(/\\s/g, '') ===
                               (r.dataset.od || r.dataset.uptick || r.querySelector('.oid').dataset.copy)),
        pills: [...sheet.querySelectorAll('.otag')].filter(e => {
          const c = getComputedStyle(e);
          return c.backgroundColor !== 'rgba(0, 0, 0, 0)' || parseFloat(c.borderTopLeftRadius) > 3; }).length,
        heads: [...sheet.querySelectorAll('.oday')].map(e => getComputedStyle(e).letterSpacing !== 'normal'),
      };
    }"""
    s.open_settle()
    s.reach(s.cell(34))
    s.open_cell(26)
    day = s.page.evaluate(rows_js)
    s.eq((day["n"], day["box"], day["boxes"], len(day["time"]), len(day["number"]), day["end"]),
         (2, None, 0, 1, 1, [16]), "the day sheet's rows")
    s.expect(day["mono"] and day["words"] and day["whole"] and not day["pills"], f"the day sheet's rows: {day}")
    s.close_sheets()
    s.open_batch("held_back")
    s.tap(".sheet.show .fold")
    read = s.page.evaluate(rows_js)
    s.eq((read["n"], read["boxes"], read["time"], read["number"], read["end"], read["heads"]),
         (4, 0, day["time"], day["number"], [16], [True, True, True]), "a batch's list against the day sheet's")
    s.expect(read["mono"] and read["words"] and read["whole"] and not read["pills"], f"a batch's list: {read}")
    s.close_sheets()
    s.open_batch("short", ".sheet.show .up-sec")
    tick = s.page.evaluate(rows_js)
    s.eq((tick["n"], tick["boxes"], tick["box"], len(tick["time"]), len(tick["number"]), tick["end"]),
         (5, 5, [18, 18, 0], 1, 1, [16]), "the tick list")
    # The box and its gap are all the other columns move by.
    s.eq((tick["time"][0] - day["time"][0], tick["number"][0] - day["number"][0]), (28, 28), "what the box moves")
    s.expect(tick["mono"] and tick["words"] and tick["whole"] and not tick["pills"], f"the tick list: {tick}")
    # The same at the narrowest phone, where a number may wrap but is whole.
    s.close_sheets()
    s.resize(340)
    s.open_batch("short", ".sheet.show .up-sec")
    narrow = s.page.evaluate(rows_js)
    s.eq((narrow["boxes"], len(narrow["time"]), len(narrow["number"]), narrow["end"], narrow["whole"]),
         (5, 1, 1, [16], True), "the tick list at 340")
    s.eq(s.writes, [], "writes")


@check("settle.sheet-actions-and-states")
def settle_sheet_actions(s: Session) -> None:
    """What moves money is a key a thumb can hit; state is said by a solid
    block or by coloured text, in the four colours and no other."""
    def box(selector: str) -> dict:
        return s.on(selector).first.evaluate(
            "e => { const c = getComputedStyle(e), r = e.getBoundingClientRect();"
            " return { h: r.height, bg: c.backgroundColor, ink: c.color, line: c.borderTopColor, radius: c.borderTopLeftRadius }; }")

    s.open_settle()
    clear = "rgba(0, 0, 0, 0)"
    green, amber, red, blue = (s.token(n) for n in ("--green", "--amber", "--red", "--blue"))
    ink, ground, solid = s.token("--text"), s.token("--bg"), s.token("--on-solid")
    # The queue: rows that open a credit, and the keys under them.
    s.tap(".foot [data-credits]")
    s.expect(all(h >= 44 for h in s.page.eval_on_selector_all(
        ".sheet.show .qrow, .sheet.show .qitem", "els => els.map(e => e.getBoundingClientRect().height)")), "a queue row under 44px")
    for sel in (".sheet.show .pbtn[data-alloc-credit]", ".sheet.show .pbtn[data-alloc-all]"):
        key = box(sel)
        s.expect(key["h"] >= 44 and key["bg"] == clear and key["radius"] == "3px", f"{sel} is not an outlined key 44px tall: {key}")
    s.eq(s.colour(".sheet.show .qrow .s"), blue, "未對 in the queue")
    s.close_sheets()
    # A credit: the group row's block and key.
    s.open_credit("group")
    tag = box(".sheet.show .ptag")
    s.eq((tag["bg"], tag["ink"]), (green, solid), "啱數 is a solid green block")
    s.expect(box(".sheet.show .pbtn")["h"] >= 44, "對晒 under 44px")
    s.eq(s.colour(".sheet.show .hero-s"), blue, "an unmatched credit's state line")
    s.expect("B612 Mono" in s.colour(".sheet.show .hero-v", "fontFamily"), "the headline figure is not in the figure face")
    s.expect("B612 Mono" in s.colour(".sheet.show .sum-row .v .num", "fontFamily"), "the reference is not in the figure face")
    s.expect("B612 Mono" not in s.on(".sheet.show .sum-row", has_text="備註").first.locator(".v").evaluate(
        "e => getComputedStyle(e).fontFamily"), "a memo is set in the figure face")
    s.close_sheets()
    # A batch paid short: the long key, the small one, the ticks' foot.
    s.open_batch("short", ".sheet.show .up-sec")
    s.eq(s.colour(".sheet.show .hero-s"), amber, "a short-paid batch's state line")
    long = s.on(".sheet.show .pbtn", has_text="對 $300（差 $80）").first
    s.expect(long.bounding_box()["height"] >= 44, "the long key under 44px")
    hit = s.page.evaluate("""() => {
      const seen = e => e.getClientRects().length > 0;
      const k = [...document.querySelectorAll('.sheet.show .xbtn')].find(seen), r = k.getBoundingClientRect();
      const at = y => { const e = document.elementFromPoint(r.left + r.width / 2, y); return !!e && e.closest('.xbtn') === k; };
      return [r.height < 44, at(r.top + r.height / 2 - 21), at(r.top + r.height / 2 + 21)];
    }""")
    s.eq(hit, [True, True, True], "解除 is small and takes a tap 44px tall")
    s.eq((s.colour(".sheet.show .up-sum"), s.on(".sheet.show .up-btn").first.is_enabled()), (green, True), "ticks that add up")
    save = box(".sheet.show .up-btn")
    s.expect(save["h"] >= 44 and (save["bg"], save["ink"]) == (ink, ground), f"記低 is not a solid key 44px tall: {save}")
    o = seed_demo_db._oid
    s.tap(f'.sheet.show [data-uptick="{o(204)}"] .up-chk')
    s.eq((s.colour(".sheet.show .up-sum"), s.on(".sheet.show .up-btn").first.is_enabled()), (amber, False), "ticks that do not")
    off = box(".sheet.show .up-btn")
    s.expect(off["bg"] == clear and off["h"] >= 44, f"記低 disabled is not an outline: {off}")
    s.tap(f'.sheet.show [data-uptick="{o(204)}"] .up-chk')
    s.eq(s.colour(f'.sheet.show [data-uptick="{o(204)}"] .otag'), amber, "未過數 on a ticked row")
    # Destructive: a red outline where it is offered, solid red where it is confirmed.
    undo = box(".sheet.show [data-undo]")
    s.eq((undo["bg"], undo["ink"], undo["line"]), (clear, red, red), "撤銷結算")
    s.expect(undo["h"] >= 44 and box(".sheet.show .ghost-btn:not(.danger)")["h"] >= 44, "a closing key under 44px")
    s.tap(".sheet.show [data-undo]")
    go = box(".sheet.show [data-undogo]")
    s.eq((go["bg"], go["ink"]), (red, solid), "確認撤銷")
    s.close_sheets()
    # A collected batch and a day on it: finished is green, as text.
    s.open_batch("paid")
    s.eq(s.colour(".sheet.show .hero-s"), green, "a collected batch's state line")
    s.expect(s.on(".sheet.show .fold").first.bounding_box()["height"] >= 44, "the fold under 44px")
    s.close_sheets()
    s.open_cell(26)
    s.eq([s.colour(".sheet.show .otag.paid"), s.colour(".sheet.show .otag.unsettled")], [green, amber], "leg states on a day")
    s.expect(s.on(".sheet.show .blink").first.bounding_box()["height"] >= 44, "the batch link under 44px")
    s.close_sheets()
    # The statement: its confirm is solid ink and names what it will do.
    s.stub("POST", "**/api/statements/read", 200, json.dumps(STATEMENT_READ), "application/json")
    s.pick_statement()
    s.on(".sheet.show .stmt-report").wait_for()
    go = box(".sheet.show [data-stmtgo]")
    s.eq((go["bg"], go["ink"], s.text(".sheet.show [data-stmtgo]")), (ink, ground, STATEMENT_READ["confirm_label"]), "the statement's confirm")
    s.expect(s.count(".sheet.show .stmt-report .num"), "the report's figures are not in the figure face")
    s.close_sheets()
    # The overlay a dragged file brings up.
    s.drag("dragenter")
    drop = box(".drop.show span")
    s.eq((drop["radius"], s.colour(".drop.show", "backgroundColor")), ("3px", s.token("--scrim")), "the drop overlay")
    s.page.evaluate("() => document.body.dispatchEvent(new DragEvent('dragleave', { bubbles: true }))")
    s.eq(len(s.writes), 1, "writes (the statement read)")


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
    s.reach(s.cell(19))
    at, top = s.scroll_y(), s.top_week()
    s.eq(s.cell_state(19)[0], "awaiting", "the day before the change")
    s.api("POST", f"/api/credits/{c['exact']}/allocate", {"settlement_id": b["awaiting"]})
    s.wait(lambda: s.cell_state(19)[0] == "received", "the day to follow a change made elsewhere")
    s.settle()
    s.eq((s.scroll_y(), s.top_week()), (at, top), "the strip's position after a live update")
    s.eq(s.totals()[-1], "入數未對 2 筆$3170", "the totals after a live update")
    # An open sheet follows too.
    s.open_batch("awaiting")
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
    s.wait(lambda: s.cell_state(1)[1] == "941", "the change made elsewhere")
    s.never(s.toast, "a timing toast for a live update", ms=500)
    s.allow("request failed: GET /api/events")     # the navigation cuts the event stream
    s.page.goto(s.base + "/settle?perf=0")
    s.on(".cell[data-d]").first.wait_for()
    s.settle()
    s.eq((s.page.url, s.page.evaluate("() => localStorage.getItem('perf')")), (s.base + "/settle", None),
         "the address and the key after ?perf=0")
    s.never(s.toast, "a timing toast with the readout off", ms=300)


@check("settle.timing-readout-after-a-failed-load")
def settle_timing_failed(s: Session) -> None:
    """The tap that switched to the view is what its first paint is timed
    from. When that load fails, a paint made later for another reason must
    not be timed from it."""
    ms = re.compile(r"\d+ ms")
    s.allow("http 500", "status of 500")
    s.open("/?perf=1", ".orders .row")
    s.wait_toast(ms)
    s.page.wait_for_timeout(2600)
    s.stub("GET", "**/api/credits?platform=*", 500, "<html>boom</html>", "text/html")
    s.press('[aria-label="埋數"]')
    s.wait_toast("載入失敗")
    s.page.wait_for_timeout(2600)
    s.eq(s.count(".cell[data-d]"), 0, "a strip drawn from a load that failed")
    # The strip is painted later, by a change made elsewhere.
    s.page.unroute("**/api/credits?platform=*")
    s.api("PATCH", "/api/orders/" + seed_demo_db._oid(12), {"price": 401})
    s.wait(lambda: s.count(".cell[data-d]"), "the strip to be painted by the change")
    s.never(lambda: ms.fullmatch(s.toast()), "a readout timed from the tap whose load failed", ms=600)
    s.settle()


@check("settle.expired-login-does-not-toast")
def settle_auth_expired(s: Session) -> None:
    s.open_settle()
    s.allow("http 401", "status of 401")
    o = seed_demo_db._oid
    quiet = 500

    def expired(method, glob: str) -> None:
        s.stub(method, glob, 401, "<html>log in</html>", "text/html")

    expired(("POST", "PATCH", "DELETE"), "**/api/**")
    s.tap(".foot [data-credits]")
    sent = len(s.writes)
    s.press(".sheet.show .qprop .pbtn", has_text="對")
    s.wait(lambda: len(s.writes) > sent, "the allocate write")
    s.never(s.toast, "a toast for an expired login (對)", ms=quiet)
    s.press(".sheet.show .pbtn[data-alloc-all]")
    s.wait(lambda: len(s.writes) > sent + 1, "the allocate-all write")
    s.never(s.toast, "a toast for an expired login (對晒)", ms=quiet)
    s.close_sheets()
    s.open_batch("held_back")
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
    s.open_batch("short", ".sheet.show .up-sec")
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
    s.eq(s.strip_problems(), [], "the strip's cells")
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
    s.reach(s.cell(19))
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
    s.eq(s.cell_state(19)[0], "received", "the batch paid while the view was hidden")
    s.eq(s.totals()[-1], "入數未對 2 筆$3170", "the credit matched while the view was hidden")
    s.eq(s.strip_problems(), [], "the strip's cells")
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
    s.reach(s.cell(19))
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
    s.eq(s.cell_state(19)[0], "received", "the change, once the view is shown")

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
    s.eq(s.strip_problems(), [], "the strip's cells")
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
    s.expect(s.date_text().startswith(date_head(s.day(2))), "the date the view was left on")
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
    s.eq(s.title(), "CX488 10:15", "the settle view's order")
    s.eq(list(s.info())[0], "單號", "the settle view's own rows")
    s.tap(".sheet.show .field-row", has_text="停車費")
    s.eq(s.count(".sheet.show .sheet-back"), 1, "the settle view's back button on the numpad")
    s.tap(".sheet.show .sheet-back")
    s.eq(s.title(), "CX488 10:15", "‹ went back one level")
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
    s.eq((s.title(), s.count(".sheet.show .field-row")), ("UO623 14:20", 5), "the day view's own sheet")
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
    s.eq((s.title(), s.count(".sheet.show .field-row")), ("UO623 14:20", 5), "the day view's own sheet after the cancel landed")
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
    s.open_batch("awaiting")
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
    # A resize and a scroll do not touch the hidden strip.
    s.page.set_viewport_size({"width": 430, "height": 700})
    s.page.evaluate("() => window.scrollTo(0, 40)")
    s.page.wait_for_timeout(500)
    s.settle()
    s.eq(s.changes("view-settle"), [], "changes to the hidden settle view")
    s.eq(s.writes, [], "writes")
    # Shown again, the strip holds at the new width.
    s.go_settle()
    s.eq(s.strip_problems(), [], "the strip's cells at the new width")
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
    s.expect(s.date_text().startswith(date_head(s.day())), "the date button without a server")
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
    s.wait(lambda: s.cell_state(1)[1] == "941", "a change made after the server came back")
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
    s.eq(s.strip_problems(), [], "the strip's cells")
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
    s.tap(".tab", has_text="接送")
    s.eq(s.scroll_y(), 0, "scroll after a filter tap")
    s.tap(".tab", has_text="接送")
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
                           " return [g.className, !!g.querySelector('.now-t')]; }")
    s.eq(last, ["now", True], "what closes the list")
    s.eq(s.count(".orders .now"), 1, "NOW lines")
    s.eq(s.count(".orders .row.next"), 0, "NEXT rows with every order done")
    s.eq(s.scroll_y(), 0, "scroll after the minute re-render")


@check("inventory.day-rows-and-sheet")
def inventory_day_rows(s: Session) -> None:
    """B2, B5, C9, D12, D13, D14, D15, D23, L8."""
    s.open_day()
    o = s.t["order"]
    # The filter outlives a change made elsewhere.
    s.tap(".tab", has_text="接送")
    s.api("PATCH", "/api/orders/" + o["dropoff"], {"price": 455})
    s.wait(lambda: s.text(s.row(o["dropoff"]) + " .price") == "$455", "the change made elsewhere")
    s.expect(s.text(".tab.on").startswith("接送"), "the filter after a live update")
    s.tap(".tab", has_text="接送")
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
    # A zero tab is dimmed and still takes a tap.
    s.go_days(1)
    s.tap(".tab.zero", has_text="滴滴")
    s.eq((s.text(".tab.on"), s.text(".empty")), ("滴滴0", "冇滴滴訂單"), "a tapped zero tab")
    # The empty line stands in the middle of the column.
    left, right, width = s.page.evaluate(
        "() => { const e = [...document.querySelectorAll('.orders .empty')].find(x => x.getClientRects().length);"
        " const r = document.createRange(); r.selectNodeContents(e); const b = r.getBoundingClientRect();"
        " return [b.left, b.right, window.innerWidth]; }")
    s.expect(abs((left + right) / 2 - width / 2) <= 1, f"the empty line is not centred: {left}, {right} in {width}")
    # A reload forgets the filter.
    s.allow("request failed: GET /api/events")     # the reload cuts the event stream
    s.page.reload()
    s.on(".orders .row").first.wait_for()
    s.settle()
    s.expect(s.text(".tab.on").startswith("全部"), "the filter after a reload")
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
        ".sheet.show .pp-opt", "els => els.map(e => e.tagName + ' ' + e.querySelector('.pp-n').textContent + (e.classList.contains('on') ? '*' : ''))"),
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
    s.open_batch("awaiting")
    s.eq(s.sum_pairs(".sheet.show .sum-rows .sum-row"), [["應收", "$1290"], ["差額", "−$20"]], "expected and difference")
    s.close_sheets()
    # A tap on an empty day falls through to the calendar and clears a focus.
    s.focus_on("short", ".sheet.show .up-sec")
    s.expect(s.lit(), "nothing was lit from the batch's sheet")
    s.page.touchscreen.tap(*empty_day(s))
    s.wait(lambda: s.lit() == [] and s.count("#grid .dim") == 0, "a tap on an empty day to clear the focus")
    s.expect(not s.sheet_open(), "an empty day opened a sheet")
    # The make-up payment arrives: the batch says which leg it was for.
    s.api("POST", f"/api/credits/{c['exact']}/allocate", {"settlement_id": b["short"]})
    s.wait(lambda: s.cell_state(26)[0] == "received", "the batch to be collected")
    s.settle()
    s.open_batch("short")
    notes = s.texts(".sheet.show .sub-note")
    s.eq(notes, ["其餘 4 程", "補 …0204"], "which legs each transfer was for")
    s.tap(".sheet.show .fold")
    made_up = s.back(14)
    s.expect(any(f"補收 {md_slash(made_up)}" in t for t in s.texts(".sheet.show .orow")), "the held-back leg in the order list")
    s.close_sheets()
    s.open_cell(26)
    s.eq(s.texts(f'.sheet.show .orow[data-od="{seed_demo_db._oid(204)}"] .otag'), [f"補收 {md_slash(made_up)}"], "the leg on its day")
    s.close_sheets()
    # A focus is dropped when what it names is gone. The batch shares its
    # statement date with an earlier one, so its name is numbered.
    s.focus_on("group")
    s.eq(s.lit(), sorted(s.back(n).isoformat() for n in (9, 8)), "the focus on a batch")
    s.expect(s.totals()[0].startswith(f"{md_slash(s.back(2))} 結算 (2) · "), f"the second statement of a day: {s.totals()}")
    s.api("DELETE", f"/api/settlements/{b['group']}")
    s.wait(lambda: s.lit() == [] and s.count("#grid .dim") == 0 and s.count(".foot .fline") == 0,
           "the focus on a batch that is gone to be dropped")
    # A read that never reaches the server.
    s.allow("request failed: POST /api/statements/read", "Failed to load resource")
    s.page.route("**/api/statements/read", lambda route: route.abort())
    s.pick_statement()
    s.wait_toast("讀唔到張圖")
    s.eq(s.text('[aria-label="讀結算圖"]'), "圖", "the read button after a failed read")
    s.settle()


@check("inventory.a-click-opens-a-day-and-resting-lights-nothing", desktop=True)
def inventory_pointer(s: Session) -> None:
    """H27, H31: with a pointer that can hover, a day still opens on one
    click, and resting on it states no relation."""
    s.open_settle()
    cell = s.cell(26)
    for _ in range(8):
        if s.count(cell):
            break
        s.on('[aria-label="前一個月"]').first.click()
        s.settle()
    s.eq(round(s.page.evaluate("() => document.body.getBoundingClientRect().width")), 640, "the column on a wide screen")
    s.on(cell).first.hover()
    s.page.wait_for_timeout(300)
    s.eq((s.lit(), s.count("#grid .dim"), s.count(".foot .fline")), ([], 0, 0), "what a resting pointer lights")
    s.expect(not s.sheet_open(), "resting on a day opened a sheet")
    s.on(cell).first.click()
    s.on(".sheet.show .orow").first.wait_for()
    s.eq(s.title(), md_label(s.back(26)) + " 星期" + WEEKDAY[s.back(26).weekday()], "the sheet one click opened")
    s.eq(s.strip_problems(), [], "the strip's cells under the desktop rule")
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
    promised((".lkey:active", ".fx:active", ".cell:not(.none):active", ".orow.tap:active", ".pbtn:active", ".nav-btn:active", ".field-row:active"),
             ("body", ".header", ".sheet", ".toast", ".foot"), (".sheet", ".scrim", ".np-pad"))
    s.open_day()
    promised((".row:active", ".field-row:active", ".key:active", ".nav-btn:active"),
             ("body", ".header", ".sheet", ".toast", ".drop", ".foot"),
             (".sheet", ".scrim", ".drop", ".paste-preview .sum-row", ".np-pad", ".st.turn"))
    # With motion on, the sheet and panel slide; with it reduced they do not.
    def moving() -> list:
        return s.page.evaluate("() => ['.sheet', '.scrim', '.drop'].map(q => {"
                               " const e = [...document.querySelectorAll(q)].find(x => x.closest('#view-day') || !document.getElementById('view-day'));"
                               " return parseFloat(getComputedStyle(e || document.querySelector(q)).transitionDuration) > 0; })")
    s.eq(moving(), [True, True, True], "sheet, scrim and panel transitions")
    s.page.emulate_media(reduced_motion="reduce")
    s.eq(moving(), [False, False, False], "sheet, scrim and panel transitions under reduced motion")
    # The toast fades where it stands: shown or not, it is in the same place.
    s.eq(s.page.evaluate("() => { const e = document.querySelector('.toast'), at = () => getComputedStyle(e).transform;"
                         " const off = at(); e.classList.add('show'); const on = at(); e.classList.remove('show');"
                         " return [off === on, getComputedStyle(e).transitionProperty]; }"),
         [True, "opacity"], "the toast under reduced motion")
    s.page.emulate_media(reduced_motion="no-preference")
    # The toast never takes a tap; the sheet stops at 88% of the screen.
    toast = s.page.eval_on_selector(".toast", "e => { const c = getComputedStyle(e); return [c.pointerEvents, c.position]; }")
    s.eq(toast, ["none", "fixed"], "the toast")
    s.open_order(s.t["order"]["landed_banner"])
    height = s.page.viewport_size["height"]
    s.eq(s.page.evaluate("() => { const e = [...document.querySelectorAll('.sheet.show')].find(x => x.getClientRects().length);"
                         " return [Math.round(parseFloat(getComputedStyle(e).maxHeight)), getComputedStyle(e).overflowY]; }"),
         [round(height * 0.88), "auto"], "the sheet's height limit")
    # The day view's sheet is a flat panel under a hairline: no corner, no grab bar.
    s.eq(s.count(".sheet.show .grab"), 0, "grab bars on the day view's sheet")
    s.eq(s.page.evaluate("() => { const e = [...document.querySelectorAll('.sheet.show')].find(x => x.getClientRects().length);"
                         " const c = getComputedStyle(e); return [c.borderTopLeftRadius, c.borderTopWidth]; }"),
         ["0px", "1px"], "the sheet's corner and top edge")
    s.tap(".sheet.show .sheet-x")
    # One text input in the whole app, at 16px.
    s.tap('[aria-label="入單"]')
    s.stage("入單")
    s.eq(s.page.evaluate("() => [...document.querySelectorAll('textarea, input:not([type=file])')].map(e => e.className)"),
         ["paste-box"], "text inputs")
    s.eq(s.page.eval_on_selector(".paste-box", "e => getComputedStyle(e).fontSize"), "16px", "the paste box's text size")
    # iOS zooms the page when a field under 16px takes the focus: none may be.
    small = s.page.evaluate("() => [...document.querySelectorAll('textarea, select, input:not([type=file])')]"
                            ".filter(e => parseFloat(getComputedStyle(e).fontSize) < 16).map(e => e.className || e.id || e.tagName)")
    s.eq(small, [], "fields set smaller than 16px")
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
