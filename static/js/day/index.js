// The day view: one day's orders as an arrivals board, the order sheet over it
// and the add panel. The router mounts it once and keeps it; it reads orders
// through the store, so a day already seen is painted before the server
// answers.
//
// The markup this file writes carries inline handlers, which resolve names on
// window and cannot reach module scope, so the functions they call are
// published under window.rd.day.

import { $, PLATFORMS, _QUICK_TYPES, apiWrite, collectContactLines, esc, fmtDate,
         isFlightPickup, money, orderTime, platform, shortId, svcLabel, tight, toast,
         weekday } from '../shared.js';
import { detailView, numpadView, useOrderHost } from '../order-sheet.js';
import { AuthExpired } from '../api.js';

let root = null;              // the view's element, set by mount
let store = null;
// The view's own elements are looked up inside its root: the other view stays
// in the document and has a header, a scrim and a sheet of its own.
const byId = id => root.querySelector('#' + id);

// ---- state ----
let cur = fmtDate(new Date());
let filter = null;            // null | 'ride' | 'didi' | 'uber' | 'foodpanda'
let orders = [];
let sheetStack = [];          // stack of view render fns
let stackHost = 'sheet';      // element the stack renders into: 'sheet' | 'drop'
let sheetOrderId = null;      // order shown in detail view, if any
let revealId = null;          // order_id the next load is to scroll into view
let scrollToNext = false;     // scroll only on user navigation; SSE/timer re-renders must not fight manual scrolling
let drawn = false;            // the list has been drawn at least once, rows or the empty line
// The status word each row showed at the last paint, and the day that paint
// was of: a block whose word differs at the next paint of the same day turns
// over. Null until something has been painted.
let shown = null;             // { day, words: Map(order_id -> word) }

const TYPE_META = {
  didi:      { label: '滴滴',      priceLabel: '車費',    tunnelLabel: '隧道費', hasTunnel: true },
  uber:      { label: 'Uber',      priceLabel: '行程收入', tunnelLabel: '通行費', hasTunnel: true },
  foodpanda: { label: 'foodpanda', priceLabel: '價錢',                          hasTunnel: false },
};

// ---- utils ----
// When an order counts as done: pickups (接机) at service time + 60min —
// service time = flight ETA (or scheduled arrival) + passenger exit minutes,
// falling back to the booked time when there is no flight data; every other
// order at its scheduled time + 30min. Display-only; never persisted.
function doneAt(o) {
  const day = (o.scheduled_time || '').split(' ')[0];
  if (!day) return null;
  let hhmm, extraMin;
  if (isFlightPickup(o.service_type)) {
    hhmm = o.flight_eta || o.flight_scheduled || orderTime(o);
    extraMin = (o.passenger_exit_minutes || 0) + 60;
  } else {
    hhmm = orderTime(o);
    extraMin = 30;
  }
  if (!hhmm) return null;
  const d = new Date(day + 'T' + hhmm + ':00');
  if (isNaN(d)) return null;
  // Flight ETA is HH:MM only, so a delay past midnight (23:30 → 00:10) would
  // be built as the same day's early morning. But an ETA *earlier* than the
  // booked time is the normal case (booking time = scheduled landing + exit
  // buffer), so a plain d < sched check would push whole batches of
  // early-landing orders a day forward — only a gap above 12h counts as a
  // midnight crossing.
  const sched = new Date(day + 'T' + (orderTime(o) || '00:00') + ':00');
  if (!isNaN(sched) && sched - d > 12 * 3600 * 1000) d.setDate(d.getDate() + 1);
  d.setMinutes(d.getMinutes() + extraMin);
  return d;
}
function isDone(o) {
  const d = doneAt(o);
  return d ? Date.now() > d.getTime() : false;
}
// ---- nav / load ----
// Diagnostic: with localStorage.perf set, a paint of data reports how long it
// took to arrive: the first one since the navigation that loaded the page, a
// later one since the tap that asked for it, on a date control or on the
// link that switched to this view.
let navAt = 0;
let tapAt = () => 0;         // when the tap that switched views was made; the router's
function reportPerf() {
  let ms = null;
  if (!window._perfSaid) ms = performance.now();
  else if (navAt) ms = performance.now() - navAt;
  window._perfSaid = true;
  navAt = 0;
  if (ms === null) return;
  try { if (localStorage.getItem('perf')) toast(Math.round(ms) + ' ms'); } catch (e) { /* no storage */ }
}
// Diagnostic switch for the readout above: holding the date button toggles
// the key. An installed home-screen app has no address bar or inspector and
// keeps its own storage, so the page is the only place the key can be set
// there. (The shell also takes ?perf=1 / ?perf=0 from the address at boot.)
function initPerfSwitch() {
  const setPerf = on => {
    try { on ? localStorage.setItem('perf', '1') : localStorage.removeItem('perf'); } catch (e) { /* no storage */ }
  };

  const btn = byId('dateBtn');
  let timer = null, held = false, x = 0, y = 0;
  const cancel = () => { clearTimeout(timer); timer = null; };
  btn.addEventListener('pointerdown', e => {
    held = false;
    x = e.clientX; y = e.clientY;
    cancel();
    timer = setTimeout(() => {
      timer = null;
      held = true;
      let on = false;
      try { on = !localStorage.getItem('perf'); } catch (err) { /* no storage */ }
      setPerf(on);
      toast(on ? '計時 開' : '計時 關');
    }, 1500);
  });
  btn.addEventListener('pointermove', e => {
    if (timer && Math.hypot(e.clientX - x, e.clientY - y) > 8) cancel();
  });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(t => btn.addEventListener(t, cancel));
  // The release of a hold still produces a click; stop it before it reaches
  // the button's own handler.
  document.addEventListener('click', e => {
    if (!held) return;
    held = false;
    if (btn.contains(e.target)) { e.stopPropagation(); e.preventDefault(); }
  }, true);
}
function shiftDate(day, d) {
  const dt = new Date(day + 'T00:00:00');
  dt.setDate(dt.getDate() + d);
  return fmtDate(dt);
}
function go(d) {
  cur = shiftDate(cur, d);
  scrollToNext = true;
  navAt = performance.now();
  load();
}
function goToday() {
  cur = fmtDate(new Date());
  scrollToNext = true;
  navAt = performance.now();
  load();
}

// The store paints the day's last known rows at once, when it has them, and
// again when the server's answer differs.
async function load() {
  // A hidden view neither asks nor draws: showing it loads.
  if (!showing) return;
  // The row a save asked to be brought into view. When the store holds the
  // day, the first paint is of rows from before the save, so the id is kept
  // until a paint has the row.
  let reveal = revealId;
  revealId = null;
  byId('dateBtn').innerHTML = dateHtml(cur);
  const date = cur;
  const asked = navAt;
  // Placeholder rows stand in only for a list that has never been drawn, and
  // only while the store has nothing to draw it from. A day reached later
  // keeps the previous day's rows until its own arrive, so the list is never
  // blanked under the operator.
  if (!drawn && store.peek('orders:' + date) === undefined) {
    byId('orders').innerHTML = placeholderHtml();
  }
  let landed;
  try {
    landed = await store.read('orders:' + date, '/api/orders?date=' + date, data => {
      // The operator can have moved to another day while this was in flight.
      if (date !== cur) return;
      // Or to the other view, which hides this one.
      if (!showing) return;
      orders = data.orders;
      if (render(reveal)) reveal = null;
      reportPerf();
      if (sheetOrderId) {
        const o = orders.find(x => x.order_id === sheetOrderId);
        if (o && sheetStack.length === 1) renderSheet();  // refresh detail view only
        if (!o) closeSheet();
      }
    });
  } catch (e) {
    // Nothing came, and nothing was ever drawn: the placeholders give way to
    // the empty line rather than promise rows for ever.
    if (!drawn && date === cur && showing) { orders = []; render(); }
    // The tap this load was timed from has had its answer, and it was a
    // failure: a paint made later for another reason is not timed from it.
    // Unless a newer tap has set the mark since, which is then its own.
    if (navAt === asked) navAt = 0;
    // An expired login is announced by the shell, not by a toast.
    if (!(e instanceof AuthExpired)) toast('載入失敗');
    return;
  }
  // The request failed with the day's last known rows left on screen.
  if (!landed) {
    if (navAt === asked) navAt = 0;
    toast('載入失敗');
    return;
  }
  // The neighbouring days are warmed only once this day's own answer is in,
  // so they never compete with it, and only for the day still showing.
  if (date !== cur) return;
  for (const d of [-1, 1]) {
    const near = shiftDate(date, d);
    store.prefetch('orders:' + near, '/api/orders?date=' + near);
  }
}

// ---- render ----
function hhmmOf(d) {
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}
// Local wall clock in the server's row_time format, so the NOW line can be
// placed by plain string comparison against the row times.
function nowStamp() {
  const d = new Date();
  return fmtDate(d) + ' ' + hhmmOf(d) + ':' + String(d.getSeconds()).padStart(2, '0');
}

function stats(list) {
  let total = 0, priced = 0;
  list.forEach(o => { if (o.price) { total += o.price + (o.banner_fee || 0); priced++; } });
  return { total, priced };
}

// MM·DD in the figure face, the weekday beside it. The year is left out
// while it is this year's, which is nearly always.
function dateHtml(day) {
  const today = fmtDate(new Date());
  const year = day.slice(0, 4);
  return '<span class="d">' + tight(day.slice(5, 7) + '·' + day.slice(8, 10)) + '</span>' +
    '<span class="w">星期' + weekday(day) + (day === today ? ' · <b>今日</b>' : '') +
    (year !== today.slice(0, 4) ? '<span class="y">' + year + '</span>' : '') + '</span>';
}

function placeholderHtml() {
  const row = '<div class="ph-row"><span class="t"></span><span class="c"></span>' +
    '<span class="p"></span><span class="f"></span></div>';
  return '<div aria-hidden="true">' + row.repeat(5) + '</div>';
}

// reveal is the id of a row to bring into view, if the caller has one; the
// answer is whether that row was there to bring.
function render(reveal) {
  const visible = filter ? orders.filter(o => platform(o) === filter) : orders;
  const s = stats(visible);
  const unpriced = visible.length - s.priced;
  byId('day-foot').innerHTML =
    '<div><span class="k">程數</span><span class="v">' + visible.length + '</span></div>' +
    (unpriced > 0 ? '<div class="warn"><span class="k">未入價</span><span class="v">' + unpriced + '</span></div>' : '') +
    '<div class="tot"><span class="k">當日車費</span><span class="v">' + tight(money(s.total)) + '</span></div>';

  // 全部 leads and clears the filter: toggleFilter(null) leaves it null
  // whatever it was.
  byId('day-tabs').innerHTML =
    '<button class="tab' + (filter ? '' : ' on') + '" onclick="rd.day.toggleFilter(null)">全部' +
    '<span class="n">' + orders.length + '</span></button>' +
    PLATFORMS.map(p => {
      const n = orders.filter(o => platform(o) === p.key).length;
      return '<button class="tab' + (filter === p.key ? ' on' : '') + (n ? '' : ' zero') + '"' +
        ' onclick="rd.day.toggleFilter(\'' + p.key + '\')">' + p.label + '<span class="n">' + n + '</span></button>';
    }).join('');

  const before = shown && shown.day === cur ? shown.words : null;
  const words = new Map();
  shown = { day: cur, words };
  drawn = true;
  const box = byId('orders');
  if (!visible.length) {
    box.innerHTML = '<div class="empty">冇' + (filter ? PLATFORMS.find(p => p.key === filter).label : '') + '訂單</div>';
    return false;
  }
  const isToday = cur === fmtDate(new Date());
  // Two independent marks: NEXT is "the order to do next" and follows the
  // first not-yet-done row; the NOW line is a clock reading and sits at its
  // chronological place, which can be above a row that is already done.
  const nextIdx = isToday ? visible.findIndex(o => !isDone(o)) : -1;
  const nextId = nextIdx >= 0 ? visible[nextIdx].order_id : null;
  // row_time is the server's sort key (flight.py:row_time), so comparing
  // against it puts the line exactly where the list's own order puts it.
  const nowStr = isToday ? nowStamp() : null;
  const nowIdx = nowStr ? visible.findIndex(o => (o.row_time || '') > nowStr) : -1;
  const html = visible.map((o, i) => {
    const word = rowWord(o);
    words.set(o.order_id, word);
    // Only a row that was on the last paint of this day can have changed.
    const turn = !!before && before.has(o.order_id) && before.get(o.order_id) !== word;
    return (i === nowIdx ? nowHtml() : '') + rowHtml(o, i ? visible[i - 1] : null, nextId, turn);
  });
  // Every row's time has passed: the line belongs after the last of them.
  if (nowStr && nowIdx < 0) html.push(nowHtml());
  box.innerHTML = html.join('');
  let found = false;
  if (reveal) {
    const el = box.querySelector('[data-oid="' + CSS.escape(reveal) + '"]');
    if (el) { el.scrollIntoView({ block: 'nearest' }); found = true; }
  } else if (scrollToNext && nextId) {
    const el = box.querySelector('[data-oid="' + CSS.escape(nextId) + '"]');
    if (el) el.scrollIntoView({ block: 'center' });
  }
  scrollToNext = false;
  return found;
}

function toggleFilter(key) {
  filter = filter === key ? null : key;
  render();
}

// ---- row ----
function toMin(hhmm) {
  const p = String(hhmm || '').split(':');
  if (p.length !== 2) return NaN;
  return (+p[0]) * 60 + (+p[1]);
}
// +48m, +2h, +1h32: short enough to sit under the time it is counted to.
function gapText(min) {
  const h = Math.floor(min / 60), m = min % 60;
  if (!h) return '+' + m + 'm';
  return '+' + h + 'h' + (m ? String(m).padStart(2, '0') : '');
}
// Display-only shortening; the full address stays in the detail sheet.
function shortPlace(s) {
  const t = String(s || '').replace(/[(（][^)）]*[)）]\s*$/, '').trim();
  if (/机场|機場/.test(t)) {
    const m = t.match(/T[12]/i);
    return '機場' + (m ? ' ' + m[0].toUpperCase() : '');
  }
  return t;
}

// Best known landing time, in feed-confidence order; '' when the flight never
// showed up in the feed.
function landingTime(o) {
  if (o.flight_status === 'gate' && o.flight_gate) return o.flight_gate;
  if ((o.flight_status === 'landed' || o.flight_status === 'est') && o.flight_eta) return o.flight_eta;
  return o.flight_scheduled || '';
}
// 接机 rows show the flight's own time because the driver plans around landing:
// it is the number he watches and the only one that moves.
// Python twin: flight.py:row_time, which sorts the list by this same choice —
// the two must pick the same field or a row sorts where it does not read.
function rowTime(o) {
  return isFlightPickup(o.service_type) ? (landingTime(o) || orderTime(o)) : orderTime(o);
}
function rowMin(o) { return toMin(rowTime(o)); }

// Exit minutes run from touchdown, matching flight.py:svc_time — not from the
// at-gate time the row may be showing as its time.
function meetTime(o) {
  const land = o.flight_eta || o.flight_scheduled;
  if (!land || !o.passenger_exit_minutes) return '';
  const total = toMin(land) + o.passenger_exit_minutes;
  if (isNaN(total)) return '';
  return String(Math.floor(total / 60) % 24).padStart(2, '0') + ':' + String(total % 60).padStart(2, '0');
}

// Only the word: the row's big time already IS the time this status refers to,
// so printing the digits again would state the same fact twice. A flight with
// nothing but a schedule has nothing to qualify, so it says nothing.
function statusWord(o) {
  if (o.flight_status === 'gate' && o.flight_gate) return '已到閘';
  if (o.flight_status === 'landed' && o.flight_eta) return '已降落';
  if (o.flight_status === 'est' && o.flight_eta) return '預計';
  return '';
}

// A status belongs to a 接机 only; any other row says nothing.
function rowWord(o) {
  return isFlightPickup(o.service_type) ? statusWord(o) : '';
}
const WORD_CLASS = { '已到閘': 'gate', '已降落': 'landed', '預計': 'est' };
const QUICK_CODE = { didi: 'DIDI', uber: 'UBER', foodpanda: 'PANDA' };

// The code column and the place column of a ride. The flight number stands in
// for a 接機 badge, so the place is where the passenger is going; a 送機 says
// so in the code column, with the terminal when the destination names one,
// and the place is where to collect them. Any other service names itself and
// shows both ends. Answers the two cells' markup, code first.
function codePlaceHtml(o) {
  const from = shortPlace(o.pickup), to = shortPlace(o.dropoff);
  const arrow = '<span class="arrow">&rarr;</span>';
  const end = (text, lead, cls) =>
    '<span class="end' + (lead ? ' to' : '') + (cls || '') + '">' + (lead ? arrow : '') + '<span>' + esc(text) + '</span></span>';
  if (isFlightPickup(o.service_type)) {
    return [o.flight_number
      ? '<span class="code">' + esc(o.flight_number) + '</span>'
      : '<span class="code org">' + esc(from) + '</span>',
      '<span class="place">' + esc(to) + '</span>'];
  }
  if (o.service_type === '送机') {
    // shortPlace has already reduced anything naming the airport to 機場 and
    // its terminal. A destination that is somewhere else is not dropped: it
    // follows the pick-up, small.
    const airport = /^機場/.test(to);
    return ['<span class="code svc">送機' + (airport ? esc(to.slice(2)) : '') + '</span>',
      '<span class="place">' + (from ? end(from) : '') +
      (to && !airport ? ' ' + end(to, from, ' sub') : '') + '</span>'];
  }
  return ['<span class="code svc">' + esc(svcLabel(o.service_type)) + '</span>',
    '<span class="place reg">' + (from ? end(from) : '') + (to ? ' ' + end(to, from) : '') + '</span>'];
}

// The row's second line: the flight's status block, the two times worked out
// from it, then the marks.
function metaHtml(o, turn) {
  const m = [];
  if (isFlightPickup(o.service_type)) {
    const st = statusWord(o);
    if (st) m.push('<span class="st ' + WORD_CLASS[st] + (turn ? ' turn' : '') + '">' + st + '</span>');
    if (o.depart_hhmm) m.push('<span class="num">出發 <b>' + tight(esc(o.depart_hhmm)) + '</b></span>');
    const meet = meetTime(o);
    if (meet) m.push('<span class="num">用車 <b>' + tight(esc(meet)) + '</b></span>');
  }
  if (o.banner_fee) m.push('<span class="mk sign">舉牌</span>');
  if (o.passenger_exit_minutes) {
    const cls = o.exit_urgency === 'urgent' ? 'urgent' : o.exit_urgency === 'tight' ? 'tight' : 'neutral';
    m.push('<span class="mk ' + cls + '">出場 <span class="num">' + esc(o.passenger_exit_minutes) + '</span></span>');
  }
  return m.length ? '<span class="meta">' + m.join('') + '</span>' : '';
}

// Gross, matching the day total: the banner fee is money in, and the deductions
// that follow it are only visible in the detail sheet's breakdown. A 判罰賠款 is
// the exception: the platform has already taken that money, so the row carries
// it under the fare while the figure above it stays the gross the day sums.
function priceHtml(o) {
  if (!o.price) return '<span class="price unset">未入價</span>';
  const pen = o.penalty_fee > 0
    ? '<span class="pen">' + tight(money(-o.penalty_fee)) + '</span>' : '';
  return '<span class="price">' + tight(money(o.price + (o.banner_fee || 0))) + pen + '</span>';
}

// One canonical row time drives the server's sort, the row's time and the NOW
// line alike, so a later row can never show an earlier number. rowMin compares
// HH:MM alone, so only a landing past midnight still yields a negative
// difference — a real state with no waiting time to label, not an error.
function gapHtml(prev, o) {
  if (!prev) return '';
  const gapMin = rowMin(o) - rowMin(prev);
  return gapMin >= 30 ? '<span class="gap">' + gapText(gapMin) + '</span>' : '';
}

function nowHtml() {
  return '<div class="now"><span class="now-t">' + tight(hhmmOf(new Date())) +
    '</span><span class="now-line"></span></div>';
}

// data-oid and the .next class name are what render() queries to scroll a row
// into view; both must survive any markup change here. Every cell of the
// first line is always written, empty or not, so the grid's columns hold.
// The fare is written before the place, inside the same cell: it floats at
// the cell's right and the place's lines run beside it and then under it.
function rowHtml(o, prev, nextId, turn) {
  const quick = _QUICK_TYPES.has(o.service_type);
  const open = '<button class="row' + (quick ? ' quick' : '') + (isDone(o) ? ' done' : '') +
    (o.order_id === nextId ? ' next' : '') + '" data-oid="' + esc(o.order_id) +
    '" onclick="rd.day.openDetail(this.dataset.oid)">';
  // A quick order is filler between legs: its platform, its own fare (not
  // gross-plus-toll) and no second line beyond the wait before it.
  if (quick) {
    return open +
      '<span class="when"><span class="time">' + tight(esc(orderTime(o))) + '</span>' + gapHtml(prev, o) + '</span>' +
      '<span class="code plat">' + QUICK_CODE[platform(o)] + '</span>' +
      '<span class="where">' +
      (o.price ? '<span class="price">' + tight(money(o.price)) + '</span>'
               : '<span class="price unset">未入價</span>') +
      '<span class="place lite">' + esc(svcLabel(o.service_type)) + '</span></span></button>';
  }
  const [code, place] = codePlaceHtml(o);
  return open +
    '<span class="when"><span class="time">' + tight(esc(rowTime(o))) + '</span>' + gapHtml(prev, o) + '</span>' +
    code + '<span class="where">' + priceHtml(o) + place + '</span>' + metaHtml(o, turn) + '</button>';
}

// ---- sheet infra ----
// One view stack serves two hosts: the bottom sheet (detail flow) and the top
// drop panel (add flow). Only one host is ever open at a time.
function openSheet(viewFn) {
  stackHost = 'sheet';
  sheetStack = [viewFn];
  renderSheet();
  byId('day-scrim').classList.add('show');
  byId('day-sheet').classList.add('show');
}
function pushView(viewFn) { sheetStack.push(viewFn); renderSheet(); }
function popView() {
  if (sheetStack.length > 1) { sheetStack.pop(); renderSheet(); }
  else if (stackHost === 'drop') closeDrop();
  else closeSheet();
}
function renderSheet() {
  // Not while hidden, when the order sheet would be drawn through the other
  // view's host. The stack is kept; the load on show redraws the detail.
  if (!showing) return;
  const el = byId('day-' + stackHost);
  el.classList.remove('np-sheet');  // the pay-layout view re-adds this itself; it must not leak into other views
  const view = sheetStack[sheetStack.length - 1];
  if (stackHost === 'sheet') {
    el.innerHTML = '';
    view(el);
    return;
  }
  // Drop host: the panel is sized by whatever view is in it; animate the
  // height change so a stage switch reads as the same surface extending or
  // retracting, with the new content animating in on its own (row stagger,
  // action-cluster rise).
  const h0 = el.offsetHeight;
  el.innerHTML = '';
  view(el);
  if (el.classList.contains('show')) animateDropHeight(el, h0);
  else el.style.height = '';
}
// CSS cannot transition height to/from auto, so snap between measured pixel
// heights and release the inline height once the transition lands (a
// persistent transitionend listener set at mount clears it).
function animateDropHeight(el, h0) {
  el.style.height = '';
  const h1 = el.offsetHeight;
  if (h1 === h0) return;
  if (!parseFloat(getComputedStyle(el).transitionDuration)) return;  // reduced motion: no transitionend will fire to clean up
  el.style.transition = 'none';
  el.style.height = h0 + 'px';
  el.offsetHeight;  // commit the start height so the next change transitions
  el.style.transition = '';
  el.style.height = h1 + 'px';
}
function closeSheet() {
  if (stackHost === 'sheet') sheetStack = [];
  sheetOrderId = null;
  byId('day-sheet').classList.remove('show');
  if (!byId('day-drop').classList.contains('show')) {
    byId('day-scrim').classList.remove('show');
  }
}
function sheetHead(title, sub) {
  return '<div class="sheet-head"><div class="sheet-title">' + title + '</div>' +
    '<button class="sheet-x" onclick="rd.day.popView()" aria-label="關閉">&#10005;</button></div>' +
    (sub ? '<div class="sheet-sub">' + sub + '</div>' : '');
}

// ---- detail view ----
// The sheet itself is order-sheet.js, which the settle view opens too; this
// view hosts it on the bottom sheet's view stack.
function openDetail(orderId) {
  useOrderHost(orderHost);
  sheetOrderId = orderId;
  openSheet(detailView);
}

// Settlement state, from the batch columns /api/orders joins in. Money moves
// unsettled -> settled (platform paying) -> paid; settling itself is done on
// the settle page, so this row is read-only.
function mdSlash(day) {
  return (+day.slice(5, 7)) + '/' + (+day.slice(8, 10));
}
function settleLabel(o) {
  if (o.settlement_paid_on) return '已收 ' + mdSlash(o.settlement_paid_on);
  if (o.settlement_settled_on) return '等過數 · 結算 ' + mdSlash(o.settlement_settled_on);
  return '未結算';
}

// Returns false on failure so the numpad resets for retry.
async function patchOrder(body) {
  try {
    await apiWrite('PATCH', '/api/orders/' + encodeURIComponent(sheetOrderId), body);
  } catch (e) {
    if (!(e instanceof AuthExpired)) toast(e.message);
    return false;
  }
  await load();
  if (sheetStack.length > 1) popView();
  return true;
}

const orderHost = {
  order: () => orders.find(x => x.order_id === sheetOrderId),
  push: view => pushView(view),
  patch: patchOrder,
  cancelled(id) {
    closeSheet();
    toast('已取消 #' + shortId(id));
    load();
  },
  gone: () => closeSheet(),
  head: sheetHead,
  pop: popView,
  rowsAfter: o => [['結算', esc(settleLabel(o))]],
};

// ---- add flow ----
// The whole flow (paste form → preview/price, or quick-type → time → price →
// tunnel → confirm) lives in the top drop panel as one view stack: later
// stages grow the same panel instead of handing over to the bottom sheet.
let addState = null;

function closeOverlays() { closeSheet(); closeDrop(); }

function closeDrop() {
  if (stackHost === 'drop') sheetStack = [];
  addState = null;
  const drop = byId('day-drop');
  drop.classList.remove('show');
  // Wipe the panel once it has slid away: hidden leftovers would stay in the
  // focus order and duplicate the numpad element ids the sheet also uses.
  setTimeout(() => { if (!drop.classList.contains('show')) drop.innerHTML = ''; }, 350);
  // the scrim is shared with the bottom sheet — keep it while the sheet is open
  if (!byId('day-sheet').classList.contains('show')) {
    byId('day-scrim').classList.remove('show');
  }
}

function openAdd() {
  addState = { date: cur };
  stackHost = 'drop';
  sheetStack = [addFormView];
  renderSheet();
  byId('day-scrim').classList.add('show');
  byId('day-drop').classList.add('show');
}

function addFormView(el) {
  el.insertAdjacentHTML('beforeend',
    sheetHead('入單', '<span class="num">' + esc(addState.date) + '</span> 星期' + weekday(addState.date)) +
    '<textarea class="paste-box" id="pasteBox" placeholder="喺度貼訂單 message"></textarea>' +
    '<button class="primary-btn" id="parseBtn">解析</button>' +
    '<div class="quick-types">' +
    Object.keys(TYPE_META).map(t =>
      '<button class="quick-type-btn ' + t + '" data-t="' + t + '">' + TYPE_META[t].label + '</button>'
    ).join('') +
    '</div>');
  // Restored on pop so backing out of the price stage keeps the pasted message
  const box = byId('pasteBox');
  box.value = addState.pasteText || '';
  // Focus synchronously: this render happens inside the tap's call chain, and
  // iOS only raises the keyboard for focus() made within a user gesture.
  box.focus({ preventScroll: true });
  byId('parseBtn').addEventListener('click', async function () {
    const text = byId('pasteBox').value.trim();
    if (!text) return;
    addState.pasteText = text;
    this.disabled = true;
    this.textContent = '解析緊…';
    try {
      addState.preview = await apiWrite('POST', '/api/orders/parse', { text });
      pushView(pastePriceView());
    } catch (e) {
      if (!(e instanceof AuthExpired)) toast(e.message);
      this.disabled = false;
      this.textContent = '解析';
    }
  });
  el.querySelectorAll('.quick-type-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      // Kept as typed, so that backing out of the quick stages finds it again.
      addState.pasteText = byId('pasteBox').value;
      addState.type = btn.dataset.t;
      pushView(addTimeView());
    });
  });
}

function addTimeView() {
  const meta = TYPE_META[addState.type];
  return numpadView({
    title: meta.label + ' · 時間', hint: addState.date, mode: 'time',
    onConfirm: v => { addState.time = v; pushView(addPriceView()); },
  });
}

function addPriceView() {
  const meta = TYPE_META[addState.type];
  return numpadView({
    title: meta.label + ' · ' + meta.priceLabel, hint: addState.time, mode: 'money',
    onConfirm: v => {
      addState.price = v;
      if (meta.hasTunnel) pushView(addTunnelView());
      else { addState.tunnel = 0; pushView(addConfirmView); }
    },
  });
}

function addTunnelView() {
  const meta = TYPE_META[addState.type];
  return numpadView({
    title: meta.label + ' · ' + meta.tunnelLabel, mode: 'money',
    quick: { label: '冇' + meta.tunnelLabel, onTap: () => { addState.tunnel = 0; pushView(addConfirmView); } },
    onConfirm: v => { addState.tunnel = v; pushView(addConfirmView); },
  });
}

function addConfirmView(sheet) {
  const meta = TYPE_META[addState.type];
  // Uber model follows the bot: price stored = trip income + toll (toll is reimbursed)
  const price = addState.type === 'uber' ? addState.price + addState.tunnel : addState.price;
  const net = addState.type === 'uber' ? addState.price : addState.price - addState.tunnel;
  const rows = [
    ['日子', addState.date + ' 星期' + weekday(addState.date)],
    ['時間', addState.time],
    [meta.priceLabel, '$' + $(addState.price)],
  ];
  if (meta.hasTunnel) rows.push([meta.tunnelLabel, addState.tunnel ? '$' + $(addState.tunnel) : '冇']);
  sheet.insertAdjacentHTML('beforeend',
    sheetHead(meta.label + ' · 確認', '') +
    '<div class="sum-rows">' +
    rows.map(r => '<div class="sum-row"><span class="k">' + esc(r[0]) + '</span><span class="v num">' + tight(esc(r[1])) + '</span></div>').join('') +
    (addState.tunnel ? '<div class="sum-row total"><span class="k">' +
      (addState.type === 'uber' ? '總收入' : '淨收入') + '</span><span class="v num">' +
      tight('$' + $(addState.type === 'uber' ? price : net)) + '</span></div>' : '') +
    '</div>' +
    '<button class="primary-btn" id="addSave">儲存</button>' +
    '<button class="ghost-btn" onclick="rd.day.popView()">返上一步</button>'
  );
  byId('addSave').addEventListener('click', async function () {
    this.disabled = true;
    this.textContent = '儲存緊…';
    try {
      const res = await apiWrite('POST', '/api/orders', {
        type: addState.type, date: addState.date, time: addState.time,
        price: price, tunnel_fee: addState.tunnel,
      });
      if (filter && filter !== addState.type) filter = null;  // keep the new card visible
      revealId = res.order_id;
      closeDrop();
      toast(meta.label + ' 已入單');
      load();
    } catch (e) {
      if (!(e instanceof AuthExpired)) toast(e.message);
      this.disabled = false;
      this.textContent = '儲存';
    }
  });
}

// ---- paste flow (message entry lives in the top drop panel; see openAdd) ----
// After a successful parse, preview and price keypad share one view: the
// operator verifies the parsed fields by eye and the confirm tap both prices
// and saves — there is deliberately no separate confirmation step.
function pastePriceView() {
  const p = addState.preview, o = p.order;
  const svc = svcLabel(o.service_type);
  const rows = [['平台', [p.source, svc].filter(Boolean).join(' · ')]];
  // A row is [label, value, urgency class, whether the value is a figure].
  if (o.scheduled_time) rows.push(['用車時間', o.scheduled_time.slice(0, 16), '', true]);
  if (o.passenger_name) rows.push(['乘客', o.passenger_name]);
  for (const [cl, cv] of collectContactLines(o)) rows.push([cl, cv, '', true]);
  if (o.flight_number) rows.push(['航班', o.flight_number, '', true]);
  if (o.passenger_exit_minutes) {
    let exitVal = o.passenger_exit_minutes + '分鐘';
    if (p.exit_urgency === 'urgent') exitVal += ' — 降落前要出發';
    if (p.exit_urgency === 'tight') exitVal += ' — 降落即刻出發';
    rows.push(['出場', exitVal, p.exit_urgency]);
  }
  if (o.vehicle_type) rows.push(['車型', o.vehicle_type]);
  if (o.pickup) rows.push(['路線', o.pickup + ' → ' + o.dropoff]);
  if (o.driver_notes) rows.push(['備註', o.driver_notes]);
  if (p.parking_fee) rows.push(['停車費', '$' + $(p.parking_fee), '', true]);
  if (p.banner_fee) rows.push(['舉牌費', '$' + $(p.banner_fee), '', true]);
  const rowsHtml = '<div class="paste-preview">' + rows.map((r, i) =>
    '<div class="sum-row" style="--i:' + i + '"><span class="k">' + esc(r[0]) + '</span><span class="v' +
    (r[2] ? ' ' + esc(r[2]) : '') + (r[3] ? ' num' : '') + '">' + (r[3] ? tight(esc(r[1])) : esc(r[1])) + '</span></div>'
  ).join('') + '</div>';

  // A live row on the same order number means the platform re-sent the
  // booking because the customer changed something: price the amendment
  // instead of refusing it, and preview only what it rewrites.
  if (p.duplicate) {
    const deadEnd = (title, warn) => function view(sheet) {
      sheet.insertAdjacentHTML('beforeend',
        sheetHead(title, '<span class="num">#' + esc(shortId(o.order_id)) + '</span>') +
        '<div class="dup-warn">' + esc(warn) + '</div>' + rowsHtml);
    };
    if (p.locked) return deadEnd('貼單 · 已結算', '已結算嘅單要先撤銷結算');
    if (!p.changes.length) {
      return deadEnd('貼單 · 冇變更', '#' + shortId(o.order_id) + ' 同 DB 一樣，冇嘢改');
    }
    const changesHtml = '<div class="paste-preview changes">' + p.changes.map((c, i) =>
      '<div class="sum-row" style="--i:' + i + '"><span class="k">' + esc(c.label) + '</span>' +
      '<span class="v"><span class="was">' + esc(c.old || '—') + '</span> &rarr; ' +
      esc(c.new || '—') + '</span></div>'
    ).join('') + '</div>';
    const kept = p.current_price;
    return numpadView({
      title: '#' + shortId(o.order_id) + ' · 更新',
      above: changesHtml, mode: 'money', layout: 'pay',
      // Confirming the prefilled price keeps what the order already earns —
      // an amendment reprices only when the operator says so.
      suggest: kept != null ? kept : null,
      skip: {
        label: kept != null ? '保留 $' + $(kept) + '，直接更新' : '先唔入價，直接更新',
        onTap: () => savePaste(null),
      },
      onConfirm: v => savePaste(v),
    });
  }
  return numpadView({
    title: '#' + shortId(o.order_id) + ' · 入價',
    above: rowsHtml, mode: 'money', layout: 'pay',
    suggest: p.suggested_price != null ? p.suggested_price : null,
    skip: { label: '先唔入價，直接儲存', onTap: () => savePaste(null) },
    onConfirm: v => savePaste(v),
  });
}

// Returns false on failure so numpadView resets for retry.
async function savePaste(price) {
  const body = { type: 'paste', text: addState.pasteText };
  if (price != null) body.price = price;
  try {
    const res = await apiWrite('POST', '/api/orders', body);
    if (res.date && res.date !== cur) cur = res.date;  // a pasted order carries its own date — navigate to it
    if (filter && filter !== 'ride') filter = null;    // pasted orders are platform rides; drop any filter that would hide the new card
    revealId = res.order_id;
    closeDrop();
    // An amendment answers with updated; a fresh entry answers with revived.
    const head = 'updated' in res ? (res.updated ? '已更新 #' : '冇嘢改 #')
      : (res.revived ? '已重新入單 #' : '已入單 #');
    toast(head + shortId(res.order_id));
    load();
  } catch (e) {
    if (!(e instanceof AuthExpired)) toast(e.message);
    return false;
  }
}

window.rd = window.rd || {};
window.rd.day = { go, goToday, toggleFilter, openDetail, openAdd, closeOverlays, popView };

// ---- the view ----
let showing = false;
let shownOnce = false;
export const dayView = {
  mount(el, deps) {
    root = el;
    store = deps.store;
    tapAt = deps.navAt;
    initPerfSwitch();
    // Release the drop panel's inline animation height once a grow/shrink lands,
    // so the stage classes (content height / 100dvh) govern it between animations.
    // When the slide-up close finishes, wipe the panel (see closeDrop).
    byId('day-drop').addEventListener('transitionend', e => {
      if (e.propertyName === 'height') e.target.style.height = '';
      if (e.propertyName === 'transform' && !e.target.classList.contains('show')) e.target.innerHTML = '';
    });
    // re-render each minute so the NOW marker moves and past rows dim; SSE only fires on DB changes.
    // A hidden view is not painted; showing it paints it.
    setInterval(() => { if (showing) render(); }, 60000);
  },
  show() {
    showing = true;
    useOrderHost(orderHost);
    // Only the first showing scrolls to the next order: coming back from the
    // other view returns the operator to where he was.
    scrollToNext = !shownOnce;
    shownOnce = true;
    navAt = tapAt();
    return load();
  },
  hide() { showing = false; },
  // The server said something changed.
  refresh() { return load(); },
};
