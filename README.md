# Ride Dispatch

Parse and track airport ride orders from WeChat dispatch groups, with real-time flight tracking to schedule pickups.

## Why this exists

I do airport pickups/dropoffs full-time, taking orders from WeChat groups. Each order is a block of text with flight, passenger, route details. Without a system, finding past order details means scrolling through WeChat, and tracking daily revenue means mental math.

Flight timing drives everything — landing time determines when to leave for the airport (30-40 min drive + 30-40 min for passenger to clear immigration and collect luggage). Delays or early arrivals affect whether I can pair a dropoff with a pickup for a round trip. I was switching between multiple apps to check times; now the dashboard shows it alongside each order.

This bot parses pasted order messages into structured records and stores them in SQLite. A web dashboard shows the day's orders, revenue, and live flight status at a glance.

## How it works

1. Paste an order message from WeChat into the Telegram bot
2. Bot parses it and shows a summary card with Confirm/Cancel buttons
3. Type the price directly — saves the order and price in one step
4. Alternatively, tap Confirm first to save, then type the price separately
5. Everything after that lives on the dashboard: tap a card to edit price, tunnel/parking/banner fees, or time, or to cancel (double-confirm).
6. Tap **+** to add a Didi/Uber/foodpanda order onto whichever date is being viewed — time, money, confirm. Backfilling old orders is just navigating to that date first.
7. Dashboard shows daily revenue, net income, and live flight landing times; platform chips (接送/滴滴/Uber/foodpanda) filter the list and show that platform's total

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | From BotFather |
| `RIDE_DB_PATH` | No | SQLite path (default: `orders.db`; use an absolute path outside cloud-synced dirs) |
| `RIDE_WEB_PORT` | No | Dashboard port (default: `3200`) |
| `ALLOWED_CHAT_IDS` | No | Comma-separated Telegram chat IDs. Empty = allow all |

Tests: `pytest tests/`

Bot and dashboard are separate processes:

```bash
python -m ride_dispatch.bot   # Telegram bot
python -m ride_dispatch.web   # Web dashboard + flight poller (default port 3200)
```

## Deploy

The dashboard is exposed via a named Cloudflare Tunnel (`~/.cloudflared/ride-dispatch.yml`) with **Cloudflare Access** (email OTP, 1-month session) as perimeter auth. The tunnel runs as a launchd service; example plist files for all three processes (bot, web, tunnel) are in `deploy/`.

Two gotchas:

- **`--config` is required** on every `cloudflared tunnel` command — the default `~/.cloudflared/config.yml` `tunnel:` key silently overrides the positional tunnel name (especially `route dns`, which will CNAME to the wrong tunnel).
- **`protocol: http2`** in the tunnel config — QUIC flaps on some networks.
