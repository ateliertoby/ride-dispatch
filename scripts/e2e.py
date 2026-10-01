"""Behaviour checks for the web app, driven through a real browser.

    python scripts/e2e.py                    every check, against the shell
    python scripts/e2e.py --only day.add     checks whose name begins with this
    python scripts/e2e.py --old-pages        also run each check the separate pages
                                             can pass against them, and require
                                             both builds to send the same writes
    python scripts/e2e.py --list             name the checks and stop

Playwright WebKit as an iPhone 14. Every check gets a server of its own on a
freshly seeded synthetic database (scripts/seed_demo_db.py) with both clocks
pinned to 14:00, so checks do not depend on each other or on when they run.
Each prints PASS or FAIL; the exit status is non-zero if any failed.

A check also fails when the page logged an error, a request failed or the
server answered with an error status, unless the check said to expect it.

A check drives the page through what is on screen only, as scripts/shots.py
does. Checks are registered with @check, in the order they run; a new view
adds its own under its own name prefix.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import seed_demo_db  # noqa: E402
from harness import (DEVICE, TIMEOUT_MS, TIMEZONE, Driver, Server, demo_now,  # noqa: E402
                     new_context, paste_message)

CHECKS = []


def check(name: str, old: bool = True, clock: str = "fixed", still: bool = True):
    """Register a check. `old` says the separate pages can pass it too, which
    is every check that does not depend on how the shell loads its data.
    `clock` is "fixed" (Date frozen, timers real) or "installed" (the check
    moves time itself). `still` turns the page's transitions and animations
    off, which is what every check wants unless motion is what it checks."""
    def register(fn):
        CHECKS.append({"name": name, "fn": fn, "old": old, "clock": clock, "still": still})
        return fn
    return register


class Failed(AssertionError):
    pass


class Session(Driver):
    """One page on one server, with everything it asked the server recorded."""

    def __init__(self, ctx, base_url: str, today: date, shell: bool):
        super().__init__(ctx, base_url)
        self.today = today
        self.shell = shell
        self.t = seed_demo_db.targets(today)
        self.requests = []     # (method, path, resource type), as they start
        self.finished = []     # (method, path), as they finish
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
                self.writes.append((req.method, path, req.post_data))

        def finished(req):
            self.finished.append((req.method, path_of(req.url)))

        def failed(req):
            self.noise.append(f"request failed: {req.method} {path_of(req.url)} ({req.failure})")

        def answered(res):
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
        out = [f"page error: {e}" for e in self.errors]
        out += [n for n in self.noise if not any(a in n for a in self.allowed)]
        return out

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

    def stub(self, method: str, glob: str, status: int, body: str, content_type: str) -> None:
        """Answer every matching request with this instead of the server's."""
        def handler(route, request):
            if request.method == method:
                route.fulfill(status=status, body=body, content_type=content_type)
            else:
                route.continue_()
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


@check("day.boot-requests", old=False)
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


@check("day.handlers-resolve", old=False)
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
    # One at a time, in the order they were asked: the separate pages paint
    # whichever answer lands last.
    answered = s.finished.count(("GET", near))
    s.release(near)
    s.wait(lambda: s.finished.count(("GET", near)) > answered, "tomorrow's answer")
    s.release(far)
    s.wait(lambda: s.count(".empty"), "the empty day to be drawn")
    s.eq(s.text(".empty"), "冇訂單", "empty day text")
    s.release_all()
    s.settle()


@check("day.prefetched-day-paints-before-the-answer", old=False)
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


@check("day.late-answer-for-a-day-already-left", old=False)
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


@check("day.failed-load-of-a-held-day", old=False)
def day_failed_held(s: Session) -> None:
    s.open_day()
    s.go_days(1)
    s.stub("GET", "**/api/orders?date=" + s.day(), 500, "<html>boom</html>", "text/html")
    s.allow("http 500", "status of 500")
    s.press('[aria-label="前一日"]')
    s.wait_toast("載入失敗")
    s.eq(s.rows(), s.ids(), "the day's last known rows")
    s.settle()


@check("day.expired-login-does-not-toast", old=False)
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
    s.on(".scrim").first.tap(position={"x": 8, "y": 8})
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


@check("day.settle-path-until-the-settle-view-exists", old=False)
def day_settle_path(s: Session) -> None:
    """The shell has one view so far: /settle shows it, and the $ link is left
    to the browser, which loads the same shell again."""
    s.open_day("/settle")
    s.eq(s.rows(), s.ids(), "rows on /settle")
    s.eq(s.on('[aria-label="埋數"]').first.get_attribute("href"), "/settle", "the $ link")
    s.expect(s.on('[aria-label="埋數"]').first.get_attribute("data-nav") is not None, "data-nav on $")
    s.open_day()
    s.allow("request failed")     # the navigation cuts the event stream
    with s.page.expect_navigation():
        s.press('[aria-label="埋數"]')
    s.wait(lambda: s.rows() == s.ids(), "the day view after following $")
    s.expect(s.page.url.endswith("/settle"), "address after following $")
    s.settle()


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


# ---- running ----

def run(playwright, browser, chk: dict, today: date, shell: bool):
    """Run one check on a server of its own. Returns (problem or None, writes)."""
    with Server(today, shell=shell) as url:
        if chk["clock"] == "installed":
            ctx = browser.new_context(**playwright.devices[DEVICE], color_scheme="dark",
                                      timezone_id=TIMEZONE, locale="zh-HK")
            ctx.clock.install(time=demo_now(today))
        else:
            ctx = new_context(playwright, browser, "dark", today, still=chk["still"])
        s = Session(ctx, url, today, shell)
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
        return problem, list(s.writes)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="", metavar="PREFIX", help="checks whose name begins with this")
    ap.add_argument("--old-pages", action="store_true",
                    help="also run against the separate pages and compare the writes")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today(),
                    help="the day the data and both clocks are built around (default: today)")
    ap.add_argument("--list", action="store_true", help="name the checks and stop")
    args = ap.parse_args()

    wanted = [c for c in CHECKS if c["name"].startswith(args.only)]
    if args.list:
        for c in wanted:
            print(c["name"] + ("" if c["old"] else "   (shell only)"))
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
            problem, writes = run(p, browser, chk, args.today, shell=True)
            report(problem is None, chk["name"], problem or "")
            if args.old_pages and chk["old"]:
                old_problem, old_writes = run(p, browser, chk, args.today, shell=False)
                report(old_problem is None, chk["name"] + "  [old pages]", old_problem or "")
                same = writes == old_writes
                report(same, chk["name"] + f"  [same {len(old_writes)} writes as the old pages]",
                       "" if same else f"shell {writes!r} / old {old_writes!r}")
        browser.close()
    print(f"{total} checks, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
