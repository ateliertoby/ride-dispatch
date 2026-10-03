# Architecture

Two processes share one SQLite file. `ride_dispatch.bot` is the Telegram bot and runs the repeating jobs (flights, car park, bank credit feed). `ride_dispatch.web` is a Flask app serving one document with two views: the day view (`/`) and the settle view (`/settle`, 埋數).

This file has three parts:

- [Design decisions](#design-decisions): the constraints and the reasons, grouped by domain.
- [Data flow](#data-flow): what calls what, and the HTTP routes.
- [Key files](#key-files): what each file is responsible for.

Terms used throughout: a **batch** is a settlement (`settlements` row) created from one platform statement; a **leg** is an order in a batch; a **credit** is one bank transfer (`bank_credits` row); the **operator** is the single user.

## Design decisions

### Foundations

**SQLite.** Single user, single machine. Nothing heavier is justified. The schema is created and migrated on start by `db.init_db`, with `ALTER TABLE ... ADD COLUMN` as the only migration mechanism.

**All auth is at the perimeter.** The web app is exposed through a named Cloudflare Tunnel with Cloudflare Access (email OTP) as the single auth layer. The Flask app binds to localhost, has no auth of its own and trusts the tunnel: whoever passes Access is the operator and can read, edit, create and cancel.

**Prices and costs are entered by hand.** The platform gives a flat price per order, and costs (tunnel, parking) are variable but predictable. Manual entry is fast enough for three to seven orders a day, and rules are added only where they pay for themselves. Two have been: parking, which is planned from the meeting point and settled by the car park visit, and a suggested price, which `pricing.py` reads off the history of fares to the same zone and holds no price constants of its own.

### Order entry

**Both the bot and the web app take orders, and parsing is shared.** A WeChat order message can be pasted into the bot (summary card, confirm, price) or into the web app's paste box (preview with fees and warnings, then price and save). Both run the same cascade, `ingest.parse_any`. The web POST parses the text again on the server and never trusts the preview the client holds.

**A message that lands on a live order is an amendment, not a duplicate.** The platform re-sends the whole message when the customer changes a detail. Both entry paths show the difference and apply it in place (`db.update_order_from_message`).

- A field the new message leaves empty means "not mentioned", never "cleared". Re-sent messages often drop the contact numbers, so the stored value survives.
- `parking_fee` and `pickup_point` are not overwritten: the operator or a car park visit may have moved them since entry. `banner_fee` is re-derived, because it is a pure function of the message.
- The price is kept unless a new one is given.
- An order in a batch is refused: its fields are frozen with the batch.

**A cancelled order's number can be entered again.** The platform re-books under the same order number. Re-entry overwrites the cancelled row (`db.save_or_revive_order`) and drops what the old booking derived: the price, because a stale price would hide the 未入價 mark, and the flight and reminder state, so tracking starts over.

**Quick orders are created directly.** 滴滴, Uber and foodpanda trips have no message to parse: time, money, done. The web app adds one onto whichever date is being viewed, which the bot's `/didi` and `/uber` cannot do, since they infer today or yesterday from the time typed.

**Corrections are made in one order sheet, opened from both views.** Everything after the save (price, fees, time, meeting point, cancel) is edited in the order's sheet. An order found wrong while settling is corrected where it was found, so the sheet is one module, `static/js/order-sheet.js`, that both views import.

- Each view hands it a host (`useOrderHost`): which order is open, how a view is stacked, how a patch reloads, how a heading is drawn, and the view's own info rows. The settle view adds the whole order number to copy, the net figure, and links to the batch that holds the leg and the batch that paid its 舉牌 ahead.
- Both views stay mounted, so the host is whichever view is showing. A view sets it when it is shown and again before it opens an order.
- On a batched order, the fields its batch was summed from and cancellation show as 已結算 before the tap, instead of waiting for the server to refuse.
- The sheet's table of meeting points twins `ingest.PICKUP_POINTS`, and a test pins the two together.
- The sheet also links to the order's card in the bot (`/start order_<id>`), which offers tunnel and parking entry and cancel.

**The web app wakes the bot when it saves an order.** A pasted order written by the web process would otherwise wait out the bot's current poll interval before its flight is tracked. The web process sends `kick` over a unix socket beside the database (`bot.sock`), and the bot polls at once. It is best-effort: with the bot down nothing happens, and the bot's first poll on start covers it.

### Flights

**Flight data comes from HKIA's undocumented endpoint.** The official public API (data.gov.hk) provides only the previous day's data, which is useless for scheduling. The endpoint used is the one behind HKIA's own website: public, no auth, no key. It can break without notice, and the system degrades to showing no flight data; orders and revenue are unaffected.

**The poller is a heartbeat that never stops.** It is a 60 s `run_repeating` job in the bot with `misfire_grace_time=None`, so a late tick runs instead of being discarded. A chain of one-shot jobs would die silently the first time a tick fired late.

- Each tick is cheap. It reads the tracking window from the database and fetches from HKIA only when a poll is due, at the interval `flight.calc_next_interval` gives: 60 s once a flight has landed, 600 s when every tracked flight is at the gate or cancelled, half the time to the nearest ETA otherwise, 1800 s with no arrival time at all.
- Termination is by time only. An order leaves tracking when its window (the later of pickup time and latest ETA, plus three hours) expires. Status picks the tier and can never stop the poll, so a wrong or stale status corrects itself on the next one.
- The interval is clamped so the next tick comes before the earliest pending reminder.

**Flights are matched by number and by date.** The feed spans adjacent days and flight numbers repeat daily. Each order takes the candidate closest to its pickup time, within 12 hours either side; beyond that it is another day's flight, and matching nothing is safer. Numbers are canonicalised first, because booking sources and the feed pad them differently. The database keeps what the platform sent and every screen and message shows the canonical form.

**Reminders are anchored to the flight, not to the booking.** For a pickup the bot pushes 出發接機 (landing plus exit minutes minus a 40 minute drive) and 用車時間到 (landing plus exit minutes, once the flight has landed). Trips with a fixed time (送机, 单程接送, 接站) get one push 30 and 10 minutes before. Each is recorded as a tag in `orders.reminders_sent`. A flight-anchored push never fires more than two hours late, and a fixed-time one never after its time, so a bot that was down does not send a backlog.

**A passed ETA is announced, never acted on.** The feed lags between touchdown and `Landed`. Five minutes after a still-`est` flight's own ETA the bot pushes 預計已落地 HH:MM（HKIA 未確認）, because the driver has the same decision to make either way. Nothing writes `flight_status` from it, so the reminder chain and the sign prompt still wait for the feed.

**Sign photos are generated by GPT-Image-2 through fal.ai, behind a button.** The platform requires a photo of a handwritten whiteboard sign before each 舉牌 pickup.

- The image is made by editing a base photo (`assets/whiteboard_base.png`, AI-generated, no personal data) so that only the whiteboard text changes. Quality is `low`: it is a compliance photo.
- Landing offers generation with a preview of the exact text, so a wrong name is caught before credits are spent. Platform VIP markers are stripped from the name first.
- A generated image is cached until it has been delivered, so a failed send is retried without paying for generation again. `/board` is the manual path.
- The fal.ai queue API is called with httpx, already a dependency. With `FAL_KEY` unset the feature is off and the bot runs normally.

### Car park

**Visits are detected, never entered by hand.** The free half hour, shared by Car Park 3 and 4 and available once in 24 hours, is invisible to the driver and to the API. Only a record of past visits can say whether it is available, and the driver cannot keep that record from the wheel.

- `parking.py` polls HKIA's online-payment status endpoint (public, unauthenticated, undocumented) from 30 minutes before a pickup's predicted landing until two hours after, and keeps polling an open visit until two consecutive not-inside replies.
- Visits are keyed by HKIA's own `pvNr`, so a restart resumes a visit instead of opening a second one.
- The lookup is HKIA's online payment service, which covers Car Park 3 and 4 only (`TRACKED_CAR_PARKS`). A Car Park 1 visit never appears, so nothing about it is detected, pushed or written back.
- With `CAR_PLATE` unset the feature is off.

**Payment is a link, not a hosted page.** Payment goes through the endpoints HKIA's own page uses, and the PayDollar gateway accepts its form as a GET, so the bot hands over a URL button. Every tap makes a new gateway order, because a link's lifetime is unknown and a stale one fails silently. A visit still unpaid at 50 minutes (`AUTO_LINK_MINUTE`) gets a link unprompted.

**A visit is priced on HKIA's clock and dated on the bot's.** Every reply seen while the car is inside carries HKIA's `parkTime` and the fee for leaving at that moment. Both are stored on the visit (`last_park_minutes`, `last_fee`) with the tick's own time (`last_seen_at`), and the close reads them back for two different questions.

- The stay is HKIA's minute count. That is the clock that charges, and it is whole minutes however often the bot polls, which is why it cannot say when those minutes ended.
- The exit time is `last_seen_at`, the last tick that saw the car. It is provably no later than the real exit, the direction an operator can reason about. The first tick to miss the car is kept apart in `gone_at` as the upper bound.
- The two are deliberately decoupled. Entry plus stay can be a minute off the recorded exit, and that difference is evidence, not an error to reconcile away.
- The fee decides free from chargeable. HKIA computes it with the allowance already applied, and its real free threshold is longer than 30 minutes and unpublished.
- `FREE_MINUTES` (30) remains as the "leave before" guidance on entry and as the fallback for a visit that was never read, and only at a car park that has the free half hour (`ALLOWANCE_CAR_PARKS`). A car park that cannot be named is assumed to charge.

**The car park has its own heartbeat.** The recorded exit is only as accurate as the tick that found the car gone, and that timestamp goes onto the visit and into the 24 hour allowance, so the tick rate is the resolution of a stored fact. The flight heartbeat cannot carry it: its gate stretches to hours for a distant flight.

- `_parking_tick` runs every `PARKING_OPEN_INTERVAL` (30 s) and is gated back to `PARKING_IDLE_INTERVAL` (60 s) whenever no visit is open. Only an open visit has an exit to date; an armed pickup with no car inside gains nothing from the fast rate.
- A check that failed leaves the cadence unknown and is assumed fast. Guessing fast costs one extra query a minute; guessing slow costs a visit recorded to the wrong minute.

**The driver can overrule the verdict, and the allowance follows.** Only the driver saw whether the gate opened, and a wrong verdict silently moves the 24 hour allowance. The exit message carries a button for each verdict it did not pick, and `/parking mark <id> free|paid|gate` corrects a visit whose buttons are gone. Either writes `observed` and moves the derived `free` column with it. `free` also zeroes the order's `parking_fee`; `paid` and `gate` leave it for the order sheet, because the amount is not known to the system. `/parking` history shows the automatic verdict, HKIA's fee reading and any correction side by side.

**A pickup's meeting point is planned at entry and settled by the visit.** What waiting costs depends on where the driver meets the passenger. Car Park 1 charges $35 for the first hour from the moment the car enters (and $50 for each hour after). Car Park 4 charges $32 an hour but has the free half hour. The Regal Airport Hotel (富豪) is outside HKIA's car parks and costs nothing.

- `orders.pickup_point` holds the place, and `ingest.PICKUP_POINTS` is the one tariff table, read by entry, by the PATCH the order sheet sends and by the entry message's price preview.
- Entry plans the place from the must-park rule: a 携程 pickup or a 舉牌 meet goes to Car Park 4, anything else to the hotel. It writes the place's first-hour charge as `parking_fee`. Moving the place writes the new charge in the same write, so the two never disagree.
- A visit linked to the order replaces the plan with what HKIA saw. The close writes the car park the car was in (HKIA's own name for one that cannot be named) and the cost: nothing when free, the link's amount when paid through the bot's link, the last fee quoted before a payment made anywhere else, and at the gate the last fee quoted inside, which is HKIA's price as of at most one tick before the exit. A chargeable visit with no reading leaves the planned fee standing.
- A Car Park 1 pickup never has a visit, so its planned fee is final unless edited and a stay past the first hour is priced by hand. The landing pushes for such an order say so.
- The close runs once, so an edit made after it stands.

**The absence of a visit settles a plan too, but only at Car Park 4.** Every visit there is seen, so no row means no visit. A pickup still planned at P4 90 minutes after landing (`NO_ENTRY_MINUTES`) is taken to have met its passenger at the hotel: the place becomes 富豪 and the fee nothing, and one push says so with the fee the order carried.

- The conditions are: no visit linked to the order, none of any order entered since 30 minutes before its landing, and none open. The second is there because one visit that collected two passengers links to only one of their orders.
- The rule can be wrong in two ways the system cannot see, so the push carries a button for each: the car was at Car Park 1, which only the operator can know, or at Car Park 4 on a visit the tracker missed. Either writes that place and its first-hour charge.
- The push goes out before the write, so a push that fails leaves the order untouched for the next tick. The `noentry` reminder tag keeps an order the operator put back from being judged again.
- The verdict is given for two hours past its due moment and no later. A bot that comes back after that was not watching while the visit would have happened. It is never given without `CAR_PLATE`.
- A visit that begins after the switch links and closes like any other and overwrites it.

### Settlement and statements

**Settlement is a batch, and an order's money state is derived from it.** The platform pays for a run of days at once, so the unit of reconciliation is a batch of orders. `settlements` holds what the operator confirmed against the platform's statement and `orders.settlement_id` points at it.

- State is derived, never stored. An order with no batch is unsettled. A batch is 等過數 with no money allocated, 部分 with some, and 已收 once its allocations cover its total. There is no second copy of the truth to fall out of step, and undoing a settlement is unlinking.
- The amount a batch expects is summed inside the write transaction from the stored rows, never taken from the client.
- The fields that sum was taken from (`price`, `tunnel_fee`, `banner_fee`) and cancellation are locked while an order is batched (`db.BATCH_LOCKED_FIELDS`). Changing them afterwards would silently make the recorded total wrong. `parking_fee` and `scheduled_time` do not feed the total and stay editable.
- A difference between expected and confirmed is recorded and shown as 差額. The operator takes it up with the platform.

**A statement is a batch, and only a statement creates one.** The platform pays one statement with one transfer whatever number of days it spans, and a leg it held back appears on a later statement under its own date. A batch is not tied to a day and an order belongs to at most one, so the statement adds only two things to it: the platform's lines as JSON, and the screenshot file beside the database (`statements/`).

- Held-back legs stay unsettled with no extra state. The next statement that lists them picks them up.
- Every batch has an image behind it, so there is a chain from the bank credit through that image to the orders it lists.
- The stored lines are keyed by the order id the matcher settled on. Where a near-miss was corrected, the text as read is kept as `read_as`.

**Statements are read on the machine with RapidOCR, not with a vision model.** The statement arrives as a screenshot, re-encoded by Telegram to 1280 px, with order numbers seven pixels tall. Measured on that photo, PP-OCR read every order number and amount exactly in about two seconds; a vision LLM mis-read two to four of twelve order numbers and took a minute per image.

- The engine's aspect-ratio threshold fails silently. Above a width/height ratio of 8, RapidOCR skips detection and recognises the whole frame as one line, returning no boxes instead of an error, and a statement day of few rows is that wide. The engine is built with `width_height_ratio=-1`.
- That threshold is the engine's setting and not the reader's, so any frame still wider than six times its height is also extended downwards with white rows before it is handed over. Rows only, below the content: the width and every original pixel stay, and the blank rows add no boxes.
- RapidOCR is installed with `--no-deps` and `opencv-python-headless`. Its metadata asks for the full OpenCV, whose `cv2.so` links libGL, which on a headless server means Mesa and LLVM for nothing.
- Reading takes about two seconds of CPU. The bot runs it in a worker thread so the heartbeats keep ticking, and a lock serialises the engine across the web server's threads.

**The reader binds to the table's schema, never to a position in the row.** The platform adds columns without notice. A 結算狀態 column to the right of 司機應結算金額 makes every row read as amountless to a reader that takes the last figure. So the table's structure is reconstructed.

- Columns are inferred from horizontal overlap, the one relation OCR preserves. A box is drawn tight around its text, so its width says nothing, but a figure and the label above it always share ground.
- The bands are named from the header row. Each cell is measured against the 27 column names the platform prints, in the traditional spelling and in the simplified one OCR returns, because every character differs between the two scripts and one spelling alone would reject a clean read of the other.
- A cell may be about two fifths of its name in edits away and no further. 司機預估收入 and 司機應結算金額 are five edits apart, and reading a day off the estimate column would settle a wrong figure while every subtotal on the image still agreed with itself.
- A tie between two columns is no match. A column named by a coin toss is worse than one left anonymous.
- The columns the reader takes nothing from are listed too, with no role. They compete for a garbled header cell, so a mangled neighbour lands on its own name and not on a column that is acted on.
- Rows are classified by shape and not by label, because the labels are what compression garbles. An order id makes a data row whatever else it holds; the account code printed above the first day group marks the grand total; a date at the left edge opens a day.
- The amount column needs two independent readings to agree: the column the header names, and the rightmost column holding money. Either carries the read alone when the other is absent, since a cropped screenshot has no header row. When both are present and name different columns, neither is trusted: no amount is read and nothing can be settled off that image. Picking one would be invisible when wrong, because the rows and the day's own 求和 would be read off the same wrong column and agree.

**A statement is checked against itself before it is matched against the book.** The statement's own subtotals are checked first, so a mis-read digit is reported as a reading error and not as a dispute with the platform. A statement whose subtotals could not be read or do not agree cannot be confirmed.

Ids are then bound to orders in four passes, weakest evidence last, so nothing weaker takes an order a stronger rule already claimed:

1. Exactly.
2. By prefix, for a code the platform's UI truncated with an ellipsis, or an id of ten characters or more, where a shared prefix is no coincidence.
3. That same opening within two edits, for codes only. Telegram's photo compression rewrites letters inside a code (a K read as X, a B as 8) that the digit fixes must not touch, so the compressed copy of an image whose original binds would otherwise lose the line. This pass refuses two candidates inside the bound instead of taking the nearer: a prefix is partial evidence, and money must not be batched against two codes that both nearly agree.
4. A unique near-miss over whole ids, within two edits.

- Three id shapes reach the reader: `SPACE` plus a short digit run, a long digit run in which S O I l B may be mis-read digits, and an alphanumeric code. A pattern that knows only some of them fails the checksum of a readable image over a line it never saw.
- `statement.reconcile` is a pure function over the parsed statement and the candidate orders, so the reader can be replaced without touching it.
- Tests replay recorded OCR output, not the image. The recording replaces every order number with a keyed hash whose salt comes from the environment and is never committed, so the fixtures keep the reader's real input without real order numbers.

**One flow serves both frontends.** The bot and the settle view both call `statement_flow.prepare` (reconcile and ask the ledger, writing nothing) and `statement_flow.confirm` (write the batch, its fines and its lines, and allocate the credit the card named). A statement cannot mean one thing in the chat and another in the browser, and the report text is the same string in both.

- The browser hands over the platform's own file byte for byte. The same screenshot sent as a chat photo has been recompressed first, which is what the reader mis-reads, so the settle view is the path for a statement the bot read badly.
- From the browser, a read is held under a token for 30 minutes and the token is spent on the way into the confirm. A double tap meets the same refusal an expired token does and cannot write a second batch. Uploads are capped at 10 MB.
- An image the reader could not read is filed under `statements/failed/`. The operator's copy scrolls away, and a reader bug can only be reproduced from the exact bytes.
- A statement whose legs are not in the system cannot become a batch. When a credit agrees with its total, the bot offers 收埋入數（單未入系統）, which archives that credit; the settle view names the same action without offering it.
- Without the OCR package the bot answers an image with the list of unsettled legs, and the web route answers 503.

**A leg's own cost belongs on the order; a cost the transfer carries belongs on the batch.** A statement is not only fares. The platform charges 判罰賠款 back for a trip it holds the driver responsible for, and it books such a line wherever it likes: against a leg of this statement, against a leg an earlier batch already holds, against a cancelled trip, or under a number the book has never seen.

- A fine against a leg of this statement is the leg's own cost. It is written to `orders.penalty_fee` by the tap that creates the batch, and `service.expected_of` nets it off for every platform. That also makes a statement idempotent: once the fine is stored, re-reading the same image agrees with the platform's figure.
- The rest are money on this transfer and nothing else. They go to `settlement_adjustments`, one row per printed line, and join the batch's `expected_amount` in the same write.
- The boundary keeps the two apart. Recording a fine against an already-settled leg on the order would mean reopening a batch whose expected total is frozen. Recording it as a line of the transfer being confirmed leaves that batch alone and still balances.
- The platform's line structure is kept, not netted. A 判罰 and the 免責 line that cancels it are two facts, and a pair that nets to zero must still read as the pair it was.
- A line that nets positive under an unknown number is not an adjustment. It is most likely a leg the book never got, and it stays flagged as unknown: money coming in must not be able to read as fully explained.
- The confirm button names every part of what it writes (確認結算 + 記判罰 + 記帳項 + 對入數), because money leaving an order is not something to discover afterwards.

**A 舉牌 paid while its trip is held back is paid ahead, not settled.** The platform can leave a trip off a statement and still pay the trip's 舉牌 line on it, under the trip's own number. Folded by order number, that reads as the trip underpaid by its whole fare, and confirming it would settle the order: the fare would drop out of what is owed, and the trip line arriving later would read as money paid extra.

- The line goes to `settlement_adjustments` flagged `ahead`, and the order stays unsettled like any held-back leg.
- `service.owed_of` takes what other batches carry for an order off `expected_of`, so the batch that later takes the trip is owed the trip alone and the trip line matches it.
- The category chip that names a line is unreadable, so the rule is arithmetic: an order whose only line is exactly its own 舉牌 fee. A trip line printed at zero beside it is a trip paid nothing, not one held back.
- The trip's batch freezes its expected total net of the line. So the batch carrying the line cannot be undone while the trip's batch stands, for the same reason a batched leg's fees are locked.
- In the settle view the carrying batch also covers the held trip's day, although that day has no leg of its own in it: the batch's list row names the day, a focus lights it, the day's sheet links the batch, and the line is counted in the month of the trip, in the state of the batch that carried it.

### Bank credits

**Every credit is recorded, whether or not anything exists for it.** A separate program publishes one JSON line per bank credit advice to a JSONL feed (`BANK_CREDITS_FEED`). The bot stats the file on each flight heartbeat, ahead of the flight gate so a credit never waits out a long flight interval, and reads it whole when its size or mtime changed.

- The bank's reference is the identity (`INSERT OR IGNORE`), so a re-read is a no-op and this side keeps no offset. A trailing line without its newline is one the producer is still writing and is left for a later tick.
- A line needs `v: 1`, `ref`, `platform`, `amount` and `value_date`. One that cannot be used is logged and skipped, so one corrupt line cannot stop the ledger.
- A complete ledger is what makes backfilling months of payouts from forwarded screenshots possible.
- One to three new credits get a push each. More is a backfill and gets one summary pointing at `/credits`.

**Money is allocated in amounts, not linked.** The platform pays a statement short when its own system failed to submit some of the legs, and makes up the difference later, alone or bundled into a bigger transfer. One credit per batch, paid in full, cannot record that. `credit_allocations` carries an amount per (credit, batch) pair: a credit can pay several batches and a batch can be paid by several credits.

- What a credit has left and what a batch is still owed are both derived from the allocations. Taking money back off a batch, or undoing the batch, needs no second write that could disagree.
- `paid_on` is written at the moment the allocations cover the total and by nothing else, from the bank's value date. Taking money back clears it.
- The amount is never the client's. A tap allocates as much of the batch as the credit can still pay.

**The legs a short payment left out are named on the batch, after the fact.** When the money is short the operator does not yet know which legs, and the answer comes only after the platform investigates. So the legs are ticked 未過數 in the settle view on the batch's own order list, against the full order numbers.

- The system guesses first (`credits.guess_unpaid`: subsets of legs whose amounts equal the shortfall, up to five legs and eight results) and pre-fills the ticks when exactly one combination adds up.
- The ticks are accepted only when they account for the shortfall to the cent. They are two independent statements of the same fact, one from the platform's message and one from its money, and a mismatch means one was misread.
- The ticks are priced in the platform's own figures (`statement.leg_amount`), never the system's. The transfer is the sum of what the platform printed, so a leg it priced differently would otherwise never account for the gap, and the operator would be told the ticks are wrong over a discrepancy that is the platform's.
- The flags (`orders.unpaid`) are history, not a to-do list. `allocate` leaves them alone when the make-up payment closes the batch, because which legs the platform held back stays true after they are paid and is the only record of it. Without it, a batch of fourteen legs paid in two transfers would tell the day's sheet that all fourteen arrived on the second date.
- Every reader therefore asks the batch's state as well as the flag. Flagged in a 部分 batch is still owed; flagged in a 已收 batch was 補收 by the allocation that completed it. Nothing may read the flag alone as money outstanding.

**Nothing allocates by itself.** `credits.py` proposes and writes nothing to `settlements`, so a matcher that grows a new rule cannot start moving money. `db.allocate` is reached only from the statement confirm and the settle view's confirm of a credit against a statement, and `db.allocate_all` only from its confirm of a group; tests pin those call sites.

**Money is matched in the settle view; the chat only announces it.** A credit arriving from the feed is pushed as what arrived and what the matcher believes (對到 批次 #26、#27、#28？ · 去埋數頁對數), with no button that moves money. Matching is a tap in the settle view, where the whole ledger and the calendar are in sight.

- With `RIDE_WEB_URL` set, the notice carries one URL button to `/settle?credit=<id>`, which opens that credit. A URL button opens an address and calls nothing back. If Telegram refuses the message with the button, the notice is sent again without it.

- A per-batch button in the chat would take three taps for a transfer covering three statements and be answered without the ledger in view.
- A whole group is one tap, allocated in one transaction that refuses the group whole unless the credit covers every batch in it. A group that half-landed would leave a part payment nobody chose.
- The statement card's 確認結算 + 對入數 stays in the chat: the credit it spends is the one that card named.
- A credit card in the chat that still carries allocation buttons is answered with 去埋數頁對數 and moves no money.

**A payment is keyed to the day a batch was confirmed, not to the days it covers.** Everything confirmed within one working day is paid as one transfer about two working days later. The matcher (`credits.match_credit`, `credits.match_batch`) is built on that.

- The window is seven days either side of the batch's `settled_on` (`WINDOW_DAYS`). It is symmetric because a screenshot confirmed late legitimately puts the bank first.
- The window is what separates a match from a coincidence. Amounts are round hundreds and the ledger holds months of them, so an amount agreeing to the cent on the wrong date is offered among the alternatives and never proposed.
- Money dated before a batch's last service day is not a candidate at all. It cannot pay for work not yet done.
- Inside the window, an exact amount is proposed, and so is a whole confirmation-day group at any size: one transfer pays every batch confirmed on the same working day, so the group is the ordinary payment.
- Only a transfer that mixes confirmation days leaves no whole group. Then combinations are searched blind, up to four batches (`MAX_SUBSET`), and only a single hit is an answer. Among equal sums, a combination drawn from one confirmation day wins, because that is the platform's own grouping; equal sums spanning days are the coincidence the ambiguity refusal exists for.
- A near miss is never proposed. A $30 gap is a question about a fee or a held-back order, not a rounding error.
- A statement being read stands in for the batch it is about to become (`credits.propose_statement`), with its total as the amount and today as the confirmation day, since that is the date the confirm writes.

**The belief leads without being acted on.** Proposals are ordered by `credits.offer`: what agrees to the cent, then what the credit could only pay part of, then the other candidates. Both settle payloads carry the proposals inline, a batch its candidate credits and a credit its candidate batches. A batch offered to a credit carries the `due_dates` and `settled_on` it is named by; a credit offered to a batch carries `near`, whether its date is inside the matcher's window. The ledger is a few hundred rows a year, which is cheaper than a round trip per sheet. The change left on a credit stays proposed against whatever is still owed, which is how a make-up payment bundled into a bigger transfer reaches the batch that is short.

**Credits that will never have a batch are archived, not deleted.** `/credits archive before <date>` puts away the payouts that predate the system, and a credit for legs the system never had is archived from its statement card. An archived credit leaves the queue with its allocations untouched and can be brought back.

### The web shell

**One document, two mounted views.** `/` and `/settle` return the same shell (`templates/app.html`), and the path only says which view is showing. The day view answers "what am I driving" and the settle view "what am I owed": different questions and different shapes, so they stay two views, but not two documents.

- A switch between documents goes through Cloudflare Access and the tunnel, parses every script again, fetches the data again and reopens the event stream. A switch between views does none of that.
- `router.js` maps the path to a view with the History API, so back and forward work, and a reload or a home-screen launch on either address lands on that view.
- A view is mounted the first time it is shown and stays mounted. Switching hides one root and shows the other; each view gets back its scroll position, the settle view its loaded months, and either view the sheet it had open.
- Each view keeps an on-screen control to the other, because an installed app has no browser chrome to go back with.
- Each view keeps its own view stack. The day view's serves two hosts (bottom sheet and top drop panel). The settle view's stacks nine kinds (day, order, batch, statement, credit, the queue of credits still waiting, the credits put away, undo and 解除入數), and a sheet opened from another hands the operator back to it.
- There is no build step. Scripts are ES modules and styles plain CSS, served as written.

**Each view is one module; only what is stateless is separate.** `day/index.js` and `settle/index.js` each hold around twenty mutable variables that most of their functions read and write. Cutting a view along its seams would turn every one of those references into an import or a parameter, and nothing but a browser can tell whether one was missed. What reads only its arguments is separate, because that is what a test can hold: `shared.js`, `dates.js`, `settle/days.js`, `api.js`, `store.js` and `stream.js`, each covered by `node --test`. What needs a real browser is checked by `scripts/e2e.py`.

**The store paints what it holds and always asks again.** The day view reads a day through `store.js`, an in-memory map from key to the server's last answer. A read paints the held answer at once, asks the server, and paints again only if the answer differs. The neighbouring days are fetched ahead once the day's own answer is in.

- A held answer is never treated as fresh. `/api/orders` computes fields at request time (row order, departure time, exit urgency), and another device or the bot can change an order at any moment.
- Requests are numbered per key, and an answer older than one already applied is dropped, so answers arriving out of order cannot put stale rows back.
- The store keeps nothing on the device, so a launch starts empty.
- The settle view does not use the store. It stays mounted, so its own map of loaded months already survives a switch, and a second copy of money data would be one more thing to keep in step.

**A hidden view neither asks nor draws.** The settle strip holds its top row in place across every paint and reads the month the header names off the rows it has laid out, and a hidden root has no geometry. The views also share the document's scroll position, the window's events and the order sheet's host. So while a view is hidden its answers are not painted, its listeners on the window stand down, and its writes do not reload it. `show()` always loads, which also redraws a sheet left open. The cost is that a hidden view learns nothing: changes are found by the showing view and by the stream.

**View styles are scoped with `:where()`, and ids are per view.** With both views in one document, a rule or an id of one would reach the other. `day.css` and `settle.css` nest their rules under `:where(.view-day)` and `:where(.view-settle)` (CSS nesting, Safari 17.2+). A plain class would raise the specificity of every rule inside it and change which rule wins against `base.css`; `:where()` adds none. Rules on the document or the body are keyed on `body[data-view]`, which the router sets. Every id both views would share carries the view's name (`day-sheet`, `settle-sheet`), and each view looks its elements up inside its own root.

**Inline handlers are published under `window.rd`.** The day view and the order sheet write markup with inline `onclick` handlers. Those resolve names on `window` and cannot see a module's scope, so the modules publish exactly the functions their markup calls (`window.rd.day`, `window.rd.sheet`), and an end-to-end check resolves every handler the sources emit. The settle view has none: its controls are found by listeners on its root.

**Live updates are one server-sent event stream for the document.** The server compares a fingerprint every two seconds and says "something changed" when it differs; a message refreshes whichever view is showing. No control on the page asks for a refresh by hand.

- The fingerprint (`web._fingerprint`) is a hash of every column of every order, plus the batches, the credits and the allocations. The orders are hashed whole so that a column a view starts to show is covered without being named: an amendment that changes only an address or a flight number has to reach another open device. None of it is scoped to a day, because the settle view holds months and the ledger changes without any order moving.
- Hashing the table costs tens of milliseconds at a few thousand rows, too much for every two seconds per connection. A stream keeps one database connection and reads `PRAGMA data_version` first, which costs microseconds and moves only when another connection's commit has modified the database file; the fingerprint is taken only then (`web._Watch`). The version is only the gate that says when to look, and the fingerprint decides. A commit to a table no view shows, such as a car park reading, moves the version and not the fingerprint, and is not reported.
- `stream.js` holds the one `EventSource`. Every connection begins with a greeting. The first greeting of the first connection is not a change, unless the stream had already failed before it; the greeting of any later connection stands for whatever was missed while the stream was down.
- The document stays open for hours, so the stream has to outlive what would end it. A browser retries a dropped line itself, but an answer that is not a 200 event stream (the tunnel's 502 while the web service restarts) closes an `EventSource` for good. A closed stream is opened again after a delay that doubles from 2 s to 30 s and starts over once a message arrives.
- The document is not reloaded between uses, so becoming visible after having been hidden refreshes the showing view and opens a waiting stream at once. A phone that slept would otherwise show data hours old.

**The service worker caches the shell and never data.** `templates/sw.js`, served as `/sw.js` so that its scope is the whole app, stores the document and every script, stylesheet and font. It answers asset addresses and navigations to `/` and `/settle` from that store, so a launch paints without the server.

- `/api/*` is not intercepted, and every API answer except the event stream carries `no-store`. A money figure must not be able to appear from a cache: a figure on screen has always come from the server on this load.
- A version is installed whole or not at all. If any file cannot be fetched, or the document the server hands over is of another version, the install fails and the worker in charge stays in charge.
- The manifest and the icons stay at fixed addresses (`/manifest.webmanifest`, `/static/icons/…`), where installed home-screen apps point, and the worker leaves them to the network.

**Assets are addressed by version, and a stale version is a 404.** `web.asset_version()` is a content hash of everything the shell is made of. The document links its assets as `/assets/<version>/…`, the worker's cache carries the version in its name, and the document states it in the `X-Asset-Version` header, as `/api/ping` does in its answer. The document also states the bot's username (`TELEGRAM_BOT_USERNAME`), which the order sheet's 喺 Telegram 開 link is built from: a value that differs per deployment reaches the page through the document, never through a file under the asset address, and is folded into the version so the worker's stored document cannot outlive a change of it.

- An address under the current version is cached as immutable. An address under any other version is refused, not answered with today's file: the address promises the content, and answering it would let a phone run one version's document against another's script.
- The server computes the version once per process, so the files must not change under a running process. A deploy stops the web service, pulls and starts it again as one step.
- `archive` directories are left out of both the version and the precache list (`web._live_files`), which walk the same files.

**A new version waits for a tap.** A reload loses whatever is open: a sheet, a statement read and not yet confirmed, unsaved 未過數 ticks. So a new worker installs and waits, the shell shows 有新版本, and the page reloads only when that banner is tapped. Another open window of the app is offered the same reload when its worker is replaced under it. The one time a waiting version is taken without asking is at launch, when nothing is open yet. An installed app is rarely navigated, so the page asks the browser to look for a new worker each time it becomes visible.

**An expired login is a banner, not a login page.** With the document served from the worker's cache, an expired Cloudflare Access session shows as API requests that fail. Observed through this tunnel, a plain request without a session gets a 302 to the access proxy's own origin, and a request marked as scripted gets a 401 HTML page.

- `api.js` makes every request with `redirect: 'manual'`, so the first is visible as an opaque redirect; followed, it is indistinguishable from being offline. It takes HTML on a successful answer, a 401 or a 403 for the second. HTML on any other status is an ordinary failure: the tunnel's own error pages are HTML too, and the login is intact behind them.
- Either way the request throws `AuthExpired`, which no caller turns into a toast, and the shell shows 登入過期. While that banner is up nothing is refreshed and the stream is not reopened.
- A tap navigates to the current address with `?login=…` added, which the worker passes to the network so that Access can run its login and send the browser back. The marker is stripped on boot. Whatever else the address carries is kept, so a view's own marker not yet answered (`?credit=`) is still there after the login.
- A hidden view asks nothing, so a failing stream is followed by a spaced request to `/api/ping`, which is what tells an expired login from a dropped line.

**The timing readout is cleared by a failure as well as by a paint.** With `localStorage.perf` set (by `?perf=1`, or by holding the day view's date button, since an installed app has no address bar), a paint reports how long it took from the tap that asked for it. A load that fails clears the mark its tap set, unless a newer tap has replaced it. Otherwise the next paint for any other reason, minutes later, would be reported as having taken that long.

### The day view

**Rows share one grid, and an endpoint is never truncated.** Every row, the column head, the placeholder rows and the NOW band use the same two fixed columns (time, code), so times and codes start on the same x down the list.

- The place is where the driver has to go, and a hotel or an estate is routinely named in twenty characters. The place wraps and nothing on the row is cut or ellipsed: a name cut short is a wrong turn.
- The fare has no column of its own. It floats at the right of the place's cell on the first line, so a name that wraps stops short of the fare and then runs the full width under it. That keeps a long name to few lines without moving where a short one starts or where a fare ends.
- The cell holds only inline text and blocks, because the row aligns its cells on their first baselines and WebKit takes a wrong one from an inline box that wraps.
- The wait since the previous row hangs from the time it is counted to, so a long name does not carry it away.

**One time drives the sort, the row and the NOW line.** A pickup's row shows the flight's own time (gate, then ETA, then schedule), because that is the number the driver watches. `flight.row_time` sorts by that same choice and sends it as `row_time`, and the view places the NOW line against it. Deriving it twice could sort a row where it does not read; `row_time` and the view's `rowTime` must pick the same field.

**A finished row recedes by colour, not by opacity.** A done row sets its text in the faintest ink and its time in regular weight. Opacity would fade the status block with the rest, and that block is the one thing a finished row still says at a glance. It would also composite differently on the two grounds, the panel under NEXT and the page under everything else.

**The status block turns over once, and only for a change seen on screen.** A status block whose word differs from the word the same order showed at the previous paint of the same day turns in over half a second.

- The list is rebuilt whole on every paint, so the view remembers the word each order last showed, keyed by day. A first paint, a change of day, a filter that brings a row in and a repaint that changes nothing all leave it still: none of them is the feed changing its mind.
- The block that leaves is gone by then, so only the arriving half is drawn, by a CSS animation with no timer behind it.
- Under `prefers-reduced-motion` the block does not turn, the sheets, the drop panel, the numpad and the paste preview are still, and the toast fades in where it stands.

**Placeholder rows stand in once, for a list that has never been drawn.** On a cold start the view shows five rows of the board's own grid with blocks where the figures will be, so the first paint has the page's final shape and the answer fills it without pushing it.

- They are still. A shimmer says "wait" for as long as it runs, and the wait is short.
- They never appear again. A day reached later keeps the previous day's rows until its own arrive (or paints from the store at once), since blanking a list the operator is reading is worse than showing it a moment too long.
- A first load that fails gives way to the empty line and does not promise rows for ever.

**The day's totals live in a fixed foot.** 程數, 未入價 (only when there is any) and 當日車費 sit in a bar fixed to the bottom of the screen and follow the platform filter. The masthead is sticky and already holds the date, the keys, the tabs and the column head; a summary line there would cost a row of the list on every screen, and the total is the figure looked at last. The list is padded to clear the foot, the toast stands above it, and sheets and their scrim cover it. The settle view keeps the same bar for what needs action.

**The add panel is one view stack in a panel that drops from the top.** Paste, preview and price, or a quick order's time, money and confirm, are stages of the same panel, which grows and shrinks between them. For a pasted order the preview and the price keypad are one view, and the confirming tap both prices and saves: the operator checks the parsed fields by eye, and a separate confirmation step would add nothing.

### The settle view

**Each of the month's totals opens the form that suits it.** Under the platform tabs stand four keys, 本月車費, 已收, 等過數 and 未結算, each a label over a figure exact to the cent, for the month the header names and the platform chosen. The page is opened to see how the month's income stands; tracing a statement or a transfer back to its orders is needed only when a figure is wrong. So the totals are what is always on screen, and each is the way into what it counts.

- Fares and unsettled money are about days and orders, which a calendar can show. Money awaiting a transfer and money received are about statements and credits, and a calendar can say of those only when, not what they hold, so they are read as a list.
- One piece of state, the lens (`setLens`), says which key is chosen and decides the body. 本月車費 is the calendar of whole fares. 未結算 is the same calendar with the days holding money on no statement lit, each printing that part, so the lit figures of a month add up to the key; every other day recedes. 等過數 and 已收 put a list of statements in the strip's place.
- The strip runs on through the months, so under 未結算 open money just across a month's line is lit as well and is seen without paging.
- The strip is hidden under a list, never emptied. Its months and its place are there on the way back, and it comes back on the month the list was paged to.
- The lens is where the operator was looking and not a setting: it is the whole fare again each time the view is shown. The platform chosen is kept in `localStorage`, since the operator works one platform for a stretch.
- The chosen key takes the panel's ground and joins the column head below it, while a tab is chosen by a rule under it, so the two rows read as two kinds of control. The keys wear the rule their state wears in a day's cell, and only while they hold money in that state, so the row of keys is the calendar's legend and the page carries no other.

**A day's cell says three things, and its figure always means the whole day's fare.** The date, small; what the whole day is worth, larger; and a short rule under that figure saying where the day's money has got to.

- A figure that meant the unsettled part on one day and the total on the next could not be added up or compared down a column. So the figure is the whole day everywhere except under 未結算, where the lit days say by their ground that they are showing a part.
- Inside the grid a figure has no `$` and no thousands comma, and its cents are set smaller. Outside it, in the keys, the lists and the foot, it has all three.
- A day in several states shows the one furthest from being paid: unsettled (amber rule and figure), paid short (a rule in two parts, green then amber, cut at the share that has arrived), awaiting (a thin rule), collected or still to come (no rule, faintest readable ink).
- Only orders already driven decide the state, as only they are counted in the month's totals. A booking later today cannot turn its day amber.
- A day with no orders is its date alone and takes no tap.
- Batches and credits are not drawn in the grid at all. A statement is a set of orders, and what a row of days can say about it (a bar cut at the week's end, dashes for the days it skips, one label on one segment) is less than its row in a list says.

**The month's totals are split order by order, so they add up by construction.** `month_totals.split_month` takes the month's orders already driven and puts each one's value, `expected_of`, into exactly one of four parts: unsettled when it is on no statement, awaiting when its statement has had no money, received when its statement is collected, and on a statement paid short, short for a leg ticked as unpaid and received for every other.

- `本月車費 = 已收 + 等過數 + 未結算 + 收少咗` therefore holds for every month and platform without two queries having to agree, and the tests hold it.
- The part of an order another statement paid ahead of its trip takes that statement's state.
- An order still to be driven is counted in no total, because it may yet be cancelled.
- A statement that straddles two months is counted in each by the legs driven in it.
- The server computes the split, and the keys and the foot print it, so the header and the body cannot disagree about the same money. Nothing in the payload totals money over all time: the only figure that reaches past the month is `earlier`, made by the same split.

**A statement paid short is kept apart from money that is awaiting.** What arrived on it is received, and what did not is in none of the three keys beside the fare: it is the foot's 收少咗. Awaiting money needs nothing from the operator, since the transfer follows the statement by itself. A shortfall is the one figure the operator has to take back to the platform, and folded into 等過數 it would be a sum that could not be acted on or found.

- The statement is listed under 已收, with the money that did come, and leads that list.
- The shortfall over all the months a statement covers is exactly what the statement is still owed. The ticked legs need not add up to that, since nothing may be ticked yet and the ticks are taken in the platform's figures. The difference is moved between received and short once, in the month of the statement's latest leg, so a statement across two months is never corrected twice.

**A figure that cannot be stated exactly is withheld, not guessed.** `split_month` refuses a statement whose shortfall its orders' fares cannot hold. Clamping at zero or spreading the difference would print a made-up number as an exact one.

- `db.get_settle_month` then sends `month_totals`, or `earlier`, as `null` and everything else as usual, so the page still loads and the statement at fault can be opened and corrected.
- A key whose month is not loaded, or whose totals were withheld, shows a dash and no state's colour, never a zero.
- The foot says 全部啱數 only when the month's totals and the earlier months' figure are both in hand. It says 今個月計唔到準數 when the totals were withheld, and shows the earlier months' item with a dash and no tap when that figure was.

**The keys count fares; a list row states the statement's own amount and says when the two differ.** A statement can be confirmed at a figure that is not the sum of its orders' fares. The keys keep to the fares the book holds, because that is what makes them add up. A row is a statement, so its figure is the statement's: what the platform confirmed while nothing has come, what has arrived once something has, with each credit that paid it on a line of its own.

- What separates that figure from what the keys count is said on the row, part by part, so the rows can be added up against the key. The identity is `confirmed figure = its part of every month it covers + 另有帳項 + 同車費差`, computed in `settle/days.js` (`inMonthPart`, `otherLines`, `fareGap`).
- 其中本月, on a statement reaching outside the month, is how much of it this month's key counts.
- 另有帳項 is the sum of the lines the statement carries that the totals count under no order: a 判罰 against a trip another statement holds, one that was cancelled or one the book never had, the 免責 line that cancels one, a 舉牌 paid ahead of a trip cancelled since.
- 同車費差 is what is left: a figure the platform put on a leg or on the whole statement that is not the book's.
- None of the three is a state, so none takes a state's colour. Only 仲差, on a statement paid short, is money still owed.
- Under 等過數 the longest wait is first. Under 已收 the statement still owed money is first and the collected ones follow, newest first by the date they are named by, receding as records.
- Credits put away with no statement belong to no month, so the way to them is the last row of whichever month's 已收 list is shown.

**A statement is named by the 應結算日期 its rows print.** `10/9 結算`, `10/8–9 結算`, `12/28、30 結算`: the set of due dates on the statement's rows, written as runs of consecutive days (`statement.due_dates` reads them off the stored statement; `days.js:statementName` writes the name). The list, the foot and the focus line use it, and the ledger carries the same dates for every batch a credit paid, so a statement known only from the ledger is named the same.

- The set is the name because nothing smaller is the statement's own. The platform prints one value per service day, several statements are confirmed on one day, and no single date, earliest or latest, is unique to one.
- A row that prints no value, or one that is not a real date, adds nothing, and nothing is derived from the service day in its place. The name has to be what the platform printed, so that it can be found on the platform's side.
- A batch whose stored statement prints none falls back on the day it was confirmed, and one with neither has no date. The bank's value date names the transfer, not the statement, and the system's own id ties to nothing the platform or the bank says.
- Two statements that would still share a name are told apart by a number on the later.
- Day sheets and batch sheets label a batch by the service days it covers; the day it was confirmed is said on the batch's sheet.

**The foot holds only what needs action.** Up to three items, in this order: 收少咗, the month's shortfall, naming the statement when there is one and counting them when there are more, which opens the 已收 list; the bank credits no statement accounts for, with their count, which opens their queue; and 之前月份未清, which goes to the earliest month still holding open money, on the calendar of whole fares. With nothing to act on the foot is one line, 全部啱數.

- The third is there because the keys are per month. What is unsettled, awaiting or short in every earlier month is summed by the same split (`earlier` in the payload). Without it, money left open in a month not on screen would be seen only by paging back to look for it, which is what the page is opened to prevent.
- The foot is one height whatever it holds, so it covers the same part of the page in every state. The strip and the list are padded so their last row clears it, and sheets and their scrim lie over it.
- The credits' item has two forms. While any credit has one sure answer (one statement agreeing to the cent on dates the matcher believes, or the group one transfer pays whole) it counts those and sums what they hold: 入數啱數 N 筆, with 啱數 as the solid green block it is in a sheet. Otherwise it counts every credit that waits: 入數未對 N 筆. A ledger can hold dozens of old credits that will never have a statement, and counted among them the one a tap can finish would read as the same backlog as yesterday. In both forms the figure is bank money not yet matched and keeps that colour.
- An item is a ruled cell like a key, at least 44px tall. A cell that leads nowhere is not a button.

**A foot label keeps its size, and room is made for it in a fixed order.** Labels and figures stay on one line, exact to the cent, and nothing is wrapped or cut. When three items are too long, `fitFoot` makes room a step at a time and stops at the first step that is enough: the labels give up their letter-spacing; the padding beside the rules goes down to 4px; the shortfall's label gives up the name of its statement, and the first two steps are tried again; the padding goes down to 2px; last, the figures are set smaller, all together.

- The name goes before the padding passes 4px. A label nearer a hairline than that reads as crowded against it, and the name is the one part another place states: the cell opens the list that the statement leads.
- The cells are measured where they stand and not counted from their characters, since a label mixes two faces and marks pulled in by `tight()`. The foot is fitted again whenever its width changes.

**The strip is one run of weeks, with no break at a month.** A week row can hold the end of one month and the start of the next, and a statement's days can lie either side of that line. A boundary is a stronger hairline and the month carried by the 1st, never a cut. The strip loads a month at a time off either end as it is scrolled, and the header names the month of the row at its top, so the keys and the foot follow the scroll.

**The strip is a ruled sheet, not a set of cards.** It is drawn the way the board's list is: a week is a row between two hairlines, a day is a column of it, and a day's number, figure and rule all start on their column's left edge. There is no box round a day and no line between days: the left edges already draw the columns.

- The 1st carries its month, because a week row is not a month and has no heading to read one off. Today's number is an inverse block.
- An amount too long for its column at the size the others are set in is set smaller, never cut.
- Header, weekday head and key are whole pixels tall, because the strip is scrolled to positions worked out from them and the browser scrolls by whole pixels.

**The strip's held months are an unbroken run, and a run is stored whole or not at all.** The strip draws every date from its first held month to its last, so a month missing between two that are held would draw as real days with no work on them, which on this page reads as nothing owed.

- A reach for a month outside the strip fetches every month in between, and the answers are stored only when all of them arrived. A failure leaves the strip as it was.
- A month another caller is already fetching is waited on through the same request, so two callers cannot leave a gap between them.
- A reload of what is held is whole in the same way: every held month and the ledger, or nothing. A half-refreshed strip would show the same batch in two states.
- A month more than `FILL_MAX` (3) fetches away is a jump, not a scroll. The strip is thrown away and refounded there instead of paying for every month in between.

**A month asked for lands with its first row at the top, however short the strip is.** The arrows, the month button, the foot's 之前月份未清 and a batch opened from a credit each name a month. A strip just opened or refounded is a month or two long, about a screen, so the row asked for can have less than a screen of strip under it and the scroll stops short at the document's end.

- The row is therefore pinned, and every paint that lengthens the strip asks for it again until it is at the top. Stopping at the document's end is also what brings the next month into reach of the strip's edge, so the growth the pin waits for is already asked for.
- Without the pin, the strip's own growth would hold whichever row the unfinished scroll left on top.
- A row that can be reached takes an earlier pin away, and so does a load that fails, so a later paint cannot move the strip to a month nobody is asking for any more.
- The row a focus goes to is placed by the same scroll.

**A statement's days are lit from its sheet, and the rest recede by colour, not by opacity.** Nothing in a day's cell says which statement the day went onto. The batch sheet has a key, 喺月曆睇, that closes the sheet, goes to the calendar of whole fares and lights that statement's days. It is the one way into a focus.

- A lit day takes the panel's ground. Every other day gives up its colour and weight and keeps its figure. Opacity would do it in one rule, but a faded amber figure cannot be read on the light ground, and a receded figure is still money the operator reads at a glance.
- The set lit is the whole connected run of allocations, not the named statement alone. Two statements paid by one credit are one statement about money, and it has to read the same from either end.
- When none of the statement's days is on screen the strip goes to the nearest row carrying one; it stays where it is when one already is.
- A tap on empty calendar puts the focus down, and so does choosing another key, since the strip lights one set of days at a time. A day still opens on one tap whatever is lit.

**While a statement is in focus the foot is one line naming it, and the line gives way part by part.** The line holds the statement's name, the days it lights, its leg count, its figure and where its money has got to, with a ✕ to put the focus down. It stays on one line at every width, so the foot is as tall with a focus as without and the calendar does not move under it.

- When the line is too long, every part is set smaller together, down to 12px. Only when that is not enough does a part go: the leg count first and then the days, both of which the lit calendar and the statement's sheet still say. The name, the figure and the state always stay.
- A part that has gone is taken out of the line, not hidden, so the line says exactly what is on screen to whatever reads it.
- The line is measured where it stands, at its full size and at the floor, for the same reason the foot's cells are.

**A settle sheet is a page of a ledger.** Each sheet states its money in one order: the figure the sheet is opened for, large, with where that money has got to under it in the colour of that state; then lines of label left and figure right; then the legs.

- Every list of legs is one grid (the time, the whole order number over what jogs the memory of the leg, the figure over what is to be said about it), so numbers start on one x down a day sheet, a batch's list and the tick list alike.
- An order number is grouped in fours, wraps where it must and is never cut. Reconciling is done number by number against the platform's statement, and a number cut short cannot be checked. A bank reference and a memo wrap for the same reason.
- Figures are in the figure face and the words around them are not.
- What moves money (確認啱數, 確認收到 $300.00（仲差 $80.00）) is an outlined key at least 44px tall that says in full what the tap records. 撤銷結算 and 解除 are confirmed in a pushed view, not by an armed button, so a repaint in the middle of the decision cannot wipe the armed state.
- The statement's report is printed as the server sent it, the same text the bot's card carries.
- Every settle sheet is a read except 撤銷結算, the 未過數 ticks, reading and confirming a statement, and putting a credit against a batch or a group or taking it back off.

**A credit against a statement is drawn as two figures and a verdict.** Putting a credit against a statement is a tap, and not automatic, because the bank's figure can differ from the statement's. So wherever the pair is offered (the queue, a credit's sheet, a batch's sheet, the row for a group) it is drawn as that comparison: 銀行入 over the statement's name, label left and figure right, both to the cent in the figure face so the two stand one over the other; then the verdict, and a key that says what the tap records. `days.js:matchWording` decides the words.

- The figures agree and the matcher believes the pair: 啱數 as a solid block, 確認啱數.
- The credit is smaller: 少 $N in the colour of money owed, 確認收到 $X（仲差 $N）.
- The credit is larger: 多 $N in the colour of unmatched bank money, 確認收到，入數剩 $N.
- The same amount on dates outside the matcher's window is said to be the same amount (銀碼一樣，日期隔得遠), not 啱數: a coincidence must not wear the block of a match.
- A credit part spent is compared by what it has left (銀行入 剩) and a statement part paid by what it is still owed (仲差), so 啱數 on a make-up payment means it agrees with what is outstanding.
- One transfer paying a whole group is one row: the count of statements and their sum, their names under it, one key.
- The statement is named as everywhere else, by its 應結算日期. The toast after a tap says the result in the same words (`confirmedText`).
- The tap calls the same endpoints as before and the amount is the server's.

**The queue lists only the credits still waiting, and leads with those that have an answer.** 入數未對 opens the open and part-matched credits in two parts. 有結算對得上 holds the credits with one sure answer, newest first, each with the comparison and its confirm in place: the newest is the one a notice has just announced. 未有結算對得上 holds the rest, oldest first, since those are the ones being chased, each a way into its own sheet. The heading counts every credit that waits, and a part with nothing in it is not drawn. Old credits with no statement stay in the second part and in the count; they are kept there as a reminder and are not what a new credit should be found behind. A matched credit is reached from the batch it paid, and listing it again would bury the ones still waiting.

**A statement waiting for its transfer says when the money is in.** Its sheet shows, above its legs under 入數到咗, the credits dated inside the matcher's window, with the same comparison and confirm. A credit dated far from it is offered only on the credit's own sheet, where every candidate is listed: on the statement's sheet it would be announced as that statement's money. With no such credit the sheet adds nothing, since waiting with no money yet is the ordinary case. A statement paid short lists every candidate under 等緊補數, because a make-up payment can arrive at any distance.

**A credit can be named in the address.** `/settle?credit=<id>` chooses the credit's platform (`GET /api/credits/<id>` says which), opens the credit's sheet over the queue, and takes the marker off the address. A credit the ledger does not hold, or one no longer waiting, opens the queue alone. The worker answers `/settle` with any query from the cached shell, so the installed app handles it. The marker is left in place until it has been answered, so a login that expired in between returns to the same credit.

**A sheet never shows an order the page could not read again.** The settle view's order sheet shows an order fetched on its own (`GET /api/orders/<id>`, because the month payload carries settle columns only), and the fetch is repeated with every reload, since a reload means something changed. When it fails, the held order is dropped and the sheet says 讀唔到. An order left standing would be the one from before the change, with editable fields, and nothing on it would say so.

**Nothing in the header, the calendar, the list or the foot is underlined to say it can be tapped.** An underline reads as a hyperlink, and on a page of figures it would also read as a rule under a sum. What can be tapped there is a ruled cell or a whole row. Inside the sheets a link is ink with a rule under it.

**The event drawing is kept, and nothing loads it.** The code and the rules that draw batches as bars and credits as chips under the days, with the lane packer that places them, are in `static/js/settle/archive/` and `static/css/archive/`. No module imports them and no document links them. Because archive directories are outside the asset version and the precache, no phone downloads them and a change to one does not send every client the app again. `lanes.js` keeps its test.

### Visual system

**Tokens are named by role and re-valued per theme, and both views draw from the one set.** `base.css` declares one set of custom properties: ground (`--bg`), panel (`--surface`, `--sheet-bg`), ink in four strengths (`--text`, `--text-2`, `--text-3`, `--text-4`), hairline (`--line`), the three status colours and `--on-solid`, the text on a solid status block. Dark values are the base and light ones sit under `prefers-color-scheme`.

- A rule names the role and never a colour, so a theme is a change of values and nothing else. There is no toggle and no stored preference.
- The two views are one document, switched between without a load. A change of ground, ink or shape at every switch would read as two apps.
- The settle view adds one colour to the board's three: `--blue`, for bank money not yet matched to a statement.

**Every value holds a contrast floor, in both themes.** The floors are stated beside the values in `base.css` and hold against the ground and the panel alike.

- `--text` at 11:1, and no more than about 13.5:1 on the ground, because brighter ink on a dark ground blooms.
- `--text-2` at 6.5:1. `--text-3` at 4.5:1 and at least 2 below `--text-2`, so the two strengths are told apart.
- The status colours at 4.5:1 as text, and `--on-solid` at 4.5:1 on each of them.
- `--line` at least 1.4:1 on the ground in both themes. The page is ruled, not boxed, and rules plain in one theme and nearly gone in the other would give it two layouts.

**A fourth ink is for coordinates.** `--text-4` sets the dates of empty days and the weekday head, at about 3:1 on the ground and no less than 3:1 on the panel, deliberately below what text meant to be read holds. They are positions to read the grid by. Set in the faintest text ink they would stand as strong as a collected day's figure, and the grid would lose the difference between a position and an amount.

**Text meant to be read is never set under 12px.** A label, a column head, a tag, a note, a secondary line and a figure are at least 12px in either view and in every sheet. The app is read on a phone for hours, by day and by night and in a moving car, and small text at regular weight is the hardest thing to read there, most of all on the dark ground.

- Three things may be smaller: a pure coordinate (the date in a settle calendar cell, the year beside the masthead date); the cents of a figure; and a figure at the moment a shrink-to-fit rule sets it smaller to hold it in its column, because cutting or wrapping a money figure is worse.
- The room a size needs is made from letter-spacing, from padding or by a line of its own, and never by cutting text, wrapping a figure or letting the page scroll sideways.
- The rule is stated in `base.css`, and the `type.*` checks of `scripts/e2e.py` walk the text on screen and hold the same list of exemptions.

**Figures are set in one monospaced face, self-hosted, and pass through `tight()`.** Times, flight numbers and money are what the day view is read for, at arm's length in a car. They are set in B612 Mono, a face drawn for cockpit displays, in which every digit takes the same width and a column of figures lines up without help.

- It is served from `static/fonts/` as two woff2 files cut down to Basic Latin and the few marks the app prints, preloaded by the shell and precached by the worker. A face fetched from someone else's server would be one more thing that can fail behind the tunnel, and the installed app must paint whole from its cache.
- Until the file arrives a figure is drawn in a local monospaced face declared with `size-adjust` and overridden ascent and descent, so that its cell and line box match. With `font-display: swap` the alternative is a row that changes width, and wraps differently, at the moment of the swap.
- A monospaced mark takes a whole cell and would open `13:42`, `$1560.50`, `9/10` and `2026-10-01` up into separate words. `shared.js:tight()` wraps each in a span the stylesheet pulls in, by a class that says how the mark sits in its cell: a colon, point, comma or middle dot is drawn at the left of its cell and is pulled from the right; a hyphen sits in the middle and a slash fills its cell, so each is pulled from both sides.
- The minus sign before money is U+2212 and is not a mark inside a figure, so it is left alone.
- `tight()` takes text that is already escaped and returns markup, skipping tags so its own output can be given back to it. It is applied to figures only, never to free text.

**Colour is reserved for status and for money that needs attention.** On the board, colour is a flight's status block (已到閘 inverse, 已降落 solid green, 預計 amber outline), a tight or urgent 出場 mark, 未入價 in amber and a 判罰 in red. In the settle view it says who owes what: amber is money the platform still owes, blue is bank money not matched, green is finished, red is what cannot be taken back or could not be done.

- Nothing else is coloured. A platform is told apart by its name in the code column, a link is ink with a rule under it, a chosen segment is solid ink, and a primary action is ink on ground reversed.
- State is a solid block or coloured text, never a tinted pill.
- A destructive action is a red outline until the step that confirms it, and solid red only there.
- A colour that also decorated (a hue per platform, a tint per service) would leave the eye nothing to find first, and what the driver needs first is which flight has landed and which fare is missing.

**Panels are drawn in the board's manner, by class and not by view.** A flat panel under a hairline, squared controls, the primary action solid ink, everything else an outline, a ledger of label-left and figure-right lines, a numpad that is one ruled grid. `.board` on a panel's element gives what it holds that manner, whichever view or module wrote it, so the order sheet looks the same in either view.

- The rules sit in `:where()`, so they weigh what the rules they replace weigh and win by coming later.
- A field a batch has frozen is drawn as text with 已結算 beside its label, not as a control made to look disabled. It cannot be edited from here at all, and the figure is still true.

## Data flow

```
Order entry
  WeChat message
    → bot: handle_message → ingest.parse_any → card (確認 / 取消)
        → price typed, or 確認 then price → db.save_or_revive_order, db.update_price
        → message for a live order → diff card (更新 / 略過) → db.update_order_from_message
    → web: POST /api/orders/parse (preview: fees, changes, suggested price)
        → POST /api/orders {type: paste} → parsed again → same db calls
        → kick over bot.sock → the bot polls at once
  Quick order
    → web: POST /api/orders {type: didi | uber | foodpanda, date, time, price}
    → bot: /didi, /uber → db.save_quick_order
  Edit
    → order sheet → PATCH /api/orders/<id> (price, fees, time, pickup_point, cancel)
    → db.update_order_fields (refuses locked fields on a batched order)

Live update
  any write → web._fingerprint differs → GET /api/events says so
    → stream.js → the showing view reloads

Flights (bot, 60 s heartbeat, fetch gated by calc_next_interval)
  reminders first: 用車時間, 出發, ETA-passed advisory, dep30 / dep10
  → flight.fetch_arrivals per tracked date → flight.match_flights
  → db.update_flight_info (scheduled, ETA, gate, status, hall)
  → status change → 已降落 / 已到閘口 / 航班取消 push, with the car park
    allowance line when CAR_PLATE is set
  → landed 舉牌 pickup → sign-text preview + 生成舉牌相
      → whiteboard.generate (fal.ai queue) → send_photo; the image is cached
        until delivered
  → day view: GET /api/orders?date= adds row_time, depart_hhmm, exit_urgency

Car park (bot, own job: 30 s with a visit open, 60 s otherwise)
  armed by a pickup's landing window (parking.arming_orders), or a visit open
  → ParkingClient.query (plate → inside?, pvNr, entry time, parkTime, fee, paid)
  → entry: parking_sessions row linked to the nearest armed order
      → 已入 push: allowance verdict or the first hour's price, + pay button
  → every inside reply: last_seen_at, last_park_minutes, last_fee stored
  → tap, or 50 minutes unpaid: fee query → storeOnlinePayment → PayDollar URL
  → alreadyPaid flips: 已收到付款 push; paid elsewhere, the previous fee
    reading becomes paid_amount
  → two not-inside replies: close → 已出閘 push, pickup_point and parking_fee
    written to the order, buttons for the two verdicts not picked
      → button or /parking mark → db.mark_parking_observed
  → every tick, from the database alone: parking.no_entry_orders
      → push, then pickup_point 富豪 and parking_fee 0, then the noentry tag
      → 其實去咗 P1 / 其實係 P4 → ingest.pickup_point_fields written

Statement (結算單)
  image → bot (photo or file), or settle view 圖 / drop → POST /api/statements/read
  → statement.read_image (RapidOCR) → Statement
      → nothing read → statement_flow.keep_unread_image → statements/failed/
  → statement_flow.prepare
      → db.settlement_candidates (orders within a day of the statement's
        dates, plus every settleable order)
      → statement.reconcile → checksum, per-line verdict, settle set, adjustments
      → credits.propose_statement → the credit line of the card:
          對到入數 (one credit agrees to the cent)
          對到入數 …，差 $510 (a credit in the window is smaller: short payment)
          入數可能係： (candidates only)
          未收到呢筆數 (nothing yet, the ordinary case)
  → confirm: bot button, or POST /api/statements/confirm {token}
  → statement_flow.confirm → db.create_settlement (batch, fines, adjustments,
    one transaction; the screenshot is written after the commit)
      → db.allocate for the credit the card named
      → short payment: the reply ends 平台查完喺 dashboard 入返邊張單

Bank credit (入數)
  feed file → credits.feed_changed → credits.ingest_feed → bank_credits
  → credits.propose_credit → push (with RIDE_WEB_URL, a link to
    /settle?credit=<id>; never a button that moves money), or one backfill summary
  → settle view:
      確認 (one statement) → POST /api/credits/<id>/allocate {settlement_id} → db.allocate
      確認 (a group) → POST /api/credits/<id>/allocate-all {settlement_ids} → db.allocate_all
      解除  → DELETE /api/settlements/<id>/allocations/<credit id> → db.deallocate
      ticks → POST /api/settlements/<id>/unpaid {order_ids} → db.mark_unpaid
      撤銷結算 → DELETE /api/settlements/<id> → db.delete_settlement (unlinks the
               orders, drops the allocations and adjustments, clears the flags)
  → bot: /credits (queue, detail, archive, unarchive, unlink) is the correction path

Settle view load
  GET /api/settle?month=&platform= per held month + GET /api/credits?platform=
  → month payload: the month's orders (settle columns); every batch the month's
    totals count something of, whole (orders with platform_amount, allocations,
    adjustments with trip_date, due_dates, unpaid_guesses, proposals); counts
    of settleable orders per platform over all time, for the tabs;
    month_totals; earlier; now, the server's clock
  → ledger payload: every credit of the platform with its state, the batches it
    paid (days, due_dates, settled_on, state), its proposals and its combo
  → cell → day sheet → leg → order sheet (GET /api/orders/<id>)
  → list row, or a link in a sheet → batch sheet → credit sheet
```

### HTTP routes

| Route | Purpose |
|---|---|
| `GET /`, `GET /settle` | The shell document |
| `GET /assets/<version>/<path>` | Versioned static files; 404 for any other version |
| `GET /sw.js` | Service worker, rendered with the version and the precache list |
| `GET /manifest.webmanifest` | Web app manifest, at a fixed address |
| `GET /api/ping` | `{ok, version}` |
| `GET /api/events` | Server-sent event stream |
| `GET /api/orders?date=` | One day's active orders |
| `POST /api/orders/parse` | Preview of a pasted message |
| `POST /api/orders` | Create a pasted or quick order, or apply an amendment |
| `GET /api/orders/<id>` | One active order, whole |
| `PATCH /api/orders/<id>` | Edit fields, move the meeting point, cancel |
| `GET /api/settle?month=&platform=` | One month of one platform for the settle view |
| `GET /api/credits?platform=` | The platform's whole credit ledger |
| `GET /api/credits/<id>` | Which platform a credit is in, and whether it still waits |
| `POST /api/statements/read` | Read a statement image (multipart `file`) |
| `POST /api/statements/confirm` | Write the batch a read statement describes |
| `POST /api/credits/<id>/allocate` | Put a credit against a batch |
| `POST /api/credits/<id>/allocate-all` | Pay a group of batches in full from one credit |
| `DELETE /api/settlements/<id>/allocations/<credit id>` | Take one credit's money back off a batch |
| `POST /api/settlements/<id>/unpaid` | Name the legs a short-paid batch is missing |
| `DELETE /api/settlements/<id>` | Undo a batch |
| `GET /api/settlements/<id>/image` | The batch's statement screenshot |

## Key files

**Python (`ride_dispatch/`)**

- `parser.py`: the `Order` dataclass and one parser per relayed message format: key-value (携程), 同程 comma format, 飛豬, SPACE and 分銷.
- `ingest.py`: `parse_any`, the cascade over those parsers, which also names the order's source; the parking and 舉牌 fee rules; the meeting-point plan and its tariff table (`PICKUP_POINTS`).
- `service.py`: classification by service type (flight pickup, departure reminder, platform), display labels, and what an order is worth: `expected_of` and `owed_of`.
- `pricing.py`: suggested price for a pasted order, from the history of fares to the same zone.
- `phone.py`: display-time E.164 formatting. Recognises every assigned country code (`E164_CC`, prefix-free, so the longest-match scan is unambiguous) and strips a trunk zero only for codes known to use one (`TRUNK_ZERO_CC`). A separated 3-digit code followed by exactly 7 digits is left alone: that is how a NANP number is written, and a wrong guess dials a stranger.
- `db.py`: schema, migrations and every query. Orders, batches (`create_settlement`, `delete_settlement`), credits and allocations (`allocate`, `allocate_all`, `deallocate`, `mark_unpaid`), the settle view's month payload (`get_settle_month`), car park visits.
- `month_totals.py`: `split_month`, pure: one month's orders into fare, received, awaiting, unsettled and short.
- `flight.py`: HKIA arrivals fetch, date-aware matching, the poll interval, `row_time`, and the pure rules behind every reminder.
- `parking.py`: HKIA car park client and the rules read off a visit: verdict, car park naming, which car parks have the allowance and which can be seen, arming window, no-entry rule, PayDollar link.
- `whiteboard.py`: sign photo generation (fal.ai queue API), name sanitising, the undelivered-image cache.
- `statement.py`: the statement reader (OCR boxes to rows, columns named off the header), `reconcile`, `leg_amount`, `due_dates`, and the report and button text both frontends print.
- `statement_flow.py`: `prepare` and `confirm`, shared by the bot and the web app; `keep_unread_image`.
- `credits.py`: feed ingestion, the pure credit and batch matcher, the `propose_*` wrappers that read the database, `guess_unpaid`, and the chat text about credits. Writes credits only, never allocations.
- `bot.py`: Telegram handlers (order cards, quick orders, statement images, `/cancel`, `/board`, `/parking`, `/credits`, `/start`) and the two repeating jobs. `_notify_chat_id()` is where every push is addressed.
- `web.py`: the Flask app: the shell, versioned assets, the service worker, the JSON API and the event stream. Holds read statements under a token until confirmed.

**Web (`templates/`, `static/`)**

- `templates/app.html`: the shell: the head, the two banners, the two view roots with the markup each starts from, the toast. Links four stylesheets and one module by versioned address.
- `templates/sw.js`: service worker source, rendered per version.
- `static/js/main.js`: boot: the store, the views and the router, the stream and the refresh on return, the worker's registration, the two banners.
- `static/js/router.js`: path to view and back; mounts, shows and hides views; keeps each view's scroll position.
- `static/js/store.js`: the day view's cache.
- `static/js/api.js`: every request to the server, and `AuthExpired`.
- `static/js/stream.js`: the one `EventSource` and its reopening.
- `static/js/shared.js`: leaf helpers shared by both views and the order sheet, and where the twins of Python functions live: `money` (`statement.money_str`), `formatPhoneE164` (`phone.format_phone_e164`, both code lists with it), `collectContactLines` (`bot.collect_contact_lines`), `platform` (`service.platform_of`), `expectedOf` and `owedOf` (`service.py`), `svcLabel` (`service.label`). Also `tight` and the toast. Changing one side of a twin without the other is the sync risk this list exists to name.
- `static/js/dates.js`: date arithmetic and labels for the settle view.
- `static/js/order-sheet.js`, `static/css/order-sheet.css`: the order sheet and its numpad. Holds the twin of `ingest.PICKUP_POINTS`.
- `static/js/day/index.js`, `static/css/day.css`: the day view: the board, the NOW line, the foot, the add panel. `rowTime` is the twin of `flight.row_time`.
- `static/js/settle/index.js`, `static/css/settle.css`: the settle view: keys and lens, the strip, the statement lists, the foot, the focus, and every settle sheet.
- `static/js/settle/days.js`: what the settle view says about a day and a statement, from the data alone: `dayState`, the figure forms, `statementName`, list order, `inMonthPart`, `otherLines`, `fareGap`, `waitedDays`, the two fitting rules (`fitLine`, `figureScale`), and what a credit against a statement says and which credits lead the queue (`matchWording`, `confirmedText`, `sureMatch`, `queueSections`).
- `static/js/settle/archive/`, `static/css/archive/`: the event drawing, kept and not loaded.
- `static/css/base.css`: the figure face and its stand-in, the tokens, and the components both views use (masthead, tabs, column head, foot, sheet and scrim, banners, toast, `.board`). A rule one view overrides belongs in that view's stylesheet.
- `static/fonts/`: B612 Mono Regular and Bold as subset woff2, with their OFL licence.
- `static/icons/`: home-screen icons at the fixed addresses installed apps point to. The two SVGs are the sources the four PNGs are rendered from. Their ground is the manifest's `background_color`, which a test holds, so a launch screen shows the mark and no square.

**Development (`scripts/`, `tests/`, `deploy/`)**

- `scripts/seed_demo_db.py`: builds a synthetic database covering every visual state; `--backlog` adds old bank credits that nothing accounts for. Most rows are placed by offset from the chosen day; what has to exist at any date (a statement across two months, a second statement collected in full in the month it reaches into, a month with nothing left to do) is placed by the calendar.
- `scripts/harness.py`: the server and page driver the two scripts below share.
- `scripts/shots.py`: screenshots every state in WebKit in both themes, each again at 340 wide and the settle view's again at 1000 wide, and compares two runs pixel by pixel.
- `scripts/e2e.py`: drives the app through its behaviour in the same browser. Every check has a server and a freshly seeded database of its own, so checks run in parallel processes. `--today-set` runs the whole set for several days, because the seed's days fall differently around the first of a month.
- `tests/`: pytest for the Python modules; `tests/js/` for the stateless browser modules under `node --test`; `tests/fixtures/` holds recorded OCR output with hashed order numbers. `tests/test_seed_demo.py` holds the synthetic database to what the scripts rely on: every month of every platform adds up, at days either side of a month's and a year's end and on a leap day.
- `deploy/`: example launchd plists and systemd units for the bot, the web app and the tunnel.
