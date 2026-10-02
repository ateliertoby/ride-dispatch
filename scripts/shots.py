"""Screenshot every state of the web app from the synthetic database.

    python scripts/shots.py --out DIR                   shoot into DIR
    python scripts/shots.py --out DIR --compare OTHER   shoot, then compare with OTHER
    python scripts/shots.py --diff DIR OTHER            compare two finished runs

Each state is saved as `<state>-dark.png` and `<state>-light.png`, taken in
Playwright WebKit as an iPhone 14. Every state is taken a second time in a
window 340 wide (`<state>-340-…`), where the columns are tightest, and the
settle view's a third time 1000 wide (`<state>-1000-…`), under its desktop
rule; `stress-sheet-…` is its sheets with seven-figure amounts and a bank
reference longer than a line;
`stress-<width>-…` is the day view with long names, every mark and wide
fares at each width it is laid out for, and `stress-settle-…` is the settle
view with a five-digit fare with cents in a day's cell and seven-figure
totals in the foot. A comparison prints the number of differing
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
import json
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import seed_demo_db  # noqa: E402
from harness import (ROOT, SCHEMES, STATEMENT_SAMPLE, Driver, Server, new_context,  # noqa: E402
                     paste_message, stress_sheets, stress_strip)


# The narrow window every state is shot at a second time, and the wide one
# the settle view's states are shot at a third time, where its desktop rule
# applies.
NARROW = 340
WIDE = 1000

# The widths the day view is shot at with the rows under stress: the narrowest
# and the widest it is laid out for, and the two phone widths between.
STRESS_WIDTHS = (320, 340, 390, 480)
LONG_PLACE = "將軍澳示範國際會議展覽中心酒店式服務住宅南翼(示範道1號)"
LONG_PLACE_2 = "港珠澳大橋香港口岸旅檢大樓示範出口(示範)"

# The order number in the synthetic message, and a leg a batch has claimed,
# five days back.
PASTE_ID = "1128000000000002"
BATCHED = (seed_demo_db._oid(503), 5)


def stress(orders: dict) -> None:
    """Make the seeded day carry what strains a row: names that wrap to
    several lines in every kind of row, a five-figure fare with cents, a 判罰
    under it, an origin standing in for a flight number, every mark at once."""
    a = orders[seed_demo_db.ORDER["done_pickup"]]
    a.update(dropoff=LONG_PLACE, price=12480.5, penalty_fee=63.45)
    b = orders[seed_demo_db.ORDER["dropoff"]]
    b.update(pickup=LONG_PLACE)
    c = orders[seed_demo_db.ORDER["landed_banner"]]
    c.update(dropoff=LONG_PLACE, price=1520.5, penalty_fee=97.38,
             passenger_exit_minutes=30, exit_urgency="tight")
    d = orders[seed_demo_db.ORDER["upcoming_hotel"]]
    d.update(flight_number="", pickup="深圳灣口岸旅檢大樓示範出口(示範)", banner_fee=40)
    e = orders[seed_demo_db.ORDER["unpriced"]]
    e.update(pickup=LONG_PLACE, dropoff=LONG_PLACE_2)


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

    def day_stressed(self) -> None:
        """The day view with today's orders rewritten on their way to it."""
        def handler(route):
            body = route.fetch().json()
            stress({o["order_id"]: o for o in body["orders"]})
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        self.ctx.route("**/api/orders?date=" + self.t["today"], handler)
        try:
            self.day()
        finally:
            self.ctx.unroute("**/api/orders?date=" + self.t["today"])

    def day_order(self) -> None:
        self.day()
        self.tap(f'.row[data-oid="{self.t["order"]["landed_banner"]}"]')
        self.on(".sheet.show .field-row").first.wait_for()

    def day_add(self) -> None:
        self.day()
        self.tap('[aria-label="入單"]')
        self.on(".drop.show .paste-box").wait_for()

    def day_quick(self, *stages: str) -> None:
        """Into the 滴滴 stages of the add panel, confirming each of `stages`."""
        self.day_add()
        self.tap(".drop.show .quick-type-btn.didi")
        for digits in stages:
            self.keys(".drop.show", digits)
            self.tap(".drop.show .primary-btn", has_text="確認")

    def day_paste(self, order_id: str, ready: str) -> None:
        """Paste the synthetic message as if it were about `order_id`. Nothing
        is saved, so the state is the same on every run."""
        text = paste_message()
        if PASTE_ID not in text:
            raise RuntimeError("the synthetic message no longer carries " + PASTE_ID)
        self.day_add()
        self.on(".drop.show .paste-box").fill(text.replace(PASTE_ID, order_id))
        self.tap(".drop.show .primary-btn", has_text="解析")
        self.on(ready).first.wait_for()
        self.settle()

    # -- the settle view --

    def settle_page(self) -> None:
        self.open("/settle", ".cell[data-d]")

    def reach(self, selector: str) -> None:
        """Bring a day onto the strip and its week row to the top of it. A
        tap on a day that is behind the header scrolls to it first, while
        the strip may still be growing above it, and where that comes to rest
        depends on timing; put there beforehand, the tap moves nothing."""
        super().reach(selector)
        self.to_top(selector)

    def day_sheet(self, day: str = "") -> None:
        self.settle_page()
        cell = f'.cell[data-d="{day or self.t["batched_day"]}"]'
        self.reach(cell)
        self.tap(cell)
        self.on(".sheet.show .orow").first.wait_for()

    def batch_sheet(self, key: str) -> None:
        """A batch's sheet, from a day it covers: the day's sheet, then its
        link to the batch."""
        self.day_sheet(self.t["batch_day"][key])
        self.tap(f'.sheet.show .blink[data-bl="{self.t["batch"][key]}"]')
        self.on(".sheet.show .hero").first.wait_for()

    def statement_list(self, lens: str, day: str) -> None:
        """The list a total key opens, paged back to the month `day` is in.
        Under a list the month is changed by the arrows alone, so the month
        button is read until it names that month."""
        self.settle_page()
        self.tap(f'.lkey[data-lens="{lens}"]')
        want = day[:4] + "·" + day[5:7]
        for _ in range(8):
            if self.on(".date-btn").first.text_content().startswith(want):
                return
            self.tap('[aria-label="前一個月"]')
        raise RuntimeError(f"the list never reached {want}")

    def credit_sheet(self, key: str) -> None:
        """An unmatched credit's sheet, through the queue in the foot."""
        self.settle_page()
        self.tap(".foot [data-credits]")
        self.tap(f'.sheet.show .qrow[data-credit="{self.t["credit"][key]}"]')
        self.on(".sheet.show .hero").first.wait_for()


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

    def day_numpad_time(s):
        s.day_order()
        s.tap(".sheet.show .field-row", has_text="時間")
        s.keys(".sheet.show", "15")
        s.save("day-numpad-time")

    def day_order_locked(s):
        oid, back = BATCHED
        s.day()
        for _ in range(back):
            s.tap('[aria-label="前一日"]')
        s.tap(f'.row[data-oid="{oid}"]')
        s.on(".sheet.show .field-row.locked").first.wait_for()
        s.save("day-order-locked")

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

    def day_add_time(s):
        s.day_quick()
        s.keys(".drop.show", "15")
        s.save("day-add-time")

    def day_add_toll(s):
        s.day_quick("1530", "128")
        s.on(".drop.show #npQuick").wait_for()
        s.save("day-add-toll")

    def day_add_confirm(s):
        s.day_quick("1530", "128", "25")
        s.on(".drop.show #addSave").wait_for()
        s.save("day-add-confirm")

    def day_paste_amend(s):
        # The message names an order the day already holds: an amendment,
        # previewed as the fields it would change.
        s.day_paste(s.t["order"]["upcoming_hotel"], ".drop.show .paste-preview.changes")
        s.save("day-paste-amend")

    def day_paste_locked(s):
        s.day_paste(BATCHED[0], ".drop.show .dup-warn")
        s.save("day-paste-locked")

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
        # The whole page shows how many months the strip loaded above the
        # screen while it was growing to fill it. In the narrow window that
        # count depends on which of two answers arrived first, and what is on
        # screen is the same either way.
        if not s.suffix:
            s.save("settle-full", full_page=True)

    def settle_focus_cells(s):
        s.batch_sheet("short")
        s.tap(".sheet.show [data-focus]")
        s.save("settle-focus-cells")

    def settle_unsettled(s):
        # From the short-paid statement's week down to today: days on a
        # statement and days on none, side by side.
        s.settle_page()
        s.reach(f'.cell[data-d="{s.t["batch_day"]["short"]}"]')
        s.tap('.lkey[data-lens="unsettled"]')
        s.save("settle-unsettled")

    def settle_awaiting(s):
        # The month the statement still waiting for its transfer was driven in.
        s.statement_list("awaiting", s.t["batch_day"]["awaiting"])
        s.on(".slist .brow").first.wait_for()
        s.save("settle-awaiting")

    def settle_received(s):
        # The month of the statement paid short, which leads the list.
        s.statement_list("received", s.t["batch_day"]["short"])
        s.on(".slist .brow").first.wait_for()
        s.save("settle-received")

    def settle_archived(s):
        s.statement_list("received", s.t["batch_day"]["short"])
        s.tap(".slist [data-archived]")
        s.on(".sheet.show .qrow").first.wait_for()
        s.save("settle-archived")

    def settle_day_sheet(s):
        s.day_sheet()
        s.save("settle-day-sheet")

    def settle_order_sheet(s):
        s.day_sheet()
        s.tap(".sheet.show .orow.tap")
        s.on(".sheet.show .field-row").first.wait_for()
        s.settle()
        s.save("settle-order-sheet")

    def settle_order_numpad(s):
        s.day_sheet()
        s.tap(".sheet.show .orow.tap")
        s.on(".sheet.show .field-row").first.wait_for()
        s.tap(".sheet.show .field-row", has_text="停車費")
        s.keys(".sheet.show", "20")
        s.save("settle-order-numpad")

    def settle_order_cancel(s):
        s.settle_page()
        cell = f'.cell[data-d="{s.t["loose_day"]}"]'
        s.reach(cell)
        s.tap(cell)
        s.tap(".sheet.show .orow.tap")
        s.on(".sheet.show .cancel-link").wait_for()
        s.settle()
        s.save("settle-order-loose")
        s.tap(".sheet.show .cancel-link")
        s.on(".sheet.show .primary-btn.danger").wait_for()
        s.save("settle-order-cancel")

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
        s.credit_sheet("exact")
        s.save("settle-credit-sheet")

    def settle_queue(s):
        s.settle_page()
        s.tap(".foot [data-credits]")
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

    def settle_statement(s):
        s.settle_page()
        s.page.route("**/api/statements/read", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(STATEMENT_SAMPLE)))
        s.page.locator("#stmtFile").set_input_files(
            {"name": "statement.png", "mimeType": "image/png", "buffer": seed_demo_db._png()})
        s.on(".sheet.show .stmt-report").wait_for()
        s.settle()
        s.save("settle-statement")

    def settle_drop(s):
        s.settle_page()
        s.page.evaluate("""() => {
          const dt = new DataTransfer();
          dt.items.add(new File(['x'], 'statement.png', { type: 'image/png' }));
          document.body.dispatchEvent(new DragEvent('dragenter', { dataTransfer: dt, bubbles: true, cancelable: true }));
        }""")
        s.on(".drop.show").wait_for()
        s.save("settle-drop")

    def sheets_stressed(width):
        """The sheets with what strains a line: seven-figure amounts with
        cents in a batch paid short, and a credit whose reference and memo
        are longer than the sheet is wide."""
        def credits(route):
            body = route.fetch().json()
            stress_sheets(body)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        def run(s):
            s.viewport = {"width": width, "height": 844}
            stress_strip(s.ctx, date.fromisoformat(s.t["today"]))
            try:
                s.batch_sheet("short")
                s.on(".sheet.show .up-sec").wait_for()
                s.save(f"stress-sheet-batch-{width}")
                s.done()
            finally:
                s.ctx.unroute("**/api/settle?*")
                s.ctx.unroute("**/api/credits?*")
            s.ctx.route("**/api/credits?*", credits)
            try:
                s.credit_sheet("partial")
                s.save(f"stress-sheet-credit-{width}")
            finally:
                s.ctx.unroute("**/api/credits?*")
        return run

    def settle_stressed(width):
        def run(s):
            # Tall enough for two months of the strip above the fixed foot.
            s.viewport = {"width": width, "height": 1500}
            stress_strip(s.ctx, date.fromisoformat(s.t["today"]))
            try:
                s.settle_page()
                s.tap('[aria-label="前一個月"]')
                s.save(f"stress-settle-{width}")
            finally:
                s.ctx.unroute("**/api/settle?*")
                s.ctx.unroute("**/api/credits?*")
        return run

    def narrow(fn):
        def run(s):
            s.viewport = {"width": NARROW, "height": 844}
            s.suffix = f"-{NARROW}"
            fn(s)
        return run

    def wide_of(fn):
        def run(s):
            s.viewport = {"width": WIDE, "height": 800}
            s.suffix = f"-{WIDE}"
            fn(s)
        return run

    def stressed(width):
        def run(s):
            # Tall enough to hold every row: a full-page capture would draw
            # the fixed foot across the middle of the list.
            s.viewport = {"width": width, "height": 1500}
            s.day_stressed()
            s.save(f"stress-{width}")
        return run

    wide = {
        "day": day, "day-filter": day_filter, "day-order-sheet": day_order_sheet,
        "day-numpad": day_numpad, "day-numpad-time": day_numpad_time,
        "day-order-locked": day_order_locked, "day-cancel-confirm": day_cancel_confirm,
        "day-add": day_add, "day-add-time": day_add_time, "day-add-price": day_add_price,
        "day-add-toll": day_add_toll, "day-add-confirm": day_add_confirm,
        "day-paste-preview": day_paste_preview, "day-paste-amend": day_paste_amend,
        "day-paste-locked": day_paste_locked,
        "settle": settle, "settle-focus-cells": settle_focus_cells, "settle-unsettled": settle_unsettled,
        "settle-awaiting": settle_awaiting, "settle-received": settle_received,
        "settle-archived": settle_archived,
        "settle-day-sheet": settle_day_sheet, "settle-order-sheet": settle_order_sheet,
        "settle-order-numpad": settle_order_numpad, "settle-order-cancel": settle_order_cancel,
        "settle-batch-sheet": settle_batch_sheet, "settle-batch-short": settle_batch_short,
        "settle-batch-list": settle_batch_list, "settle-batch-ahead": settle_batch_ahead,
        "settle-credit-sheet": settle_credit_sheet, "settle-queue": settle_queue,
        "settle-undo": settle_undo, "settle-unlink": settle_unlink,
        "settle-statement": settle_statement, "settle-drop": settle_drop,
    }
    return (wide | {f"{name}-{NARROW}": narrow(fn) for name, fn in wide.items()}
            | {f"{name}-{WIDE}": wide_of(fn) for name, fn in wide.items() if name.startswith("settle")}
            | {f"stress-sheet-{w}": sheets_stressed(w) for w in (NARROW, 390)}
            | {f"stress-{w}": stressed(w) for w in STRESS_WIDTHS}
            | {f"stress-settle-{w}": settle_stressed(w) for w in (NARROW, 390)})


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
