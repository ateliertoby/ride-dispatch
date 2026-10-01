"""What the screenshot and end-to-end scripts share: a server on a synthetic
database, a browser context shaped like the operator's phone, and a driver
that knows when a page has come to rest.

    python scripts/harness.py APP_ROOT DB_PATH PORT NOW    (internal: the served app)

Both clocks are pinned to 14:00 on the chosen day, the browser's and the
server's, so a run gives the same result whenever it is made.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import seed_demo_db  # noqa: E402

DEVICE = "iPhone 14"
SCHEMES = ("dark", "light")
TIMEZONE = "Asia/Hong_Kong"
TIMEOUT_MS = 10_000

# Motion is the page's, and a screenshot taken part-way through it differs from
# the next one. Scrollbars fade on a timer of their own.
STILL_CSS = ("*,*::before,*::after{transition:none!important;animation:none!important}"
             "::-webkit-scrollbar{display:none}")
STILL_JS = """
document.addEventListener('DOMContentLoaded', () => {
  const s = document.createElement('style');
  s.textContent = %r;
  document.head.appendChild(s);
});
""" % STILL_CSS

# Resolves once the document has stopped moving: same scroll position and same
# height for a run of consecutive frames. A smooth scroll and a strip that is
# still growing both fail it.
STABLE_JS = """
() => new Promise(resolve => {
  let last = '', same = 0;
  const tick = () => {
    const now = window.scrollX + ',' + window.scrollY + ',' +
      document.documentElement.scrollHeight;
    same = now === last ? same + 1 : 0;
    last = now;
    if (same >= 8) resolve(true); else requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
})
"""


def demo_now(today: date) -> datetime:
    """The moment both clocks are pinned to, in the operator's timezone."""
    return datetime.combine(today, dtime(seed_demo_db.DEMO_HOUR, 0), ZoneInfo(TIMEZONE))


# ---- the server ----

def serve(app_root: str, db_path: str, port: int, now: str) -> None:
    """Run the app from `app_root` on `db_path`, its clock pinned to `now`.

    Runs in a child process. The app asks the server what time it is to decide
    which legs are finished, so a clock left running would change the figures
    between two runs made at different hours.
    """
    sys.path.insert(0, app_root)
    os.chdir(app_root)
    # Seeding has already loaded the package from this script's own checkout;
    # the import below must find the one under app_root.
    for name in [m for m in sys.modules if m.split(".")[0] == "ride_dispatch"]:
        del sys.modules[name]
    from ride_dispatch import db, web
    fixed = datetime.fromisoformat(now)
    real_now_str = db._now_str
    db._now_str = lambda moment=None: real_now_str(moment or fixed)
    web.DB_PATH = db_path
    _install_faults(web.app, os.path.join(os.path.dirname(db_path), FAULTS_FILE), port)
    web.app.run(host="127.0.0.1", port=port, threaded=True)


FAULTS_FILE = "faults.json"
SHELL_ROUTES = ("/", "/settle")
LOGIN_PATH = "/__login"


def _install_faults(app, path: str, port: int) -> None:
    """Let a check make the served app misbehave the way its surroundings can.

    The file at `path` is read on every request; Server.fault() writes it.

    expired       every request is answered as the access proxy answers one
                  that carries no session: a redirect to a login on another
                  origin (the same server under its other name, `localhost`)
    doc_version   the shell document claims this version instead of its own,
                  as after a deploy the worker being installed predates
    doc_redirect  the shell document is reached through a same-origin redirect
    no_assets     every asset address is a 404, as after a deploy
    no_shell      the document, the assets and the worker script are refused
    bad_gateway   every request is answered 502 with a page of the tunnel's,
                  as while the server behind it is restarting
    """
    import json
    from flask import redirect, request

    def faults() -> dict:
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def login():
        # Stands in for the proxy's login page; its link is the way back the
        # proxy offers once the session is good again.
        back = f"http://127.0.0.1:{port}" + request.args.get("next", "/")
        return ('<!doctype html><meta name="viewport" content="width=device-width">'
                f'<title>login</title><a id="back" href="{back}">log in</a>')

    app.add_url_rule(LOGIN_PATH, "harness_login", login)

    @app.before_request
    def misbehave():
        f = faults()
        path_ = request.path
        if path_ == LOGIN_PATH:
            return None
        if f.get("expired"):
            nxt = request.full_path.rstrip("?")
            return redirect(f"http://localhost:{port}{LOGIN_PATH}?next=" + urllib.parse.quote(nxt, safe=""))
        if f.get("bad_gateway"):
            return "<html>bad gateway</html>", 502
        static = path_.startswith("/assets/")
        if f.get("no_assets") and static:
            return "gone", 404
        if f.get("no_shell") and (static or path_ in SHELL_ROUTES or path_ == "/sw.js"):
            return "unavailable", 503
        if f.get("doc_redirect") and path_ in SHELL_ROUTES and "redirected" not in request.args:
            return redirect(path_ + "?redirected=1")
        return None

    @app.after_request
    def misreport(resp):
        version = faults().get("doc_version")
        if version and "X-Asset-Version" in resp.headers:
            resp.headers["X-Asset-Version"] = version
        return resp


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def copy_app(dst: str, app_root: str = ROOT) -> str:
    """A throwaway copy of what the app is served from, so a check can change
    a file the way a deploy does without writing into the working tree."""
    for part in ("ride_dispatch", "templates", "static"):
        shutil.copytree(os.path.join(app_root, part), os.path.join(dst, part),
                        ignore=shutil.ignore_patterns("__pycache__"))
    return dst


class Server:
    """A seeded database and the app serving it, for the length of a `with`.

    stop() and start() replace the serving process and keep the address and
    the database, which is what a deploy does.
    """

    def __init__(self, today: date, app_root: str = ROOT):
        self.today = today
        self.app_root = app_root
        self.proc = None

    def __enter__(self) -> str:
        self.dir = tempfile.TemporaryDirectory(prefix="ride-shots-")
        self.db_path = os.path.join(self.dir.name, "demo.db")
        seed_demo_db.seed(self.db_path, self.today)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = open(os.path.join(self.dir.name, "server.log"), "w")
        self.start()
        return self.url

    def start(self) -> None:
        now = datetime.combine(self.today, dtime(seed_demo_db.DEMO_HOUR, 0)).isoformat()
        self.proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), self.app_root, self.db_path,
             str(self.port), now],
            stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited:\n" + self._log_text())
            try:
                urllib.request.urlopen(self.url + "/api/orders?date=" + self.today.isoformat(), timeout=1)
                return
            except urllib.error.HTTPError:
                return      # answering, if only to refuse: a fault is set
            except OSError:
                if time.monotonic() > deadline:
                    self.__exit__(None, None, None)
                    raise RuntimeError("server did not start:\n" + self._log_text())
                time.sleep(0.1)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)

    def fault(self, **faults) -> None:
        """Set how the served app misbehaves from now on (see _install_faults);
        with no argument, not at all."""
        path = os.path.join(self.dir.name, FAULTS_FILE)
        with open(path + ".new", "w") as f:
            json.dump(faults, f)
        os.replace(path + ".new", path)

    def version(self) -> str:
        """The asset version the serving process answers with."""
        with urllib.request.urlopen(self.url + "/api/ping", timeout=5) as res:
            return json.loads(res.read())["version"]

    def asked(self) -> list:
        """Every request the server has answered, oldest first, as (method,
        path, status): the one record that does not depend on what the
        browser chooses to report."""
        plain = re.sub(r"\x1b\[[0-9;]*m", "", self._log_text())     # the log colours by status
        return [(m, p, int(st)) for m, p, st in
                re.findall(r'"([A-Z]+) (\S+) HTTP/[\d.]+" (\d{3})', plain)]

    def _log_text(self) -> str:
        self.log.flush()
        with open(self.log.name) as f:
            return f.read()

    def __exit__(self, *exc) -> None:
        self.stop()
        self.log.close()
        self.dir.cleanup()


# ---- the browser ----

DESKTOP = {"viewport": {"width": 1000, "height": 800}}


def new_context(playwright, browser, scheme: str, today: date, still: bool = True,
                workers: bool = False, desktop: bool = False):
    """A context shaped like the operator's phone, its clock pinned to the
    demo hour. Date is frozen and timers keep running: the NOW line and the
    done-dimming stay put, and the app's own timeouts still fire.

    `desktop` shapes it like a desktop browser instead: a wide window and a
    pointer that hovers, with no touch.

    The shell's service worker is refused unless `workers` is set. A page a
    worker controls is out of reach of request interception in WebKit, which
    most checks depend on, and a worker installing in the background would
    make the moment a page comes to rest depend on timing."""
    ctx = browser.new_context(**(DESKTOP if desktop else playwright.devices[DEVICE]), color_scheme=scheme,
                              timezone_id=TIMEZONE, locale="zh-HK",
                              service_workers="allow" if workers else "block")
    if still:
        ctx.add_init_script(STILL_JS)
    ctx.clock.set_fixed_time(demo_now(today))
    return ctx


class Driver:
    """Opens the app and drives it through what is on screen only (classes,
    data attributes, aria labels, text), never through its own functions, so
    the same steps can be pointed at a build whose scripts are laid out
    differently."""

    def __init__(self, ctx, base_url: str):
        self.ctx = ctx
        self.base = base_url
        self.page = None
        self.viewport = None    # a window size to open pages at, in place of the device's

    # -- waiting --

    def _watch(self, page) -> None:
        self.pending = set()
        self.activity = 0
        self.stream_open = False
        self.errors = []

        def started(req):
            # The event stream stays open for the life of the page and is never
            # "done"; everything else is.
            if req.url.endswith("/api/events"):
                return
            self.pending.add(req)
            self.activity += 1

        def ended(req):
            self.pending.discard(req)
            self.activity += 1

        def answered(res):
            if res.url.endswith("/api/events"):
                self.stream_open = True

        page.on("request", started)
        page.on("requestfinished", ended)
        page.on("requestfailed", ended)
        page.on("response", answered)
        page.on("pageerror", lambda e: self.errors.append(str(e)))

    def _quiet(self, ms: int = 300) -> None:
        """No request in flight, and none started or finished for `ms`."""
        deadline = time.monotonic() + TIMEOUT_MS / 1000
        while True:
            seen = self.activity
            self.page.wait_for_timeout(ms)
            if not self.pending and self.activity == seen:
                return
            if time.monotonic() > deadline:
                raise TimeoutError("requests still in flight: " +
                                   ", ".join(r.url for r in self.pending))

    def settle(self) -> None:
        """Wait until the page has nothing left to do: requests answered, the
        document no longer moving or growing, fonts in."""
        deadline = time.monotonic() + TIMEOUT_MS / 1000
        while True:
            self._quiet()
            seen = self.activity
            self.page.evaluate(STABLE_JS)
            self.page.evaluate("() => document.fonts.ready.then(() => true)")
            # Coming to rest can itself ask for data (a month scrolled into
            # reach), in which case the page is not at rest yet.
            if not self.pending and self.activity == seen:
                return
            if time.monotonic() > deadline:
                raise TimeoutError("page did not settle")

    # -- finding and tapping --

    def on(self, selector: str, **kw):
        """What the operator can see: with both views in one document, the
        hidden one holds the same classes."""
        return self.page.locator(selector, **kw).locator("visible=true")

    def tap(self, selector: str, **kw) -> None:
        self.on(selector, **kw).first.tap()
        self.settle()

    def open(self, path: str, ready: str) -> None:
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(TIMEOUT_MS)
        self._watch(self.page)
        if self.viewport:
            self.page.set_viewport_size(self.viewport)
        self.page.goto(self.base + path)
        self.on(ready).first.wait_for()
        # Each connection of the event stream begins with a greeting, and a
        # build may reload its data on it. Waiting for the stream first puts
        # that reload before the first tap instead of somewhere after it.
        deadline = time.monotonic() + TIMEOUT_MS / 1000
        while not self.stream_open:
            if time.monotonic() > deadline:
                raise TimeoutError("the page never opened its event stream")
            self.page.wait_for_timeout(50)
        self.settle()

    def reach(self, selector: str) -> None:
        """Bring a mark onto the strip: it is drawn only once its month is
        loaded, and the strip loads backwards a month at a time."""
        for _ in range(8):
            if self.on(selector).count():
                return
            self.tap('[aria-label="前一個月"]')
        raise RuntimeError(f"not on the strip after 8 months back: {selector}")

    def keys(self, host: str, digits: str) -> None:
        """Type on the on-screen numpad inside `host`."""
        for d in digits:
            self.tap(f'{host} .key[data-k="{d}"]')


# ---- the settle strip under stress ----

LONG_AMOUNT = 1234567.89


def stress_settle(body: dict, today: date) -> None:
    """Rewrite one month's answer from /api/settle so the strip carries what
    strains its lanes: seven-figure amounts with cents on a short-paid and an
    awaiting batch, and a batch whose days are three separate runs either
    side of the first of today's month."""
    first = today.replace(day=1)
    runs = [first - timedelta(days=3), first - timedelta(days=1), first + timedelta(days=1)]
    for b in body["settlements"]:
        if b["id"] == seed_demo_db.BATCH["short"]:
            b.update(confirmed_amount=LONG_AMOUNT, received=LONG_AMOUNT - 380.55, outstanding=380.55)
        elif b["id"] == seed_demo_db.BATCH["group"]:
            b.update(confirmed_amount=1048576.5, outstanding=1048576.5)
        elif b["id"] == seed_demo_db.BATCH["ahead"]:
            b["adjustments"] = []
            for o, d in zip(b["orders"], runs):
                o["scheduled_time"] = d.isoformat() + o["scheduled_time"][10:]
    body["totals"].update(unsettled=LONG_AMOUNT, awaiting=1048576.5)


def stress_credits(body: dict, today: date) -> None:
    """Rewrite the answer from /api/credits to match: five more unmatched
    credits with seven-figure amounts, all on one day of a week that already
    holds bars, so that week needs a lane for each; and today's credit paid
    into three batches."""
    like = next(c for c in body["credits"] if c["id"] == seed_demo_db.CREDIT["exact"])
    for i in range(5):
        body["credits"].append(dict(like, id=901 + i, ref=f"DEMO-REF-09{i}", amount=LONG_AMOUNT - i,
                                    remaining=LONG_AMOUNT - i, proposals=[], batches=[], combo=None,
                                    value_date=seed_demo_db.day(today, 8)))
    paid = next(c for c in body["credits"] if c["id"] == seed_demo_db.CREDIT["group"])
    for key, backs in (("short", (27, 26, 25)), ("awaiting", (19, 18)), ("group", (9, 8))):
        paid["batches"].append({
            "id": seed_demo_db.BATCH[key], "dates": [seed_demo_db.day(today, n) for n in backs],
            "orders": len(backs), "amount": 100.0, "confirmed_amount": 100.0, "outstanding": 0.0,
            "state": "paid", "has_image": False})
    body["sums"]["open"] = LONG_AMOUNT


def stress_strip(ctx, today: date) -> None:
    """From now on, every page of this context is served the settle view's
    data rewritten by the two functions above."""
    def rewrite(change):
        def handler(route):
            body = route.fetch().json()
            change(body, today)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        return handler
    ctx.route("**/api/settle?*", rewrite(stress_settle))
    ctx.route("**/api/credits?*", rewrite(stress_credits))


def paste_message() -> str:
    """A synthetic 携程 order message, the one the parser's own tests use."""
    from tests.test_parser import PICKUP_MSG
    return PICKUP_MSG


if __name__ == "__main__":
    serve(sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4])
