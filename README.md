# Ride Dispatch

Parse and track airport ride orders from WeChat dispatch groups, with real-time flight tracking to schedule pickups.

## Why this exists

I do airport pickups/dropoffs full-time, taking orders from WeChat groups. Each order is a block of text with flight, passenger, route details. Without a system, finding past order details means scrolling through WeChat, and tracking daily revenue means mental math.

Flight timing drives everything — landing time determines when to leave for the airport (30-40 min drive + 30-40 min for passenger to clear immigration and collect luggage). Delays or early arrivals affect whether I can pair a dropoff with a pickup for a round trip. I was switching between multiple apps to check times; now the dashboard shows it alongside each order.

This bot parses pasted order messages into structured records and stores them in SQLite. A web dashboard shows the day's orders, revenue, and live flight status at a glance.

## How it works

1. Paste an order message from WeChat into the Telegram bot, or into the dashboard's paste box (parse preview → price → save in one flow)
2. In the bot: it parses the message and shows a summary card with Confirm/Cancel buttons
3. Type the price directly — saves the order and price in one step
4. Alternatively, tap Confirm first to save, then type the price separately
5. Everything after that lives on the dashboard: tap a card to edit price, tunnel/parking/banner fees, or time, or to cancel (double-confirm).
6. Tap **+** to add a Didi/Uber/foodpanda order onto whichever date is being viewed — time, money, confirm. Backfilling old orders is just navigating to that date first.
7. Dashboard shows daily revenue, net income, and live flight landing times; platform chips (接送/滴滴/Uber/foodpanda) filter the list and show that platform's total
8. Tap **$** to open 埋數: a month grid of what each day earned, coloured by whether the money is still to chase (amber), settled and waiting on the transfer (plain), or banked (green). Tap a day, tick its legs, type what the platform confirmed, and it becomes one settlement batch; 已到帳 marks the transfer in, 撤銷結算 unwinds the whole batch. An order in a batch is locked against price and cancellation edits until it is unwound
9. On landing, pickup orders with 舉牌 service get a preview of the sign text plus a 生成舉牌相 button — tapping it generates the whiteboard sign photo (via GPT-Image-2). `/board` generates manually for any pickup
10. Around a pickup's landing time the bot watches the airport car parks for your plate: 已降落 says whether the once-per-24h free half hour is still available, entry and exit are pushed and recorded, and a tap on the entry message (or 50 minutes inside unpaid) returns an Apple Pay link that pays the car park online, which is cheaper than paying at the gate. `/parking` shows the current visit and the last five
11. Forward the platform's settlement screenshot (結算單) to the bot as a photo or file: it reads the table (RapidOCR, on-device), checks it against its own subtotals and against the orders it names, and replies with a per-day card — what matches, what the platform priced differently, what it left out, what is not in the system. One tap creates the settlement batch with the platform's figure; the card also carries a one-line confirmation to paste back. When the transfer lands, `/paid 2540` finds the batch by amount and marks it paid. Without the OCR package installed the bot lists the unsettled legs instead

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install --no-deps -r requirements-ocr.txt   # statement OCR; skip to run without it
cp .env.example .env
```

`requirements-ocr.txt` is installed with `--no-deps` on purpose: RapidOCR's own metadata asks for the full `opencv-python`, which needs libGL and cannot import on a headless server; its real dependencies (with `opencv-python-headless`) are listed in `requirements.txt`, and it is pinned to `1.2.3` because later releases cap `Requires-Python` below the 3.14 this runs on.

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | From BotFather |
| `RIDE_DB_PATH` | No | SQLite path (default: `orders.db`). `~` is expanded, so one `.env` works across hosts; keep it outside cloud-synced dirs |
| `RIDE_WEB_PORT` | No | Dashboard port (default: `3200`) |
| `ALLOWED_CHAT_IDS` | No | Comma-separated Telegram chat IDs. Empty = allow all |
| `FAL_KEY` | No | fal.ai API key for whiteboard sign photo generation. Unset = feature off |
| `CAR_PLATE` | No | Plate to watch in the HKIA car parks. Unset = parking tracking off |
| `PARKING_EMAIL` | No | Address HKIA attaches to an online parking payment. Blank is accepted |

Tests: `pytest tests/`

Bot and dashboard are separate processes:

```bash
python -m ride_dispatch.bot   # Telegram bot + flight poller
python -m ride_dispatch.web   # Web dashboard (default port 3200)
```

## Deploy

The dashboard is exposed via a named Cloudflare Tunnel (`~/.cloudflared/ride-dispatch.yml`) with **Cloudflare Access** (email OTP, 1-month session) as perimeter auth. All three processes (bot, web, tunnel) run as supervised services; `deploy/` carries example definitions for both launchd (macOS plists) and systemd (Linux units). The systemd tunnel unit runs the tunnel by UUID, so the credentials JSON alone is enough — no account `cert.pem` needed on the host.

Statement OCR is a separate install on the server: `pip install -r requirements.txt`, then `pip install --no-deps -r requirements-ocr.txt`, in that order. It adds roughly 360 MB to the venv (onnxruntime plus the PP-OCR models), so it is worth checking disk before running it. Restart `ride-dispatch-bot` afterwards — a bot that started without the package keeps replying with the no-OCR fallback until it does.

Two gotchas:

- **`--config` is required** on every `cloudflared tunnel` command — the default `~/.cloudflared/config.yml` `tunnel:` key silently overrides the positional tunnel name (especially `route dns`, which will CNAME to the wrong tunnel).
- **`protocol: http2`** in the tunnel config — QUIC flaps on some networks.
