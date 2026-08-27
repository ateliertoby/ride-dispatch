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
8. Tap **$** to open 埋數: an event calendar of the money. A day cell carries what is still to chase in amber, or the day's total in muted text once all of it is on a statement; a settlement batch is drawn as a bar over the service days it covers, labelled with the platform's figure and coloured by whether it waits on the transfer, was paid short, or is banked, with a day the platform held back drawn as a dashed segment pointing at it; a bank credit is a chip on its value date. Tapping a bar reads the money first: what the platform confirmed, what has arrived and what is still owed (已收 $2,950（08-24） · 差 $510), one row per credit, then the order numbers folded behind a count, and for a batch paid short that same list is where the legs it has not paid for are ticked 未過數 until they add up to the shortfall. Tapping a chip reads one credit and the batches it paid; tapping the cell still reads the day's legs and links to the batches they belong to; 撤銷結算 unwinds a whole batch. Batches are not created here — a batch exists only because a statement screenshot was read (step 11), so every one of them traces back to the image it came from and on to the orders that image lists. The header counts the credits no statement accounts for yet (入數未對 N 筆) and tapping it opens that queue, oldest first; a credit already matched is found on the calendar by its value date. An order in a batch is locked against price and cancellation edits until it is unwound
9. On landing, pickup orders with 舉牌 service get a preview of the sign text plus a 生成舉牌相 button — tapping it generates the whiteboard sign photo (via GPT-Image-2). `/board` generates manually for any pickup
10. Around a pickup's landing time the bot watches the airport car parks for your plate: 已降落 says whether the once-per-24h free half hour is still available, entry and exit are pushed and recorded, and a tap on the entry message (or 50 minutes inside unpaid) returns an Apple Pay link that pays the car park online, which is cheaper than paying at the gate. `/parking` shows the current visit and the last five
11. Forward the platform's settlement screenshot (結算單) to the bot as a photo or file: it reads the table (RapidOCR, on-device) — every order-number shape the platform prints, including the alphanumeric ones its own UI truncates with an ellipsis, which are bound to the order they are the start of — checks it against its own subtotals and against the orders it names, and replies with a per-day card — what matches, what the platform priced differently, what it left out, what is not in the system. This is the only thing that creates a settlement batch, and one tap does it, with the platform's figure; the card also carries a one-line confirmation to paste back. Before anything is written the card also says where the money stands: 對到入數 when one bank credit already on record agrees with the statement's total to the cent, in which case the same tap allocates it; 對到入數 … 差 $510 when a credit in the same week is smaller than the statement, because the platform pays short when its own system failed to submit some of the legs; 入數可能係 with the credits that could contain it; or 未收到呢筆數, the ordinary case, because the platform pays days later. A short payment ends in Telegram with 已收 $2,950 · 未收 $510 · 平台查完喺 dashboard 入返邊張單: the operator does not know which legs yet, so the legs are named afterwards on the settle page, where the system guesses which subsets of legs could account for the shortfall (to the cent, in the platform's own amounts) and pre-fills the ticks when exactly one combination adds up. Those legs then read 等到帳 until the make-up payment arrives. A statement whose legs are not in the system cannot become a batch, so when a credit matches its total the only thing offered is 收埋入數, which takes that credit out of the queue. Without the OCR package installed the bot lists the unsettled legs instead

12. The bank emails a credit advice for every payout, and a sibling program (first-reader) publishes those advices as a JSONL feed. The bot reads the feed every minute and records each credit as a ledger row. Money is **allocated in amounts**: one credit can pay several batches and one batch can be paid by several credits, because the platform pays a statement short and makes up the difference later, alone or bundled into a bigger transfer. A batch is **paid** only once everything allocated to it covers its total, and `paid_on` is then the bank's own value date. **Nothing allocates by itself.** The matcher proposes and a tap decides: a credit arriving posts a card that names the batch it agrees with to the cent (對到 批次 #4？) inside the week between a statement being settled and the bank paying it, or the unique combination of up to four batches that sums to it — which is how one Monday transfer pays several statements — then the batches it could only pay part of (對 $2,950（差 $510）), then the rest, one tap each. A near miss is never proposed as the answer, because a $30 gap is a question rather than a rounding error, and neither is an exact amount outside that week, because the amounts are round hundreds and a match months apart is a coincidence; both are offered among the alternatives instead. After any allocation the change left on a credit is offered against whatever is still owed (剩 $510 · 可能係：), which is how a bundled make-up payment reaches the batch that is short without a second command. `/credits` is the work queue of credits not yet fully accounted for, oldest first; `/credits archive before <date>` parks the payouts that predate the system, `/credits unlink <批次>` takes all the money back off a batch. Every credit is kept whether or not a batch exists for it, which is what makes backfilling months of payouts from forwarded screenshots possible

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
| `BANK_CREDITS_FEED` | No | Path to first-reader's published bank credit feed (JSONL). Unset = the ledger is off and batches stay unpaid |

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
