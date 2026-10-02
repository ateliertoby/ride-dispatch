import test from 'node:test';
import assert from 'node:assert/strict';

import {
  dayState, cellFigure, statementName, dayRunsLabel, inMonthPart, waitedDays,
} from '../../static/js/settle/days.js';

// Every order here is invented. A 接机 is worth price + banner_fee - penalty_fee.
// The payload's clock: an order scheduled at or after it has not been driven.
const NOW = '2026-10-22 12:00:00';
function leg(id, when, price, extra = {}) {
  return { order_id: id, scheduled_time: when, service_type: '接机', price, ...extra };
}
// The lookup the page hands in: order id -> the batch holding it, or null.
function lookup(batches) {
  const by = new Map();
  for (const b of batches) for (const o of b.orders) by.set(o.order_id, b);
  return id => by.get(id) || null;
}
const nowhere = () => null;

test('dayState of a day with no orders', () => {
  assert.deepEqual(dayState([], nowhere, NOW), { n: 0, total: 0, loose: 0, state: 'none' });
});

test('dayState of a mixed day shows the whole fare and the worst state', () => {
  const settled = [leg('a1', '2026-10-07 09:00', 380), leg('a2', '2026-10-07 13:00', 300)];
  const loose = leg('a3', '2026-10-07 18:00', 400);
  const paid = { id: 1, state: 'paid', orders: settled };
  assert.deepEqual(dayState([...settled, loose], lookup([paid]), NOW),
    { n: 3, total: 1080, loose: 400, state: 'unsettled' });
});

test('dayState is short when an order is on a partial batch and nothing is loose', () => {
  const orders = [leg('b1', '2026-10-06 08:00', 500), leg('b2', '2026-10-06 20:00', 420)];
  const partial = { id: 2, state: 'partial', orders: [orders[0]] };
  const paid = { id: 3, state: 'paid', orders: [orders[1]] };
  assert.deepEqual(dayState(orders, lookup([partial, paid]), NOW),
    { n: 2, total: 920, loose: 0, state: 'short' });
});

test('dayState ranks short over awaiting and awaiting over received', () => {
  const o = [leg('c1', '2026-10-09 08:00', 100), leg('c2', '2026-10-09 09:00', 100),
             leg('c3', '2026-10-09 10:00', 100)];
  const partial = { id: 4, state: 'partial', orders: [o[0]] };
  const awaiting = { id: 5, state: 'awaiting', orders: [o[1]] };
  const paid = { id: 6, state: 'paid', orders: [o[2]] };
  assert.equal(dayState(o, lookup([partial, awaiting, paid]), NOW).state, 'short');
  assert.equal(dayState(o.slice(1), lookup([awaiting, paid]), NOW).state, 'awaiting');
  assert.equal(dayState(o.slice(2), lookup([paid]), NOW).state, 'received');
  // The order the day's legs come in does not change which state wins.
  assert.equal(dayState([o[2], o[1], o[0]], lookup([partial, awaiting, paid]), NOW).state, 'short');
});

test('dayState of a day wholly after now is future and owes nothing yet', () => {
  const ahead = [leg('d1', '2026-10-23 09:00', 380)];
  assert.deepEqual(dayState(ahead, nowhere, NOW),
    { n: 1, total: 380, loose: 0, state: 'future' });
  // Later today counts as not driven; the minute before now counts as driven.
  assert.deepEqual(dayState([leg('d2', '2026-10-22 23:00', 380)], nowhere, NOW),
    { n: 1, total: 380, loose: 0, state: 'future' });
  assert.equal(dayState([leg('d3', '2026-10-22 12:00:00', 380)], nowhere, NOW).state, 'future');
  assert.deepEqual(dayState([leg('d4', '2026-10-22 11:59', 380)], nowhere, NOW),
    { n: 1, total: 380, loose: 380, state: 'unsettled' });
});

test('dayState of today counts only the orders already driven', () => {
  // The server's month totals leave an undriven order out, so a day's state
  // must too, or the calendar would mark a day the 未結算 total does not hold.
  const driven = leg('d5', '2026-10-22 08:00', 380);
  const later = leg('d6', '2026-10-22 20:00', 420);
  const paid = { id: 10, state: 'paid', orders: [driven] };
  assert.deepEqual(dayState([driven, later], lookup([paid]), NOW),
    { n: 2, total: 800, loose: 0, state: 'received' });
  // An undriven order already on a batch does not lend the day its state.
  const partial = { id: 11, state: 'partial', orders: [later] };
  assert.equal(dayState([driven, later], lookup([paid, partial]), NOW).state, 'received');
});

test('dayState takes money paid ahead off the loose part only', () => {
  // The 舉牌 another batch already carries is not owed again for this leg.
  const o = leg('e1', '2026-10-12 09:00', 380, { banner_fee: 50, paid_ahead: 50 });
  assert.deepEqual(dayState([o], nowhere, NOW),
    { n: 1, total: 430, loose: 380, state: 'unsettled' });
});

test('dayState sums to the cent', () => {
  const orders = [leg('f1', '2026-10-13 09:00', 0.1), leg('f2', '2026-10-13 10:00', 0.2),
                  leg('f3', '2026-10-13 11:00', 1008.05, { penalty_fee: 0.3 })];
  const got = dayState(orders, nowhere, NOW);
  assert.equal(got.total, 1008.05);
  assert.equal(got.loose, 1008.05);
});

test('cellFigure splits dollars from cents and writes neither $ nor comma', () => {
  assert.deepEqual(cellFigure(1234.5), { dollars: '1234', cents: '50' });
  assert.deepEqual(cellFigure(940), { dollars: '940', cents: '' });
  assert.deepEqual(cellFigure(1008.05), { dollars: '1008', cents: '05' });
  assert.deepEqual(cellFigure(12345.67), { dollars: '12345', cents: '67' });
  assert.deepEqual(cellFigure(0), { dollars: '0', cents: '' });
  assert.deepEqual(cellFigure(0.07), { dollars: '0', cents: '07' });
});

test('cellFigure is not fooled by float noise', () => {
  assert.deepEqual(cellFigure(0.1 + 0.2), { dollars: '0', cents: '30' });
  assert.deepEqual(cellFigure(1080.0000000000002), { dollars: '1080', cents: '' });
  assert.deepEqual(cellFigure(19.999999999999996), { dollars: '20', cents: '' });
});

test('cellFigure puts a minus sign before a day the fines outweigh', () => {
  // U+2212 MINUS SIGN, as money() writes it.
  assert.deepEqual(cellFigure(-97.38), { dollars: '−97', cents: '38' });
  assert.deepEqual(cellFigure(-0.5), { dollars: '−0', cents: '50' });
});

test('statementName is the statement date, numbered when a date is shared', () => {
  const b = { id: 41, settled_on: '2026-10-09', paid_on: '2026-10-12' };
  assert.equal(statementName(b, 0), '10/9 結算');
  assert.equal(statementName(b), '10/9 結算');
  assert.equal(statementName(b, 1), '10/9 結算 (2)');
  assert.equal(statementName(b, 2), '10/9 結算 (3)');
});

test('statementName of a batch with no statement date has no date in it', () => {
  assert.equal(statementName({ id: 42, settled_on: null, paid_on: '2026-10-12' }, 0), '結算');
  assert.equal(statementName({ id: 43 }, 1), '結算 (2)');
});

test('dayRunsLabel inside the month names days only', () => {
  assert.equal(dayRunsLabel(['2026-10-10', '2026-10-18', '2026-10-19', '2026-10-20'], '2026-10'),
    '10日、18–20日');
  assert.equal(dayRunsLabel(['2026-10-06'], '2026-10'), '6日');
  assert.equal(dayRunsLabel(['2026-10-01', '2026-10-02'], '2026-10'), '1–2日');
  assert.equal(dayRunsLabel([], '2026-10'), '');
});

test('dayRunsLabel sorts and takes each date once', () => {
  assert.equal(dayRunsLabel(['2026-10-20', '2026-10-18', '2026-10-19', '2026-10-18', '2026-10-10'],
    '2026-10'), '10日、18–20日');
});

test('dayRunsLabel names the month of every run once a date leaves the month', () => {
  assert.equal(dayRunsLabel(['2026-09-27', '2026-10-02'], '2026-10'), '9/27、10/2');
  assert.equal(dayRunsLabel(['2026-09-29', '2026-09-30', '2026-10-01'], '2026-10'), '9/29–10/1');
  // A run that stays in one month names it once.
  assert.equal(dayRunsLabel(['2026-09-27', '2026-09-28', '2026-10-02', '2026-10-03'], '2026-10'),
    '9/27–28、10/2–3');
  // The same dates read from the other month they touch.
  assert.equal(dayRunsLabel(['2026-09-27', '2026-10-02'], '2026-09'), '9/27、10/2');
  // All in one month, but not the one shown.
  assert.equal(dayRunsLabel(['2026-09-10', '2026-09-11'], '2026-10'), '9/10–11');
});

test('inMonthPart counts only the orders scheduled in the month', () => {
  const batch = {
    id: 7, state: 'awaiting', confirmed_amount: 1500,
    orders: [
      leg('g1', '2026-09-27 09:00', 380, { banner_fee: 50 }),
      leg('g2', '2026-10-02 09:00', 420.5),
      leg('g3', '2026-10-02 21:00', 300.05, { penalty_fee: 20 }),
    ],
  };
  assert.equal(inMonthPart(batch, '2026-10'), 700.55);
  assert.equal(inMonthPart(batch, '2026-09'), 430);
  assert.equal(inMonthPart(batch, '2026-11'), 0);
  assert.equal(inMonthPart({ id: 8, orders: [] }, '2026-10'), 0);
});

test('inMonthPart follows the orders, not the statement figure', () => {
  // The statement was confirmed for less than its legs are worth.
  const batch = { id: 9, state: 'partial', confirmed_amount: 300,
                  orders: [leg('h1', '2026-10-05 09:00', 380)] };
  assert.equal(inMonthPart(batch, '2026-10'), 380);
});

test('waitedDays counts whole calendar days since the statement date', () => {
  assert.equal(waitedDays({ settled_on: '2026-10-09' }, '2026-10-22'), 13);
  assert.equal(waitedDays({ settled_on: '2026-10-22' }, '2026-10-22'), 0);
  assert.equal(waitedDays({ settled_on: '2026-09-29' }, '2026-10-01'), 2);
  assert.equal(waitedDays({ settled_on: '2025-12-31' }, '2026-01-01'), 1);
  assert.equal(waitedDays({ settled_on: '2028-02-28' }, '2028-03-01'), 2);
});

test('waitedDays is never negative', () => {
  assert.equal(waitedDays({ settled_on: '2026-10-23' }, '2026-10-22'), 0);
});

test('waitedDays of a batch with no statement date is unknown, not zero', () => {
  assert.equal(waitedDays({ settled_on: null }, '2026-10-22'), null);
  assert.equal(waitedDays({}, '2026-10-22'), null);
});
