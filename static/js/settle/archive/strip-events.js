// ARCHIVE. Nothing imports this file, and it does not run on its own.
//
// What it drew: under the days of each week of the settle strip, lanes of
// events. A batch was a bar across the service days it covers, cut where the
// week wraps, carrying the statement's figure as a label; a run of days the
// batch held apart from its main run was a dashed outline pointing at that
// run; a bank credit was a chip on its value date. Labels were never
// shortened: each was measured in the page and reserved the columns it
// needed, and ./lanes.js packed the reservations into rows. A tap on a bar
// or a chip, or a pointer resting on one, lit the whole money relation it
// belonged to, bars and chips and days, and a tap scrolled the strip to the
// far end of that relation, loading its month when the strip did not hold it.
//
// The calendar now shows one figure and one state mark per day, and none of
// this is drawn. The code is kept as it stood in ../index.js, in the order
// it stood there. It reads that module's state and helpers (data, ledger,
// focus, months, showing, viewMonth, root, byId, spanRuns, aheadIn, cellHtml,
// stripWeeks, weekMonth, weekId, weekIdOf, takeAnchor, putAnchor, monthAtTop,
// pokeEdges, stripTop, scrollToWeek, ensureMonth, focusSets, batchDatesOf,
// isFocus, setFocus, clearFocus, openView, openDay, pinId and the rest), so
// bringing a part back means moving it back into that module. Where the view
// still has a function of the same name (renderCalendar, paintFocus,
// revealFocus), the one here is the version that drew or lit events. The
// rules that styled it are in static/css/archive/settle-strip-events.css.

import { $, esc, tight } from '../../shared.js';
import { mdSlash, monthKey } from '../../dates.js';
import { packLanes } from './lanes.js';

// ---- labels of a bar and the state of a chip ----
// Money to the cent, so the label carries both figures rather than the one the
// column has room for: what the statement is worth, and what is still owed.
function barLabel(b) {
  return b.state === 'partial'
    ? '$' + $(b.confirmed_amount) + ' · 差 $' + $(b.outstanding)
    : '$' + $(b.confirmed_amount);
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

// ---- the events of one week ----
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
    // A label longer than its bar starts inside the bar and runs out of its
    // far end. Anchored to the right edge it would have to run out of the
    // near end instead, starting wherever its length put it: on the bar's
    // own edge as often as not, with its first figure cut by it. Such a
    // label is written whole beside the bar, as a pointer's is, and the
    // reservation is the bar, the gap and the label.
    if (e.spillL && e.type === 'bar' && !e.makeup && e.label &&
        e.lw + m.kinds.bar.pad > spanW(e.cols, m) + 0.5) {
      e.out = true;
      const w = spanW(e.cols, m) + 4 + e.lw;
      while (e.reach < 7 && spanW(e.reach, m) < w + 1) e.reach++;
    }
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

// ---- the paint that laid the lanes out ----
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

// ---- reaching a row on a strip too short to scroll to it ----
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

// ---- pointer and listeners ----
// Where a pointer exists, resting it on a bar or a chip is the whole ask and
// moving off is the answer given back. Both tests are needed: hover is a
// capability rather than a width, and a screen without one still synthesises a
// mouse pointerover after every scroll, which would drop the focus the tap
// above had just put down.
const hoverMQ = window.matchMedia('(hover: hover)');
function hovering(e) { return hoverMQ.matches && e.pointerType !== 'touch'; }
let calResizeT = null;

// These stood in the view's mount(), which wired the calendar's taps, the
// hover focus, and the re-layout a label measured in the page needs when a
// face arrives or the window changes width.
function wireStripEvents() {
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
}
