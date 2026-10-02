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

// A statement is named by its own date, `settled_on`: the day it was
// confirmed. `paid_on` is the bank's value date and names the transfer, not
// the statement. `sameDayIndex` is the statement's place among those of one
// platform sharing that date, from 0; a later one is numbered so a name never
// stands for two statements. A batch stored without a date gets no date
// rather than a borrowed one.
export function statementName(batch, sameDayIndex = 0) {
  const on = batch.settled_on;
  return (on ? mdSlash(on) + ' ' : '') + '結算' +
    (sameDayIndex > 0 ? ' (' + (sameDayIndex + 1) + ')' : '');
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
    if (a.ahead && a.date.slice(0, 7) === monthKey) sum += cents(a.amount);
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

// Whole calendar days from the statement date to `today`, never negative.
// null when the batch has no statement date: how long it has waited is then
// unknown, which is not the same as no wait. Counted in UTC so a clock change
// between the two dates cannot shorten a day.
export function waitedDays(batch, today) {
  if (!batch.settled_on) return null;
  const at = s => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
  return Math.max(0, Math.round((at(today) - at(batch.settled_on)) / 864e5));
}
