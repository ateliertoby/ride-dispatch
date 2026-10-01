"""Screenshot every state of the web app from the synthetic database.

    python scripts/shots.py --out DIR                   shoot into DIR
    python scripts/shots.py --out DIR --compare OTHER   shoot, then compare with OTHER
    python scripts/shots.py --diff DIR OTHER            compare two finished runs

Each state is saved as `<state>-dark.png` and `<state>-light.png`, taken in
Playwright WebKit as an iPhone 14. The day view's states are taken a second
time in a window 340 wide (`<state>-340-…`), where its columns are tightest. A comparison prints the number of differing
pixels per file and exits non-zero unless every file matches exactly.

By default the script seeds a database (scripts/seed_demo_db.py), serves the
app from it on a free port and stops it afterwards. Both clocks are pinned to
14:00 on --today, the browser's and the server's, so two runs for the same day
produce identical images whenever they are taken. With --base-url the server
is somebody else's: it must be serving a database seeded for the same --today,
and its clock is its own, so figures that depend on the time of day can move.
With --app-root the app is served from another checkout of the repository,
which is how two versions are shot from the same data for a comparison.

The script drives the app through what is on screen only (classes, data
attributes, aria labels, text), never through its own functions, so the same
run can be pointed at a build whose scripts are laid out differently.
"""
import argparse
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import seed_demo_db  # noqa: E402
from harness import ROOT, SCHEMES, Driver, Server, new_context, paste_message  # noqa: E402


# The narrow window the day view's states are shot at a second time.
NARROW = 340


class Shooter(Driver):
    """One page per state: open it, drive it to the state, save the image."""

    def __init__(self, ctx, base_url: str, out: str, scheme: str, targets: dict):
        super().__init__(ctx, base_url)
        self.out = out
        self.scheme = scheme
        self.t = targets
        self.suffix = ""

    def save(self, name: str, full_page: bool = False) -> None:
        if self.errors:
            raise RuntimeError(f"{name}: page error: {self.errors[0]}")
        self.page.screenshot(path=os.path.join(self.out, f"{name}{self.suffix}-{self.scheme}.png"),
                             full_page=full_page, animations="disabled", caret="hide")

    def done(self) -> None:
        if self.errors:
            raise RuntimeError(f"page error: {self.errors[0]}")
        self.page.close()

    # -- the day view --

    def day(self) -> None:
        self.open("/", ".row")

    def day_order(self) -> None:
        self.day()
        self.tap(f'.row[data-oid="{self.t["order"]["landed_banner"]}"]')
        self.on(".sheet.show .field-row").first.wait_for()

    def day_add(self) -> None:
        self.day()
        self.tap('[aria-label="入單"]')
        self.on(".drop.show .paste-box").wait_for()

    # -- the settle view --

    def settle_page(self) -> None:
        self.open("/settle", ".cell[data-d]")

    def open_mark(self, selector: str, ready: str) -> None:
        """A bar or chip: the first tap lights its relation, the second opens it."""
        self.settle_page()
        self.reach(selector)
        self.tap(selector)
        self.tap(selector)
        self.on(ready).first.wait_for()

    def batch_sheet(self, key: str) -> None:
        self.open_mark(f'[data-bar="{self.t["batch"][key]}"]', ".sheet.show .hero")

    def day_sheet(self) -> None:
        self.settle_page()
        cell = f'.cell[data-d="{self.t["batched_day"]}"]'
        self.reach(cell)
        self.tap(cell)
        self.on(".sheet.show .orow").first.wait_for()


def states() -> dict:
    """State name -> how to reach it and save it. Order is the order of the run."""
    def day(s):
        s.day()
        s.save("day")
        s.save("day-full", full_page=True)

    def day_filter(s):
        s.day()
        s.tap(".tab", has_text="接送")
        s.save("day-filter")

    def day_order_sheet(s):
        s.day_order()
        s.save("day-order-sheet")

    def day_numpad(s):
        s.day_order()
        s.tap(".sheet.show .field-row", has_text="隧道費")
        s.keys(".sheet.show", "45")
        s.save("day-numpad")

    def day_cancel_confirm(s):
        s.day_order()
        s.tap(".sheet.show .cancel-link")
        s.on(".sheet.show .primary-btn.danger").wait_for()
        s.save("day-cancel-confirm")

    def day_add(s):
        s.day_add()
        s.save("day-add")

    def day_add_price(s):
        s.day_add()
        s.tap(".drop.show .quick-type-btn.didi")
        s.keys(".drop.show", "1530")
        s.tap(".drop.show .primary-btn", has_text="確認")
        s.on(".drop.show .sheet-title", has_text="車費").wait_for()
        s.keys(".drop.show", "128")
        s.save("day-add-price")

    def day_paste_preview(s):
        s.day_add()
        s.on(".drop.show .paste-box").fill(paste_message())
        s.tap(".drop.show .primary-btn", has_text="解析")
        s.on(".drop.show .paste-preview").wait_for()
        s.settle()
        s.save("day-paste-preview")

    def settle(s):
        s.settle_page()
        s.save("settle")
        s.save("settle-full", full_page=True)

    def settle_focus_bar(s):
        s.settle_page()
        bar = f'[data-bar="{s.t["batch"]["short"]}"]'
        s.reach(bar)
        s.tap(bar)
        s.save("settle-focus-bar")

    def settle_day_sheet(s):
        s.day_sheet()
        s.save("settle-day-sheet")

    def settle_order_sheet(s):
        s.day_sheet()
        s.tap(".sheet.show .orow.tap")
        s.on(".sheet.show .field-row").first.wait_for()
        s.settle()
        s.save("settle-order-sheet")

    def settle_batch_sheet(s):
        s.batch_sheet("paid")
        s.save("settle-batch-sheet")

    def settle_batch_short(s):
        s.batch_sheet("short")
        s.on(".sheet.show .up-sec").wait_for()
        s.save("settle-batch-short")

    def settle_batch_list(s):
        s.batch_sheet("held_back")
        s.tap(".sheet.show .fold")
        s.on(".sheet.show .oday").first.wait_for()
        s.save("settle-batch-list")

    def settle_batch_ahead(s):
        s.batch_sheet("ahead")
        s.save("settle-batch-ahead")

    def settle_credit_sheet(s):
        s.open_mark(f'[data-chip="{s.t["credit"]["exact"]}"]', ".sheet.show .hero")
        s.save("settle-credit-sheet")

    def settle_queue(s):
        s.settle_page()
        s.tap(".sum-link")
        s.on(".sheet.show .qrow").first.wait_for()
        s.save("settle-queue")

    def settle_undo(s):
        s.batch_sheet("awaiting")
        s.tap(".sheet.show [data-undo]")
        s.on(".sheet.show [data-undogo]").wait_for()
        s.save("settle-undo")

    def settle_unlink(s):
        s.batch_sheet("held_back")
        s.tap(".sheet.show .xbtn")
        s.on(".sheet.show [data-unlinkgo]").wait_for()
        s.save("settle-unlink")

    def narrow(fn):
        def run(s):
            s.viewport = {"width": NARROW, "height": 844}
            s.suffix = f"-{NARROW}"
            fn(s)
        return run

    wide = {
        "day": day, "day-filter": day_filter, "day-order-sheet": day_order_sheet,
        "day-numpad": day_numpad, "day-cancel-confirm": day_cancel_confirm,
        "day-add": day_add, "day-add-price": day_add_price,
        "day-paste-preview": day_paste_preview,
        "settle": settle, "settle-focus-bar": settle_focus_bar,
        "settle-day-sheet": settle_day_sheet, "settle-order-sheet": settle_order_sheet,
        "settle-batch-sheet": settle_batch_sheet, "settle-batch-short": settle_batch_short,
        "settle-batch-list": settle_batch_list, "settle-batch-ahead": settle_batch_ahead,
        "settle-credit-sheet": settle_credit_sheet, "settle-queue": settle_queue,
        "settle-undo": settle_undo, "settle-unlink": settle_unlink,
    }
    return wide | {f"{name}-{NARROW}": narrow(fn) for name, fn in wide.items()
                   if name.startswith("day")}


def shoot(base_url: str, out: str, today: date, only: str) -> None:
    from playwright.sync_api import sync_playwright
    os.makedirs(out, exist_ok=True)
    targets = seed_demo_db.targets(today)
    wanted = {name: fn for name, fn in states().items() if name.startswith(only)}
    if not wanted:
        raise SystemExit(f"no state begins with {only!r}")
    with sync_playwright() as p:
        browser = p.webkit.launch()
        for scheme in SCHEMES:
            ctx = new_context(p, browser, scheme, today)
            for name, fn in wanted.items():
                shooter = Shooter(ctx, base_url, out, scheme, targets)
                try:
                    fn(shooter)
                    shooter.done()
                except Exception as e:
                    raise RuntimeError(f"{name} ({scheme}): {e}") from e
                print(f"shot {name}-{scheme}")
            ctx.close()
        browser.close()


# ---- comparing ----

def differing_pixels(a_path: str, b_path: str) -> int:
    from PIL import Image, ImageChops
    with Image.open(a_path) as a, Image.open(b_path) as b:
        if a.size != b.size:
            return max(a.size[0] * a.size[1], b.size[0] * b.size[1])
        diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
        r, g, bl = diff.split()
        any_channel = ImageChops.lighter(ImageChops.lighter(r, g), bl)
        return a.size[0] * a.size[1] - any_channel.histogram()[0]


def compare(ours: str, theirs: str, only: str) -> int:
    """Print one line per file; return how many files do not match."""
    def names(d):
        return {f for f in os.listdir(d) if f.endswith(".png") and f.startswith(only)}
    a, b = names(ours), names(theirs)
    bad = 0
    for name in sorted(a | b):
        if name not in a or name not in b:
            print(f"{name}: only in {theirs if name in b else ours}")
            bad += 1
            continue
        n = differing_pixels(os.path.join(ours, name), os.path.join(theirs, name))
        print(f"{name}: {n}")
        bad += n > 0
    print(f"{len(a | b)} files, {bad} differ")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="directory to save the screenshots in")
    ap.add_argument("--compare", metavar="OTHER_DIR",
                    help="after shooting, compare --out with this directory")
    ap.add_argument("--diff", nargs=2, metavar=("DIR", "OTHER_DIR"),
                    help="compare two directories without shooting")
    ap.add_argument("--base-url", help="shoot a server that is already running")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today(),
                    help="the day the data and both clocks are built around (default: today)")
    ap.add_argument("--only", default="", metavar="PREFIX",
                    help="restrict to states whose name begins with this, e.g. day")
    ap.add_argument("--app-root", default=ROOT,
                    help="serve the app from another checkout of the repository")
    args = ap.parse_args()

    if args.diff:
        sys.exit(1 if compare(args.diff[0], args.diff[1], args.only) else 0)
    if not args.out:
        ap.error("--out is required unless --diff is given")
    if args.base_url:
        shoot(args.base_url.rstrip("/"), args.out, args.today, args.only)
    else:
        with Server(args.today, os.path.abspath(args.app_root)) as url:
            shoot(url, args.out, args.today, args.only)
    if args.compare:
        sys.exit(1 if compare(args.out, args.compare, args.only) else 0)


if __name__ == "__main__":
    main()
