import test from 'node:test';
import assert from 'node:assert/strict';

import {
  pad2, addDays, dow, monthKey, addMonths, monthEnd, monthsBetween, mdLabel,
  mdSlash, round2, tailId, groupId, runsOf, dateSpanLabel,
} from '../../static/js/dates.js';

test('pad2', () => {
  assert.equal(pad2(3), '03');
  assert.equal(pad2(12), '12');
});

test('addDays crosses a month end, a year end and a leap day', () => {
  assert.equal(addDays('2026-07-04', 1), '2026-07-05');
  assert.equal(addDays('2026-07-04', 0), '2026-07-04');
  assert.equal(addDays('2026-01-31', 1), '2026-02-01');
  assert.equal(addDays('2026-12-31', 1), '2027-01-01');
  assert.equal(addDays('2026-01-01', -1), '2025-12-31');
  assert.equal(addDays('2028-02-28', 1), '2028-02-29');
  assert.equal(addDays('2028-02-29', 1), '2028-03-01');
  assert.equal(addDays('2027-02-28', 1), '2027-03-01');
  assert.equal(addDays('2028-03-01', -1), '2028-02-29');
  assert.equal(addDays('2026-07-04', 7), '2026-07-11');
  assert.equal(addDays('2026-07-04', -6), '2026-06-28');
});

test('dow counts from Sunday', () => {
  assert.equal(dow('2026-10-04'), 0);
  assert.equal(dow('2026-10-01'), 4);
  assert.equal(dow('2026-10-03'), 6);
});

test('monthKey', () => {
  assert.equal(monthKey('2026-10-01'), '2026-10');
});

test('addMonths crosses a year end both ways', () => {
  assert.equal(addMonths('2026-06', 0), '2026-06');
  assert.equal(addMonths('2026-06', 1), '2026-07');
  assert.equal(addMonths('2026-12', 1), '2027-01');
  assert.equal(addMonths('2026-01', -1), '2025-12');
  assert.equal(addMonths('2026-11', 14), '2028-01');
  assert.equal(addMonths('2026-01', -13), '2024-12');
  assert.equal(addMonths('2026-12', -12), '2025-12');
});

test('monthEnd knows February', () => {
  assert.equal(monthEnd('2028-02'), '2028-02-29');
  assert.equal(monthEnd('2027-02'), '2027-02-28');
  assert.equal(monthEnd('2026-04'), '2026-04-30');
  assert.equal(monthEnd('2026-12'), '2026-12-31');
});

test('monthsBetween is inclusive, and empty when the range runs backwards', () => {
  assert.deepEqual(monthsBetween('2026-11', '2027-02'),
    ['2026-11', '2026-12', '2027-01', '2027-02']);
  assert.deepEqual(monthsBetween('2026-06', '2026-06'), ['2026-06']);
  assert.deepEqual(monthsBetween('2027-02', '2026-11'), []);
});

test('mdLabel and mdSlash drop the leading zeros', () => {
  assert.equal(mdLabel('2026-07-05'), '7月5日');
  assert.equal(mdLabel('2026-12-25'), '12月25日');
  assert.equal(mdSlash('2026-07-05'), '7/5');
  assert.equal(mdSlash('2026-12-25'), '12/25');
});

test('round2 clears float noise', () => {
  assert.equal(0.1 + 0.2 === 0.3, false);
  assert.equal(round2(0.1 + 0.2), 0.3);
  assert.equal(round2(12), 12);
  assert.equal(round2(12.344), 12.34);
  assert.equal(round2(12.346), 12.35);
});

test('round2 on a half cent goes where the float lands', () => {
  // 1.125 is exact in binary and rounds up; 1.005 times 100 comes out a hair
  // under 100.5 and rounds down. A negative half goes towards zero, as
  // Math.round does.
  assert.equal(round2(1.125), 1.13);
  assert.equal(round2(1.005), 1);
  assert.equal(round2(-1.125), -1.12);
});

test('tailId keeps the last four characters', () => {
  assert.equal(tailId('1000200030004000'), '…4000');
  assert.equal(tailId(1234567), '…4567');
  assert.equal(tailId('42'), '…42');
});

test('groupId chunks a long run of digits and nothing else', () => {
  const thin = ' ';
  assert.equal(groupId('1000200030004000'), ['1000', '2000', '3000', '4000'].join(thin));
  assert.equal(groupId('1000200030004000555'), ['1000', '2000', '3000', '4000', '555'].join(thin));
  assert.equal(groupId('100020003'), ['1000', '2000', '3'].join(thin));
  assert.equal(groupId(1000200030), ['1000', '2000', '30'].join(thin));
  assert.equal(groupId('10002000'), '10002000');
  assert.equal(groupId('QK1000200030004000'), 'QK1000200030004000');
  assert.equal(groupId('quick_ab12'), 'quick_ab12');
});

test('runsOf splits dates into runs of consecutive days', () => {
  assert.deepEqual(runsOf([]), []);
  assert.deepEqual(runsOf(['2026-07-03']), [['2026-07-03']]);
  assert.deepEqual(runsOf(['2026-07-30', '2026-07-31', '2026-08-01']),
    [['2026-07-30', '2026-07-31', '2026-08-01']]);
  assert.deepEqual(runsOf(['2026-07-01', '2026-07-02', '2026-07-05']),
    [['2026-07-01', '2026-07-02'], ['2026-07-05']]);
  assert.deepEqual(runsOf(['2026-07-01', '2026-07-03', '2026-07-05']),
    [['2026-07-01'], ['2026-07-03'], ['2026-07-05']]);
});

test('runsOf sorts what it is given and leaves the input alone', () => {
  const given = ['2026-07-05', '2026-07-02', '2026-07-01'];
  assert.deepEqual(runsOf(given), [['2026-07-01', '2026-07-02'], ['2026-07-05']]);
  assert.deepEqual(given, ['2026-07-05', '2026-07-02', '2026-07-01']);
  assert.deepEqual(runsOf(new Set(['2026-07-02', '2026-07-01'])),
    [['2026-07-01', '2026-07-02']]);
});

test('runsOf starts a new run on a repeated date', () => {
  assert.deepEqual(runsOf(['2026-07-01', '2026-07-01', '2026-07-02']),
    [['2026-07-01'], ['2026-07-01', '2026-07-02']]);
});

test('dateSpanLabel names a day, a range, or the days listed out', () => {
  assert.equal(dateSpanLabel([]), '');
  assert.equal(dateSpanLabel(['2026-07-05']), '7月5日');
  assert.equal(dateSpanLabel(['2026-07-01', '2026-07-02', '2026-07-03']), '7月1–3日');
  assert.equal(dateSpanLabel(['2026-07-31', '2026-08-01']), '7月31日–8月1日');
  assert.equal(dateSpanLabel(['2026-12-30', '2026-12-31', '2027-01-01']), '12月30日–1月1日');
  // Out of order reads the same as in order.
  assert.equal(dateSpanLabel(['2026-07-03', '2026-07-01', '2026-07-02']), '7月1–3日');
});

test('dateSpanLabel lists out up to three scattered days of one month', () => {
  assert.equal(dateSpanLabel(['2026-07-01', '2026-07-03']), '7月1、3日');
  assert.equal(dateSpanLabel(['2026-07-01', '2026-07-02', '2026-07-09']), '7月1、2、9日');
  // Four scattered days, or any that cross a month, fall back to first–last.
  assert.equal(dateSpanLabel(['2026-07-01', '2026-07-03', '2026-07-05', '2026-07-07']),
    '7月1日–7月7日');
  assert.equal(dateSpanLabel(['2026-07-30', '2026-08-02']), '7月30日–8月2日');
});
