// The settle view: the calendar strip of what each platform owes and has paid,
// the sheets that read a day, a batch and a bank credit, and the statement
// intake. The router mounts it once and keeps it, so the months it has loaded
// and the sheet it has open are still there when the operator comes back.
//
// It shares its document, its window and the document's scroll position with
// the day view. While it is hidden it has no geometry and the order sheet is
// the other view's, so it draws nothing then: an answer that lands while it
// is hidden is not painted, its listeners on window stand down, and showing it
// loads again.

import { $, PLATFORMS, apiWrite, esc, expectedOf, fmtDate, money, orderTime, owedOf,
         platform, shortId, svcLabel, tight, toast, weekday } from '../shared.js';
import { detailView, useOrderHost } from '../order-sheet.js';
import { AuthExpired, apiFetch } from '../api.js';
import { addDays, addMonths, dateSpanLabel, dow, groupId, mdLabel, mdSlash, monthEnd,
         monthKey, monthsBetween, round2, runsOf, tailId } from '../dates.js';
import { packLanes } from '../lanes.js';

let root = null;              // the view's element, set by mount
// The view's own elements are looked up inside its root: the other view stays
// in the document and has a foot, tabs, a scrim and a sheet of its own.
const byId = id => root.querySelector('#' + id);
// Whether the view is the one on screen; set by show and hide.
let showing = false;
// When the tap that switched to this view was made, or 0; the router's.
let tapAt = () => 0;

// ---- state ----
// /api/settle answers one month at a time, but the calendar is a continuous
// strip, so the page holds every month it has scrolled through: 'YYYY-MM' ->
// that month's payload. The keys are always an unbroken run, because a gap
// would draw as real dates carrying no data -- indistinguishable from days
// with no work on them.
let months = new Map();
// Months with a fetch in flight. A sentinel can enter the viewport twice
// before its month lands, and the second entry must not fetch again.
let loading = new Set();
// Bumped whenever the strip is thrown away and refounded (platform switch, a
// jump far outside it). A fetch that started before the bump is answering a
// question nobody is asking any more.
let gen = 0;
// The merged read of every loaded month, which is what the whole page below
// works off.
let data = { orders: [], settlements: [], counts: {}, totals: { unsettled: 0, awaiting: 0 }, now: '' };
// The bank ledger, exactly what /api/credits returns: every credit of the
// platform, whatever the strip has loaded.
let ledger = { counts: { open: 0, partial: 0, done: 0, archived: 0 }, sums: { open: 0, done: 0 }, credits: [] };
let curPlat = 'ride';
// The month the header names: the one the strip is showing, updated by the
// scroll rather than by a paging button.
let viewMonth = fmtDate(new Date()).slice(0, 7);
// Settle-ability is decided against the server's clock, not the browser's, so
// the page and the API agree on which legs are done.
let NOW = '';
let TODAY = '';
let BATCH_OF = new Map();
// Which batches have their order list unfolded. Page state rather than DOM
// state, so an SSE repaint mid-read does not fold the list away.
let foldOpen = new Set();
// Which money relation the strip is lighting: null, or { kind, id } naming a
// batch or a credit. Page state rather than DOM state, because the relation
// outlives the paint that drew it -- a month arriving under the scroll, or an
// SSE repaint, must not drop what the operator is reading.
let focus = null;

// The operator works one platform for a stretch, so reopening lands where he
// left off. Storage can be unavailable (private mode), and the page must
// still render if it is.
function loadPlat() {
  try {
    const p = localStorage.getItem('settlePlatform');
    if (p && PLATFORMS.some(x => x.key === p)) curPlat = p;
  } catch (e) { /* no stored preference */ }
}
function savePlat() {
  try { localStorage.setItem('settlePlatform', curPlat); } catch (e) { /* not stored */ }
}

// ---- derived state ----
// expectedOf() is the twin of service.py:expected_of and lives in shared.js,
// because the day view's detail sheet reads it too.
function orderDate(o) { return (o.scheduled_time || '').split(' ')[0]; }
function batchById(id) { return data.settlements.find(b => b.id === +id) || null; }
function creditById(id) { return ledger.credits.find(c => c.id === +id) || null; }
// The work queue: the credits no statement accounts for yet. Oldest value date
// first, the order list_credits returns -- those are the ones being chased.
function openCredits() { return ledger.credits.filter(c => c.state === 'open' || c.state === 'partial'); }
function batchOf(orderId) { return BATCH_OF.get(orderId) || null; }
function reindex() {
  // A batch can straddle months, so its own member list is the only place legs
  // outside the loaded months appear.
  BATCH_OF = new Map();
  for (const b of data.settlements) {
    for (const o of b.orders) BATCH_OF.set(o.order_id, b);
  }
}
// Mirrors db.py's settleable predicate: finished, priced, not already batched.
// Both other kinds of leg stay visible; neither can enter a batch.
function isSettleable(o) {
  return !batchOf(o.order_id) && (o.price || 0) > 0 && o.scheduled_time < NOW;
}
function ordersOn(dateStr) {
  return data.orders.filter(o => orderDate(o) === dateStr);
}
// Two numbers per day: what it earned, and how much of that no statement has
// claimed. Amber is the second one; a day fully on statements shows its total
// muted, because the bar under it carries the state.
function dayInfo(dateStr) {
  const list = ordersOn(dateStr);
  let total = 0, loose = 0;
  for (const o of list) {
    total += expectedOf(o);
    if (!batchOf(o.order_id)) loose += owedOf(o);
  }
  return { n: list.length, total, loose };
}

// ---- labels ----
function todayMonth() { return TODAY ? monthKey(TODAY) : fmtDate(new Date()).slice(0, 7); }
function platLabel(key) { return PLATFORMS.find(p => p.key === key).label; }
function paidLabel(b) { return '已收 ' + mdSlash(b.paid_on); }
// Money to the cent, so the label carries both figures rather than the one the
// column has room for: what the statement is worth, and what is still owed.
function barLabel(b) {
  return b.state === 'partial'
    ? '$' + $(b.confirmed_amount) + ' · 差 $' + $(b.outstanding)
    : '$' + $(b.confirmed_amount);
}
// What a batch is owed, in one phrase: a part-paid batch is neither 已收 nor
// simply 等過數 -- the outstanding half is what the operator is chasing.
function batchTag(b) {
  if (b.state === 'paid') return paidLabel(b);
  if (b.state === 'partial') return '差 $' + $(b.outstanding);
  return '等過數';
}
// A batch is the account and a credit is one of its lines, so a chip's colour
// is read off the batches it went into, not off how much of itself is left:
// money already put against a batch that is still short is still open work.
function chipState(c) {
  if (c.state === 'archived') return ' gone';
  // Money still unmatched is what the queue chases, so it keeps the queue's
  // colour whatever else the credit has already paid; only a fully spent
  // credit takes its colour from the batches it went to.
  if (c.state === 'open' || c.state === 'partial') return ' open';
  if ((c.batches || []).some(b => b.state === 'partial')) return ' short';
  return '';
}
// Display-only shortening; the raw string stays on the order.
function shortPlace(s) {
  const t = String(s || '').replace(/[(（][^)）]*[)）]\s*$/, '').trim();
  if (/机场|機場/.test(t)) {
    const m = t.match(/T[12]/i);
    return '機場' + (m ? ' ' + m[0].toUpperCase() : '');
  }
  return t;
}
// What the operator needs to recognise the leg on a platform statement: the
// service, plus the endpoint he actually drove to (or the flight he met).
function orderLabel(o) {
  if (platform(o) !== 'ride') return svcLabel(o.service_type);
  const svc = svcLabel(o.service_type);
  const who = o.flight_number || shortPlace(o.service_type === '送机' ? o.pickup : o.dropoff) || o.passenger_name;
  return who ? svc + ' · ' + who : svc;
}
function batchDates(b) {
  return [...new Set(b.orders.map(orderDate))].sort();
}
// A 舉牌 paid ahead of a trip the platform held back sits on the held trip's
// day, which none of the batch's own legs may reach; the calendar still has to
// show that part of the day's money went into the batch.
function aheadLines(b) { return (b.adjustments || []).filter(a => a.ahead); }
function batchSpan(b) {
  return [...new Set([...batchDates(b), ...aheadLines(b).map(a => a.date)])].sort();
}
// The runs a batch covers, and the one carrying its amount: the latest run
// with a leg in it.  Every other run points at that one.
function spanRuns(b) {
  const legDays = new Set(batchDates(b));
  const runs = runsOf(batchSpan(b));
  const main = [...runs].reverse().find(run => run.some(d => legDays.has(d))) ||
    runs[runs.length - 1];
  return { runs, main, legDays };
}
function aheadIn(b, run) {
  return round2(aheadLines(b).filter(a => run.includes(a.date)).reduce((sum, a) => sum + a.amount, 0));
}
function primaryRunOf(dates) { const r = runsOf(dates); return r[r.length - 1] || []; }
function spanLabelOf(dates) { return dateSpanLabel(primaryRunOf(dates)); }
function batchLabel(b) { return spanLabelOf(batchDates(b)); }
function runShortLabel(run) {
  const a = run[0], z = run[run.length - 1];
  if (a.slice(0, 7) !== z.slice(0, 7)) return dateSpanLabel(run);
  return (+a.slice(8)) + (a === z ? '' : '–' + (+z.slice(8))) + '日';
}
function heldLabel(b) {
  const { runs, main } = spanRuns(b);
  const held = runs.filter(run => run !== main);
  if (!held.length) return '';
  return '連 ' + held.map(run => {
    const legs = b.orders.filter(o => run.includes(orderDate(o))).length;
    const ahead = aheadIn(b, run);
    return runShortLabel(run) + ' ' + [legs ? legs + ' 程' : '', ahead ? '舉牌 $' + $(ahead) : '']
      .filter(Boolean).join(' + ');
  }).join(' · ');
}

// ---- load ----
function loadedMonths() { return [...months.keys()].sort(); }

// Every reader below asks the merged book, not a month, so the merge is where
// the strip stops being a pile of month payloads.
function remerge(book) {
  const orders = [];
  const settlements = [];
  const seen = new Set();
  for (const key of loadedMonths()) {
    const p = months.get(key);
    for (const o of p.orders) orders.push(o);
    // Orders are scoped to their month server-side, but a batch straddling a
    // boundary comes back whole in both months' payloads, so it has to be
    // taken once or its bar would be drawn twice.
    for (const b of p.settlements) {
      if (seen.has(b.id)) continue;
      seen.add(b.id);
      settlements.push(b);
    }
  }
  // counts, totals and the clock span all time, so any payload carries them and
  // the freshest one wins.
  data = { orders, settlements, counts: book.counts, totals: book.totals, now: book.now };
  NOW = data.now;
  TODAY = NOW.slice(0, 10);
  reindex();
}

// One month into the map, nothing drawn: a caller loading several fills them
// all in and paints once.
async function fetchMonth(key) {
  if (months.has(key) || loading.has(key)) return;
  // Refounding swaps the set rather than emptying it, so the entry has to come
  // out of the one it went into and not out of whatever is current by then.
  const inflight = loading;
  const g = gen;
  inflight.add(key);
  let payload;
  try {
    const res = await apiFetch('/api/settle?month=' + key + '&platform=' + curPlat);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    payload = await res.json();
  } catch (e) {
    // An expired login is announced by the shell, not by a toast.
    if (!(e instanceof AuthExpired)) toast('載入失敗');
    return;
  } finally {
    inflight.delete(key);
  }
  if (g !== gen) return;
  months.set(key, payload);
}

// The strip fills in every month between what it holds and the one asked for,
// because its keys have to stay an unbroken run. A month far outside it is a
// jump rather than a scroll, so past this many the strip is refounded there
// instead of paying for every month in between.
const FILL_MAX = 3;
async function ensureMonth(key) {
  if (months.has(key)) return true;
  const keys = loadedMonths();
  const lo = keys.length && keys[0] < key ? keys[0] : key;
  const hi = keys.length && keys[keys.length - 1] > key ? keys[keys.length - 1] : key;
  const want = monthsBetween(lo, hi).filter(k => !months.has(k) && !loading.has(k));
  if (want.length > FILL_MAX) return refound(key);
  await Promise.all(want.map(fetchMonth));
  // Hidden by the time the months arrived: they stay held and are drawn by
  // the load that showing the view makes, and the caller, who would go on to
  // scroll or to open a sheet, is told the month is not there.
  if (!showing) return false;
  if (!months.has(key)) return false;
  remerge(months.get(key));
  render();
  return true;
}

// Throw the strip away and start it again on one month: the platform changed,
// or the operator asked for somewhere the strip cannot reach by scrolling.
// The old rows go first, so the paint has nothing to hold position against and
// the new strip -- which begins on that month's first week -- comes up at the
// top of the strip area on its own.
async function refound(key) {
  gen++;
  months = new Map();
  loading = new Set();
  anchorDebt = 0;
  pinId = '';
  byId('grid').innerHTML = '';
  window.scrollTo(0, 0);
  await load(key);
  return months.has(key);
}

// Diagnostic: with localStorage.perf set, a paint of data reports how long it
// took to arrive: the first one since the navigation that loaded the
// document, a later one since the tap that switched to this view.
let navAt = 0;
function reportPerf() {
  let ms = null;
  if (!window._perfSaid) ms = performance.now();
  else if (navAt) ms = performance.now() - navAt;
  window._perfSaid = true;
  navAt = 0;
  if (ms === null) return;
  try { if (localStorage.getItem('perf')) toast(Math.round(ms) + ' ms'); } catch (e) { /* no storage */ }
}

// A write here or on another device lands through load(): every month the strip
// holds is refetched at once, because a batch or a credit can have moved in any
// of them, and a half-refreshed strip would show the same batch in two states.
async function load(seed) {
  // A hidden view neither asks nor draws: the strip has no width to lay labels
  // out in, the document's scroll position is the other view's, and the order
  // sheet is drawn through the other view's host. Showing the view loads.
  if (!showing) return;
  const keys = loadedMonths();
  if (!keys.length) keys.push(seed || todayMonth());
  const g = gen;
  let payloads, credits;
  try {
    // The ledger comes with the months: the header counts it and the sheet
    // lists it, and a credit landing has to reach both at once.
    const res = await Promise.all(
      keys.map(k => apiFetch('/api/settle?month=' + k + '&platform=' + curPlat))
        .concat([apiFetch('/api/credits?platform=' + curPlat)]));
    const bad = res.find(r => !r.ok);
    if (bad) throw new Error('HTTP ' + bad.status);
    const bodies = await Promise.all(res.map(r => r.json()));
    credits = bodies.pop();
    payloads = bodies;
  } catch (e) {
    if (!(e instanceof AuthExpired)) toast('載入失敗');
    return;
  }
  if (g !== gen) return;
  // Hidden by the time the answer arrived, so it is not used at all.
  if (!showing) return;
  keys.forEach((k, i) => months.set(k, payloads[i]));
  ledger = credits;
  remerge(payloads[payloads.length - 1]);
  dropStaleFocus();
  render();
  reportPerf();
  // An open order sheet shows the order itself, which the month payload does
  // not carry, so it is fetched again with the rest.
  const ov = orderView();
  if (ov) await fetchOrder(ov);
  repaintOpenView();
}

// A write here or on another device lands through load(): an open sheet has to
// show the new state, and a batch that no longer exists cannot be shown at all.
function repaintOpenView() {
  if (!views.length) return;
  views = views.filter(v => {
    if (v.kind === 'credit') return !!creditById(v.id);
    if (v.kind !== 'batch' && v.kind !== 'undo' && v.kind !== 'unlink') return true;
    const b = batchById(v.id);
    if (!b) return false;
    // A confirm view for money already off the batch has nothing left to
    // confirm: another device, or the bot, can have taken it back first.
    return v.kind !== 'unlink' || b.allocations.some(a => a.credit_id === v.credit);
  });
  if (!views.length) { closeSheet(); return; }
  paintView();
}

// ---- render ----
// The calendar goes first: the header names the month the strip is showing, and
// that is read off the rows the calendar has just laid out.
function render() {
  renderTabs();
  renderCalendar();
  renderHeader();
}

function renderHeader() {
  // YYYY·MM in the figure face. The second span is always there, empty when
  // the month is not the current one: the stylesheet gives it a line of its
  // own on a narrow screen, and the header must not change height with the
  // month it names.
  byId('monthBtn').innerHTML =
    '<span class="d">' + tight(viewMonth.slice(0, 4) + '·' + viewMonth.slice(5, 7)) + '</span>' +
    '<span class="w">' + (viewMonth === todayMonth() ? '<b>今個月</b>' : '&nbsp;') + '</span>';
  // Totals span the whole book, not the months the strip has loaded: old
  // unsettled days are exactly the ones the operator is here to clear. The
  // server computes them, so the tabs and the calendar cannot disagree about
  // the same money.
  // Only what is still to be done: a matched or archived credit is finished
  // business and is read off the calendar by its value date instead.
  const open = openCredits();
  const figs = [money(data.totals.unsettled), '$' + $(data.totals.awaiting)]
    .concat(open.length ? ['$' + $(ledger.sums.open)] : []).map(tight);
  const foot = byId('settle-foot');
  // The stylesheet sizes the figures so that all of them fit the one line.
  foot.style.setProperty('--n', figs.reduce((n, f) => n + +cellsOf(f), 0).toFixed(2));
  foot.innerHTML =
    '<div class="warn"><span class="k">未結算</span><span class="v">' + figs[0] + '</span></div>' +
    '<div><span class="k">等過數</span><span class="v">' + figs[1] + '</span></div>' +
    (open.length ? '<button class="tot queue" data-credits="1"><span class="k">入數未對 ' + open.length +
      ' 筆</span><span class="v">' + figs[2] + '</span></button>' : '');
}

function renderTabs() {
  byId('settle-tabs').innerHTML = PLATFORMS.map(p => {
    const n = data.counts[p.key] || 0;
    const on = curPlat === p.key;
    return '<button class="tab' + (on ? ' on' : (n ? '' : ' zero')) + '" data-f="' + p.key + '">' +
      esc(p.label) + '<span class="n">' + n + '</span></button>';
  }).join('');
}

// How many character cells a figure takes once its punctuation is pulled in:
// tight() takes about half a cell off each mark it wraps (.34em of a .65em
// cell).
function cellsOf(html) {
  const text = html.replace(/<[^>]*>/g, '');
  return (text.length - 0.52 * (html.match(/class="p"/g) || []).length).toFixed(2);
}

function cellHtml(dateStr) {
  const day = +dateStr.slice(8);
  // The 1st carries its month, because a week row is not a month and there is
  // no heading above it to read the month off.
  const num = day === 1 ? mdSlash(dateStr) : String(day);
  const info = dayInfo(dateStr);
  const today = dateStr === TODAY ? ' today' : '';
  // Only an empty day is inert; every day holding work opens, past or future.
  if (!info.n) return '<button class="cell none' + today + '" disabled><span class="d">' + tight(num) + '</span></button>';
  const future = dateStr > TODAY;
  const cls = future ? 'future' : (info.loose > 0 ? 'unsettled' : 'done');
  const amt = future ? info.total : (info.loose > 0 ? info.loose : info.total);
  const fig = tight(money(amt));
  return '<button class="cell' + today + '" data-d="' + dateStr + '">' +
    '<span class="d">' + tight(num) + '</span><span class="amt ' + cls + '" style="--n:' + cellsOf(fig) + '">' +
    fig + '</span></button>';
}

// Every week of the loaded run, end to end, with no month break in it: from the
// Sunday of the week holding the 1st of the earliest month to the Saturday of
// the week holding the last day of the latest. Every cell is a real date, so a
// week row can span two months and a bar across the boundary stays one bar. The
// edge weeks reach into months not loaded yet; those days draw as empty and
// fill in when their month arrives.
function stripWeeks() {
  const keys = loadedMonths();
  if (!keys.length) return [];
  const last = monthEnd(keys[keys.length - 1]);
  const end = addDays(last, 6 - dow(last));
  const weeks = [];
  const first = keys[0] + '-01';
  for (let s = addDays(first, -dow(first)); s <= end; s = addDays(s, 7)) {
    const w = [];
    for (let i = 0; i < 7; i++) w.push(addDays(s, i));
    weeks.push(w);
  }
  return weeks;
}

// Which month a week row belongs to, for the header label and for the scroll
// targets. The week holding the 1st is the first row of the new month: it is
// the row the operator scrolls to for that month, and everything under it is
// that month, whichever side of the boundary most of its own seven days sit.
function weekMonth(week) { return monthKey(week[6]); }
// A week row is addressed by the Sunday it starts on, both ways round: the
// scroll code has to find a row again across a repaint that rebuilt every one
// of them, and has to read a row's dates back off the row it found.
function weekId(sunday) { return 'wk' + sunday; }
function idWeek(id) { return id.slice(2); }
// The row a given day is drawn on.
function weekIdOf(dateStr) { return weekId(addDays(dateStr, -dow(dateStr))); }
// The Sunday of the week holding the 1st, which is that month's first row.
function monthWeekId(key) { return weekIdOf(key + '-01'); }

// Each run of a batch is laid out on its own and cut again at the week
// boundary, so a segment is the unit both the grid and the lane packer see.
// Batches come before credits so a bar is never pushed under a chip.
function weekEvents(week) {
  const idx = new Map();
  week.forEach((d, i) => idx.set(d, i));
  const out = [];
  for (const b of data.settlements) {
    const { runs, main, legDays } = spanRuns(b);
    const point = main[0];
    runs.forEach(run => {
      const at = run.filter(d => idx.has(d)).map(d => idx.get(d));
      if (!at.length) return;
      const start = Math.min(...at), end = Math.max(...at);
      const makeup = run !== main;
      // The label rides the run's first segment only, so a wrapped bar is
      // not read as two batches of the same amount.
      const head = idx.get(run[0]) === start;
      // A day with only a 舉牌 paid ahead has no leg the pointer could stand
      // for, so it names the money it put into the batch.
      const ahead = makeup && !run.some(d => legDays.has(d)) ? '$' + $(aheadIn(b, run)) : '';
      out.push({
        type: 'bar', start, cols: end - start + 1, batch: b,
        makeup, point, from: run[0], head,
        label: !head ? '' : makeup ? ahead + makeupLabel(point, run[0]) : barLabel(b),
        // Squared off whenever the run carries on past this segment, so the
        // pieces read as one bar wrapping. The strip is continuous, so the only
        // thing that cuts a bar is the week wrap -- a batch straddling a month
        // boundary is drawn whole.
        cutR: run[run.length - 1] > week[end],
        cutL: run[0] < week[start],
      });
    });
  }
  for (const c of ledger.credits) {
    if (!idx.has(c.value_date)) continue;
    out.push({ type: 'chip', start: idx.get(c.value_date), cols: 1, credit: c,
               label: '入$' + $(c.amount) });
  }
  return out;
}

// ---- label geometry ----
// A calendar label is a reconciliation figure, so it is never shortened to fit
// the column: the layout yields to the number instead. Every event therefore
// carries a reach -- the columns its label needs, never fewer than the columns
// its days occupy -- and that has to be measured rather than guessed. The
// desktop rule changes both the font size and the column width, so the metrics
// are read off the live grid on every paint.
//
// A label is measured as it will be drawn: a hidden element of the mark's own
// class holding the label's own markup, laid out by the browser. The label is
// set in the figure face with its punctuation pulled in and falls back to the
// system face for Chinese, and no arithmetic on a canvas metric reproduces
// all of that to the pixel; the element does by construction. Widths are kept
// per class, type and label, and thrown away when a face finishes loading,
// since a label measured before the figure face arrived was measured in its
// stand-in.
const KINDS = { bar: 'bar', makeup: 'bar makeup', chip: 'cchip' };
let labelW = new Map();
let calProbe = null;
function probe(grid, cls) {
  if (!calProbe) {
    calProbe = document.createElement('span');
    calProbe.style.cssText = 'position:absolute;visibility:hidden;left:0;top:0;width:max-content;' +
      'max-width:none;container-type:normal';
    calProbe.setAttribute('aria-hidden', 'true');
  }
  if (calProbe.parentNode !== grid.parentNode) grid.parentNode.appendChild(calProbe);
  calProbe.className = cls;
  return calProbe;
}
function calMetrics(grid) {
  // Per kind of mark: the type it is set in, which keys the kept widths (the
  // desktop rule sets another size), and the padding and border its label
  // sits inside, read off the stylesheet.
  const kinds = {};
  for (const k in KINDS) {
    const cs = getComputedStyle(probe(grid, KINDS[k]));
    kinds[k] = {
      key: k + '|' + cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily + '|',
      pad: parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) +
           parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth),
    };
  }
  // The grid divides its width the way the CSS does: seven columns and the six
  // 4px gaps between them. A hidden page measures zero, and zero would reserve
  // every label the whole week, so it falls back to the phone-width grid (390
  // less the two 16px gutters).
  return { colW: ((grid.clientWidth || 358) - 6 * 4) / 7, kinds, grid };
}
function kindOf(e) { return e.type === 'chip' ? 'chip' : e.makeup ? 'makeup' : 'bar'; }
function labelHtml(e) { return '<span class="lb">' + tight(esc(e.label)) + '</span>'; }
// The width of an event's label as drawn, without the mark's padding.
function labelWidth(e, m) {
  const kind = m.kinds[kindOf(e)];
  const key = kind.key + e.label;
  if (!labelW.has(key)) {
    const el = probe(m.grid, KINDS[kindOf(e)]);
    el.innerHTML = labelHtml(e);
    labelW.set(key, el.firstChild.getBoundingClientRect().width);
    el.innerHTML = '';
  }
  return labelW.get(key);
}
// n columns are n column widths plus the n - 1 gaps they span.
function spanW(n, m) { return n * m.colW + (n - 1) * 4; }
function reachOf(e, m) {
  e.lw = labelWidth(e, m);
  let w = e.lw + m.kinds[kindOf(e)].pad;
  // A pointer's outline stands on its own days and cannot be widened to hold
  // a longer label, and a label printed across a dashed edge reads as
  // neither. One that does not fit inside is written beside the outline, so
  // the reservation is the outline, the gap and the label.
  e.out = !!e.makeup && w > spanW(e.cols, m) + 0.5;
  if (e.out) w = spanW(e.cols, m) + 4 + e.lw;
  let n = e.cols;
  while (n < 7 && spanW(n, m) < w + 1) n++;
  return n;
}
// The packer collides on the reservation, not on the days: two labels that
// would print over each other belong on different lanes even when their bars
// do not touch. A reservation that would run off the last column is anchored
// to the right edge instead, and its label leaves the fill leftwards.
function reserve(events, m) {
  for (const e of events) {
    e.reach = e.label ? reachOf(e, m) : e.cols;
    e.spillL = e.start + e.reach > 7;
    e.rs = e.spillL ? Math.max(0, 7 - e.reach) : e.start;
  }
}
// A face that finishes loading can change what every label measures, so the
// kept widths go. A strip on screen is laid out again only if a label is no
// longer drawn at the width it was reserved at: the stand-in face is matched
// to the figure face's metrics, so as a rule nothing has moved, and a paint
// that changes nothing is still one more paint for the scroll anchor to
// carry the strip through while it may be growing.
function fontsChanged() {
  labelW = new Map();
  if (!showing || !months.size) return;
  for (const el of byId('grid').querySelectorAll('[data-lw]')) {
    if (Math.abs(el.querySelector('.lb').getBoundingClientRect().width - el.dataset.lw) > 0.5) {
      renderCalendar();
      return;
    }
  }
}

// A 補結 segment carries no amount: it points at the run that does.
function makeupLabel(point, from) {
  return '→' + (point.slice(0, 7) === from.slice(0, 7) ? (+point.slice(8)) + '日' : mdSlash(point));
}

function eventHtml(e, lane) {
  // Both axes are explicit: grid auto-placement is sparse and never moves
  // backwards, so an item whose column precedes its predecessor's would be
  // pushed to a new row and the lane would silently stop being one row.
  const place = 'grid-column:' + (e.rs + 1) + '/span ' + e.reach + ';grid-row:' + (lane + 1);
  // The width the label was reserved at, for whoever checks the layout
  // against what was drawn.
  const lw = e.label ? ' data-lw="' + e.lw.toFixed(2) + '"' : '';
  if (e.type === 'bar') {
    const b = e.batch;
    const cls = ['bar', b.state];
    if (e.makeup) cls.push('makeup');
    if (e.cutR) cls.push('cut-r');
    if (e.cutL) cls.push('cut-l');
    if (e.spillL) cls.push('spill-l');
    if (e.out) cls.push('out');
    // Repeating the lane's track inside the reservation puts the fill's edges
    // on the same column edges the cells above use.
    const at = e.spillL ? e.reach - e.cols + 1 : 1;
    // A part-paid batch draws what has arrived against what is owed: the paid
    // fraction on the left in the paid colour, the rest in the owed one. Not on
    // a 補結 segment, which carries no fill at all.
    let fill = '';
    if (b.state === 'partial' && !e.makeup && b.confirmed_amount > 0) {
      const pct = Math.max(0, Math.min(100, (b.received / b.confirmed_amount) * 100)).toFixed(2);
      fill = ';background:linear-gradient(90deg,var(--green) 0 ' + pct + '%,var(--amber) ' +
        pct + '% 100%)';
    }
    return '<span class="slot" style="' + place +
      ';grid-template-columns:repeat(' + e.reach + ',minmax(0,1fr))">' +
      '<button class="' + cls.join(' ') + '" style="grid-column:' + at + '/span ' + e.cols + fill +
      '" data-bar="' + b.id + '"' + lw + '>' + (e.label ? labelHtml(e) : '') + '</button></span>';
  }
  const c = e.credit;
  const cls = 'cchip' + chipState(c) + (e.spillL ? ' spill-l' : '');
  return '<button class="' + cls + '" style="' + place + '" data-chip="' + c.id + '"' + lw + '>' +
    labelHtml(e) + '</button>';
}

function renderCalendar() {
  const grid = byId('grid');
  const m = calMetrics(grid);
  const weeks = stripWeeks();
  const html = weeks.map((week, wi) => {
    const events = weekEvents(week);
    reserve(events, m);
    const lanes = packLanes(events);
    const start = wi > 0 && weekMonth(week) !== weekMonth(weeks[wi - 1]);
    return '<div class="wkblock' + (start ? ' mstart' : '') + '" id="' + weekId(week[0]) +
      '"><div class="grid">' + week.map(cellHtml).join('') + '</div>' +
      (lanes.length ? '<div class="lane">' +
        lanes.map((lane, li) => lane.map(e => eventHtml(e, li)).join('')).join('') + '</div>' : '') +
      '</div>';
  }).join('');
  const anchor = takeAnchor();
  grid.innerHTML = html;
  putAnchor(anchor);
  const mon = monthAtTop();
  if (mon) viewMonth = mon;
  // A month arriving under the scroll draws bars and days of a relation the
  // operator is already reading, so the paint ends by restating it.
  paintFocus();
  pokeEdges();
}

// ---- scrolling the strip ----
// Everything above the strip is sticky, so the top row of the strip sits flush
// against the bottom of the header. Flush rather than spaced: a gap would show
// the tail of the week above through it, and the header carries its own bottom
// padding as breathing room. This is one line, and the scroll target, the
// anchor and the header label all have to read it as the same line -- give any
// of them its own offset and a month scrolled to the top leaves the sliver of
// the week above it naming the header.
function stripTop() {
  return root.querySelector('.header').getBoundingClientRect().bottom;
}
function topRow() {
  // The row above the top one ends exactly on that line, and subpixel layout
  // drops its end a fraction either side, so the test carries a pixel of slack
  // or the row above would win.
  const edge = stripTop() + 1;
  for (const el of root.querySelectorAll('#grid .wkblock')) {
    if (el.getBoundingClientRect().bottom > edge) return el;
  }
  return null;
}
function monthAtTop() {
  const el = topRow();
  return el ? monthKey(addDays(idWeek(el.id), 6)) : null;
}
// Put a week row at the top of the strip.
function scrollToWeek(id, smooth) {
  const el = byId(id);
  if (!el) return;
  const y = window.scrollY + el.getBoundingClientRect().top - stripTop();
  window.scrollTo({ top: Math.max(0, y), behavior: smooth ? 'smooth' : 'auto' });
}
// Put a month's first week row at the top of the strip.
function scrollToMonth(key, smooth) { scrollToWeek(monthWeekId(key), smooth); }
// Put a week row at the top of a strip that may be too short to scroll that
// far yet: a strip just refounded holds one month, which is about a screen.
// Where the scroll falls short the row is pinned, so each paint that grows
// the strip asks for it again until it is there. Without the pin the first
// growth would pin the row then on top, which is the month's first, and the
// row asked for would never be reached.
function pinWeek(id) {
  scrollToWeek(id);
  const el = byId(id);
  if (el && Math.abs(el.getBoundingClientRect().top - stripTop()) >= 1) pinId = id;
}

// A repaint rebuilds every week row, and rows above the viewport change height
// whenever a month is prepended or a lane appears -- which would slide the
// strip out from under the operator. The fix is an anchor: where the top row
// sat before the paint, put it back after.
//
// A strip only a month or two long is barely taller than the screen, so the
// scroll that would hold it still runs out of document. What could not be
// scrolled is owed to the next paint, by which time the strip has grown. A
// pinned row states the same target every paint and simply asks again; an
// ordinary one has no target to restate, so it carries the shortfall.
let pinId = '';
let anchorDebt = 0;
function takeAnchor() {
  const edge = stripTop();
  // A pin left over from an earlier paint outranks whatever is on screen now,
  // because what is on screen is where the unfinished scroll left it.
  if (pinId && byId(pinId)) return { id: pinId, off: edge, pin: true };
  const el = topRow();
  if (!el) return null;
  const top = el.getBoundingClientRect().top;
  // A top row sitting below the strip's top line has page chrome above it --
  // the colour key at the head of the page, which only the untouched strip is
  // ever short enough to show. Growth at the top displaces that chrome, so
  // such a row is pinned to the line. Anywhere else the row is where the
  // operator scrolled it to, and goes back exactly there.
  if (top <= edge) return { id: el.id, off: top, pin: false };
  pinId = el.id;
  return { id: el.id, off: edge, pin: true };
}
function putAnchor(a) {
  const el = a && byId(a.id);
  if (!el) { anchorDebt = 0; pinId = ''; return; }
  const want = el.getBoundingClientRect().top - a.off + (a.pin ? 0 : anchorDebt);
  const from = window.scrollY;
  if (want) window.scrollBy(0, want);
  const left = want - (window.scrollY - from);
  if (!a.pin) { anchorDebt = left; return; }
  anchorDebt = 0;
  if (Math.abs(left) < 1) pinId = '';
}

// The strip grows by being scrolled rather than by a button, so its two ends
// are watched: a sentinel entering the viewport is the ask for the month past
// that end. The margin fires it early, so the month is there before the edge is
// reached, and one entry loads one month.
let edgeIO = null;
function watchEdges() {
  edgeIO = new IntersectionObserver(entries => {
    // A hidden strip has no edges to approach.
    if (!showing) return;
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const keys = loadedMonths();
      if (!keys.length) continue;
      ensureMonth(e.target.id === 'sentTop'
        ? addMonths(keys[0], -1) : addMonths(keys[keys.length - 1], 1));
    }
  }, { rootMargin: '150px 0px' });
  pokeEdges();
}
// An observer only speaks when a sentinel crosses the edge of the viewport, and
// a sentinel that was already showing when the strip grew under it never does.
// Re-observing after a paint makes it restate where it is, which is what
// terminates the growth: a month prepended pushes the top sentinel out of
// range, and until it is out of range the strip is still too short.
function pokeEdges() {
  if (!edgeIO) return;
  edgeIO.disconnect();
  edgeIO.observe(byId('sentTop'));
  edgeIO.observe(byId('sentBot'));
}

// ---- focus ----
// A credit chip sits on the day the bank paid, the batch it paid for sits on
// the days that were driven, and nothing on the strip says the two are the same
// money. Focus is that statement: one end is named, and everything not part of
// the relation recedes.
//
// The relation is read off the ledger rather than off the loaded months,
// because that is the only place an end outside them appears: a credit carries
// every batch it went into, and each of those carries its own days.
//
// It is the whole connected run of allocations, not one hop out from the end
// named. Two credits paying one batch are one money statement, and a statement
// has to read the same from every end of it -- entering at either chip or at
// the bar must light the identical set, or the operator is being told the sum
// depends on where he looked. Closing over the edges buys that invariance at
// the cost of a long chain lighting entirely, which is what a chain of money
// crossing over itself is.

// A batch's days wherever the page holds them: the ledger carries them for
// every batch some credit paid, whatever the strip has loaded; the book carries
// them for a batch no credit has been put against yet.
function batchDatesOf(id) {
  // A loaded batch knows the days of the 舉牌 it paid ahead as well.
  const own = batchById(id);
  if (own) return batchSpan(own);
  for (const c of ledger.credits) {
    for (const b of c.batches) if (b.id === id) return b.dates;
  }
  return [];
}

function focusSets() {
  const s = { batches: new Set(), credits: new Set(), dates: new Set() };
  if (!focus) return s;
  // Every allocation in both directions, so the walk crosses the graph the same
  // way from either side of an edge.
  const edges = { batch: new Map(), credit: new Map() };
  const link = (m, from, to) => { if (!m.has(from)) m.set(from, []); m.get(from).push(to); };
  for (const c of ledger.credits) {
    for (const b of c.batches) { link(edges.credit, c.id, b.id); link(edges.batch, b.id, c.id); }
  }
  const seen = { batch: s.batches, credit: s.credits };
  const walk = [focus];
  seen[focus.kind].add(focus.id);
  for (let i = 0; i < walk.length; i++) {
    const n = walk[i];
    if (n.kind === 'batch') for (const d of batchDatesOf(n.id)) s.dates.add(d);
    const other = n.kind === 'batch' ? 'credit' : 'batch';
    for (const id of edges[n.kind].get(n.id) || []) {
      if (seen[other].has(id)) continue;
      seen[other].add(id);
      walk.push({ kind: other, id });
    }
  }
  return s;
}

// Lit and dim are set on what the paint has already laid out rather than woven
// into the markup: a focus is taken and dropped by a pointer moving across the
// strip, and rebuilding every week row for that would fight the scroll anchor.
// One place decides, so a month painted later and a hover both read the same.
function paintFocus() {
  const s = focus ? focusSets() : null;
  const grid = byId('grid');
  // The two ends of the same statement, so they are set together: with no
  // focus down neither is set and the strip is back to its own strengths.
  const mark = (el, held) => {
    el.classList.toggle('lit', !!s && held);
    el.classList.toggle('dim', !!s && !held);
  };
  grid.querySelectorAll('[data-bar]').forEach(el =>
    mark(el, s && s.batches.has(+el.dataset.bar)));
  grid.querySelectorAll('[data-chip]').forEach(el =>
    mark(el, s && s.credits.has(+el.dataset.chip)));
  // An empty day already reads as a calendar coordinate rather than money, so
  // it is left alone: dimming it again would only separate it from the other
  // empty days.
  grid.querySelectorAll('.cell[data-d]').forEach(el =>
    mark(el, s && s.dates.has(el.dataset.d)));
}

function focusTarget(el) {
  const bar = el.closest('[data-bar]');
  if (bar) return { kind: 'batch', id: +bar.dataset.bar };
  const chip = el.closest('[data-chip]');
  if (chip) return { kind: 'credit', id: +chip.dataset.chip };
  return null;
}
function isFocus(t) { return !!focus && focus.kind === t.kind && focus.id === t.id; }
function setFocus(t) {
  if (isFocus(t)) return;
  focus = t;
  paintFocus();
}
function clearFocus() {
  if (!focus) return;
  focus = null;
  paintFocus();
}
// A focus is a claim that something exists. A batch undone or a credit archived
// elsewhere leaves the claim pointing at nothing, and a paint off it would dim
// the whole strip against a relation that is gone.
function dropStaleFocus() {
  if (!focus) return;
  const alive = focus.kind === 'credit'
    ? !!creditById(focus.id)
    : !!batchById(focus.id) || ledger.credits.some(c => c.batches.some(b => b.id === focus.id));
  if (!alive) focus = null;
}

// The far end of the relation: everything it lit bar the end the operator
// named, since that end is where he already is and cannot be what tells him the
// strip is worth moving. Naming a batch names the days its own bar runs across
// too -- they came into view with it. Everything else in the relation counts,
// a second credit on the same statement included: it answers the question the
// focus asked as well as the batch does.
function nearDates() {
  return new Set(focus.kind === 'batch' ? batchDatesOf(focus.id) : []);
}
// Read back off the paint, which has already run, so the two agree by
// construction on what is in the relation.
function farNodes() {
  const grid = byId('grid');
  const own = nearDates();
  const named = el => focus.kind === 'batch'
    ? +el.dataset.bar === focus.id || own.has(el.dataset.d)
    : +el.dataset.chip === focus.id;
  return [...grid.querySelectorAll('[data-bar]:not(.dim), [data-chip]:not(.dim), ' +
    '.cell[data-d]:not(.dim)')].filter(el => !named(el));
}
// Where that far end sits on the strip. A credit is only ever on its value
// date, a batch on each of its days, so both kinds of member offer the scroll a
// row and it can take whichever is nearest.
function farDates(s) {
  const own = nearDates();
  const dates = [...s.dates].filter(d => !own.has(d));
  for (const c of ledger.credits) {
    if (s.credits.has(c.id) && !(focus.kind === 'credit' && c.id === focus.id)) {
      dates.push(c.value_date);
    }
  }
  return dates;
}
function onScreen(el) {
  const r = el.getBoundingClientRect();
  // Everything above the strip's top line is behind the sticky header.
  return r.bottom > stripTop() && r.top < window.innerHeight;
}

// Nothing of the far end on screen means the relation was stated and cannot be
// seen, so the strip goes to the nearest row carrying it. Only then: a strip
// already showing the answer must not be moved out from under the operator, and
// a credit nothing has been allocated to has no far end to go to at all.
//
// Asked for by the tap that names a relation, never by a pointer resting on
// one: scrolling under a stationary pointer takes the element out from under it
// and cancels the very focus that asked for the scroll.
async function revealFocus() {
  const want = focus;
  const s = focusSets();
  const dates = farDates(s);
  if (!dates.length) return;
  for (const el of farNodes()) if (onScreen(el)) return;
  const rows = [...new Set(dates.map(weekIdOf))]
    .map(id => byId(id)).filter(Boolean);
  if (rows.length) {
    const edge = stripTop();
    rows.sort((a, b) => Math.abs(a.getBoundingClientRect().top - edge) -
                        Math.abs(b.getBoundingClientRect().top - edge));
    scrollToWeek(rows[0].id, true);
    return;
  }
  // Every far day is outside the loaded strip, so the month holding the nearest
  // one has to be brought in before there is a row to scroll to.
  const mid = new Date(viewMonth + '-15T00:00:00').getTime();
  const near = d => Math.abs(new Date(d + 'T00:00:00').getTime() - mid);
  const d = dates.slice().sort((a, b) => near(a) - near(b))[0];
  if (!await ensureMonth(monthKey(d))) return;
  // The month arrives asynchronously, by which time the operator can have
  // dropped the focus or moved to another one.
  if (focus !== want) return;
  pinWeek(weekIdOf(d));
}

// ---- sheet ----
// Sheets stack instead of replacing each other: a batch reached from a day has
// to hand the operator back to that day, not to the calendar.
let views = [];
function curView() { return views[views.length - 1] || null; }
function viewHtml(v) {
  if (v.kind === 'day') return dayViewHtml(v);
  if (v.kind === 'undo') return undoViewHtml(v);
  if (v.kind === 'unlink') return unlinkViewHtml(v);
  if (v.kind === 'credit') return creditViewHtml(v);
  if (v.kind === 'order') return orderViewHtml(v);
  if (v.kind === 'queue') return queueViewHtml();
  if (v.kind === 'stmt') return stmtViewHtml(v);
  return batchViewHtml(v);
}
// keepScroll is for a repaint the operator asked for from inside the sheet
// (unfolding a list): the sheet is where he was reading, so it must not jump
// back to the top under him.
function paintView(keepScroll) {
  // Not while hidden, when the order sheet would be drawn through the other
  // view's host. The stack is kept, and the load on show paints its top.
  if (!showing) return;
  const v = curView();
  if (!v) return;
  const el = byId('settle-sheet');
  const top = keepScroll ? el.scrollTop : 0;
  // The order's own sheet and the views it stacks (its numpad, its cancel
  // confirm) are order-sheet.js, which fills the element itself.
  const draw = v.kind === 'fn' ? v.render : v.kind === 'order' && v.order ? detailView : null;
  el.classList.remove('np-sheet');
  if (draw) {
    el.innerHTML = '<div class="grab"></div>';
    draw(el);
  } else {
    el.innerHTML = '<div class="grab"></div>' + viewHtml(v);
  }
  el.scrollTop = top;
  el.classList.add('show');
  byId('settle-scrim').classList.add('show');
}
function openView(v) { views = [v]; paintView(); }
function pushView(v) { views.push(v); paintView(); }
function popView() {
  views.pop();
  if (views.length) paintView(); else closeSheet();
}
function closeSheet() {
  views = [];
  byId('settle-sheet').classList.remove('show');
  byId('settle-scrim').classList.remove('show');
}
function sheetHead(title, sub) {
  return '<div class="sheet-head">' +
    (views.length > 1 ? '<button class="sheet-back" data-back="1" aria-label="返上一層">&#8249;</button>' : '') +
    '<div class="sheet-title">' + title + '</div>' +
    '<button class="sheet-x" data-close="1">&#10005;</button></div>' +
    (sub ? '<div class="sheet-sub">' + sub + '</div>' : '');
}

// One row shape for every sheet. The mode decides what the right-hand end
// says: 'day' states where the leg's money has got to, 'batch' shows the
// platform's own figure beside the system's. A tick option turns the batch row
// into the operator's answer to which leg the platform has not paid; without
// it the row is a read, since a batch is created from a statement image in the
// bot, never from here.
function orderRowHtml(o, mode, opts) {
  const tick = opts && opts.tick;
  let right;
  if (mode === 'day') {
    const tag = dayTag(o);
    const amt = owedOf(o);
    right = '<span class="oend">' + (amt ? '<span class="oa">' + money(amt) + '</span>' : '') +
      '<span class="otag ' + tag[1] + '">' + esc(tag[0]) + '</span>' + aheadTag(o) + '</span>';
  } else if (tick) {
    // The figure to tick against is the platform's, because the outstanding
    // amount the ticks have to add up to is the platform's own arithmetic.
    right = '<span class="oend"><span class="oa">$' + $(o.platform_amount) + '</span>' +
      (opts.ticked ? '<span class="otag unsettled">未過數</span>' : '') + '</span>';
  } else {
    const plat = opts && opts.plat;
    const mark = !o.price ? '未入價' : (o.scheduled_time >= NOW ? '未完成' : '');
    // In batch detail the platform's own figure is shown when it disagrees;
    // an equal figure would only repeat the number.
    const platNote = plat !== undefined && Math.abs(plat - owedOf(o)) >= 0.005
      ? ' · <span class="oplat">平台 $' + $(plat) + '</span>' : '';
    // The fine is why the leg is worth less than its fare; both figures are
    // already net, so this only names the deduction.
    const penNote = (o.penalty_fee || 0) > 0
      ? ' · <span class="ofine">判罰 ' + money(-o.penalty_fee) + '</span>' : '';
    // Likewise the 舉牌 another batch carries: the leg is owed the rest here.
    const aheadNote = (o.paid_ahead || 0) > 0
      ? ' · <span class="oahead">舉牌 ' + money(o.paid_ahead) + ' 先結</span>' : '';
    right = mark ? '<span class="omark">' + mark + '</span>'
                 : '<span class="oa">' + money(owedOf(o)) + penNote + aheadNote + platNote + '</span>';
    // A leg the platform held back says when its money finally came, so the
    // list keeps the record the flags were kept for.
    if (opts && opts.madeUp) right = '<span class="omark paid">補收 ' + esc(opts.madeUp) +
      '</span>' + right;
  }
  const chk = tick
    ? '<span class="up-chk' + (opts.ticked ? ' on' : '') + '">' + (opts.ticked ? '☑' : '☐') + '</span>' : '';
  // What the row itself is: the tick answer in a short-paid batch, the way
  // into the order in the day sheet, and nothing at all in a batch read.
  const rowAttrs = tick ? ' tick" data-uptick="' + esc(o.order_id) + '"'
    : mode === 'day' ? ' tap" data-od="' + esc(o.order_id) + '"'
    : '"';
  // A copy target inside a row that is itself the target would swallow the
  // tap, so in the day sheet the copy lives in the sheet the row opens.
  const idAttrs = mode === 'day' ? '' : ' data-copy="' + esc(o.order_id) + '"';
  return '<div class="orow' + rowAttrs + '>' + chk +
    '<span class="ot">' + esc(orderTime(o)) + '</span>' +
    '<span class="ol">' +
      '<span class="oid"' + idAttrs + '>' + esc(groupId(o.order_id)) + '</span>' +
      '<span class="oll">' + esc(orderLabel(o)) + '</span></span>' +
    right + '</div>';
}

// Grouped by day whenever the batch holds more than one, because each day is a
// page of the statement the batch was read from. A tick set turns every row
// into a tick row.
function orderListHtml(b, rows, tickSet) {
  const plat = new Map();
  (b.statement ? b.statement.days.flatMap(d => d.rows) : []).forEach(r =>
    plat.set(r.order_id, (plat.get(r.order_id) || 0) + r.amount));
  // Which allocation paid a held-back leg is the last one, the one that made
  // the batch whole; a batch still short has not paid them at all.
  const madeUp = b.state === 'paid' && b.allocations.length
    ? mdSlash(b.allocations[b.allocations.length - 1].value_date) : '';
  const row = o => orderRowHtml(o, 'batch', tickSet
    ? { tick: true, ticked: tickSet.has(o.order_id) }
    : { plat: plat.get(o.order_id), madeUp: o.unpaid ? madeUp : '' });
  const dates = batchDates(b);
  if (dates.length === 1) return rows.map(row).join('');
  return dates.map(d => '<div class="oday">' + esc(mdLabel(d)) + ' 星期' + weekday(d) + '</div>' +
    rows.filter(o => orderDate(o) === d).map(row).join('')).join('');
}

// ---- day sheet ----
// A read of the day: what it earned, where each leg's money has got to, and
// the batches it belongs to. Nothing is written from here.
// A 舉牌 paid ahead of a held-back trip has already arrived on another batch,
// so the leg's figure is the rest and this line names the part that came.
function aheadTag(o) {
  return (o.paid_ahead || 0) > 0
    ? '<span class="otag paid">舉牌 ' + money(o.paid_ahead) + ' 先結</span>' : '';
}
function dayTag(o) {
  const b = batchOf(o.order_id);
  if (b) {
    // The flags outlive the payment: they name the legs the platform held back
    // on this statement, so in a batch that has since been collected they say
    // which legs the make-up payment paid for and which the first transfer did.
    if (b.state === 'paid') {
      const at = b.allocations;
      if (!at.length) return [paidLabel(b), 'paid'];
      if (o.unpaid) return ['補收 ' + mdSlash(at[at.length - 1].value_date), 'paid'];
      return ['已收 ' + mdSlash(at[0].value_date), 'paid'];
    }
    // The tag states where this leg's money has got to, not the batch's: a
    // short-paid batch is short one leg, and the outstanding figure on every
    // leg of it would read as each leg being short that much. Once the ticks
    // name the short leg, every other leg has been paid.
    if (b.state === 'partial') {
      if (o.unpaid) return ['未過數', 'unsettled'];
      if (b.orders.some(x => x.unpaid)) {
        return ['已收 ' + mdSlash(b.allocations[b.allocations.length - 1].value_date), 'paid'];
      }
      return ['批次差 $' + $(b.outstanding), 'awaiting'];
    }
    return [batchTag(b), 'awaiting'];
  }
  if (isSettleable(o)) return ['未結算', 'unsettled'];
  return [o.price ? '未完成' : '未入價', 'mute'];
}
function batchesOn(dateStr) {
  const seen = new Set();
  const out = [];
  const add = b => { if (b && !seen.has(b.id)) { seen.add(b.id); out.push(b); } };
  for (const o of ordersOn(dateStr)) add(batchOf(o.order_id));
  for (const b of data.settlements) if (aheadLines(b).some(a => a.date === dateStr)) add(b);
  return out;
}
function openDay(dateStr) {
  openView({ kind: 'day', date: dateStr });
}

function dayViewHtml(v) {
  const batches = batchesOn(v.date);
  return sheetHead(esc(mdLabel(v.date)) + ' 星期' + weekday(v.date), esc(platLabel(curPlat))) +
    ordersOn(v.date).map(o => orderRowHtml(o, 'day')).join('') +
    (batches.length ? '<div class="blinks">' + batches.map(b =>
      '<button class="blink" data-bl="' + b.id + '"><span class="blink-t">批次 ' +
      esc(batchLabel(b)) + ' · ' + b.orders.length + ' 程 · $' + $(b.confirmed_amount) +
      ' · ' + batchTag(b) + (heldLabel(b) ? ' · ' + esc(heldLabel(b)) : '') + '</span>' +
      '<span class="blink-c">&rsaquo;</span></button>').join('') + '</div>' : '');
}

// ---- order sheet ----
// The same sheet the day view opens (order-sheet.js), so an order found
// while settling is corrected where it was found.  The month payload carries
// settle columns only, so the order itself is fetched per open and kept on
// the view; the fetch is what the sheet waits on.
async function openOrder(orderId) {
  useOrderHost(orderHost);
  const v = { kind: 'order', id: orderId, order: null, err: '' };
  pushView(v);
  await fetchOrder(v);
  // The operator can have moved on while the fetch was in flight.
  if (curView() === v) paintView();
}
async function fetchOrder(v) {
  try {
    const res = await apiFetch('/api/orders/' + encodeURIComponent(v.id));
    const body = await res.json().catch(() => ({}));
    if (res.ok) { v.order = body; v.err = ''; }
    else { v.order = null; v.err = body.error || ('HTTP ' + res.status); }
  } catch (e) {
    v.err = '讀唔到';
  }
}
function orderView() {
  for (let i = views.length - 1; i >= 0; i--) if (views[i].kind === 'order') return views[i];
  return null;
}
// Drawn only until the order has arrived, or when it could not be read.
function orderViewHtml(v) {
  return sheetHead('單 ' + esc(tailId(v.id)), '') +
    (v.err ? '<div class="order-err">' + esc(v.err) + '</div>' : '<div class="empty">讀緊…</div>');
}
// The settle columns the order endpoint does not carry live on the loaded
// rows, the same place batchOf reads the leg's batch from.
function settleRowOf(id) {
  return data.orders.find(o => o.order_id === id) ||
    data.settlements.flatMap(b => b.orders).find(o => o.order_id === id) || null;
}
function batchLinkHtml(b, lead) {
  return '<button class="info-link" data-bl="' + b.id + '">' + lead + '批次 ' +
    esc(batchLabel(b)) + ' · ' + esc(batchTag(b)) + ' &rsaquo;</button>';
}

const orderHost = {
  order() { const v = orderView(); return v ? v.order : null; },
  push(render) { pushView({ kind: 'fn', render }); },
  async patch(body) {
    const v = orderView();
    try {
      await apiWrite('PATCH', '/api/orders/' + encodeURIComponent(v.id), body);
    } catch (e) {
      if (!(e instanceof AuthExpired)) toast(e.message);
      return false;
    }
    // Back to the order before the reload redraws it, so the numpad is not
    // drawn once more with the old figure.
    if (curView().kind === 'fn') views.pop();
    await load();
    return true;
  },
  cancelled(id) {
    // Out of the order and everything stacked on it, back to where it was
    // opened from, whose rows no longer hold it.
    const v = orderView();
    while (views.length && curView() !== v) views.pop();
    views.pop();
    toast('已取消 #' + shortId(id));
    if (!views.length) closeSheet();
    load();
  },
  gone() {
    // The order could not be read again (cancelled elsewhere, say): back to
    // its own view, which says so.
    const v = orderView();
    while (views.length && curView() !== v) views.pop();
    paintView();
  },
  head: sheetHead,
  pop: popView,
  subtitle: o => '#' + esc(shortId(o.order_id)) + ' · ' + esc(mdLabel(orderDate(o))) + ' 星期' +
    weekday(orderDate(o)),
  // The whole number, to copy: statements and the platform name a leg by it.
  rowsBefore: o => [['單號', '<span class="oid" data-copy="' + esc(o.order_id) + '">' +
    esc(groupId(o.order_id)) + '</span>']],
  rowsAfter(o) {
    const rows = [];
    const fees = (o.banner_fee || 0) + (o.tunnel_fee || 0) + (o.penalty_fee || 0);
    if (fees && !(o.penalty_fee > 0)) rows.push(['淨收', money(expectedOf(o))]);
    const b = batchOf(o.order_id);
    rows.push(['結算', b ? batchLinkHtml(b, '') + (o.unpaid ? ' · 未過數' : '') : esc(dayTag(o)[0])]);
    // The other place part of this leg's money went: a 舉牌 paid ahead of a
    // trip the platform held back sits on the batch it arrived with.
    const s = settleRowOf(o.order_id);
    if (s && s.paid_ahead > 0) {
      const ahead = batchById(s.ahead_batch);
      rows.push(['舉牌', ahead ? batchLinkHtml(ahead, money(s.paid_ahead) + ' 先結 · ')
                               : money(s.paid_ahead) + ' 先結 · 批次 #' + esc(s.ahead_batch)]);
    }
    return rows;
  },
};

// ---- batch sheet ----
// Read money first: the confirmed figure and where its payment has got to are
// what the sheet is opened for. The order numbers are only read when tracing a
// problem, so they fold away -- except in a short-paid batch, where the same
// list is where the operator names the leg that was not paid.
function legLabel(o) {
  return (+orderDate(o).slice(8)) + '日 ' + orderTime(o) + ' ' + orderLabel(o);
}
// A guess of more than two legs is a sum, not a list: naming four legs in a
// chip is unreadable, while the count and the total are enough to choose by.
function guessLabel(ids, b) {
  const legs = ids.map(id => b.orders.find(o => o.order_id === id)).filter(Boolean);
  if (legs.length > 2) return legs.length + ' 程 $' + $(legs.reduce((sum, o) => sum + o.platform_amount, 0));
  return legs.map(legLabel).join(' + ');
}

// A credit offered against a batch that is short. The button says what a tap
// would actually do, the way the chat card's does: money that cannot cover the
// shortfall names what would still be owed after it.
function creditPropHtml(p, b) {
  const gap = round2(b.outstanding - p.remaining);
  const label = gap > 0.005 ? '對 $' + $(p.remaining) + '（差 $' + $(gap) + '）' : '對';
  const left = p.remaining < p.amount - 0.005 ? ' · 剩 $' + $(p.remaining) : '';
  return '<div class="prow"><span class="t">入數 ' + esc(mdSlash(p.value_date)) + ' · $' +
    $(p.amount) + left + '</span>' +
    (p.exact ? '<span class="ptag">啱數</span>' : '') +
    '<button class="pbtn" data-alloc-credit="' + p.id + '" data-alloc-batch="' + b.id +
    '">' + label + '</button></div>';
}

// The mirror: a batch offered against a credit that has money left.
function batchPropHtml(p, c) {
  const gap = round2(p.outstanding - c.remaining);
  const label = gap > 0.005 ? '對 $' + $(c.remaining) + '（差 $' + $(gap) + '）' : '對';
  // A batch of the group is exact only together with the others, and the
  // group row above already says so.
  const exact = p.exact && !(c.combo && c.combo.ids.includes(p.id));
  return '<div class="prow"><span class="t">批次 ' + esc(spanLabelOf(p.dates)) + ' · ' +
    p.orders + ' 程 · 差 $' + $(p.outstanding) + '</span>' +
    (exact ? '<span class="ptag">啱數</span>' : '') +
    '<button class="pbtn" data-alloc-credit="' + c.id + '" data-alloc-batch="' + p.id +
    '">' + label + '</button></div>';
}

// The lines the batch carries itself: money on the transfer that no leg of it
// could hold -- a 判罰 booked against a trip outside the batch, the 免責 line
// that cancels it, a 舉牌 paid ahead of a trip the platform held back. The
// platform's own line structure is kept, so a pair that nets to zero still
// reads as the pair it was.
function adjustmentsHtml(b) {
  const rows = b.adjustments || [];
  if (!rows.length) return '';
  const total = round2(rows.reduce((sum, a) => sum + a.amount, 0));
  return '<div class="prop-sec"><div class="prop-head">帳項 · ' + money(total) + '</div>' +
    rows.map(a => '<div class="sum-row"><span class="k">' + (a.ahead ? '舉牌先結 ' : '') +
      esc(tailId(a.order_ref)) + ' · ' + esc(mdSlash(a.date)) + '</span><span class="v">' +
      (a.amount < 0 ? '&minus;' : '+') + '$' + $(Math.abs(a.amount)) + '</span></div>').join('') +
    '</div>';
}

function batchViewHtml(v) {
  const b = batchById(v.id);
  if (!b) return '';
  const rows = b.orders.slice().sort((x, y) => x.scheduled_time.localeCompare(y.scheduled_time));
  const diff = b.confirmed_amount - b.expected_amount;
  // A batch is not paid or unpaid but owed a figure: what the bank has sent
  // and what it still owes are two different numbers until the last one lands.
  let state;
  if (b.state === 'paid') state = '<div class="hero-s paid">已收齊 · ' + mdSlash(b.paid_on) + '</div>';
  else if (b.state === 'partial') state = '<div class="hero-s warn">已收 $' + $(b.received) +
    '（' + mdSlash(b.allocations[b.allocations.length - 1].value_date) + '） · 差 $' + $(b.outstanding) + '</div>';
  else state = '<div class="hero-s">等過數</div>';

  const sums = ['<div class="sum-row"><span class="k">應收</span><span class="v">$' + $(b.expected_amount) + '</span></div>'];
  if (diff) sums.push('<div class="sum-row"><span class="k">差額</span><span class="v">' +
    (diff < 0 ? '&minus;' : '+') + '$' + $(Math.abs(diff)) + '</span></div>');
  // The flags outlive the payment, so a collected batch can still say which
  // legs each transfer covered: the first allocation paid the rest of the
  // statement, the one that made the batch whole paid the held-back legs.
  const heldBack = b.orders.filter(o => o.unpaid);
  b.allocations.forEach((a, i) => {
    sums.push('<div class="alloc">' +
      '<button class="sum-row link" data-credit="' + a.credit_id + '">' +
      '<span class="k">入數 ' + mdSlash(a.value_date) + '</span>' +
      '<span class="v">$' + $(a.amount) + '<span class="c">&rsaquo;</span></span></button>' +
      '<button class="xbtn" data-unlink-batch="' + b.id + '" data-unlink-credit="' +
      a.credit_id + '">解除</button></div>');
    if (b.state !== 'paid' || !heldBack.length) return;
    if (i === b.allocations.length - 1) {
      sums.push('<div class="sub-note mute">補 ' +
        heldBack.map(o => esc(tailId(o.order_id))).join(' · ') + '</div>');
    } else if (i === 0) {
      sums.push('<div class="sub-note mute">其餘 ' + (b.orders.length - heldBack.length) + ' 程</div>');
    }
  });
  if (b.statement) {
    // Platform amount per order id, 舉牌 lines folded in (same fold as reconcile()).
    const inBatch = new Set(b.orders.map(o => o.order_id));
    const plat = new Map();
    b.statement.days.flatMap(d => d.rows).forEach(r => plat.set(r.order_id, (plat.get(r.order_id) || 0) + r.amount));
    // A line the batch records is accounted for, so it comes off what is left
    // over: this row is the alarm for statement money nothing explains, and an
    // explained line showing up in it twice would be noise wearing a warning.
    (b.adjustments || []).forEach(a =>
      plat.set(a.order_ref, round2((plat.get(a.order_ref) || 0) - a.amount)));
    [...plat.keys()].filter(id => !inBatch.has(id) && Math.abs(plat.get(id)) >= 0.005).forEach(id =>
      sums.push('<div class="sum-row"><span class="k">平台多出 ' + esc(groupId(id)) +
        '</span><span class="v">$' + $(plat.get(id)) + '</span></div>'));
    if (b.statement_image) sums.push('<div class="sum-row"><span class="k">結算單</span><span class="v">' +
      '<a href="/api/settlements/' + b.id + '/image" target="_blank" rel="noopener">睇圖</a></span></div>');
  }

  let mid;
  if (b.state === 'partial') {
    // Pre-tick: existing marks win; else if exactly one guess, pre-tick that.
    const ticked = new Set(b.orders.filter(o => o.unpaid).map(o => o.order_id));
    const guesses = b.unpaid_guesses || [];
    if (!ticked.size && guesses.length === 1) guesses[0].forEach(id => ticked.add(id));
    let note;
    if (guesses.length === 1) note = '<div class="up-note">系統估：' + esc(guessLabel(guesses[0], b)) + '，啱差額，已剔</div>';
    else if (guesses.length > 1) note = '<div class="up-chips"><span class="up-note">系統估：</span>' +
      guesses.map((g, i) => '<button class="up-chip" data-upguess="' + i + '">' +
        esc(guessLabel(g, b)) + '</button>').join('') + '</div>';
    else note = '<div class="up-note">冇組合啱 $' + $(b.outstanding) + '，自己剔</div>';
    const tickedAmt = rows.reduce((sum, o) => ticked.has(o.order_id) ? sum + o.platform_amount : sum, 0);
    const match = Math.abs(tickedAmt - b.outstanding) < 0.005;
    // What could close the batch comes before what is missing from it: the
    // money is the answer, the ticks only say which legs it is for.
    const props = b.proposals || [];
    mid = '<div class="prop-sec"><div class="prop-head">等緊補數 · 差 $' + $(b.outstanding) +
      '</div>' + (props.length ? props.map(p => creditPropHtml(p, b)).join('')
                               : '<div class="up-note">未收到補數</div>') + '</div>' +
      '<div class="up-sec" data-upbatch="' + b.id + '">' +
      '<div class="up-head">邊張單未過？ · 差 $' + $(b.outstanding) + '</div>' + note +
      orderListHtml(b, rows, ticked) +
      '<div class="up-foot"><span class="up-sum' + (match ? '' : ' warn') + '">' +
      esc(tickFootText(tickedAmt, b)) + '</span>' +
      '<button class="up-btn" data-upsave="' + b.id + '"' + (match ? '' : ' disabled') +
      '>記低</button></div></div>';
  } else {
    const open = foldOpen.has(b.id);
    mid = '<button class="fold" data-fold="' + b.id + '"><span>' + b.orders.length + ' 程</span>' +
      '<span class="c">' + (open ? '&#9662;' : '&#9656;') + '</span></button>' +
      (open ? orderListHtml(b, rows, null) : '');
  }

  const held = heldLabel(b);
  return sheetHead('結算 ' + esc(batchLabel(b)),
      esc(platLabel(b.platform)) + ' · ' + b.orders.length + ' 程 · 結算日 ' + mdSlash(b.settled_on) +
      (held ? ' · ' + esc(held) : '')) +
    '<div class="hero"><div class="hero-k">平台確認</div>' +
    '<div class="hero-v">$' + $(b.confirmed_amount) + '</div>' + state + '</div>' +
    '<div class="sum-rows">' + sums.join('') + '</div>' +
    adjustmentsHtml(b) +
    mid +
    '<button class="ghost-btn danger" data-undo="' + b.id + '">撤銷結算</button>' +
    '<button class="ghost-btn" data-back="1">收埋</button>';
}

// ---- credit sheet ----
// One bank credit: how much arrived, who sent it, and which batch it was read
// against. The batch row hands the operator on to that batch, and says what it
// is still owed, because a credit covering a batch in full is not the same as
// one that left it short.
// One transfer pays a whole confirmation day, so the group the matcher found
// is offered as one row and one tap; its batches stay offered one by one below.
function comboLabel(c) {
  const members = c.combo.ids.map(id => (c.proposals || []).find(p => p.id === id)).filter(Boolean);
  return c.combo.ids.length + ' 個批次 · ' +
    members.map(p => spanLabelOf(p.dates)).join('、') + ' · $' + $(c.combo.total);
}
function comboBtnHtml(c, label) {
  return '<button class="pbtn" data-alloc-all="' + c.id + '" data-alloc-ids="' +
    c.combo.ids.join(',') + '">' + label + '</button>';
}
function comboPropHtml(c) {
  return '<div class="prow"><span class="t">' + esc(comboLabel(c)) + '</span>' +
    '<span class="ptag">啱數</span>' + comboBtnHtml(c, '對晒') + '</div>';
}
function creditViewHtml(v) {
  const c = creditById(v.id);
  if (!c) return '';
  let state, extra = '';
  if (c.state === 'done') state = '<div class="hero-s paid">已對</div>';
  else if (c.state === 'partial') state = '<div class="hero-s blue">已對 $' + $(c.allocated) +
    ' · 剩 $' + $(c.remaining) + '</div>';
  else if (c.state === 'open') state = '<div class="hero-s blue">未對</div>';
  else state = '<div class="hero-s">收埋' +
    (c.archived_reason ? '（' + esc(c.archived_reason) + '）' : '') + '</div>';
  // Money still to be accounted for is offered against the batches that are
  // owed it, so the sheet the operator is already reading is where he answers.
  if (c.state === 'open' || c.state === 'partial') {
    const props = c.proposals || [];
    extra = props.length
      ? '<div class="prop-sec"><div class="prop-head">可能對</div>' +
        (c.combo ? comboPropHtml(c) : '') +
        props.map(p => batchPropHtml(p, c)).join('') + '</div>'
      : '<div class="sub-note mute">冇 statement 對得上</div>';
  }
  // The label comes from the entry's own dates: the batch itself can sit in a
  // month the page has not loaded.
  const rows = c.batches.map(b =>
    '<button class="sum-row link" data-bl="' + b.id + '">' +
    '<span class="k">批次 ' + esc(spanLabelOf(b.dates)) + '</span>' +
    '<span class="v">' + b.orders + ' 程 · $' + $(b.amount) + '<span class="c">&rsaquo;</span></span></button>' +
    (b.state === 'partial' ? '<div class="sub-note warn">批次仲差 $' + $(b.outstanding) + '</div>' : ''));
  rows.push('<div class="sum-row"><span class="k">Ref</span><span class="v">' + esc(c.ref) + '</span></div>');
  if (c.memo) rows.push('<div class="sum-row"><span class="k">備註</span><span class="v">' +
    esc(c.memo) + '</span></div>');
  // /api/credits scopes the ledger to one platform rather than stamping each
  // credit with it, so the platform on screen is the credit's platform.
  return sheetHead('入數 ' + esc(mdLabel(c.value_date)),
      esc(platLabel(curPlat)) + (c.payer ? ' · ' + esc(c.payer) : '')) +
    '<div class="hero"><div class="hero-k">到帳</div>' +
    '<div class="hero-v">$' + $(c.amount) + '</div>' + state + '</div>' +
    extra +
    '<div class="sum-rows">' + rows.join('') + '</div>' +
    '<button class="ghost-btn" data-back="1">收埋</button>';
}

// Only the work queue is a list: a matched or archived credit is found on the
// calendar by its value date, so listing it again would only bury the ones
// still waiting for a statement.
function queueViewHtml() {
  const open = openCredits();
  return sheetHead('入數未對', esc(platLabel(curPlat)) + ' · ' + open.length + ' 筆 $' + $(ledger.sums.open)) +
    (open.length ? '' : '<div class="empty">冇未對嘅入數</div>') +
    open.map(queueRowHtml).join('') +
    '<button class="ghost-btn" data-back="1">收埋</button>';
}

// A row whose match is not in question answers itself: the batch it agrees
// with, and the tap that puts it there without leaving the queue. Anything
// less certain stays a way into the credit's own sheet.
function queueRowHtml(c) {
  const row = '<button class="qrow" data-credit="' + c.id + '">' +
    '<span class="t">' + esc(mdSlash(c.value_date)) + ' · $' + $(c.amount) + '</span>' +
    '<span class="s">未對' + (c.state === 'partial' ? ' · 剩 $' + $(c.remaining) : '') + '</span>' +
    '<span class="c">&rsaquo;</span></button>';
  if (c.combo) {
    return '<div class="qitem">' + row +
      '<div class="qprop"><span class="t">&rarr; ' + esc(comboLabel(c)) + '</span>' +
      comboBtnHtml(c, '對晒') + '</div></div>';
  }
  const exact = (c.proposals || []).filter(p => p.exact);
  if (exact.length !== 1) return row;
  const p = exact[0];
  return '<div class="qitem">' + row +
    '<div class="qprop"><span class="t">&rarr; 批次 ' + esc(spanLabelOf(p.dates)) +
    ' 差 $' + $(p.outstanding) + '</span>' +
    '<button class="pbtn" data-alloc-credit="' + c.id + '" data-alloc-batch="' + p.id +
    '">對</button></div></div>';
}

// ---- statement sheet ----
// What the reader made of the screenshot, and the one tap that writes the
// batch from it. The report is printed as the server sent it, which is the
// same string the bot's card carries.
function stmtViewHtml(v) {
  if (v.done) {
    return sheetHead('已結算') +
      '<pre class="stmt-report">' + esc(v.done.text) + '</pre>' +
      '<button class="ghost-btn" data-bl="' + v.done.settlement_id + '">睇批次</button>' +
      '<button class="ghost-btn" data-close="1">收埋</button>';
  }
  const r = v.read;
  // A spent read cannot be confirmed again: the server has said why, and the
  // way out is another upload rather than another tap.
  const canConfirm = r.can_settle && !!r.token && !v.err;
  return sheetHead('結算單', esc(platLabel('ride'))) +
    (v.err ? '<div class="stmt-err">' + esc(v.err) + '</div>' : '') +
    '<pre class="stmt-report">' + esc(r.report) + '</pre>' +
    (r.credit_line ? '<div class="stmt-credit">' + esc(r.credit_line) + '</div>' : '') +
    // No batch can come out of this statement; what is left to do is to the
    // credit, and archiving one is a chat-card action the page does not have.
    // Naming it here says what the money is waiting on rather than offering a
    // control that would do nothing.
    (r.no_orders_offer ? '<div class="stmt-note">' + esc(r.no_orders_offer.label) + '</div>' : '') +
    (canConfirm ? '<button class="primary-btn" data-stmtgo="1">' + esc(r.confirm_label) + '</button>' : '') +
    '<button class="ghost-btn" data-close="1">' + (canConfirm ? '唔確認' : '收埋') + '</button>';
}

// Taking money back off a batch is destructive the same way undo is, and is
// confirmed the same way: a pushed view rather than an armed button, so a
// repaint mid-decision cannot wipe the armed state.
function unlinkViewHtml(v) {
  const b = batchById(v.id);
  if (!b) return '';
  const a = b.allocations.find(x => x.credit_id === v.credit);
  if (!a) return '';
  return sheetHead('解除入數', esc(batchLabel(b)) + ' · 入數 ' + esc(mdSlash(a.value_date))) +
    '<div class="undo-info">$' + $(a.amount) + ' 會由呢個批次拎返出嚟，' +
      '批次變返差 $' + $(round2(b.outstanding + a.amount)) + '，錢返到入數度。</div>' +
    '<button class="primary-btn danger" data-unlinkgo="1" data-unlink-batch="' + b.id +
      '" data-unlink-credit="' + a.credit_id + '">確定解除</button>' +
    '<button class="ghost-btn" data-back="1">返回</button>';
}

// Undo is destructive enough to confirm, and confirming in a pushed view
// rather than an armed button means a refresh mid-decision cannot wipe the
// armed state.
function undoViewHtml(v) {
  const b = batchById(v.id);
  if (!b) return '';
  return sheetHead('撤銷結算', esc(batchLabel(b)) + ' · ' + b.orders.length + ' 程') +
    '<div class="undo-info">呢 ' + b.orders.length + ' 程會變返未結算，' +
      '$' + $(b.confirmed_amount) + ' 嘅結算紀錄會刪走。</div>' +
    '<button class="primary-btn danger" data-undogo="' + b.id + '">確認撤銷</button>' +
    '<button class="ghost-btn" data-back="1">返回</button>';
}

// ---- actions ----

// Undo returns the legs to unsettled rather than flagging the batch: an
// unbatched order is already the "not settled" state, so a mistake costs
// nothing to unwind and the days simply go amber again.
async function undoBatch(id) {
  try {
    await apiWrite('DELETE', '/api/settlements/' + id);
  } catch (e) {
    if (!(e instanceof AuthExpired)) toast(e.message);
    return;
  }
  // The batch the sheet was showing no longer exists, so it closes back to the
  // day, where its legs are loose again.
  while (views.length && (curView().kind === 'undo' || curView().kind === 'batch')) views.pop();
  await load();
  toast('已撤銷結算');
}

// The one write the page has that moves money: the matcher named a credit and
// a batch, and this is the tap. The amount is the server's — as much of the
// batch as the credit can still pay — so the page never proposes a figure.
async function allocateCredit(creditId, batchId) {
  let batch;
  try {
    batch = await apiWrite('POST', '/api/credits/' + creditId + '/allocate',
                           { settlement_id: batchId });
  } catch (e) {
    if (!(e instanceof AuthExpired)) toast(e.message);
    return;
  }
  const put = (batch.allocations.find(a => a.credit_id === +creditId) || {}).amount || 0;
  await load();
  toast(batch.state === 'paid'
    ? '已對 $' + $(put) + ' · 批次收齊'
    : '已對 $' + $(put) + ' · 仲差 $' + $(batch.outstanding));
}

async function allocateAll(creditId, batchIds) {
  let res;
  try {
    res = await apiWrite('POST', '/api/credits/' + creditId + '/allocate-all',
                         { settlement_ids: batchIds });
  } catch (e) {
    if (!(e instanceof AuthExpired)) toast(e.message);
    return;
  }
  await load();
  toast('已對 ' + res.batches.length + ' 個批次 · $' +
    $(round2(res.batches.reduce((sum, b) => sum + b.confirmed_amount, 0))) + ' · 收齊');
}

async function unlinkCredit(batchId, creditId) {
  try {
    await apiWrite('DELETE', '/api/settlements/' + batchId + '/allocations/' + creditId);
  } catch (e) {
    if (!(e instanceof AuthExpired)) toast(e.message);
    return;
  }
  // Back to the batch the money came off, which is where the operator was.
  while (views.length && curView().kind === 'unlink') views.pop();
  await load();
  toast('已解除入數');
}

// A credit's batch can sit outside every month the strip has loaded, and the
// batch sheet reads from what is loaded. Bring its month into the strip first,
// or the sheet opens on nothing.
async function openBatch(id) {
  if (!batchById(id)) {
    const b = ledger.credits.flatMap(c => c.batches).find(x => x.id === id);
    const month = b && b.dates.length ? monthKey(b.dates[b.dates.length - 1]) : '';
    if (!month) return;
    if (!await ensureMonth(month)) return;
    scrollToMonth(month);
    if (!batchById(id)) return;
  }
  pushView({ kind: 'batch', id: id });
}

// ---- reading a statement ----
// The file picked here is the platform's own: a screenshot forwarded through a
// chat has been recompressed first, and that is what the reader mis-reads.
let stmtBtn = null;
let stmtFile = null;
let stmtBusy = false;

function setStmtBusy(on) {
  stmtBusy = on;
  stmtBtn.disabled = on;
  stmtBtn.textContent = on ? '⋯' : '圖';
}

async function readStatement(file) {
  if (!file || stmtBusy) return;
  setStmtBusy(true);
  let body;
  try {
    const form = new FormData();
    form.append('file', file);
    const res = await apiFetch('/api/statements/read', { method: 'POST', body: form });
    body = await res.json().catch(() => ({}));
    if (!res.ok) { toast(body.error || ('HTTP ' + res.status)); return; }
  } catch (e) {
    if (!(e instanceof AuthExpired)) toast('讀唔到張圖');
    return;
  } finally {
    setStmtBusy(false);
  }
  openView({ kind: 'stmt', read: body, err: '' });
}

// The tap that writes the batch. The token is spent server-side, so a refusal
// leaves the sheet with the server's wording and no second tap to give.
async function confirmStatement() {
  const v = curView();
  if (!v || v.kind !== 'stmt' || v.done || !v.read.token) return;
  const btn = root.querySelector('[data-stmtgo]');
  if (btn) btn.disabled = true;
  try {
    v.done = await apiWrite('POST', '/api/statements/confirm', { token: v.read.token });
  } catch (e) {
    // An expired login never reached the server: the token is unspent, and
    // the shell says what happened.
    if (e instanceof AuthExpired) { if (btn) btn.disabled = false; return; }
    v.err = e.message;
    v.read.token = null;
  }
  // The batch is new money on the calendar, and load() repaints the open sheet
  // with whatever this tap left on the view.
  await load();
}

let dragDepth = 0;
function showDrop(on) { byId('settle-drop').classList.toggle('show', on); }

function copyId(id) {
  // The async clipboard can be refused depending on where the page is served
  // from; the toast is the feedback either way.
  if (navigator.clipboard) navigator.clipboard.writeText(id).catch(() => {});
  toast('已複製 ' + id);
}

// ---- events ----
// The arrows move the strip a month at a time, relative to the month it is
// showing, and load whatever is not in it yet. They move the scroll, not the
// data: everything already loaded stays loaded and scrollable.
async function shiftMonth(n) {
  const key = addMonths(viewMonth, n);
  if (await ensureMonth(key)) scrollToMonth(key);
}
// Where a pointer exists, resting it on a bar or a chip is the whole ask and
// moving off is the answer given back. Both tests are needed: hover is a
// capability rather than a width, and a screen without one still synthesises a
// mouse pointerover after every scroll, which would drop the focus the tap
// above had just put down.
const hoverMQ = window.matchMedia('(hover: hover)');
function hovering(e) { return hoverMQ.matches && e.pointerType !== 'touch'; }
let calResizeT = null;

// ---- unpaid tick interaction ----
// The tick state lives in the DOM: toggling repaints only the section rather
// than the whole sheet, so scroll position and the rest of the view survive.
function getTickedIds() {
  const sec = root.querySelector('[data-upbatch]');
  if (!sec) return new Set();
  return new Set([...sec.querySelectorAll('.up-chk.on')].map(
    el => el.closest('[data-uptick]').dataset.uptick));
}
// A row's tag mirrors its box, so a ticked leg already reads 未過數 the way it
// will once the save has round-tripped.
function setTickRow(row, on) {
  const chk = row.querySelector('.up-chk');
  chk.classList.toggle('on', on);
  chk.textContent = on ? '☑' : '☐';
  const end = row.querySelector('.oend');
  const tag = end.querySelector('.otag');
  if (on && !tag) end.insertAdjacentHTML('beforeend', '<span class="otag unsettled">未過數</span>');
  if (!on && tag) tag.remove();
}
function toggleUnpaid(orderId) {
  const sec = root.querySelector('[data-upbatch]');
  if (!sec) return;
  const bid = +sec.dataset.upbatch;
  const b = batchById(bid);
  if (!b) return;
  const row = sec.querySelector('[data-uptick="' + orderId + '"]');
  if (!row) return;
  setTickRow(row, !row.querySelector('.up-chk').classList.contains('on'));
  refreshUnpaidFoot(sec, b);
}
function applyGuess(idx) {
  const sec = root.querySelector('[data-upbatch]');
  if (!sec) return;
  const bid = +sec.dataset.upbatch;
  const b = batchById(bid);
  if (!b) return;
  const guessIds = new Set((b.unpaid_guesses || [])[idx] || []);
  sec.querySelectorAll('[data-uptick]').forEach(row => {
    const want = guessIds.has(row.dataset.uptick);
    if (want !== row.querySelector('.up-chk').classList.contains('on')) setTickRow(row, want);
  });
  refreshUnpaidFoot(sec, b);
}
// One phrasing for the footer, so the first paint and every toggle after it
// cannot drift apart.
function tickFootText(amt, b) {
  return Math.abs(amt - b.outstanding) < 0.005
    ? '剔咗 $' + $(amt) + ' = 差額'
    : '剔咗 $' + $(amt) + ' ≠ 差 $' + $(b.outstanding);
}
function refreshUnpaidFoot(sec, b) {
  const ticked = getTickedIds();
  let amt = 0;
  b.orders.forEach(o => { if (ticked.has(o.order_id)) amt += o.platform_amount; });
  amt = Math.round(amt * 100) / 100;
  const match = Math.abs(amt - b.outstanding) < 0.005;
  const foot = sec.querySelector('.up-foot');
  const sumEl = foot.querySelector('.up-sum');
  sumEl.className = match ? 'up-sum' : 'up-sum warn';
  sumEl.textContent = tickFootText(amt, b);
  foot.querySelector('.up-btn').disabled = !match;
}
async function saveUnpaid(bid) {
  const ticked = [...getTickedIds()];
  try {
    await apiWrite('POST', '/api/settlements/' + bid + '/unpaid', { order_ids: ticked });
  } catch (e) {
    if (!(e instanceof AuthExpired)) toast(e.message);
    return;
  }
  await load();
  toast('已記低');
}

let monthTick = 0;

// ---- the view ----
// The strip opens on today's month at the top of the strip area, and every
// growth after that holds whatever it was showing, so no positioning step is
// needed here.
let started = false;
export const settleView = {
  mount(el, deps) {
    root = el;
    tapAt = deps.navAt;
    stmtBtn = byId('stmtBtn');
    stmtFile = byId('stmtFile');
    stmtBtn.addEventListener('click', () => stmtFile.click());
    stmtFile.addEventListener('change', () => {
      const file = stmtFile.files[0];
      // Cleared first, so picking the same file again still fires a change.
      stmtFile.value = '';
      readStatement(file);
    });
    // Desktop only in practice: a phone has no drag. Both handlers preventDefault
    // because a browser left to itself navigates away to the dropped file. The
    // window is the day view's too: a file dragged over that view is none of
    // this one's business.
    window.addEventListener('dragover', e => { if (showing) e.preventDefault(); });
    window.addEventListener('dragenter', e => {
      if (!showing) return;
      e.preventDefault();
      if (!e.dataTransfer || ![...e.dataTransfer.types].includes('Files')) return;
      dragDepth++;
      showDrop(true);
    });
    window.addEventListener('dragleave', () => {
      if (!showing) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) showDrop(false);
    });
    window.addEventListener('drop', e => {
      if (!showing) return;
      e.preventDefault();
      dragDepth = 0;
      showDrop(false);
      readStatement(e.dataTransfer && e.dataTransfer.files[0]);
    });
    byId('prevM').addEventListener('click', () => shiftMonth(-1));
    byId('nextM').addEventListener('click', () => shiftMonth(1));
    byId('monthBtn').addEventListener('click', async () => {
      const key = todayMonth();
      if (key === viewMonth) return;
      if (await ensureMonth(key)) scrollToMonth(key, true);
    });
    byId('settle-tabs').addEventListener('click', e => {
      const c = e.target.closest('.tab');
      if (!c || c.dataset.f === curPlat) return;
      curPlat = c.dataset.f;
      savePlat();
      // Another platform's book is another set of months worth loading, so the
      // strip starts again where the operator would start reading it.
      refound(todayMonth());
    });
    byId('settle-foot').addEventListener('click', e => {
      if (e.target.closest('[data-credits]')) openView({ kind: 'queue' });
    });
    // The whole calendar area, not just the week rows: the legend and the padding
    // around the strip answer nothing else, so a tap there is the way out of a
    // focus.
    const cal = root.querySelector('.cal');
    cal.addEventListener('click', e => {
      const t = focusTarget(e.target);
      if (t) {
        // The first ask is for the relation, the second for the sheet. A pointer
        // has already made the first by resting there, so its click opens straight
        // away; a finger cannot rest, so its first tap lights and its second opens.
        if (!isFocus(t)) { setFocus(t); revealFocus(); return; }
        openView({ kind: t.kind, id: t.id });
        return;
      }
      // A day opens on one tap whatever is focused: settling is the work this page
      // exists for and it does not gain a step.
      const cell = e.target.closest('.cell');
      if (cell && cell.dataset.d) { openDay(cell.dataset.d); return; }
      clearFocus();
    });
    cal.addEventListener('pointerover', e => {
      if (!hovering(e)) return;
      const t = focusTarget(e.target);
      if (t) setFocus(t); else clearFocus();
    });
    cal.addEventListener('pointerleave', e => { if (hovering(e)) clearFocus(); });
    document.fonts.ready.then(fontsChanged);
    document.fonts.addEventListener('loadingdone', fontsChanged);
    // Column width and font size are measured, so a rotation or a resized window
    // has to re-reserve every label against the new geometry.
    window.addEventListener('resize', () => {
      clearTimeout(calResizeT);
      // A hidden strip has no width to measure; showing it lays it out again.
      calResizeT = setTimeout(() => { if (showing) renderCalendar(); }, 150);
    });
    byId('settle-scrim').addEventListener('click', closeSheet);
    byId('settle-sheet').addEventListener('click', e => {
      if (e.target.closest('[data-close]')) { closeSheet(); return; }
      if (e.target.closest('[data-back]')) { popView(); return; }
      // Before the row: the order number sits inside a row that would otherwise
      // treat the tap as a toggle.
      const cp = e.target.closest('[data-copy]');
      if (cp) { copyId(cp.dataset.copy); return; }
      const undo = e.target.closest('[data-undo]');
      if (undo) { pushView({ kind: 'undo', id: +undo.dataset.undo }); return; }
      const undoGo = e.target.closest('[data-undogo]');
      if (undoGo) { undoBatch(undoGo.dataset.undogo); return; }
      const stmtGo = e.target.closest('[data-stmtgo]');
      if (stmtGo && !stmtGo.disabled) { confirmStatement(); return; }
      const all = e.target.closest('[data-alloc-all]');
      if (all) { allocateAll(+all.dataset.allocAll, all.dataset.allocIds.split(',').map(Number)); return; }
      const alloc = e.target.closest('[data-alloc-credit]');
      if (alloc) { allocateCredit(+alloc.dataset.allocCredit, +alloc.dataset.allocBatch); return; }
      // The confirm button carries the same pair as the row that opened it, so it
      // is matched first or the row handler below would reopen the confirm view.
      const unlinkGo = e.target.closest('[data-unlinkgo]');
      if (unlinkGo) {
        unlinkCredit(+unlinkGo.dataset.unlinkBatch, +unlinkGo.dataset.unlinkCredit);
        return;
      }
      const unlink = e.target.closest('[data-unlink-batch]');
      if (unlink) {
        pushView({ kind: 'unlink', id: +unlink.dataset.unlinkBatch,
                   credit: +unlink.dataset.unlinkCredit });
        return;
      }
      const tick = e.target.closest('[data-uptick]');
      if (tick) { toggleUnpaid(tick.dataset.uptick); return; }
      const guess = e.target.closest('[data-upguess]');
      if (guess) { applyGuess(+guess.dataset.upguess); return; }
      const save = e.target.closest('[data-upsave]');
      if (save && !save.disabled) { saveUnpaid(+save.dataset.upsave); return; }
      const fold = e.target.closest('[data-fold]');
      if (fold) {
        const id = +fold.dataset.fold;
        foldOpen.has(id) ? foldOpen.delete(id) : foldOpen.add(id);
        paintView(true);
        return;
      }
      const cr = e.target.closest('[data-credit]');
      if (cr) { pushView({ kind: 'credit', id: +cr.dataset.credit }); return; }
      const od = e.target.closest('[data-od]');
      if (od) { openOrder(od.dataset.od); return; }
      const link = e.target.closest('[data-bl]');
      if (link) { openBatch(+link.dataset.bl); return; }
    });
    // The header names the month the strip is showing, which changes as it scrolls
    // rather than when something is loaded. One read per frame at most: this fires
    // on every scroll tick.
    window.addEventListener('scroll', () => {
      // The document scrolls under the other view as well.
      if (!showing || monthTick) return;
      monthTick = requestAnimationFrame(() => {
        monthTick = 0;
        if (!showing) return;
        const mon = monthAtTop();
        if (mon && mon !== viewMonth) { viewMonth = mon; renderHeader(); }
      });
    }, { passive: true });
  },
  show() {
    showing = true;
    useOrderHost(orderHost);
    navAt = tapAt();
    if (!started) { started = true; loadPlat(); watchEdges(); }
    // Loaded again on every showing: nothing was drawn while the view was
    // hidden, and the load ends by painting the open sheet.
    return load();
  },
  hide() { showing = false; },
  // The server said something changed.
  refresh() { return load(); },
};
