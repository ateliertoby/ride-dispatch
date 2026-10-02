import test from 'node:test';
import assert from 'node:assert/strict';

import {
  dayState, cellFigure, keyFigure, statementName, dayRunsLabel, countedDay, inMonthPart, fareGap,
  otherLines, fitLine, waitedDays, collectedOrder,
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

test('collectedOrder is newest first by the latest due date, not by the day confirmed', () => {
  const rows = [
    stmt(['2026-09-14'], { id: 1, state: 'paid', settled_on: '2026-10-21' }),
    stmt(['2026-09-28', '2026-10-02'], { id: 2, state: 'paid', settled_on: '2026-10-05' }),
    stmt(['2026-09-29', '2026-09-30'], { id: 3, state: 'paid', settled_on: '2026-10-20' }),
  ];
  assert.deepEqual(collectedOrder(rows).map(b => b.id), [2, 3, 1]);
  // The latest is the greatest, in whatever order the dates arrive.
  rows[0].due_dates = ['2026-10-03', '2026-09-14'];
  assert.deepEqual(collectedOrder(rows).map(b => b.id), [1, 2, 3]);
});

test('collectedOrder places a statement that prints no due date by the day it was confirmed', () => {
  const rows = [
    stmt(['2026-10-01'], { id: 1, state: 'paid' }),
    stmt([], { id: 2, state: 'paid', settled_on: '2026-10-09' }),
    { id: 3, state: 'paid', settled_on: '2026-09-30' },
    stmt([], { id: 4, state: 'paid', settled_on: null }),
  ];
  assert.deepEqual(collectedOrder(rows).map(b => b.id), [2, 1, 3, 4]);
});

test('collectedOrder leads with a statement paid short, and breaks a tie by the later statement', () => {
  const rows = [
    stmt(['2026-10-02'], { id: 1, state: 'paid' }),
    stmt(['2026-09-01'], { id: 2, state: 'partial' }),
    stmt(['2026-10-02'], { id: 3, state: 'paid' }),
    stmt(['2026-09-20'], { id: 4, state: 'partial' }),
  ];
  assert.deepEqual(collectedOrder(rows).map(b => b.id), [4, 2, 3, 1]);
});

test('collectedOrder leaves the list it was given as it was', () => {
  const rows = [stmt(['2026-09-01'], { id: 1, state: 'paid' }), stmt(['2026-10-01'], { id: 2, state: 'paid' })];
  collectedOrder(rows);
  assert.deepEqual(rows.map(b => b.id), [1, 2]);
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
      { order_ref: 'm9', date: '2026-10-02', trip_date: '2026-10-02', amount: 40, ahead: true },
      // A line of the statement's own that belongs to no trip's fare.
      { order_ref: 'm8', date: '2026-10-03', amount: -25 },
    ],
  };
  assert.equal(inMonthPart(batch, '2026-09'), 900);
  assert.equal(inMonthPart(batch, '2026-10'), 40);
});

test('countedDay is the day of the trip a 舉牌 was paid ahead of, and of no other line', () => {
  assert.equal(countedDay({ order_ref: 'm9', date: '2026-09-30', trip_date: '2026-09-30', amount: 40, ahead: true }), '2026-09-30');
  // Moved since the statement: the trip's day, not the day the line was printed under.
  assert.equal(countedDay({ order_ref: 'm9', date: '2026-09-30', trip_date: '2026-10-02', amount: 40, ahead: true }), '2026-10-02');
  // The trip is no longer an order the totals read.
  assert.equal(countedDay({ order_ref: 'm9', date: '2026-09-30', amount: 40, ahead: true }), null);
  // A line that is no trip's fare is counted on no day, whatever it carries.
  assert.equal(countedDay({ order_ref: 'm8', date: '2026-09-30', amount: -25 }), null);
  assert.equal(countedDay({ order_ref: 'm8', date: '2026-09-30', trip_date: '2026-09-30', amount: -25 }), null);
});

test('inMonthPart follows a held-back trip to the month it was moved to', () => {
  // The statement printed the 舉牌 under 30 September, and the trip has since
  // been moved to 2 October: the server counts the 40 in October.
  const batch = {
    id: 19, state: 'awaiting', confirmed_amount: 940,
    orders: [leg('m1', '2026-09-29 09:00', 500), leg('m2', '2026-09-30 09:00', 400)],
    adjustments: [{ order_ref: 'm9', date: '2026-09-30', trip_date: '2026-10-02', amount: 40, ahead: true }],
  };
  assert.equal(inMonthPart(batch, '2026-09'), 900);
  assert.equal(inMonthPart(batch, '2026-10'), 40);
  // Its parts of the two months are still the whole of what the keys count.
  assert.equal(inMonthPart(batch, '2026-09') + inMonthPart(batch, '2026-10') + otherLines(batch) + fareGap(batch), 940);
});

test('inMonthPart counts a 舉牌 in no month once its trip is cancelled', () => {
  const batch = {
    id: 20, state: 'awaiting', confirmed_amount: 940,
    orders: [leg('m1', '2026-09-29 09:00', 500), leg('m2', '2026-09-30 09:00', 400)],
    adjustments: [{ order_ref: 'm9', date: '2026-09-30', amount: 40, ahead: true }],
  };
  assert.equal(inMonthPart(batch, '2026-09'), 900);
  assert.equal(inMonthPart(batch, '2026-10'), 0);
  // It is then a line no order carries, and the row says so.
  assert.equal(otherLines(batch), 40);
  assert.equal(fareGap(batch), 0);
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

test('otherLines is the signed sum of the lines that are no order\'s fare', () => {
  const orders = [leg('s1', '2026-10-05 09:00', 500)];
  assert.equal(otherLines({ id: 20, orders }), 0);
  assert.equal(otherLines({ id: 21, orders, adjustments: [] }), 0);
  // A 判罰 against a trip another statement holds.
  assert.equal(otherLines({ id: 22, orders, adjustments: [{ order_ref: 's8', date: '2026-09-20', amount: -63.45 }] }), -63.45);
  // The 免責 line that cancels it: the pair is nothing.
  assert.equal(otherLines({ id: 23, orders, adjustments: [
    { order_ref: 's8', date: '2026-09-20', amount: -63.45 }, { order_ref: 's8', date: '2026-09-20', amount: 63.45 },
  ] }), 0);
  assert.equal(otherLines({ id: 24, orders, adjustments: [
    { order_ref: 's8', date: '2026-09-20', amount: -0.1 }, { order_ref: 's7', date: '2026-09-21', amount: -0.2 },
    { order_ref: 's6', date: '2026-09-22', amount: 25.5 },
  ] }), 25.2);
});

test('otherLines leaves out a 舉牌 paid ahead, which the totals count under its trip', () => {
  const batch = { id: 25, orders: [leg('t1', '2026-10-05 09:00', 500)], adjustments: [
    { order_ref: 't9', date: '2026-10-08', trip_date: '2026-10-08', amount: 40, ahead: true },
    { order_ref: 't8', date: '2026-09-20', amount: -30 },
  ] };
  assert.equal(otherLines(batch), -30);
});

test('a statement figure is what the keys count of it, its other lines and its gap', () => {
  // What the keys count of a statement: its part of every month it touches.
  const inKeys = b => {
    const months = new Set([...b.orders.map(o => o.scheduled_time.slice(0, 7)),
                            ...(b.adjustments || []).flatMap(a => [a.date, a.trip_date || a.date])
                              .map(d => d.slice(0, 7))]);
    return [...months].reduce((sum, m) => sum + Math.round(inMonthPart(b, m) * 100), 0);
  };
  const c = n => Math.round(n * 100);
  const legs = [leg('u1', '2026-09-29 09:00', 500.5), leg('u2', '2026-09-30 21:00', 400.05, { penalty_fee: 20 }),
                leg('u3', '2026-10-01 08:00', 480, { banner_fee: 40, paid_ahead: 40 })];
  const ahead = { order_ref: 'u9', date: '2026-10-02', trip_date: '2026-10-02', amount: 40, ahead: true };
  const moved = { order_ref: 'u6', date: '2026-10-02', trip_date: '2026-11-03', amount: 35, ahead: true };
  const dropped = { order_ref: 'u5', date: '2026-10-02', amount: 45, ahead: true };
  const fine = { order_ref: 'u8', date: '2026-08-20', amount: -63.45 };
  const waiver = { order_ref: 'u8', date: '2026-08-20', amount: 63.45 };
  const unknown = { order_ref: 'u7', date: '2026-09-28', amount: -25.1 };
  const held = 500.5 + 380.05 + 480;
  const cases = [
    // Its legs to the cent, and nothing else.
    { id: 30, confirmed_amount: held, orders: legs },
    // A line of its own, agreed with: the row differs from the keys by it alone.
    { id: 31, confirmed_amount: held - 63.45, orders: legs, adjustments: [fine] },
    // A pair that nets to nothing, beside a 舉牌 paid ahead.
    { id: 32, confirmed_amount: held + 40, orders: legs, adjustments: [ahead, fine, waiver] },
    // Lines of its own and a figure the platform put 12.3 under the book's.
    { id: 33, confirmed_amount: held + 40 - 63.45 - 25.1 - 12.3, orders: legs, adjustments: [ahead, fine, unknown] },
    // Confirmed above the book, with no line at all.
    { id: 34, confirmed_amount: held + 0.01, orders: legs },
    // Nothing but lines.
    { id: 35, confirmed_amount: 40 - 25.1, orders: [], adjustments: [ahead, unknown] },
    // A 舉牌 paid ahead of a trip moved to a third month, and one of a trip
    // cancelled since.
    { id: 36, confirmed_amount: held + 35 + 45, orders: legs, adjustments: [moved, dropped] },
  ];
  for (const b of cases) {
    assert.equal(c(b.confirmed_amount), inKeys(b) + c(otherLines(b)) + c(fareGap(b)), 'statement ' + b.id);
  }
  // Each part is the one meant, not only their sum.
  assert.deepEqual([inKeys(cases[3]) / 100, otherLines(cases[3]), fareGap(cases[3])], [1400.55, -88.55, -12.3]);
  assert.deepEqual([otherLines(cases[1]), fareGap(cases[1])], [-63.45, 0]);
  assert.deepEqual([otherLines(cases[2]), fareGap(cases[2])], [0, 0]);
  assert.deepEqual([inKeys(cases[6]) / 100, inMonthPart(cases[6], '2026-11'), inMonthPart(cases[6], '2026-10'),
                    otherLines(cases[6]), fareGap(cases[6])], [1395.55, 35, 480, 45, 0]);
});

// The widths of a line's three forms at the full size, 13px, and what they
// come to at the floor, 11px, when widths scale with the size.
const at11 = widths => widths.map(w => w * 11 / 13);
const fit = (room, widths) => fitLine(room, widths, at11(widths), 13, 11);

test('fitLine sets the whole line at full size when it fits', () => {
  assert.deepEqual(fit(300, [280, 240, 200]), { form: 0, size: 13 });
  assert.deepEqual(fit(280, [280, 240, 200]), { form: 0, size: 13 });
});

test('fitLine sets every part smaller together before any part gives way', () => {
  // 13 * 266 / 300 = 11.52..., rounded down to a twentieth.
  assert.deepEqual(fit(266, [300, 260, 220]), { form: 0, size: 11.5 });
  // Exactly at the floor is still the whole line.
  assert.deepEqual(fit(275, [325, 260, 220]), { form: 0, size: 11 });
});

test('fitLine gives up one part at a time, and only below the floor', () => {
  // The whole line would need 10.4px, so its last part goes; the rest fits as it is.
  assert.deepEqual(fit(266, [332, 260, 220]), { form: 1, size: 13 });
  // Without that part the line still has to be set smaller, above the floor.
  assert.deepEqual(fit(266, [360, 300, 220]), { form: 1, size: 11.5 });
  // Two parts go only when one is not enough.
  assert.deepEqual(fit(266, [420, 340, 280]), { form: 2, size: 12.35 });
});

test('fitLine decides by the widths measured at the floor, not by scaling', () => {
  // Scaled from its full width the whole line would need 10.95px, but set
  // at the floor it is measured to fit: it stays whole, at the floor.
  assert.deepEqual(fitLine(266, [316, 260, 220], [265.5, 219, 186], 13, 11), { form: 0, size: 11 });
  // Scaled, it would fit at 11.05px; measured at the floor it does not.
  assert.deepEqual(fitLine(266, [312.5, 260, 220], [266.4, 219, 186], 13, 11), { form: 1, size: 13 });
});

test('fitLine never cuts the last form: it is set as small as it takes', () => {
  assert.deepEqual(fit(200, [420, 340, 280]), { form: 2, size: 9.25 });
});

test('fitLine never asks for more room than its widths say there is', () => {
  for (const [room, widths] of [[266, [300.4, 260, 220]], [251.3, [287.9, 250, 201]], [199.99, [333.33, 301, 250.5]]]) {
    const { form, size } = fit(room, widths);
    assert.ok(widths[form] * size / 13 <= room, room + ' ' + widths);
  }
});

test('fitLine with no room to judge by leaves the whole line at full size', () => {
  assert.deepEqual(fit(0, [300, 260, 220]), { form: 0, size: 13 });
  assert.deepEqual(fitLine(266, [], [], 13, 11), { form: 0, size: 13 });
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
