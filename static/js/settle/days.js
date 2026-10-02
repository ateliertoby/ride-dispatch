// What the settle view says about a day and about a statement, decided from
// the data alone: no DOM, no page state. Dates are 'YYYY-MM-DD' strings and
// months 'YYYY-MM' keys.

import { expectedOf, owedOf } from '../shared.js';
import { mdSlash, pad2, runsOf } from '../dates.js';

// Money is added in whole cents: fares are floats, and a sum of them carries
// noise (0.1 + 0.2) that would otherwise reach the screen as a wrong cent.
function cents(n) { return Math.round(n * 100); }

function orderDate(o) { return (o.scheduled_time || '').split(' ')[0]; }

// A day shows one mark, so a day in several states shows the one furthest
// from being paid.
const RANK = ['received', 'awaiting', 'short', 'unsettled'];
const BATCH_STATE = { paid: 'received', awaiting: 'awaiting', partial: 'short' };

// One day's orders -> { n, total, loose, state }. `batchOf` maps an order id
// to the batch holding it, or null. `now` is the payload's clock,
// 'YYYY-MM-DD HH:MM:SS'. `total` is what the whole day is worth, driven or
// not. `loose` and `state` are decided from the orders already driven
// (scheduled before `now`) and from no other: one not yet driven may still be
// cancelled, the server's month totals count it nowhere, and a day marked for
// it would disagree with them. `loose` is the driven part no statement has
// claimed. `state` is one of 'none' (no orders), 'future' (none driven yet),
// 'unsettled', 'short', 'awaiting', 'received'.
export function dayState(orders, batchOf, now) {
  let total = 0, loose = 0, worst = -1;
  for (const o of orders) {
    total += cents(expectedOf(o));
    if ((o.scheduled_time || '') >= now) continue;
    const b = batchOf(o.order_id);
    if (!b) loose += cents(owedOf(o));
    worst = Math.max(worst, RANK.indexOf(b ? BATCH_STATE[b.state] : 'unsettled'));
  }
  const state = !orders.length ? 'none' : worst < 0 ? 'future' : RANK[worst];
  return { n: orders.length, total: total / 100, loose: loose / 100, state };
}

// An amount as a calendar cell writes it: dollars and cents apart, because
// the cents are set smaller, with no $ and no thousands comma. Whole dollars
// have no cents part. The minus sign is U+2212, the one money() writes.
export function cellFigure(amount) {
  const c = cents(amount), abs = Math.abs(c);
  return {
    dollars: (c < 0 ? '−' : '') + Math.trunc(abs / 100),
    cents: abs % 100 ? pad2(abs % 100) : '',
  };
}

// An amount as a total key writes it, outside the grid: $, the thousands
// comma, and the cents always there, apart from the dollars because they are
// set smaller. The sign goes outside the symbol, as money() puts it.
export function keyFigure(amount) {
  const c = cents(amount), abs = Math.abs(c);
  return {
    dollars: (c < 0 ? '−' : '') + '$' +
      String(Math.trunc(abs / 100)).replace(/\B(?=(\d{3})+$)/g, ','),
    cents: pad2(abs % 100),
  };
}

// A set of dates as runs of consecutive days, as short as it can be read
// without a doubt: a date carries its month when that month is not the one
// the date written just before it carried, so a bare number is always a day
// of the last month written. '9/12–13', '9/13、15', '9/30–10/1', '9/29、10/2',
// '9/30–10/1、3'.
function runsLabel(dates) {
  let month = '';
  const write = d => {
    const named = d.slice(0, 7) === month;
    month = d.slice(0, 7);
    return named ? String(+d.slice(8)) : mdSlash(d);
  };
  return runsOf([...new Set(dates)]).map(run =>
    write(run[0]) + (run.length > 1 ? '–' + write(run[run.length - 1]) : '')).join('、');
}

// A statement is named by the 應結算日期 values the platform prints on it,
// `due_dates`: the set of them is what the platform's own statement is told
// apart by, where the day it was confirmed in the app, `settled_on`, is shared
// by every statement confirmed that day. A batch whose stored statement
// carries none falls back on `settled_on`, and one with neither gets no date
// rather than a borrowed one; `paid_on` is the bank's value date and names
// the transfer, not the statement. `sameNameIndex` is the statement's place
// among those that would otherwise be given the same name, from 0; a later
// one is numbered so a name never stands for two statements.
export function statementName(batch, sameNameIndex = 0) {
  const due = batch.due_dates || [];
  const on = due.length ? runsLabel(due) : batch.settled_on ? mdSlash(batch.settled_on) : '';
  return (on ? on + ' ' : '') + '結算' +
    (sameNameIndex > 0 ? ' (' + (sameNameIndex + 1) + ')' : '');
}

// Collected statements in the order their list shows them. One still owed
// money leads, since it is the one needing action; the rest are records,
// newest first by the date they are named by: the latest due date a
// statement prints, or the day it was confirmed when it prints none, so the
// names read down the list in order. A statement with neither goes last.
export function collectedOrder(batches) {
  const on = b => (b.due_dates || []).reduce((a, d) => d > a ? d : a, '') || b.settled_on || '';
  return batches.slice().sort((a, z) => (z.state === 'partial') - (a.state === 'partial') ||
    on(z).localeCompare(on(a)) || z.id - a.id);
}

// The service days a statement covers, as runs of consecutive days. Inside
// the month shown the days stand alone ('10日、18–20日'); once any date lies
// outside it, a bare day number would be ambiguous, so every run carries its
// month ('9/27、10/2', '9/29–10/1').
export function dayRunsLabel(dates, monthKey) {
  const days = [...new Set(dates)];
  const inMonth = days.every(d => d.slice(0, 7) === monthKey);
  return runsOf(days).map(run => {
    const a = run[0], z = run[run.length - 1];
    if (inMonth) return (+a.slice(8)) + (a === z ? '' : '–' + (+z.slice(8))) + '日';
    if (a === z) return mdSlash(a);
    return mdSlash(a) + '–' + (a.slice(0, 7) === z.slice(0, 7) ? +z.slice(8) : mdSlash(z));
  }).join('、');
}

// The day the month's totals count a line of a statement on, or null when
// they count it on none. Only a 舉牌 paid ahead of a held-back trip is counted
// at all, and it is counted with its trip's order, on the day that order is
// scheduled now: `trip_date`, which the server reads off the order. That is
// not the line's own `date`, the day the statement printed it under: the two
// are one day until the trip is moved, and after that only the trip's is
// where the money is counted. A line with no `trip_date` has no active order
// left to be counted under.
export function countedDay(line) {
  return line.ahead && line.trip_date ? line.trip_date : null;
}

// How much of a statement falls in one month, as the server's month totals
// count it: each leg at what this statement is owed for it, in the month the
// leg was driven, and each 舉牌 the statement paid ahead of a held-back trip
// in the month of that trip. Counted from the legs, not from the statement's
// figure, and not at a leg's whole fare: the part of it an earlier statement
// paid ahead is that statement's.
export function inMonthPart(batch, monthKey) {
  let sum = 0;
  for (const o of batch.orders) {
    if (orderDate(o).slice(0, 7) === monthKey) sum += cents(owedOf(o));
  }
  for (const a of batch.adjustments || []) {
    const day = countedDay(a);
    if (day && day.slice(0, 7) === monthKey) sum += cents(a.amount);
  }
  return sum / 100;
}

// The lines of a statement that the month's totals count under no order, as
// one signed sum. The totals are split order by order (split_month): a leg
// is counted at what its statement is owed for it, and a 舉牌 paid ahead of a
// held-back trip is counted under that trip, so both are in a key. Every
// other line the statement carries itself is money on the transfer and on no
// order the totals read: a 判罰 against a trip another statement holds, one
// that was cancelled or one the book never had, the 免責 line that cancels
// one, a 舉牌 paid ahead of a trip cancelled since. A row's figure therefore
// differs from what the keys count of its statement by this sum and by
// fareGap, and by nothing else:
//
//   confirmed figure = inMonthPart over every month + otherLines + fareGap
export function otherLines(batch) {
  let sum = 0;
  for (const a of batch.adjustments || []) {
    if (!countedDay(a)) sum += cents(a.amount);
  }
  return sum / 100;
}

// By how much a statement's figure differs from what the book holds for it:
// the figure the platform confirmed, less its legs and the lines it carries
// itself. Zero when they agree to the cent.
//
// The book's side is everything the statement is legitimately the sum of. A
// leg counts at what this statement is owed for it, so a 舉牌 an earlier
// statement paid ahead is not asked of this one. The statement's own lines
// count as printed: a 舉牌 it paid ahead of a trip it held back, a 判罰 against
// a trip another statement holds, the 免責 line that cancels one. They are
// money on the transfer that no leg carries, and leaving them out would
// report every such statement as disagreeing by exactly those lines. What is
// left is a figure the platform put on a leg, or on the whole statement, that
// is not the one the book has.
export function fareGap(batch) {
  let held = 0;
  for (const o of batch.orders) held += cents(owedOf(o));
  for (const a of batch.adjustments || []) held += cents(a.amount);
  return (cents(batch.confirmed_amount || 0) - held) / 100;
}

// How a line that must stay on one line is set in the room it has. The line
// can take several forms, the whole line first and each later form one part
// shorter; `atFull` and `atFloor` are the widths of those forms, in px, set
// at the size `full` and at the smallest size the text may be read at,
// `floor`. The first form that fits at the floor is taken, so every part is
// set smaller together before any part gives way; the last form is taken
// when none does, at whatever size it needs, because what it holds is never
// cut. The size is `full` when the form fits as it is; otherwise the one
// that makes it fit, worked out from its width at `full`, rounded down to a
// twentieth of a pixel and never under the floor for a form that fits there.
// With no room to judge by, the whole line at full size.
export function fitLine(room, atFull, atFloor, full, floor) {
  if (!(room > 0) || !atFull.length) return { form: 0, size: full };
  const fits = atFloor.findIndex(w => w <= room);
  const form = fits < 0 ? atFull.length - 1 : fits;
  if (atFull[form] <= room) return { form, size: full };
  const size = Math.floor(full * room / atFull[form] * 20) / 20;
  return { form, size: fits < 0 ? size : Math.max(floor, size) };
}

// How much smaller the figures of a row of ruled cells, each a label over a
// figure, have to be set for the row to fit `room`: a share of their full
// size, 1 when they fit as they are. Each cell is { pad, label, figure }: its
// padding and rule, and the widths of its label and of its figure at full
// size, in px. A cell is as wide as the wider of the two and a label keeps
// its size, so only the figures give way, all by the same share. With
// `equal` the cells share the room equally and each has to fit its own
// share; otherwise each takes what it needs and their sum has to fit.
// Null when the labels alone are too long for the room, which no size of
// figure can mend.
export function figureScale(room, cells, equal) {
  if (equal) {
    const share = room / cells.length;
    if (cells.some(c => c.pad + c.label > share)) return null;
    return Math.min(1, ...cells.map(c => (share - c.pad) / c.figure));
  }
  const left = room - cells.reduce((sum, c) => sum + c.pad, 0);
  if (cells.reduce((sum, c) => sum + c.label, 0) > left) return null;
  // The cells whose figures are wider than their labels at a given share
  // are the first few in this order, whatever the share. The row's width
  // is at least what any such few would make it (their figures, the other
  // cells' labels), and exactly what the true few make it, so the share
  // that fits is the smallest any count of them allows.
  const led = [...cells].sort((a, b) => a.label / a.figure - b.label / b.figure);
  let scale = 1, figures = 0, labels = led.reduce((sum, c) => sum + c.label, 0);
  for (const c of led) {
    figures += c.figure;
    labels -= c.label;
    scale = Math.min(scale, (left - labels) / figures);
  }
  return scale;
}

// Whole calendar days from the statement date to `today`, never negative.
// null when the batch has no statement date: how long it has waited is then
// unknown, which is not the same as no wait. Counted in UTC so a clock change
// between the two dates cannot shorten a day.
export function waitedDays(batch, today) {
  if (!batch.settled_on) return null;
  const at = s => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
  return Math.max(0, Math.round((at(today) - at(batch.settled_on)) / 864e5));
}
