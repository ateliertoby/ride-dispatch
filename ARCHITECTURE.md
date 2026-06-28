# Architecture

## Design decisions

**Telegram bot for input, web for viewing.** Building a custom input UI would take longer than the problem is worth. Telegram handles the UX — message parsing, confirmation buttons, inline replies — and I just use what's there. The dashboard is read-only: no forms, no auth, no input complexity.

**Manual price and cost entry.** The dispatch platform gives a flat price per order. Costs (tunnel fees, parking) are variable but predictable. Encoding cost logic upfront would slow down shipping. Manual entry is fast enough for 3-7 orders/day, and rules can be added incrementally.

**SQLite.** Single user, single machine. No reason for anything heavier.

**HKIA undocumented endpoint for flight data.** The official public API (data.gov.hk) only provides D-1 historical data — useless for real-time scheduling. The endpoint used here is the same one powering HKIA's own website: public, no auth, no API key. It's undocumented, so it could break without notice. The system degrades gracefully — if the endpoint fails, the dashboard just shows no flight data; orders and revenue are unaffected.

**Flight poller as a daemon thread.** The poller runs inside the web server process as a `threading.Thread(daemon=True)`, polling every 5 minutes. No separate process, no extra launchd service. When the web server exits, the poller dies automatically. The bot is a separate process and does not poll flights.

**SSE for live updates.** The dashboard uses server-sent events to push updates rather than polling on a fixed interval. Currently unstable — not fully debugged yet. The dashboard still works without it via manual refresh.

## Data flow

```
WeChat order message
  → paste into Telegram bot
  → parser.py extracts fields
  → confirm callback
  → db.py writes to SQLite
  → web.py serves dashboard + API
  → dashboard.html renders

HKIA endpoint
  → flight.py polls every 5 min (daemon thread in web.py)
  → matches flights to orders by flight number + date
  → db.py updates flight columns (scheduled, ETA, gate, status)
  → dashboard.html shows landing time + computed 用車時間
```

## Key files

- `ride_dispatch/parser.py` — Order dataclass and field parser. Two parsers: key-value pairs (standard format) and comma-separated (Tongcheng format).
- `ride_dispatch/bot.py` — Telegram bot handlers. Confirm/cancel flow, price input, cost tracking, banner fee detection.
- `ride_dispatch/db.py` — SQLite schema and queries. Auto-migrates columns on startup.
- `ride_dispatch/flight.py` — HKIA flight fetcher. Polls the undocumented endpoint, matches flights to orders, writes structured data (scheduled/ETA/gate/status).
- `ride_dispatch/web.py` — Flask app. JSON API + SSE event stream. Starts flight poller as daemon thread.
- `templates/dashboard.html` — Single-page dashboard. Vanilla JS, mobile-first, no build step. Shows flight phase, landing time, and computed 用車時間.
