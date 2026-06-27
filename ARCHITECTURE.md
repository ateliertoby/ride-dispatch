# Architecture

## Design decisions

**Telegram bot for input, web for viewing.** Building a custom input UI would take longer than the problem is worth. Telegram handles the UX — message parsing, confirmation buttons, inline replies — and I just use what's there. The dashboard is read-only: no forms, no auth, no input complexity.

**Manual price and cost entry.** The dispatch platform gives a flat price per order. Costs (tunnel fees, parking) are variable but predictable. Encoding cost logic upfront would slow down shipping. Manual entry is fast enough for 3-7 orders/day, and rules can be added incrementally.

**SQLite.** Single user, single machine. No reason for anything heavier.

**Live updates (planned).** The dashboard has SSE wiring for auto-refresh, but it's not working yet. Current workaround is a manual refresh button. Good enough for 3-7 orders/day.

## Data flow

WeChat order message → paste into Telegram → `parser.py` extracts fields → confirm callback → `db.py` writes to SQLite → `web.py` serves dashboard + API → `dashboard.html` renders

## Key files

- `ride_dispatch/parser.py` — Order dataclass and field parser. Handles both simplified Chinese field labels from the dispatch platform. Two parsers: key-value pairs (standard format) and comma-separated (Tongcheng format).
- `ride_dispatch/bot.py` — Telegram bot handlers. Confirm/cancel flow, price input, cost tracking.
- `ride_dispatch/db.py` — SQLite schema and queries. Auto-migrates columns on startup.
- `ride_dispatch/web.py` — Flask app. JSON API + SSE event stream.
- `templates/dashboard.html` — Single-page dashboard. Vanilla JS, mobile-first, no build step.
