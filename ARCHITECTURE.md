# Architecture

## Design decisions

**Telegram bot for parsing, web for everything else.** Telegram is where WeChat order messages get pasted, so parsing lives there — message in, confirm button, saved. Everything after the save (price, fees, time corrections, cancellation) happens on the dashboard: tapping a card opens an edit sheet instead of deep-linking back into Telegram, which used to cost an app switch per correction. The dashboard also creates quick orders directly (Didi/Uber/foodpanda — time, money, done) so a missed order can be backfilled onto any date being viewed, which the bot's today/yesterday inference can't do. `/didi` and `/uber` remain in the bot as an alternative path for just-finished trips.

**Write ops behind a PIN, reads stay open.** The dashboard is exposed via Cloudflare Tunnel, so mutation endpoints can't be anonymous. A single PIN (`RIDE_WEB_PIN` in `.env`; unset = read-only dashboard) exchanges for a stateless HMAC token on first write — stored in localStorage, survives server restarts, nothing persisted server-side. Failed attempts are rate-limited. Reads stay unauthenticated so opening the dashboard mid-drive costs zero friction.

**Manual price and cost entry.** The dispatch platform gives a flat price per order. Costs (tunnel fees, parking) are variable but predictable. Encoding cost logic upfront would slow down shipping. Manual entry is fast enough for 3-7 orders/day, and rules can be added incrementally.

**SQLite.** Single user, single machine. No reason for anything heavier.

**HKIA undocumented endpoint for flight data.** The official public API (data.gov.hk) only provides D-1 historical data — useless for real-time scheduling. The endpoint used here is the same one powering HKIA's own website: public, no auth, no API key. It's undocumented, so it could break without notice. The system degrades gracefully — if the endpoint fails, the dashboard just shows no flight data; orders and revenue are unaffected.

**Flight poller as an immortal heartbeat in the bot.** The poller lives in the bot process on a 60s `run_repeating` heartbeat (`misfire_grace_time=None`, so late ticks run instead of being discarded). Each tick is cheap: it checks the time-gated tracking window from the DB and only hits HKIA when a poll is actually due, at the tier interval computed by `calc_next_interval` (60s landed / 600s watchdog / ETA-halving / 1800s no-data). Termination is time-based only — flight status can slow polling but can never stop it; a wrong or stale status self-corrects on the next poll. Flight matching is date-aware: the HKIA feed spans adjacent days and flight numbers repeat daily, so each order matches the candidate closest to its pickup time within ±12h.

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

Dashboard edit / quick order (Didi, Uber, foodpanda)
  → PIN → /api/auth → HMAC token (once, cached in localStorage)
  → PATCH /api/orders/<id> (price, fees, time, cancel)
    or POST /api/orders (type + date + time + money)
  → db.py writes to SQLite
  → SSE fingerprint changes → other open dashboards refresh

HKIA endpoint
  → bot.py heartbeat (60s tick, tier-gated fetch)
  → flight.py matches flights to orders by flight number + closest date/time
  → db.py updates flight columns (scheduled, ETA, gate, status)
  → bot.py pushes 已降落/已到閘口 notifications on status transitions
  → dashboard.html shows landing time + computed 用車時間
```

## Key files

- `ride_dispatch/parser.py` — Order dataclass and field parser. Two parsers: key-value pairs (standard format) and comma-separated (Tongcheng format).
- `ride_dispatch/bot.py` — Telegram bot handlers. Confirm/cancel flow, price input, cost tracking, banner fee detection.
- `ride_dispatch/db.py` — SQLite schema and queries. Auto-migrates columns on startup.
- `ride_dispatch/flight.py` — HKIA flight fetcher and matcher. Date-aware flight matching, poll tier calculation, tracking window logic.
- `ride_dispatch/web.py` — Flask app. JSON API + SSE event stream + PIN-gated write endpoints (quick order create, field patch, cancel).
- `templates/dashboard.html` — Single-page dashboard. Vanilla JS, mobile-first, no build step, dark-first auto theme. Shows flight phase, landing time, and computed 用車時間. Bottom-sheet editing, numpad input, platform filter chips (接送/滴滴/Uber/foodpanda).
