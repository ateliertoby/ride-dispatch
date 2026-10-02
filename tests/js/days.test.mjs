import test from 'node:test';
import assert from 'node:assert/strict';

import {
  dayState, cellFigure, keyFigure, statementName, dayRunsLabel, inMonthPart, fareGap, waitedDays,
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

test('keyFigure writes $, the thousands comma and always two cents', () => {
  assert.deepEqual(keyFigure(19103.5), { dollars: '$19,103', cents: '50' });
  assert.deepEqual(keyFigure(5320), { dollars: '$5,320', cents: '00' });
  assert.deepEqual(keyFigure(940), { dollars: '$940', cents: '00' });
  assert.deepEqual(keyFigure(0), { dollars: '$0', cents: '00' });
  assert.deepEqual(keyFigure(1008.05), { dollars: '$1,008', cents: '05' });
  assert.deepEqual(keyFigure(1234567.89), { dollars: '$1,234,567', cents: '89' });
  assert.deepEqual(keyFigure(999.99), { dollars: '$999', cents: '99' });
  assert.deepEqual(keyFigure(1000), { dollars: '$1,000', cents: '00' });
});

test('keyFigure is exact to the cent through float noise', () => {
  assert.deepEqual(keyFigure(0.1 + 0.2), { dollars: '$0', cents: '30' });
  assert.deepEqual(keyFigure(999.9999999999999), { dollars: '$1,000', cents: '00' });
});

test('keyFigure puts the minus sign outside the symbol', () => {
  // U+2212 MINUS SIGN, as money() writes it.
  assert.deepEqual(keyFigure(-1097.38), { dollars: '−$1,097', cents: '38' });
});

// A statement as the payload carries it: the due dates its rows print, and
// the day it was confirmed.
function stmt(due, extra = {}) {
  return { id: 41, settled_on: '2026-10-20', paid_on: '2026-10-22', due_dates: due, ...extra };
}

test('statementName is the one due date a statement prints', () => {
  assert.equal(statementName(stmt(['2026-09-14'])), '9/14 結算');
  assert.equal(statementName(stmt(['2026-10-01'])), '10/1 結算');
  // Not the day it was confirmed, and not the day the bank paid it.
  assert.equal(statementName(stmt(['2026-09-14'], { settled_on: '2026-09-16', paid_on: '2026-09-18' })), '9/14 結算');
});

test('statementName writes consecutive due dates as a run, the month once', () => {
  assert.equal(statementName(stmt(['2026-09-12', '2026-09-13'])), '9/12–13 結算');
  assert.equal(statementName(stmt(['2026-09-12', '2026-09-13', '2026-09-14'])), '9/12–14 結算');
});

test('statementName lists due dates apart, the month once while it stays the same', () => {
  assert.equal(statementName(stmt(['2026-09-13', '2026-09-15'])), '9/13、15 結算');
  assert.equal(statementName(stmt(['2026-09-08', '2026-09-12', '2026-09-13', '2026-09-20'])), '9/8、12–13、20 結算');
});

test('statementName names a month again each time the month changes', () => {
  assert.equal(statementName(stmt(['2026-09-30', '2026-10-01'])), '9/30–10/1 結算');
  assert.equal(statementName(stmt(['2026-09-29', '2026-10-02'])), '9/29、10/2 結算');
  // A bare number is a day of the last month written, here October.
  assert.equal(statementName(stmt(['2026-09-30', '2026-10-01', '2026-10-03'])), '9/30–10/1、3 結算');
  assert.equal(statementName(stmt(['2026-09-28', '2026-09-30', '2026-10-01', '2026-10-02', '2026-10-05'])),
    '9/28、30–10/2、5 結算');
  assert.equal(statementName(stmt(['2026-09-29', '2026-09-30', '2026-10-02', '2026-10-03'])), '9/29–30、10/2–3 結算');
  // Across a year's end the month changes as anywhere else.
  assert.equal(statementName(stmt(['2026-12-31', '2027-01-01'])), '12/31–1/1 結算');
  // The same month of another year is another month.
  assert.equal(statementName(stmt(['2026-01-31', '2027-01-31'])), '1/31、1/31 結算');
});

test('statementName sorts the due dates and takes each once', () => {
  assert.equal(statementName(stmt(['2026-09-15', '2026-09-13', '2026-09-15'])), '9/13、15 結算');
});

test('statementName is numbered only by its place among statements of one name', () => {
  const b = stmt(['2026-09-12', '2026-09-13']);
  assert.equal(statementName(b, 0), '9/12–13 結算');
  assert.equal(statementName(b, 1), '9/12–13 結算 (2)');
  assert.equal(statementName(b, 2), '9/12–13 結算 (3)');
});

test('statementName falls back on the confirm date when the statement prints no due date', () => {
  const b = { id: 41, settled_on: '2026-10-09', paid_on: '2026-10-12' };
  assert.equal(statementName(b), '10/9 結算');
  assert.equal(statementName({ ...b, due_dates: [] }, 0), '10/9 結算');
  assert.equal(statementName(b, 1), '10/9 結算 (2)');
});

test('statementName of a batch with neither date has no date in it', () => {
  assert.equal(statementName({ id: 42, settled_on: null, paid_on: '2026-10-12', due_dates: [] }, 0), '結算');
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

test('inMonthPart counts a leg at what its own statement is owed for it', () => {
  // 40 of the 520 arrived on an earlier statement, which counts it.
  const batch = { id: 10, state: 'awaiting', confirmed_amount: 480,
                  orders: [leg('k1', '2026-10-05 09:00', 480, { banner_fee: 40, paid_ahead: 40 })] };
  assert.equal(inMonthPart(batch, '2026-10'), 480);
});

test('inMonthPart counts a 舉牌 the statement paid ahead in the month of its trip', () => {
  const batch = {
    id: 11, state: 'awaiting', confirmed_amount: 940,
    orders: [leg('m1', '2026-09-29 09:00', 500), leg('m2', '2026-09-30 09:00', 400)],
    adjustments: [
      { order_ref: 'm9', date: '2026-10-02', amount: 40, ahead: true },
      // A line of the statement's own that belongs to no trip's fare.
      { order_ref: 'm8', date: '2026-10-03', amount: -25 },
    ],
  };
  assert.equal(inMonthPart(batch, '2026-09'), 900);
  assert.equal(inMonthPart(batch, '2026-10'), 40);
});

test('fareGap is nothing when the statement is its legs to the cent', () => {
  const batch = { id: 12, confirmed_amount: 700.55, orders: [
    leg('n1', '2026-10-02 09:00', 420.5), leg('n2', '2026-10-02 21:00', 300.05, { penalty_fee: 20 }),
  ] };
  assert.equal(fareGap(batch), 0);
  // Float noise in the stored figure is not a difference.
  assert.equal(fareGap({ id: 13, confirmed_amount: 0.1 + 0.2, orders: [leg('n3', '2026-10-02 09:00', 0.3)] }), 0);
});

test('fareGap is the statement figure less the legs, signed', () => {
  const orders = [leg('p1', '2026-10-05 09:00', 470, { banner_fee: 40 }), leg('p2', '2026-10-05 18:00', 780)];
  assert.equal(fareGap({ id: 14, confirmed_amount: 1270, orders }), -20);
  assert.equal(fareGap({ id: 15, confirmed_amount: 1310.5, orders }), 20.5);
  assert.equal(fareGap({ id: 16, confirmed_amount: 1290.01, orders }), 0.01);
});

test('fareGap does not count the lines a statement carries itself', () => {
  const batch = {
    id: 17, confirmed_amount: 1385 + 40 - 63.45 + 63.45,
    orders: [leg('q1', '2026-10-05 09:00', 515), leg('q2', '2026-10-05 17:00', 395),
             leg('q3', '2026-10-06 12:00', 475)],
    adjustments: [
      { order_ref: 'q9', date: '2026-10-08', amount: 40, ahead: true },
      { order_ref: 'q8', date: '2026-09-20', amount: -63.45 },
      { order_ref: 'q8', date: '2026-09-20', amount: 63.45 },
    ],
  };
  assert.equal(fareGap(batch), 0);
  assert.equal(fareGap({ ...batch, confirmed_amount: 1400 }), -25);
});

test('fareGap counts a leg net of the 舉牌 an earlier statement paid', () => {
  const batch = { id: 18, confirmed_amount: 480,
                  orders: [leg('r1', '2026-10-05 09:00', 480, { banner_fee: 40, paid_ahead: 40 })] };
  assert.equal(fareGap(batch), 0);
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
