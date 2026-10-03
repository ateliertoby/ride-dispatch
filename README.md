# Ride Dispatch

Parse and track airport ride orders from WeChat dispatch groups, follow each pickup's flight, and reconcile what the platform pays against what was driven.

A single-operator tool: a Telegram bot and a web app over one SQLite file.

## Why this exists

I do airport pickups and dropoffs full-time, taking orders from WeChat groups. Each order is a block of text with flight, passenger and route details. Without a system, finding a past order means scrolling through WeChat, and tracking daily revenue means mental math.

Flight timing drives everything. Landing time decides when to leave for the airport (a 30 to 40 minute drive, plus 30 to 40 minutes for the passenger to clear immigration and collect luggage), and a delay or an early arrival decides whether a dropoff can be paired with a pickup. I was switching between several apps to check times; the day view shows them beside each order.

The money side has the same shape. The platform pays for a run of days with one bank transfer, days later, sometimes short. The settle view answers what has been paid, what is waiting and what still has to be chased.

## How it works

1. **Enter an order.** Paste the WeChat order message into the Telegram bot or into the web app (**+**, then the paste box). The bot replies with a summary card and 確認 / 取消; the web app shows a preview with the fees it worked out and a suggested price.

2. **Price it.** In the bot, typing a number while the card is up confirms and prices in one step; or tap 確認 and type the price afterwards. In the web app the preview and the price keypad are one screen, and confirming saves.

3. **Amend or re-enter.** The platform re-sends the whole message when a customer changes something. Pasting it again shows only what changed (更新 / 略過 in the bot) and keeps the price unless a new one is typed. Pasting a cancelled order's message enters it again.

4. **Add a quick order.** **+** also takes a 滴滴, Uber or foodpanda trip: time, money, save. It lands on the date being viewed, so backfilling is a matter of going to that date first. `/didi` and `/uber` do the same in the bot for a trip that just finished.

5. **Read the day.** The day view lists the day's orders as a board: time, flight number or service, place, fare, and for a pickup the flight's status with the times to leave and to meet. Tabs (全部 / 接送 / 滴滴 / Uber / 熊貓) filter the list, and the foot shows the number of trips, how many have no price yet and the day's fares for whatever is filtered.

6. **Correct an order.** Tap a row to open its sheet: price, tunnel, parking and 舉牌 fees, time, cancel (with a second confirming step). An airport pickup's sheet also sets where to meet the passenger (P1 / P4 / 富豪), which writes that place's first-hour parking charge ($35 / $32 / free). The same sheet opens from the settle view. On an order already settled, the fields its batch was summed from and cancelling read 已結算 until the batch is undone.

7. **Follow the flight.** The bot polls HKIA's arrivals for every pickup and pushes 出發接機 when it is time to leave, 已降落 and 已到閘口 as the status changes, and 用車時間到 when the passenger is due out. Dropoffs and other booked trips get a push 30 and 10 minutes before their time.

8. **Make the 舉牌 sign.** When a pickup with 舉牌 lands, the bot previews the sign text with a 生成舉牌相 button; tapping it generates the whiteboard photo the platform asks for. `/board` offers the same for any of today's pickups.

9. **Watch the car park.** Around a pickup's landing the bot watches HKIA's Car Park 3 and 4 for the car's plate. It pushes entry and exit, says whether the once-per-24-hours free half hour is still available, offers an Apple Pay link for the fee (on a tap, or unprompted after 50 minutes unpaid; paying online is cheaper than paying at the gate), and writes the car park and what the visit cost onto the order. Car Park 1 cannot be seen this way, so a pickup planned there keeps its planned fee unless edited. A pickup planned at P4 with no visit 90 minutes after landing is moved to 富豪 at $0, with buttons to overrule. `/parking` shows the current visit and the last five.

10. **Open 埋數.** **$** switches to the settle view, one platform at a time. Four keys state the month: 本月車費, 已收, 等過數 and 未結算. Each is also a way in:
    - 本月車費 shows a calendar of days, each with its fare and a mark for where its money has got to.
    - 未結算 lights the days holding money that is on no statement.
    - 等過數 and 已收 list the statements themselves.

    A day opens its legs, a leg opens the order sheet, and a statement opens its batch: what the platform confirmed, what the bank has sent, and the orders it covers. The foot shows only what needs action: 收少咗 (a statement paid short), 入數未對 (bank credits no statement accounts for; 入數啱數 while one of them agrees with a statement and waits for its confirm) and 之前月份未清 (money still open in an earlier month). With none of them it says 全部啱數.

11. **Read a statement.** Send the platform's settlement screenshot (結算單) to the bot as a photo or a file, or tap **圖** in the settle view and pick it (on desktop, drop the file on the page). The image is read on the machine by OCR and checked against its own subtotals, then against the orders it names. The reply says, day by day, what matches, what the platform priced differently, what it left out and what it charged back, and whether a bank credit already agrees with the total. One tap confirms, and the button names everything it writes (確認結算 + 記判罰 + 記帳項 + 對入數). Confirming a statement is the only thing that creates a settlement batch. The reply ends with a one-line confirmation to paste back to the platform.

    The browser hands the reader the original file, where a Telegram photo has been recompressed first, so the settle view is the path for a statement the bot read badly. Without the OCR package the bot lists the unsettled legs instead and the settle view refuses the upload.

12. **Match the bank.** A separate program (first-reader) publishes the bank's credit advices as a JSONL feed, which the bot reads every minute. Every credit is recorded and announced in the chat with what it appears to pay (對到 批次 #4？), with a link to that credit on the settle view when `RIDE_WEB_URL` is set. Nothing is allocated automatically, because the bank's figure can differ from the statement's. The settle view shows the two side by side, what the bank paid in over what the statement is owed, with a verdict (啱數, 少 $N, 多 $N) and a key that says what the tap records: 確認啱數, or 確認收到 $X（仲差 $N） when the money is short. One transfer paying a whole group of statements is one row and one tap. The queue of credits opens on the ones that have such an answer, and a statement waiting for its transfer shows the credit on its own sheet. 解除 takes money back off. When a statement is paid short, the batch's sheet is where the unpaid legs are ticked (未過數) until the make-up payment arrives.

How the matcher decides, how a statement is read and why the settle view is shaped the way it is are in [`ARCHITECTURE.md`](ARCHITECTURE.md).

### Bot commands

| Command | What it does |
|---|---|
| (paste a message) | Order card; a re-sent message shows its changes |
| (send an image) | Reads it as a settlement statement |
| `/didi`, `/uber` | Enter a trip that just finished, step by step |
| `/cancel` | Pick one of today's orders to cancel |
| `/board` | Pick one of today's pickups to generate a sign photo for |
| `/parking` | Current car park visit, the free allowance, the last five visits |
| `/parking mark <id> free\|paid\|gate` | Correct what a finished visit cost |
| `/credits` | Bank credits not yet fully matched, oldest first |
| `/credits <id>` | One credit and what it was matched to |
| `/credits archive before <YYYY-MM-DD>` | Put away every unmatched credit dated before a day (payouts that predate the system) |
| `/credits archive <id> [note]`, `/credits unarchive <id>` | Put one credit away, or bring it back |
| `/credits unlink <batch> [credit]` | Take all the money back off a batch, or one credit's |

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install --no-deps -r requirements-ocr.txt   # statement OCR; skip to run without it
cp .env.example .env
```

`requirements-ocr.txt` is installed with `--no-deps` on purpose. RapidOCR's own metadata asks for the full `opencv-python`, which needs libGL and cannot import on a headless server; its real dependencies, with `opencv-python-headless`, are listed in `requirements.txt`. It is pinned to `1.2.3` because later releases cap `Requires-Python` below the 3.14 this runs on.

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot only | From BotFather |
| `TELEGRAM_BOT_USERNAME` | No | The bot's username, without the `@`. The web app's order sheet links to the order in the bot's chat with it (喺 Telegram 開). Unset = no link |
| `RIDE_DB_PATH` | No | SQLite path (default: `orders.db` in the working directory). `~` is expanded. Statement screenshots (`statements/`) and the socket the two processes share (`bot.sock`) are created beside it; keep it outside cloud-synced directories |
| `RIDE_WEB_PORT` | No | Web app port on `127.0.0.1` (default: `3200`) |
| `ALLOWED_CHAT_IDS` | No | Comma-separated Telegram chat IDs the bot answers. Empty = it answers anyone |
| `NOTIFY_CHAT_ID` | No | Chat every push goes to (flight, reminders, car park, credits). When the variable is absent or blank, the first of `ALLOWED_CHAT_IDS` is used. With no chat to push to, the bot still answers messages but runs no flight, car park or credit checks |
| `FAL_KEY` | No | fal.ai API key for the sign photo. Unset = feature off |
| `CAR_PLATE` | No | Plate to watch in the HKIA car parks. Unset = car park tracking off |
| `PARKING_EMAIL` | No | Address HKIA attaches to an online parking payment. Blank is accepted |
| `RIDE_WEB_URL` | No | Address the web app is opened at, such as `https://ride.example.com`. The bot's notice of a bank credit then carries a link to that credit on the settle view. Unset = the notice has no link |
| `BANK_CREDITS_FEED` | No | Path to the bank credit feed (JSONL). Unset = no credits are recorded, so no batch can become paid |

The bot and the web app are separate processes. Run both from the repository root, where they write `logs/`:

```bash
python -m ride_dispatch.bot   # Telegram bot, and the flight, car park and bank credit jobs
python -m ride_dispatch.web   # Web app: day view and settle view
```

The web app is one document, served on `/` and `/settle`, that switches between its two views in the browser. It is plain ES modules and CSS under `static/` with no build step. A service worker keeps the document and its assets on the phone, so the installed app opens without waiting for the server; order and money data is always fetched.

Both processes read statements, so both need the OCR install if statements are to be read from the chat and from the browser.

## Develop

```bash
pytest tests/                        # Python: parsing, database, API, the shell and its worker
node --test "tests/js/*.test.mjs"    # the browser modules that need no DOM (Node 22+, no packages)

pip install -r requirements-dev.txt && playwright install webkit
python scripts/e2e.py                          # behaviour, driven through a real browser
python scripts/e2e.py --list                   # name the checks and stop
python scripts/e2e.py --only day.add           # the checks whose name begins with this
python scripts/e2e.py --jobs 1                 # one check at a time
python scripts/e2e.py --timings                # and where the time went, with the slowest checks
python scripts/e2e.py --today-set 2026-10-08,2026-10-31   # once for each day, through the same workers
python scripts/shots.py --out /tmp/shots       # a screenshot of every state, dark and light
python scripts/shots.py --out /tmp/shots --only stress   # the states whose name begins with this
python scripts/shots.py --out /tmp/new --compare /tmp/shots   # and a pixel count of what changed
python scripts/shots.py --diff /tmp/new /tmp/shots            # compare two runs without shooting
```

Both scripts start their own server on a free port against a synthetic database built by `scripts/seed_demo_db.py` (with `--backlog`, a ledger that also holds dozens of old credits nothing accounts for, which the `backlog-…` screenshots and one check run on). The browser's clock and the server's are pinned to 14:00 on one day (`--today`, today by default), so two runs for the same day give the same result and neither touches real data. They run in WebKit as an iPhone, the engine the installed app runs on.

`e2e.py` gives every check a server and a database of its own and runs the checks in several processes side by side, reporting them in the order they are registered. `--jobs` defaults to one worker per core and per 2 GB of memory, at most 8.

`requirements-dev.txt` is for development only; nothing in it is needed to run the app. Playwright and Pillow serve the two scripts. fonttools and brotli provide `pyftsubset`, which rebuilds the two files in `static/fonts/` from the upstream B612 Mono TrueType files when the set of characters the app prints in that face changes (the set is the `unicode-range` in `static/css/base.css`).

## Deploy

The web app is exposed through a named Cloudflare Tunnel (`~/.cloudflared/ride-dispatch.yml`) with **Cloudflare Access** (email OTP, 1-month session) as the only authentication. All three processes (bot, web, tunnel) run as supervised services; `deploy/` carries example definitions for launchd (macOS plists) and systemd (Linux units), with paths, user and tunnel id to replace. The systemd tunnel unit runs the tunnel by UUID, so the credentials JSON alone is enough and no account `cert.pem` is needed on the host.

**Statement OCR** is a separate install on the server: `pip install -r requirements.txt`, then `pip install --no-deps -r requirements-ocr.txt`, in that order. It adds roughly 360 MB to the venv (onnxruntime plus the PP-OCR models), so check disk first. Restart the bot and web services afterwards.

**Updating the web app** is one step in three moves: stop the web service, `git pull`, start it again. The server works out the version of its assets once per process and serves them under that version as immutable, so a process left running over changed files would hand out new files at the old version's address. The tunnel answers 502 for the second or two this takes, and an open app reconnects by itself. A phone shows 有新版本 once it has fetched the new version in the background, and takes it when that is tapped or the next time the app is launched.

Two gotchas with `cloudflared`:

- **`--config` is required** on every `cloudflared tunnel` command. The `tunnel:` key of the default `~/.cloudflared/config.yml` silently overrides the positional tunnel name (especially `route dns`, which will CNAME to the wrong tunnel).
- **`protocol: http2`** in the tunnel config. QUIC flaps on some networks.
