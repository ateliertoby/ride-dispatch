# Ride Dispatch

Parse and track airport ride orders from WeChat dispatch groups.

## Why this exists

I do airport pickups/dropoffs full-time, taking orders from WeChat groups. Each order is a block of text with flight, passenger, route details. Without a system, finding past order details means scrolling through WeChat, and tracking daily revenue means mental math. Apple Notes didn't stick — updating old entries was too much friction.

This bot parses pasted order messages into structured records and stores them in SQLite. A web dashboard shows the day's orders and revenue at a glance.

## How it works

1. Paste an order message from WeChat into the Telegram bot
2. Bot parses it and shows a summary card — tap Confirm to save
3. Type the price directly after confirming
4. Add costs (tunnel/parking fees) via the order detail view
5. Dashboard shows daily orders, revenue, and net income

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # add your Telegram bot token
```

Bot and dashboard are separate processes:

```bash
python -m ride_dispatch.bot   # Telegram bot
python -m ride_dispatch.web   # Web dashboard (default port 3200)
```

## Deploy

The dashboard is exposed via Cloudflare Tunnel for mobile access on any network. Example launchd plist files are in `deploy/` for running both processes as macOS services.
