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
import { cellFigure, dayRunsLabel, dayState, fareGap, inMonthPart, keyFigure, statementName,
         waitedDays } from './days.js';

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
// Months with a fetch in flight: 'YYYY-MM' -> the promise of its payload. A
// sentinel can enter the viewport twice before its month lands, and the
// second entry must not fetch again; a caller that needs a month another
// caller is already fetching waits on the same promise.
let loading = new Map();
// Bumped whenever the strip is thrown away and refounded (platform switch, a
// jump far outside it). A fetch that started before the bump is answering a
// question nobody is asking any more.
let gen = 0;
// The merged read of every loaded month, which is what the whole page below
// works off. monthTotals is the one part that stays apart: 'YYYY-MM' -> that
// month's own totals, or null where the server withheld them.
let data = { orders: [], settlements: [], counts: {}, totals: { unsettled: 0, awaiting: 0 },
             monthTotals: {}, now: '' };
// The bank ledger, exactly what /api/credits returns: every credit of the
// platform, whatever the strip has loaded.
let ledger = { counts: { open: 0, partial: 0, done: 0, archived: 0 }, sums: { open: 0, done: 0 }, credits: [] };
let curPlat = 'ride';
// The month the header names: the one the strip is showing, updated by the
// scroll rather than by a paging button.
let viewMonth = fmtDate(new Date()).slice(0, 7);
// Which of the month's totals is chosen. It decides what the body under the
// header shows, and is changed through setLens() and nowhere else.
const LENSES = ['fare', 'received', 'awaiting', 'unsettled'];
let lens = 'fare';
// Two of the totals are about statements, not days, and open a list of them
// in the strip's place.
function isList() { return lens === 'awaiting' || lens === 'received'; }
// Settle-ability is decided against the server's clock, not the browser's, so
// the page and the API agree on which legs are done.
let NOW = '';
let TODAY = '';
let BATCH_OF = new Map();
// Which batches have their order list unfolded. Page state rather than DOM
// state, so an SSE repaint mid-read does not fold the list away.
let foldOpen = new Set();
// Which statement's days the strip is lighting: null, or { kind: 'batch', id }.
// Page state rather than DOM state, because the relation outlives the paint
// that drew it -- a month arriving under the scroll, or an SSE repaint, must
// not drop what the operator is reading.
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

// ---- figures ----
// A sheet is read for its figures, so each is set in the figure face: a time,
// a date, an amount, an order number. The words around them are not.
// num() takes text that is a figure and nothing else.
const num = text => '<span class="num">' + tight(esc(text)) + '</span>';
// An order number grouped for the eye (groupId). The gap between groups is
// a thin space, which the figure face does not have and its stand-in draws a
// whole cell wide, so each is handed to the text face, where it is thin.
function idHtml(id) {
  return tight(esc(groupId(id))).replace(/\u2009/g, '<span class="gs">\u2009</span>');
}
// figs() takes a line of text and sets the figures in it: a run of digits
// with the sign, the symbol and the marks that belong to it ($1,376.55, 9/10,
// 19:15, 4–6, #…0041), or a code that carries a digit (CX488, P4). It takes
// raw text and escapes it, so what comes back is markup.
const FIG = /[−+]?[$#]?…?[0-9A-Za-z]*\d[0-9A-Za-z]*(?:[.,:/·–-]\d+)*/g;
function figs(text) {
  const s = String(text ?? '');
  let out = '', at = 0;
  for (const m of s.matchAll(FIG)) {
    out += esc(s.slice(at, m.index)) + num(m[0]);
    at = m.index + m[0].length;
  }
  return out + esc(s.slice(at));
}

// An amount as it is written outside the calendar: the $, the thousands
// comma, and the cents always there.
function fullMoney(n) {
  const f = keyFigure(n);
  return f.dollars + '.' + f.cents;
}

// A line made of parts joined by a middle dot. Each part is kept together
// where the line has to wrap, so a break falls between two parts and not
// between a word and its figure.
function parts(list) {
  return list.map(p => '<span class="pt">' + figs(p) + '</span>').join(' · ');
}

// ---- labels ----
function todayMonth() { return TODAY ? monthKey(TODAY) : fmtDate(new Date()).slice(0, 7); }
function platLabel(key) { return PLATFORMS.find(p => p.key === key).label; }
function paidLabel(b) { return '已收 ' + mdSlash(b.paid_on); }
// What a batch is owed, in one phrase: a part-paid batch is neither 已收 nor
// simply 等過數 -- the outstanding half is what the operator is chasing.
function batchTag(b) {
  if (b.state === 'paid') return paidLabel(b);
  if (b.state === 'partial') return '差 $' + $(b.outstanding);
  return '等過數';
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
  const monthTotals = {};
  const seen = new Set();
  for (const key of loadedMonths()) {
    const p = months.get(key);
    // A month's totals are that month's alone, so each is kept under its own
    // key rather than taken from the freshest payload.
    monthTotals[key] = p.month_totals || null;
    for (const o of p.orders) orders.push(o);
    // Orders are scoped to their month server-side, but a batch straddling a
    // boundary comes back whole in both months' payloads, so it is taken
    // once.
    for (const b of p.settlements) {
      if (seen.has(b.id)) continue;
      seen.add(b.id);
      settlements.push(b);
    }
  }
  // counts, totals and the clock span all time, so any payload carries them and
  // the freshest one wins.
  data = { orders, settlements, counts: book.counts, totals: book.totals, monthTotals, now: book.now };
  NOW = data.now;
  TODAY = NOW.slice(0, 10);
  reindex();
}

// One month's payload, stored nowhere: whoever asked decides what to keep. A
// month already being fetched is not fetched again.
function fetchMonth(key) {
  // Refounding swaps the map rather than emptying it, so the entry has to
  // come out of the one it went into and not out of whatever is current by
  // then.
  const inflight = loading;
  if (inflight.has(key)) return inflight.get(key);
  const p = (async () => {
    try {
      const res = await apiFetch('/api/settle?month=' + key + '&platform=' + curPlat);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return await res.json();
    } finally {
      inflight.delete(key);
    }
  })();
  inflight.set(key, p);
  return p;
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
  const want = monthsBetween(lo, hi).filter(k => !months.has(k));
  if (want.filter(k => !loading.has(k)).length > FILL_MAX) return refound(key);
  const g = gen;
  // The run is stored whole or not at all. Kept one by one, a month that
  // failed between two that answered would leave a gap in the keys, and the
  // strip draws a gap as real days with no work on them. A month another
  // caller is fetching is part of this run too: both wait on the one request
  // and each stores only a run that is complete.
  let payloads;
  try {
    payloads = await Promise.all(want.map(fetchMonth));
  } catch (e) {
    // An expired login is announced by the shell, not by a toast.
    if (!(e instanceof AuthExpired)) toast('載入失敗');
    return false;
  }
  if (g === gen) want.forEach((k, i) => months.set(k, payloads[i]));
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
  loading = new Map();
  anchorDebt = 0;
  pinId = '';
  stripAt = null;
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
  // A hidden view neither asks nor draws: the document's scroll position is
  // the other view's, and the order sheet is drawn through the other view's
  // host. Showing the view loads.
  if (!showing) return;
  const keys = loadedMonths();
  if (!keys.length) keys.push(seed || todayMonth());
  const g = gen;
  const asked = navAt;
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
    // The tap this load was timed from has had its answer, and it was a
    // failure: a paint made later for another reason is not timed from it.
    // Unless a newer showing has set the mark since, which is then its own.
    if (navAt === asked) navAt = 0;
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
  renderBody();
}

function renderHeader() {
  // YYYY·MM in the figure face. The second span is always there, empty when
  // the month is not the current one: the stylesheet gives it a line of its
  // own on a narrow screen, and the header must not change height with the
  // month it names.
  byId('monthBtn').innerHTML =
    '<span class="d">' + tight(viewMonth.slice(0, 4) + '·' + viewMonth.slice(5, 7)) + '</span>' +
    '<span class="w">' + (viewMonth === todayMonth() ? '<b>今個月</b>' : '&nbsp;') + '</span>';
  renderLens();
  renderFoot();
}

function renderFoot() {
  const foot = byId('settle-foot');
  // A focus takes the foot: the lit days say which, and this line says what.
  if (focus) { foot.innerHTML = focusLineHtml(); return; }
  // Totals span the whole book, not the months the strip has loaded: old
  // unsettled days are exactly the ones the operator is here to clear. The
  // server computes them, so the tabs and the calendar cannot disagree about
  // the same money.
  // Only what is still to be done: a matched or archived credit is finished
  // business.
  const open = openCredits();
  const vals = [money(data.totals.unsettled), '$' + $(data.totals.awaiting)]
    .concat(open.length ? ['$' + $(ledger.sums.open)] : []).map(tight);
  // The stylesheet sizes the figures so that all of them fit the one line.
  foot.style.setProperty('--n', vals.reduce((n, f) => n + +cellsOf(f), 0).toFixed(2));
  foot.innerHTML =
    '<div class="warn"><span class="k">未結算</span><span class="v">' + vals[0] + '</span></div>' +
    '<div><span class="k">等過數</span><span class="v">' + vals[1] + '</span></div>' +
    (open.length ? '<button class="tot queue" data-credits="1"><span class="k">入數未對 ' + open.length +
      ' 筆</span><span class="v">' + vals[2] + '</span></button>' : '');
}

// A statement's name. Its place among the loaded statements confirmed on the
// same date decides whether the name is numbered.
function nameOf(b) {
  const same = data.settlements.filter(x => x.settled_on === b.settled_on)
    .map(x => x.id).sort((a, z) => a - z);
  return statementName(b, same.indexOf(b.id));
}

// The focused statement in one line: its name, the days it lights, its legs,
// its figure and where its money has got to. A batch the strip no longer
// holds is known only from the ledger, which carries neither its statement
// date nor the day it was collected, so the line says less of it.
function focusLineHtml() {
  const b = batchById(focus.id);
  const known = b || ledger.credits.flatMap(c => c.batches).find(x => x.id === focus.id);
  if (!known) return '';
  const amount = keyFigure(known.confirmed_amount);
  const state = b ? batchTag(b)
    : known.state === 'paid' ? '已收'
    : known.state === 'partial' ? '差 $' + $(known.outstanding) : '等過數';
  return '<div class="fline"><span class="ft">' +
    parts([b ? nameOf(b) : '結算', dayRunsLabel(batchDatesOf(focus.id), viewMonth),
           (b ? b.orders.length : known.orders) + ' 程', amount.dollars + '.' + amount.cents]) +
    ' · <span class="pt bs ' + known.state + '">' + figs(state) + '</span></span>' +
    '<button class="fx" data-unfocus="1" aria-label="取消">&#10005;</button></div>';
}

// The four totals of the month the header names, for the platform chosen.
// The keys themselves are in the document from the start; this fills them in.
// A month the strip has not loaded and a month whose totals the server
// withheld both show a dash: a zero would state a figure nobody has.
function renderLens() {
  const box = byId('settle-lens');
  const t = data.monthTotals[viewMonth] || null;
  let cells = 0;
  for (const key of box.querySelectorAll('.lkey')) {
    const name = key.dataset.lens;
    let html = '—', n = 1;
    if (t) {
      const f = keyFigure(t[name]);
      const dollars = tight(f.dollars), cts = tight('.' + f.cents);
      html = dollars + '<span class="ct">' + cts + '</span>';
      // The cents are set at .7 of the figure's size (.ct in the stylesheet).
      n = +cellsOf(dollars) + +cellsOf(cts) * 0.7;
    }
    cells += n;
    key.querySelector('.v').innerHTML = html;
    // The rule and the colour of a state are worn only by a figure that has
    // money in that state: amber on nothing owed would be a false alarm.
    const owing = name !== 'fare' && name !== 'received' && t && t[name] > 0;
    key.className = 'lkey' + (name === 'fare' ? '' : owing ? ' st-' + name : ' st-received') +
      (name === lens ? ' on' : '');
    key.setAttribute('aria-pressed', String(name === lens));
  }
  // The stylesheet sizes the four figures so that they share the one line.
  box.style.setProperty('--n', cells.toFixed(2));
}

// The one place the lens changes, so whatever the body shows for a lens is
// switched from here.
function setLens(name) {
  if (!LENSES.includes(name)) return;
  lens = name;
  // A focus is read on the whole-fare calendar and nowhere else. The strip
  // lights one set of days at a time, and a statement's days are not the
  // days holding unsettled money; a list shows no days at all.
  if (name !== 'fare' && focus) { focus = null; renderFoot(); }
  renderLens();
  renderBody();
  paintLit();
}

// ---- body: the strip, or a list of statements ----
// The strip is hidden while a list stands in its place, never emptied: the
// months it holds and the rows it has drawn are still there on the way back.
// Hiding it takes its height out of the document, so the document's scroll
// position stops meaning anything for it; where it stood is kept here
// instead: its top row, how far under the strip's top line that row sat, and
// the month the header named. Null while the strip is showing.
let stripAt = null;
// What the list on screen was drawn for, so a repaint of the same list (a
// live update) leaves it where the operator is reading and a different one
// starts from its top.
let listKey = '';
function stripShown() { return !root.querySelector('.cal').hidden; }

// The one place the body is switched and the list is drawn, called from
// setLens and from every paint of data.
function renderBody() {
  const list = isList();
  const cal = root.querySelector('.cal');
  const back = !list && cal.hidden;
  if (list && !cal.hidden) {
    const el = topRow();
    stripAt = el
      ? { id: el.id, off: el.getBoundingClientRect().top - stripTop(), month: viewMonth } : null;
  }
  cal.hidden = list;
  // The weekday head belongs to the calendar and goes with it; the list has
  // a head of its own in the same place.
  root.querySelector('.header .wk').hidden = list;
  byId('settle-lhead').hidden = !list;
  byId('settle-list').hidden = !list;
  if (list) {
    renderList();
    const key = lens + ' ' + curPlat + ' ' + viewMonth;
    if (key !== listKey) window.scrollTo(0, 0);
    listKey = key;
    return;
  }
  listKey = '';
  if (!back) return;
  // Back on the calendar. The strip goes where it was left if the month is
  // still the one it was left on, and to the month the header names now if
  // the list was paged to another. What the anchor owed belongs to a scroll
  // position that is gone.
  pinId = '';
  anchorDebt = 0;
  const el = stripAt && stripAt.month === viewMonth && byId(stripAt.id);
  if (el) {
    window.scrollTo(0, Math.max(0, window.scrollY + el.getBoundingClientRect().top - stripTop() - stripAt.off));
  } else {
    scrollToMonth(viewMonth);
  }
  stripAt = null;
  pokeEdges();
}

// The statements a list lens shows for the month the header names: those
// with a day in that month, a leg's or a 舉牌 line's, which are the ones the
// month's total counts something of. Under 等過數, only statements no
// transfer has come for: one paid short is neither waiting nor finished, and
// is listed with the money that did come.
function listBatches() {
  const want = lens === 'awaiting' ? ['awaiting'] : ['paid', 'partial'];
  const rows = data.settlements.filter(b =>
    want.includes(b.state) && batchSpan(b).some(d => monthKey(d) === viewMonth));
  if (lens === 'awaiting') {
    // Longest wait first. A statement stored without its date has an unknown
    // wait, which is not a short one, so it goes after every known one.
    const wait = b => { const n = waitedDays(b, TODAY); return n === null ? -1 : n; };
    return rows.sort((a, z) => wait(z) - wait(a) || a.id - z.id);
  }
  // The one still owed money leads, since it is the one needing action; the
  // rest are records, newest statement first.
  const on = b => b.settled_on || '';
  return rows.sort((a, z) => (z.state === 'partial') - (a.state === 'partial') ||
    on(z).localeCompare(on(a)) || z.id - a.id);
}

// One statement as a row: what it is at the left, its own figure at the
// right with what is to be said about it underneath. The figure is the
// statement's, not the month's: what the platform confirmed while nothing
// has come, what has arrived once something has.
function stmtRowHtml(b) {
  const span = batchSpan(b);
  const sub = [[dayRunsLabel(span, viewMonth), b.orders.length + ' 程']];
  const end = [];
  let amount;
  if (lens === 'awaiting') {
    amount = b.confirmed_amount;
  } else {
    amount = b.received;
    if (b.state === 'partial') sub.push(['應收 ' + fullMoney(b.confirmed_amount)]);
    for (const a of b.allocations) sub.push(['入數 ' + mdSlash(a.value_date), fullMoney(a.amount)]);
  }
  // The total above counts this month's orders only, so a statement that
  // reaches outside the month says how much of it the total counts.
  if (span.some(d => monthKey(d) !== viewMonth)) {
    end.push('<span class="bmon">其中本月 ' + figs(fullMoney(inMonthPart(b, viewMonth))) + '</span>');
  }
  // The total counts fares; the row states the statement's figure. Where the
  // two differ the row says by how much, so the rows can be added up against
  // the total. It is information, not a state: no colour.
  const gap = fareGap(b);
  if (gap) end.push('<span class="bgap">同車費差 ' + figs(fullMoney(gap)) + '</span>');
  if (lens === 'awaiting') {
    const waited = waitedDays(b, TODAY);
    if (waited !== null) end.push('<span class="btag">' + figs('等咗 ' + waited + ' 日') + '</span>');
  } else if (b.state === 'partial') {
    end.push('<span class="btag warn">' + figs('仲差 ' + fullMoney(b.outstanding)) + '</span>');
  } else {
    end.push('<span class="btag paid">已收齊</span>');
  }
  // A collected statement is a record and recedes; one still owed does not.
  return '<button class="brow' + (b.state === 'paid' ? ' rec' : '') + '" data-bl="' + b.id + '">' +
    '<span class="bl"><span class="bt">' + figs(nameOf(b)) + '</span>' +
    sub.map(line => '<span class="bsub">' + parts(line) + '</span>').join('') + '</span>' +
    '<span class="bend"><span class="ba">' + num(fullMoney(amount)) + '</span>' + end.join('') + '</span>' +
    '<span class="bc">&rsaquo;</span></button>';
}

function renderList() {
  // A month the strip does not hold has no statements to show yet, which is
  // not the same as having none: the head gives no count and the list no
  // wording until the month is in.
  const loaded = months.has(viewMonth);
  const rows = loaded ? listBatches() : [];
  byId('settle-lhead').innerHTML =
    '<span>結算單' + (loaded ? ' ' + num(String(rows.length)) + ' 張' : '') + '</span>' +
    '<span>' + (lens === 'awaiting' ? '未過數' : '已過數') + '</span>';
  let html = '';
  if (loaded) {
    html = rows.map(stmtRowHtml).join('') || '<div class="empty">' +
      (lens === 'awaiting' ? '今個月冇等過數嘅結算單' : '今個月未有入數') + '</div>';
  }
  byId('settle-list').innerHTML = html;
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
// what the stylesheet takes off each mark tight() wraps (.34em, .24em and
// .12em by class), in cells of .65em.
const PULLED = { p: 0.52, pd: 0.37, ps: 0.18 };
function cellsOf(html) {
  const text = html.replace(/<[^>]*>/g, '');
  const off = (html.match(/class="p[ds]?"/g) || [])
    .reduce((n, m) => n + PULLED[m.slice(7, -1)], 0);
  return (text.length - off).toFixed(2);
}

// How much of a short-paid day's rule is drawn as money that came: the share
// of its statement that has arrived. Of the rule's 24px, 3px is the break
// between the two parts and neither part is under 6px, so both are seen
// however lopsided the share.
function shortGot(orders) {
  const b = orders.map(o => batchOf(o.order_id)).find(x => x && x.state === 'partial');
  const share = b && b.confirmed_amount > 0 ? b.received / b.confirmed_amount : 0;
  return Math.min(15, Math.max(6, Math.round(21 * share)));
}

// A cell's money figure: its markup, and its length in cells of the dollars'
// size, which the stylesheet sizes it by. The cents are set at .7 of that
// size (.ct in the stylesheet).
function figureOf(amount) {
  const f = cellFigure(amount);
  const dollars = tight(f.dollars), cts = f.cents ? tight('.' + f.cents) : '';
  return {
    html: dollars + (cts ? '<span class="ct">' + cts + '</span>' : ''),
    n: (+cellsOf(dollars) + (cts ? +cellsOf(cts) * 0.7 : 0)).toFixed(2),
  };
}

// A day says three things and no more: its date, what the whole day is worth,
// and by a rule under that figure where its money has got to.
//
// The cell also carries, unseen, the two amounts it can be asked to print:
// the whole day's fare, and the part of it no statement has claimed when
// there is one. Whatever swaps the figure later reads them off the cell, so
// both come from the one reading of the day's orders made here.
function cellHtml(dateStr) {
  const day = +dateStr.slice(8);
  // The 1st carries its month, because a week row is not a month and there is
  // no heading above it to read the month off.
  const num = day === 1 ? mdSlash(dateStr) : String(day);
  const orders = ordersOn(dateStr);
  const info = dayState(orders, batchOf, NOW);
  const today = dateStr === TODAY ? ' today' : '';
  // Only an empty day is inert; every day holding work opens, past or future.
  if (!info.n) return '<button class="cell none' + today + '" disabled><span class="d">' + tight(num) + '</span></button>';
  const f = figureOf(info.total);
  const got = info.state === 'short' ? ' style="--got:' + shortGot(orders) + 'px"' : '';
  return '<button class="cell st-' + info.state + today + '" data-d="' + dateStr +
    '" data-total="' + info.total + '"' + (info.loose > 0 ? ' data-loose="' + info.loose + '"' : '') + '>' +
    '<span class="d">' + tight(num) + '</span><span class="amt" style="--n:' + f.n + '">' +
    f.html + '</span><i class="mk"' + got + '></i></button>';
}

// Every week of the loaded run, end to end, with no month break in it: from the
// Sunday of the week holding the 1st of the earliest month to the Saturday of
// the week holding the last day of the latest. Every cell is a real date, so a
// week row can span two months. The edge weeks reach into months not loaded
// yet; those days draw as empty and fill in when their month arrives.
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

function renderCalendar() {
  const grid = byId('grid');
  const weeks = stripWeeks();
  const html = weeks.map((week, wi) => {
    const start = wi > 0 && weekMonth(week) !== weekMonth(weeks[wi - 1]);
    return '<div class="wkblock' + (start ? ' mstart' : '') + '" id="' + weekId(week[0]) +
      '"><div class="grid">' + week.map(cellHtml).join('') + '</div></div>';
  }).join('');
  // A hidden strip has no geometry: it is rebuilt where it lies, and neither
  // held in place nor asked which month it is showing.
  const shown = stripShown();
  const anchor = shown ? takeAnchor() : null;
  grid.innerHTML = html;
  if (shown) {
    putAnchor(anchor);
    const mon = monthAtTop();
    if (mon) viewMonth = mon;
  }
  // A month arriving under the scroll draws days of a statement the operator
  // is already reading, or days the chosen lens lights, so the paint ends by
  // restating which days are lit.
  paintLit();
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

// A repaint rebuilds every week row, and a month prepended adds rows above the
// viewport -- which would slide the strip out from under the operator. The fix
// is an anchor: where the top row sat before the paint, put it back after.
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
  // A top row sitting below the strip's top line has the head of the page
  // above it, which only the untouched strip is ever short enough to show.
  // Growth at the top displaces it, so such a row is pinned to the line.
  // Anywhere else the row is where the operator scrolled it to, and goes back
  // exactly there.
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
// A statement covers days that can be weeks apart on the strip, and nothing in
// a day's cell says which statement it went onto. Focus is that statement: one
// batch is named, its days are lit and every other day recedes.
//
// The relation is read off the ledger rather than off the loaded months,
// because that is the only place an end outside them appears: a credit carries
// every batch it went into, and each of those carries its own days.
//
// It is the whole connected run of allocations, not the named batch alone. Two
// batches paid by one credit are one money statement, and a statement has to
// read the same from every end of it -- entering at either batch must light
// the identical set, or the operator is being told the sum depends on where he
// looked. Closing over the edges buys that invariance at the cost of a long
// chain lighting entirely, which is what a chain of money crossing over itself
// is.

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

// Which days are lit, and which recede. Two things light days and never both
// at once: a focus lights one statement's days; with no focus, the 未結算 lens
// lights every day holding money no statement has claimed. Only orders
// already driven count towards that money, so a day still to come is never
// lit by the lens, as the month's total counts it nowhere.
//
// Under the lens a lit day prints its unclaimed part in place of its whole
// fare, so the lit figures of a month add up to the 未結算 key to the cent; a
// day partly on a statement would otherwise overstate it. Every other day
// keeps its whole fare.
//
// Lit, dim and the figure are set on what the paint has already laid out
// rather than woven into the markup, so a focus or a lens is taken and
// dropped without rebuilding every week row, which would fight the scroll
// anchor. One place decides, so a month painted later reads the same.
function paintLit() {
  const s = focus ? focusSets() : null;
  const loose = !focus && lens === 'unsettled';
  // An empty day already reads as a calendar coordinate rather than money, so
  // it is left alone: dimming it again would only separate it from the other
  // empty days.
  byId('grid').querySelectorAll('.cell[data-d]').forEach(el => {
    const held = s ? s.dates.has(el.dataset.d) : loose && 'loose' in el.dataset;
    el.classList.toggle('lit', held);
    el.classList.toggle('dim', (!!s || loose) && !held);
    // `part` says which of its two amounts the cell is printing, so the
    // figure is rewritten only when that changes. Its length changes with
    // it, and the stylesheet sizes the figure to its column from --n.
    const part = loose && held;
    if (part === el.classList.contains('part')) return;
    el.classList.toggle('part', part);
    const f = figureOf(+(part ? el.dataset.loose : el.dataset.total));
    const amt = el.querySelector('.amt');
    amt.innerHTML = f.html;
    amt.style.setProperty('--n', f.n);
  });
}

function isFocus(t) { return !!focus && focus.kind === t.kind && focus.id === t.id; }
function setFocus(t) {
  if (isFocus(t)) return;
  focus = t;
  paintLit();
  renderFoot();
}
function clearFocus() {
  if (!focus) return;
  focus = null;
  paintLit();
  renderFoot();
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

// Whether a cell can be read where it is: below the sticky header and above
// the fixed foot.
function onScreen(el) {
  const r = el.getBoundingClientRect();
  return r.bottom > stripTop() && r.top < root.querySelector('.foot').getBoundingClientRect().top;
}

// A focus is put down from a sheet, which can have been reached from a day
// far from the statement's own. None of the named statement's days on screen
// means the relation was stated and cannot be seen, so the strip goes to the
// nearest row carrying one. Only then: a strip already showing the answer
// must not be moved out from under the operator. The days are always on the
// strip, because a batch's sheet opens only once its month is loaded.
function revealFocus() {
  if (!focus) return;
  const own = new Set(batchDatesOf(focus.id));
  const cells = [...byId('grid').querySelectorAll('.cell.lit')].filter(el => own.has(el.dataset.d));
  if (!cells.length || cells.some(onScreen)) return;
  const edge = stripTop();
  const rows = [...new Set(cells.map(el => el.closest('.wkblock')))];
  rows.sort((a, b) => Math.abs(a.getBoundingClientRect().top - edge) -
                      Math.abs(b.getBoundingClientRect().top - edge));
  scrollToWeek(rows[0].id, true);
}

// The way into a focus: from a statement's sheet, back to the calendar with
// that statement's days lit.
function showOnCalendar(id) {
  closeSheet();
  setLens('fare');
  setFocus({ kind: 'batch', id });
  revealFocus();
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
    el.innerHTML = '';
    draw(el);
  } else {
    el.innerHTML = viewHtml(v);
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

// One row shape for every sheet, on one grid: the time, the whole order number
// over what jogs the memory of which leg it was, and the figure over what is
// to be said about it. The mode decides what that is: 'day' states where the
// leg's money has got to, 'batch' shows the platform's own figure beside the
// system's. A tick option turns the batch row into the operator's answer to
// which leg the platform has not paid; without it the row is a read, since a
// batch is created from a statement image in the bot, never from here.
function orderRowHtml(o, mode, opts) {
  const tick = opts && opts.tick;
  const tag = (cls, text) => '<span class="otag ' + cls + '">' + figs(text) + '</span>';
  let fig = '';
  const notes = [];
  if (mode === 'day') {
    const state = dayTag(o);
    const amt = owedOf(o);
    if (amt) fig = money(amt);
    notes.push(tag(state[1], state[0]));
    // A 舉牌 paid ahead of a held-back trip has already arrived on another
    // batch, so the leg's figure is the rest and this line names the part
    // that came.
    if ((o.paid_ahead || 0) > 0) notes.push(tag('paid', '舉牌 ' + money(o.paid_ahead) + ' 先結'));
  } else if (tick) {
    // The figure to tick against is the platform's, because the outstanding
    // amount the ticks have to add up to is the platform's own arithmetic.
    fig = '$' + $(o.platform_amount);
    if (opts.ticked) notes.push(tag('unsettled', '未過數'));
  } else {
    const plat = opts && opts.plat;
    const mark = !o.price ? '未入價' : (o.scheduled_time >= NOW ? '未完成' : '');
    // A leg the platform held back says when its money finally came, so the
    // list keeps the record the flags were kept for.
    if (opts && opts.madeUp) notes.push(tag('paid', '補收 ' + opts.madeUp));
    if (mark) {
      notes.push(tag('warn', mark));
    } else {
      fig = money(owedOf(o));
      // The fine is why the leg is worth less than its fare; both figures are
      // already net, so this only names the deduction.
      if ((o.penalty_fee || 0) > 0) notes.push(tag('fine', '判罰 ' + money(-o.penalty_fee)));
      // Likewise the 舉牌 another batch carries: the leg is owed the rest here.
      if ((o.paid_ahead || 0) > 0) notes.push(tag('paid', '舉牌 ' + money(o.paid_ahead) + ' 先結'));
      // In batch detail the platform's own figure is shown when it disagrees;
      // an equal figure would only repeat the number.
      if (plat !== undefined && Math.abs(plat - owedOf(o)) >= 0.005) notes.push(tag('plat', '平台 $' + $(plat)));
    }
  }
  const chk = tick ? '<span class="up-chk' + (opts.ticked ? ' on' : '') + '"></span>' : '';
  // What the row itself is: the tick answer in a short-paid batch, the way
  // into the order in the day sheet, and nothing at all in a batch read.
  const rowAttrs = tick ? ' tick" data-uptick="' + esc(o.order_id) + '"'
    : mode === 'day' ? ' tap" data-od="' + esc(o.order_id) + '"'
    : '"';
  // A copy target inside a row that is itself the target would swallow the
  // tap, so in the day sheet the copy lives in the sheet the row opens.
  const idAttrs = mode === 'day' ? '' : ' data-copy="' + esc(o.order_id) + '"';
  return '<div class="orow' + rowAttrs + '>' + chk +
    '<span class="ot">' + num(orderTime(o)) + '</span>' +
    '<span class="ol">' +
      '<span class="oid num"' + idAttrs + '>' + idHtml(o.order_id) + '</span>' +
      '<span class="oll">' + figs(orderLabel(o)) + '</span></span>' +
    '<span class="oend">' + (fig ? '<span class="oa">' + num(fig) + '</span>' : '') + notes.join('') +
    '</span></div>';
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
  return dates.map(d => '<div class="oday">' + figs(mdLabel(d)) + ' 星期' + weekday(d) + '</div>' +
    rows.filter(o => orderDate(o) === d).map(row).join('')).join('');
}

// ---- day sheet ----
// A read of the day: what it earned, where each leg's money has got to, and
// the batches it belongs to. Nothing is written from here.
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
  return sheetHead(figs(mdLabel(v.date)) + ' 星期' + weekday(v.date), esc(platLabel(curPlat))) +
    ordersOn(v.date).map(o => orderRowHtml(o, 'day')).join('') +
    (batches.length ? '<div class="blinks">' + batches.map(b =>
      '<button class="blink" data-bl="' + b.id + '"><span class="blink-t">' +
      figs('批次 ' + batchLabel(b) + ' · ' + b.orders.length + ' 程 · $' + $(b.confirmed_amount)) +
      ' · <span class="bs ' + b.state + '">' + figs(batchTag(b)) + '</span>' +
      (heldLabel(b) ? ' · ' + figs(heldLabel(b)) : '') + '</span>' +
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
    // The order held from before is dropped with the error: this fetch is
    // made again whenever the page reloads, and an order left standing would
    // be the one from before whatever changed.
    v.order = null;
    v.err = '讀唔到';
  }
}
function orderView() {
  for (let i = views.length - 1; i >= 0; i--) if (views[i].kind === 'order') return views[i];
  return null;
}
// Drawn only until the order has arrived, or when it could not be read.
function orderViewHtml(v) {
  return sheetHead('單 ' + num(tailId(v.id)), '') +
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
    figs(batchLabel(b)) + ' · ' + figs(batchTag(b)) + ' &rsaquo;</button>';
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
  subtitle: o => num('#' + shortId(o.order_id)) + ' · ' + figs(mdLabel(orderDate(o))) + ' 星期' +
    weekday(orderDate(o)),
  // The whole number, to copy: statements and the platform name a leg by it.
  rowsBefore: o => [['單號', '<span class="oid num" data-copy="' + esc(o.order_id) + '">' +
    idHtml(o.order_id) + '</span>']],
  rowsAfter(o) {
    const rows = [];
    const fees = (o.banner_fee || 0) + (o.tunnel_fee || 0) + (o.penalty_fee || 0);
    if (fees && !(o.penalty_fee > 0)) rows.push(['淨收', num(money(expectedOf(o)))]);
    const b = batchOf(o.order_id);
    rows.push(['結算', b ? batchLinkHtml(b, '') + (o.unpaid ? ' · 未過數' : '') : figs(dayTag(o)[0])]);
    // The other place part of this leg's money went: a 舉牌 paid ahead of a
    // trip the platform held back sits on the batch it arrived with.
    const s = settleRowOf(o.order_id);
    if (s && s.paid_ahead > 0) {
      const ahead = batchById(s.ahead_batch);
      rows.push(['舉牌', ahead ? batchLinkHtml(ahead, num(money(s.paid_ahead)) + ' 先結 · ')
                               : num(money(s.paid_ahead)) + ' 先結 · 批次 ' + num('#' + s.ahead_batch)]);
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
  const left = p.remaining < p.amount - 0.005 ? ['剩 $' + $(p.remaining)] : [];
  return '<div class="prow"><span class="t">' +
    parts(['入數 ' + mdSlash(p.value_date), '$' + $(p.amount)].concat(left)) + '</span>' +
    (p.exact ? '<span class="ptag">啱數</span>' : '') +
    '<button class="pbtn" data-alloc-credit="' + p.id + '" data-alloc-batch="' + b.id +
    '">' + figs(label) + '</button></div>';
}

// The mirror: a batch offered against a credit that has money left.
function batchPropHtml(p, c) {
  const gap = round2(p.outstanding - c.remaining);
  const label = gap > 0.005 ? '對 $' + $(c.remaining) + '（差 $' + $(gap) + '）' : '對';
  // A batch of the group is exact only together with the others, and the
  // group row above already says so.
  const exact = p.exact && !(c.combo && c.combo.ids.includes(p.id));
  return '<div class="prow"><span class="t">' +
    parts(['批次 ' + spanLabelOf(p.dates), p.orders + ' 程', '差 $' + $(p.outstanding)]) + '</span>' +
    (exact ? '<span class="ptag">啱數</span>' : '') +
    '<button class="pbtn" data-alloc-credit="' + c.id + '" data-alloc-batch="' + p.id +
    '">' + figs(label) + '</button></div>';
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
  return '<div class="prop-sec"><div class="prop-head">' + figs('帳項 · ' + money(total)) + '</div>' +
    rows.map(a => '<div class="sum-row"><span class="k">' +
      figs((a.ahead ? '舉牌先結 ' : '') + tailId(a.order_ref) + ' · ' + mdSlash(a.date)) +
      '</span><span class="v">' + num((a.amount < 0 ? '−' : '+') + '$' + $(Math.abs(a.amount))) +
      '</span></div>').join('') +
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
  if (b.state === 'paid') state = '<div class="hero-s paid">' + figs('已收齊 · ' + mdSlash(b.paid_on)) + '</div>';
  else if (b.state === 'partial') state = '<div class="hero-s warn">' + figs('已收 $' + $(b.received) +
    '（' + mdSlash(b.allocations[b.allocations.length - 1].value_date) + '） · 差 $' + $(b.outstanding)) + '</div>';
  else state = '<div class="hero-s">等過數</div>';

  const sums = ['<div class="sum-row total"><span class="k">應收</span><span class="v">' +
    num('$' + $(b.expected_amount)) + '</span></div>'];
  if (diff) sums.push('<div class="sum-row"><span class="k">差額</span><span class="v">' +
    num((diff < 0 ? '−' : '+') + '$' + $(Math.abs(diff))) + '</span></div>');
  // The flags outlive the payment, so a collected batch can still say which
  // legs each transfer covered: the first allocation paid the rest of the
  // statement, the one that made the batch whole paid the held-back legs.
  const heldBack = b.orders.filter(o => o.unpaid);
  b.allocations.forEach((a, i) => {
    sums.push('<div class="alloc">' +
      '<button class="sum-row link" data-credit="' + a.credit_id + '">' +
      '<span class="k">' + figs('入數 ' + mdSlash(a.value_date)) + '</span>' +
      '<span class="v">' + num('$' + $(a.amount)) + '<span class="c">&rsaquo;</span></span></button>' +
      '<button class="xbtn" data-unlink-batch="' + b.id + '" data-unlink-credit="' +
      a.credit_id + '">解除</button></div>');
    if (b.state !== 'paid' || !heldBack.length) return;
    if (i === b.allocations.length - 1) {
      sums.push('<div class="sub-note mute">' +
        figs('補 ' + heldBack.map(o => tailId(o.order_id)).join(' · ')) + '</div>');
    } else if (i === 0) {
      sums.push('<div class="sub-note mute">' + figs('其餘 ' + (b.orders.length - heldBack.length) + ' 程') + '</div>');
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
      sums.push('<div class="sum-row"><span class="k">平台多出 <span class="num">' + idHtml(id) + '</span>' +
        '</span><span class="v">' + num('$' + $(plat.get(id))) + '</span></div>'));
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
    if (guesses.length === 1) note = '<div class="up-note">' + figs('系統估：' + guessLabel(guesses[0], b) + '，啱差額，已剔') + '</div>';
    else if (guesses.length > 1) note = '<div class="up-chips"><span class="up-note">系統估：</span>' +
      guesses.map((g, i) => '<button class="up-chip" data-upguess="' + i + '">' +
        figs(guessLabel(g, b)) + '</button>').join('') + '</div>';
    else note = '<div class="up-note">' + figs('冇組合啱 $' + $(b.outstanding) + '，自己剔') + '</div>';
    const tickedAmt = rows.reduce((sum, o) => ticked.has(o.order_id) ? sum + o.platform_amount : sum, 0);
    const match = Math.abs(tickedAmt - b.outstanding) < 0.005;
    // What could close the batch comes before what is missing from it: the
    // money is the answer, the ticks only say which legs it is for.
    const props = b.proposals || [];
    mid = '<div class="prop-sec"><div class="prop-head">' + figs('等緊補數 · 差 $' + $(b.outstanding)) +
      '</div>' + (props.length ? props.map(p => creditPropHtml(p, b)).join('')
                               : '<div class="up-note">未收到補數</div>') + '</div>' +
      '<div class="up-sec" data-upbatch="' + b.id + '">' +
      '<div class="up-head">' + figs('邊張單未過？ · 差 $' + $(b.outstanding)) + '</div>' + note +
      orderListHtml(b, rows, ticked) +
      '<div class="up-foot"><span class="up-sum' + (match ? '' : ' warn') + '">' +
      figs(tickFootText(tickedAmt, b)) + '</span>' +
      '<button class="up-btn" data-upsave="' + b.id + '"' + (match ? '' : ' disabled') +
      '>記低</button></div></div>';
  } else {
    const open = foldOpen.has(b.id);
    mid = '<button class="fold" data-fold="' + b.id + '"><span>' + figs(b.orders.length + ' 程') + '</span>' +
      '<span class="c">' + (open ? '&#9662;' : '&#9656;') + '</span></button>' +
      (open ? orderListHtml(b, rows, null) : '');
  }

  const held = heldLabel(b);
  return sheetHead('結算 ' + figs(batchLabel(b)),
      figs(platLabel(b.platform) + ' · ' + b.orders.length + ' 程 · 結算日 ' + mdSlash(b.settled_on) +
      (held ? ' · ' + held : ''))) +
    '<div class="hero"><div class="hero-k">平台確認</div>' +
    '<div class="hero-v">' + num('$' + $(b.confirmed_amount)) + '</div>' + state + '</div>' +
    '<div class="sum-rows">' + sums.join('') + '</div>' +
    adjustmentsHtml(b) +
    mid +
    '<div class="sheet-acts"><button class="ghost-btn" data-focus="' + b.id + '">喺月曆睇</button>' +
    '<button class="ghost-btn danger" data-undo="' + b.id + '">撤銷結算</button>' +
    '<button class="ghost-btn" data-back="1">收埋</button></div>';
}

// ---- credit sheet ----
// One bank credit: how much arrived, who sent it, and which batch it was read
// against. The batch row hands the operator on to that batch, and says what it
// is still owed, because a credit covering a batch in full is not the same as
// one that left it short.
// One transfer pays a whole confirmation day, so the group the matcher found
// is offered as one row and one tap; its batches stay offered one by one below.
function comboHtml(c) {
  const members = c.combo.ids.map(id => (c.proposals || []).find(p => p.id === id)).filter(Boolean);
  const pt = text => '<span class="pt">' + figs(text) + '</span>';
  return pt(c.combo.ids.length + ' 個批次') + ' · ' +
    members.map(p => pt(spanLabelOf(p.dates))).join('、') + ' · ' + pt('$' + $(c.combo.total));
}
function comboBtnHtml(c, label) {
  return '<button class="pbtn" data-alloc-all="' + c.id + '" data-alloc-ids="' +
    c.combo.ids.join(',') + '">' + label + '</button>';
}
function comboPropHtml(c) {
  return '<div class="prow"><span class="t">' + comboHtml(c) + '</span>' +
    '<span class="ptag">啱數</span>' + comboBtnHtml(c, '對晒') + '</div>';
}
function creditViewHtml(v) {
  const c = creditById(v.id);
  if (!c) return '';
  let state, extra = '';
  if (c.state === 'done') state = '<div class="hero-s paid">已對</div>';
  else if (c.state === 'partial') state = '<div class="hero-s blue">' + figs('已對 $' + $(c.allocated) +
    ' · 剩 $' + $(c.remaining)) + '</div>';
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
    '<span class="k">' + figs('批次 ' + spanLabelOf(b.dates)) + '</span>' +
    '<span class="v">' + figs(b.orders + ' 程 · $' + $(b.amount)) + '<span class="c">&rsaquo;</span></span></button>' +
    (b.state === 'partial' ? '<div class="sub-note warn">' + figs('批次仲差 $' + $(b.outstanding)) + '</div>' : ''));
  // The bank's reference is an identifier and is set as one; a memo is
  // whatever the payer typed. Either can be long, and both wrap.
  rows.push('<div class="sum-row"><span class="k">Ref</span><span class="v">' + num(c.ref) + '</span></div>');
  if (c.memo) rows.push('<div class="sum-row"><span class="k">備註</span><span class="v">' +
    esc(c.memo) + '</span></div>');
  // /api/credits scopes the ledger to one platform rather than stamping each
  // credit with it, so the platform on screen is the credit's platform.
  return sheetHead('入數 ' + figs(mdLabel(c.value_date)),
      esc(platLabel(curPlat)) + (c.payer ? ' · ' + esc(c.payer) : '')) +
    '<div class="hero"><div class="hero-k">到帳</div>' +
    '<div class="hero-v">' + num('$' + $(c.amount)) + '</div>' + state + '</div>' +
    extra +
    '<div class="sum-rows">' + rows.join('') + '</div>' +
    '<div class="sheet-acts"><button class="ghost-btn" data-back="1">收埋</button></div>';
}

// Only the work queue is a list: a matched credit is reached from the batch it
// paid, so listing it again would only bury the ones still waiting for a
// statement.
function queueViewHtml() {
  const open = openCredits();
  return sheetHead('入數未對', figs(platLabel(curPlat) + ' · ' + open.length + ' 筆 $' + $(ledger.sums.open))) +
    (open.length ? '' : '<div class="empty">冇未對嘅入數</div>') +
    open.map(queueRowHtml).join('') +
    '<div class="sheet-acts"><button class="ghost-btn" data-back="1">收埋</button></div>';
}

// A row whose match is not in question answers itself: the batch it agrees
// with, and the tap that puts it there without leaving the queue. Anything
// less certain stays a way into the credit's own sheet.
function queueRowHtml(c) {
  const row = '<button class="qrow" data-credit="' + c.id + '">' +
    '<span class="t">' + figs(mdSlash(c.value_date) + ' · $' + $(c.amount)) + '</span>' +
    '<span class="s">' + figs('未對' + (c.state === 'partial' ? ' · 剩 $' + $(c.remaining) : '')) + '</span>' +
    '<span class="c">&rsaquo;</span></button>';
  if (c.combo) {
    return '<div class="qitem">' + row +
      '<div class="qprop"><span class="t">&rarr; ' + comboHtml(c) + '</span>' +
      comboBtnHtml(c, '對晒') + '</div></div>';
  }
  const exact = (c.proposals || []).filter(p => p.exact);
  if (exact.length !== 1) return row;
  const p = exact[0];
  return '<div class="qitem">' + row +
    '<div class="qprop"><span class="t">&rarr; ' + figs('批次 ' + spanLabelOf(p.dates) +
    ' 差 $' + $(p.outstanding)) + '</span>' +
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
      '<pre class="stmt-report">' + figs(v.done.text) + '</pre>' +
      '<div class="sheet-acts"><button class="ghost-btn" data-bl="' + v.done.settlement_id + '">睇批次</button>' +
      '<button class="ghost-btn" data-close="1">收埋</button></div>';
  }
  const r = v.read;
  // A spent read cannot be confirmed again: the server has said why, and the
  // way out is another upload rather than another tap.
  const canConfirm = r.can_settle && !!r.token && !v.err;
  return sheetHead('結算單', esc(platLabel('ride'))) +
    (v.err ? '<div class="stmt-err">' + figs(v.err) + '</div>' : '') +
    '<pre class="stmt-report">' + figs(r.report) + '</pre>' +
    (r.credit_line ? '<div class="stmt-credit">' + figs(r.credit_line) + '</div>' : '') +
    // No batch can come out of this statement; what is left to do is to the
    // credit, and archiving one is a chat-card action the page does not have.
    // Naming it here says what the money is waiting on rather than offering a
    // control that would do nothing.
    (r.no_orders_offer ? '<div class="stmt-note">' + figs(r.no_orders_offer.label) + '</div>' : '') +
    '<div class="sheet-acts">' +
    (canConfirm ? '<button class="primary-btn" data-stmtgo="1">' + figs(r.confirm_label) + '</button>' : '') +
    '<button class="ghost-btn" data-close="1">' + (canConfirm ? '唔確認' : '收埋') + '</button></div>';
}

// Taking money back off a batch is destructive the same way undo is, and is
// confirmed the same way: a pushed view rather than an armed button, so a
// repaint mid-decision cannot wipe the armed state.
function unlinkViewHtml(v) {
  const b = batchById(v.id);
  if (!b) return '';
  const a = b.allocations.find(x => x.credit_id === v.credit);
  if (!a) return '';
  return sheetHead('解除入數', figs(batchLabel(b) + ' · 入數 ' + mdSlash(a.value_date))) +
    '<div class="undo-info">' + figs('$' + $(a.amount) + ' 會由呢個批次拎返出嚟，' +
      '批次變返差 $' + $(round2(b.outstanding + a.amount)) + '，錢返到入數度。') + '</div>' +
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
  return sheetHead('撤銷結算', figs(batchLabel(b) + ' · ' + b.orders.length + ' 程')) +
    '<div class="undo-info">' + figs('呢 ' + b.orders.length + ' 程會變返未結算，' +
      '$' + $(b.confirmed_amount) + ' 嘅結算紀錄會刪走。') + '</div>' +
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
    // Under a list the strip is hidden and has nowhere to be scrolled to.
    if (stripShown()) scrollToMonth(month);
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
// The arrows and the month button name a month, and whatever of it is not
// loaded yet is loaded first. On the calendar they move the scroll, not the
// data: everything already loaded stays loaded and scrollable, and the header
// follows the strip. Under a list there is no scroll to follow, so the month
// is set here and the header and the list are drawn for it; the strip is put
// on that month when the calendar comes back.
async function goMonth(key, smooth) {
  if (!await ensureMonth(key)) return;
  if (!isList()) { scrollToMonth(key, smooth); return; }
  viewMonth = key;
  renderHeader();
  renderBody();
}
function shiftMonth(n) { return goMonth(addMonths(viewMonth, n), false); }
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
  row.querySelector('.up-chk').classList.toggle('on', on);
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
  sumEl.innerHTML = figs(tickFootText(amt, b));
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
    byId('monthBtn').addEventListener('click', () => {
      const key = todayMonth();
      if (key === viewMonth) return;
      goMonth(key, true);
    });
    byId('settle-tabs').addEventListener('click', e => {
      const c = e.target.closest('.tab');
      if (!c || c.dataset.f === curPlat) return;
      curPlat = c.dataset.f;
      savePlat();
      // Another platform's book is another set of months worth loading, so the
      // strip starts again where the operator would start reading it. The
      // strip names that month itself once it is drawn; a list has to be told.
      if (isList()) viewMonth = todayMonth();
      refound(todayMonth());
    });
    byId('settle-lens').addEventListener('click', e => {
      const key = e.target.closest('.lkey');
      if (key) setLens(key.dataset.lens);
    });
    byId('settle-foot').addEventListener('click', e => {
      if (e.target.closest('[data-unfocus]')) { clearFocus(); return; }
      if (e.target.closest('[data-credits]')) openView({ kind: 'queue' });
    });
    // The whole calendar area, not just the week rows: an empty day and the
    // padding around the strip answer nothing else, so a tap there is the way
    // out of a focus.
    root.querySelector('.cal').addEventListener('click', e => {
      // A day opens on one tap whatever is focused: settling is the work this page
      // exists for and it does not gain a step.
      const cell = e.target.closest('.cell');
      if (cell && cell.dataset.d) { openDay(cell.dataset.d); return; }
      clearFocus();
    });
    byId('settle-list').addEventListener('click', e => {
      const row = e.target.closest('[data-bl]');
      if (row) openBatch(+row.dataset.bl);
    });
    byId('settle-scrim').addEventListener('click', closeSheet);
    byId('settle-sheet').addEventListener('click', e => {
      if (e.target.closest('[data-close]')) { closeSheet(); return; }
      if (e.target.closest('[data-back]')) { popView(); return; }
      // Before the row: the order number sits inside a row that would otherwise
      // treat the tap as a toggle.
      const cp = e.target.closest('[data-copy]');
      if (cp) { copyId(cp.dataset.copy); return; }
      const onCal = e.target.closest('[data-focus]');
      if (onCal) { showOnCalendar(+onCal.dataset.focus); return; }
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
      // The document scrolls under the other view as well, and under a list,
      // where the strip is hidden and names no month.
      if (!showing || monthTick || !stripShown()) return;
      monthTick = requestAnimationFrame(() => {
        monthTick = 0;
        if (!showing || !stripShown()) return;
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
    // Each visit starts from the month's whole fare: the lens is where the
    // operator was looking, not a setting.
    setLens('fare');
    // Loaded again on every showing: nothing was drawn while the view was
    // hidden, and the load ends by painting the open sheet.
    return load();
  },
  hide() { showing = false; },
  // The server said something changed.
  refresh() { return load(); },
};
