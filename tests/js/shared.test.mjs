import test from 'node:test';
import assert from 'node:assert/strict';

import {
  $, money, platform, expectedOf, owedOf, formatPhoneE164, collectContactLines,
  svcLabel, shortId, orderTime, weekday, fmtDate, esc,
} from '../../static/js/shared.js';

test('money shows cents only when there are some', () => {
  assert.equal(money(0), '$0');
  assert.equal(money(120), '$120');
  assert.equal(money(97.38), '$97.38');
  assert.equal(money(0.5), '$0.50');
  assert.equal($(120), '120');
  assert.equal($(97.5), '97.50');
});

test('money puts a negative sign outside the symbol', () => {
  // U+2212 MINUS SIGN, not the ASCII hyphen.
  assert.equal(money(-97.38), '\u2212$97.38');
  assert.equal(money(-40), '\u2212$40');
});

test('money writes no thousands separator', () => {
  assert.equal(money(1234), '$1234');
  assert.equal(money(12345.6), '$12345.60');
});

test('platform reads the counterparty off the service type', () => {
  assert.equal(platform({ service_type: '滴滴' }), 'didi');
  assert.equal(platform({ service_type: 'Uber' }), 'uber');
  assert.equal(platform({ service_type: 'foodpanda' }), 'foodpanda');
  assert.equal(platform({ service_type: '接机' }), 'ride');
  assert.equal(platform({ service_type: '送机' }), 'ride');
  assert.equal(platform({}), 'ride');
});

test('expectedOf counts what each platform pays for', () => {
  // 接送: the fare and the 舉牌; the toll is the driver's own.
  const ride = { service_type: '接机', price: 300, banner_fee: 40, tunnel_fee: 50, parking_fee: 28 };
  assert.equal(expectedOf(ride), 340);
  // 滴滴 and Uber reimburse the toll and know no 舉牌.
  assert.equal(expectedOf({ service_type: '滴滴', price: 80, tunnel_fee: 20, banner_fee: 40 }), 100);
  assert.equal(expectedOf({ service_type: 'Uber', price: 90.5, tunnel_fee: 8 }), 98.5);
  assert.equal(expectedOf({ service_type: 'foodpanda', price: 50, tunnel_fee: 20, banner_fee: 40 }), 50);
});

test('expectedOf takes a 判罰賠款 off on every platform', () => {
  assert.equal(expectedOf({ service_type: '接机', price: 300, banner_fee: 40, penalty_fee: 100 }), 240);
  assert.equal(expectedOf({ service_type: '滴滴', price: 80, tunnel_fee: 20, penalty_fee: 30 }), 70);
  assert.equal(expectedOf({ service_type: 'foodpanda', price: 50, penalty_fee: 60 }), -10);
});

test('expectedOf counts a missing fee as nothing', () => {
  assert.equal(expectedOf({ service_type: '接机' }), 0);
  assert.equal(expectedOf({ service_type: '接机', price: null, banner_fee: null, penalty_fee: null }), 0);
  assert.equal(expectedOf({ service_type: '滴滴', price: 80, tunnel_fee: null }), 80);
});

test('owedOf takes off what other batches already paid ahead', () => {
  const o = { service_type: '接机', price: 300, banner_fee: 40 };
  assert.equal(owedOf(o), 340);
  assert.equal(owedOf({ ...o, paid_ahead: 40 }), 300);
  assert.equal(owedOf({ ...o, paid_ahead: 40, penalty_fee: 100 }), 200);
});

// The input/output pairs tests/test_phone.py pins for phone.py's
// format_phone_e164, which this function duplicates.
const PHONES = [
  // Mainland with explicit CC
  ['86 13800000001', '+8613800000001'],
  // Bare mainland mobile (11-digit, ^1[3-9])
  ['13900000002', '+8613900000002'],
  // HK local (8-digit, ^[2-9])
  ['41111111', '+85241111111'],
  ['62222222', '+85262222222'],
  // HK with explicit CC
  ['852 51111111', '+85251111111'],
  // Taiwan with trunk zero (strip the 0)
  ['886 0911111111', '+886911111111'],
  // Taiwan without trunk zero (keep as-is)
  ['886 922222222', '+886922222222'],
  // NANP (US/Canada)
  ['1 2015550123', '+12015550123'],
  // Various international
  ['61 411111111', '+61411111111'],
  ['63 9171111111', '+639171111111'],
  ['62 811111111', '+62811111111'],
  ['65 91111111', '+6591111111'],
  ['65 92222222', '+6592222222'],
  ['66 911111111', '+66911111111'],
  ['7 9111111111', '+79111111111'],
  ['971 501111111', '+971501111111'],
  // Already has +
  ['+852 5111 1111', '+85251111111'],
  ['+86-138-0000-0000', '+8613800000000'],
  // + form with trunk zero left after the CC (strip the 0)
  ['+8108012345678', '+818012345678'],
  ['+81 080-1234-5678', '+818012345678'],
  // + form without trunk zero — untouched
  ['+85251111111', '+85251111111'],
  ['+886922222222', '+886922222222'],
  // Italy: 39 is outside TRUNK_ZERO_CC, so the 0 stays part of the number
  ['+390212345678', '+390212345678'],
  ['+39 06 1234567', '+39061234567'],
  ['39 06 1234567', '+39061234567'],
  // Spain: a CC recognised only via the full E.164 table, both forms
  ['34 612 345 678', '+34612345678'],
  ['+34 612 345 678', '+34612345678'],
  ['33 612345678', '+33612345678'],
  // 3-digit CC + exactly 7 digits reads as a NANP area code — leave it alone
  ['212 555 0100', '212 555 0100'],
  ['254 555-0100', '254 555-0100'],
  // Same CC, remainder that cannot be a NANP subscriber number — formatted
  ['212 612345678', '+212612345678'],
  // The guard is 3-digit-CC only: a 2-digit CC with 7 digits still formats
  ['34 6123456', '+346123456'],
  // A + declares an international number, so no NANP guard applies there
  ['+212 555 0100', '+2125550100'],
  // Withdrawn CC (42 Czechoslovakia) — not in the table, passthrough
  ['42 1234567', '42 1234567'],
  // Unrecognised CC after a + still falls back to the digits
  ['+999 12345', '+99912345'],
  // Unknown shape — passthrough unchanged
  ['12345', '12345'],
  ['999 12345', '999 12345'],
  ['', ''],
  ['ABCDE', 'ABCDE'],
  // Dash separator
  ['86-13800000001', '+8613800000001'],
  ['852-51111111', '+85251111111'],
];

test('formatPhoneE164 agrees with its Python twin', () => {
  for (const [raw, expected] of PHONES) {
    assert.equal(formatPhoneE164(raw), expected, JSON.stringify(raw));
  }
});

test('formatPhoneE164 hands back a missing number as it came', () => {
  assert.equal(formatPhoneE164(null), null);
  assert.equal(formatPhoneE164(undefined), undefined);
});

test('collectContactLines labels each number and drops a repeat', () => {
  assert.deepEqual(collectContactLines({
    passenger_phone: '+852 5111 1111',
    overseas_phone: '852 51111111',
    third_party_contact: '【聯絡人】86 13800000001',
    more_contacts: '62222222',
  }), [
    ['電話', '+85251111111'],
    ['聯絡人', '+8613800000001'],
    ['更多', '+85262222222'],
  ]);
  assert.deepEqual(collectContactLines({}), []);
});

test('collectContactLines leaves an unlabelled third-party contact as written', () => {
  assert.deepEqual(collectContactLines({ third_party_contact: ' 852 51111111 ' }),
    [['聯絡', '852 51111111']]);
});

test('svcLabel', () => {
  assert.equal(svcLabel('接机'), '接機');
  assert.equal(svcLabel('送机'), '送機');
  assert.equal(svcLabel('接站'), '接站');
  assert.equal(svcLabel('滴滴'), '滴滴');
  assert.equal(svcLabel('Uber'), 'Uber');
  assert.equal(svcLabel('foodpanda'), 'foodpanda');
  assert.equal(svcLabel('包车'), '單程');
});

test('shortId is a quick order\'s own suffix, else the last six characters', () => {
  assert.equal(shortId('1000200030004000'), '004000');
  assert.equal(shortId('didi_ab12'), 'ab12');
});

test('orderTime is the HH:MM of the scheduled time', () => {
  assert.equal(orderTime({ scheduled_time: '2026-07-01 14:20:00' }), '14:20');
  assert.equal(orderTime({ scheduled_time: '2026-07-01' }), '');
  assert.equal(orderTime({}), '');
});

test('weekday and fmtDate', () => {
  assert.equal(weekday('2026-10-01'), '四');
  assert.equal(weekday('2026-10-04'), '日');
  assert.equal(fmtDate(new Date(2026, 0, 5)), '2026-01-05');
});

test('esc', () => {
  assert.equal(esc('<a href="x">&\'</a>'), '&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;');
  assert.equal(esc(null), '');
  assert.equal(esc(0), '0');
});
