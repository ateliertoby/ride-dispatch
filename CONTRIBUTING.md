# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | From BotFather |
| `RIDE_DB_PATH` | No | SQLite path (default: `orders.db`) |
| `RIDE_WEB_PORT` | No | Dashboard port (default: `3200`) |
| `ALLOWED_CHAT_IDS` | No | Comma-separated Telegram chat IDs. Empty = allow all |

## Running tests

```bash
pytest tests/
```

## Project structure

```
ride_dispatch/
  parser.py    — Order parsing (entry point for new dispatch formats)
  bot.py       — Telegram handlers (entry point for new commands)
  db.py        — Schema + queries (entry point for new fields)
  flight.py    — HKIA flight data fetcher + matcher
  web.py       — Dashboard API + SSE + flight poller
templates/
  dashboard.html — Single-file dashboard UI
deploy/
  *.plist      — macOS launchd service configs
tests/
  test_parser.py, test_db.py, test_flight.py
```

## Common changes

**Add a new dispatch format:** Add a parser function in `parser.py`, call it from `parse_order()`. See `parse_tongcheng()` for reference.

**Add a new order field:** Add to the `Order` dataclass in `parser.py`, add column in `db.py` `init_db()`, update `_INSERT_SQL` and `save_order()`.

**Add a new bot command:** Add handler function in `bot.py`, register in `main()`.

**Change flight polling interval:** In `web.py`, the poller loop sleeps for 300 seconds (5 min) between fetches. Adjust there.

**Change flight matching logic:** `flight.py` `match_flights()` matches by flight number and date. If HKIA changes their response format, this is where it breaks.
