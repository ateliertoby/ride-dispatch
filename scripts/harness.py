"""What the screenshot and end-to-end scripts share: a server on a synthetic
database, a browser context shaped like the operator's phone, and a driver
that knows when a page has come to rest.

    python scripts/harness.py APP_ROOT DB_PATH PORT NOW    (internal: the served app)

Both clocks are pinned to 14:00 on the chosen day, the browser's and the
server's, so a run gives the same result whenever it is made.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import date, datetime, time as dtime
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

    Runs in a child process. The pages ask the server what time it is to decide
    which legs are finished, so a clock left running would change the figures
    between two runs made at different hours.
    """
    sys.path.insert(0, app_root)
    os.chdir(app_root)
    from ride_dispatch import db, web
    fixed = datetime.fromisoformat(now)
    real_now_str = db._now_str
    db._now_str = lambda moment=None: real_now_str(moment or fixed)
    web.DB_PATH = db_path
    web.app.run(host="127.0.0.1", port=port, threaded=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """A seeded database and the app serving it, for the length of a `with`.

    `shell` says which pages the app serves: True the single-document shell,
    False the two separate pages, None whatever RIDE_SHELL says in the
    caller's own environment.
    """

    def __init__(self, today: date, app_root: str = ROOT, shell: bool | None = None):
        self.today = today
        self.app_root = app_root
        self.shell = shell

    def __enter__(self) -> str:
        self.dir = tempfile.TemporaryDirectory(prefix="ride-shots-")
        self.db_path = os.path.join(self.dir.name, "demo.db")
        seed_demo_db.seed(self.db_path, self.today)
        port = free_port()
        now = datetime.combine(self.today, dtime(seed_demo_db.DEMO_HOUR, 0)).isoformat()
        env = dict(os.environ)
        if self.shell is not None:
            env["RIDE_SHELL"] = "1" if self.shell else "0"
        self.log = open(os.path.join(self.dir.name, "server.log"), "w")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), self.app_root, self.db_path,
             str(port), now],
            stdout=self.log, stderr=subprocess.STDOUT, env=env)
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited:\n" + self._log_text())
            try:
                urllib.request.urlopen(url + "/api/orders?date=" + self.today.isoformat(), timeout=1)
                return url
            except OSError:
                if time.monotonic() > deadline:
                    self.__exit__(None, None, None)
                    raise RuntimeError("server did not start:\n" + self._log_text())
                time.sleep(0.1)

    def _log_text(self) -> str:
        self.log.flush()
        with open(self.log.name) as f:
            return f.read()

    def __exit__(self, *exc) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.log.close()
        self.dir.cleanup()


# ---- the browser ----

def new_context(playwright, browser, scheme: str, today: date, still: bool = True):
    """A context shaped like the operator's phone, its clock pinned to the
    demo hour. Date is frozen and timers keep running: the NOW line and the
    done-dimming stay put, and the pages' own timeouts still fire."""
    ctx = browser.new_context(**playwright.devices[DEVICE], color_scheme=scheme,
                              timezone_id=TIMEZONE, locale="zh-HK")
    if still:
        ctx.add_init_script(STILL_JS)
    ctx.clock.set_fixed_time(demo_now(today))
    return ctx


class Driver:
    """Opens a page and drives it through what is on screen only (classes,
    data attributes, aria labels, text), never through a page's own functions,
    so the same steps can be pointed at a build whose scripts are laid out
    differently."""

    def __init__(self, ctx, base_url: str):
        self.ctx = ctx
        self.base = base_url
        self.page = None

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
        self.page.goto(self.base + path)
        self.on(ready).first.wait_for()
        # Each connection of the event stream begins with a greeting, and a page
        # may reload its data on it. Waiting for the stream first puts that
        # reload before the first tap instead of somewhere after it.
        deadline = time.monotonic() + TIMEOUT_MS / 1000
        while not self.stream_open:
            if time.monotonic() > deadline:
                raise TimeoutError("the page never opened its event stream")
            self.page.wait_for_timeout(50)
        self.settle()

    def keys(self, host: str, digits: str) -> None:
        """Type on the on-screen numpad inside `host`."""
        for d in digits:
            self.tap(f'{host} .key[data-k="{d}"]')


def paste_message() -> str:
    """A synthetic 携程 order message, the one the parser's own tests use."""
    from tests.test_parser import PICKUP_MSG
    return PICKUP_MSG


if __name__ == "__main__":
    serve(sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4])
