// Date arithmetic and labels of the settle view that read nothing but their
// arguments. Dates are 'YYYY-MM-DD' strings and months 'YYYY-MM' keys, in the
// browser's own time zone.

import { fmtDate } from './shared.js';

export function pad2(n) { return String(n).padStart(2, '0'); }
export function addDays(dateStr, n) {
  const d = new Date(dateStr + 'T00:00:00');
  d.setDate(d.getDate() + n);
  return fmtDate(d);
}
// 0 = Sunday, the column the week strip starts on.
export function dow(dateStr) { return new Date(dateStr + 'T00:00:00').getDay(); }
export function monthKey(dateStr) { return dateStr.slice(0, 7); }
export function addMonths(key, n) {
  const y = +key.slice(0, 4), m = +key.slice(5, 7) - 1 + n;
  return (y + Math.floor(m / 12)) + '-' + pad2((m % 12 + 12) % 12 + 1);
}
// Day 0 of the next month is the last day of this one, whatever its length.
export function monthEnd(key) {
  return key + '-' + pad2(new Date(+key.slice(0, 4), +key.slice(5, 7), 0).getDate());
}
export function monthsBetween(a, b) {
  const out = [];
  for (let k = a; k <= b; k = addMonths(k, 1)) out.push(k);
  return out;
}
export function mdLabel(dateStr) {
  const p = dateStr.split('-');
  return (+p[1]) + '月' + (+p[2]) + '日';
}
export function mdSlash(dateStr) { return (+dateStr.slice(5, 7)) + '/' + (+dateStr.slice(8, 10)); }
export function round2(n) { return Math.round(n * 100) / 100; }
// The last four characters, the way the platform's own message names a leg.
export function tailId(id) { return '…' + String(id).slice(-4); }
// Batches are normally whole consecutive days, so a range reads best; a
// deliberately non-contiguous batch is listed out instead of being flattened
// into a range that would claim days it does not hold.
export function dateSpanLabel(dates) {
  const s = [...dates].sort();
  if (!s.length) return '';
  if (s.length === 1) return mdLabel(s[0]);
  const contiguous = s.every((d, i) => i === 0 || d === addDays(s[i - 1], 1));
  const a = s[0], z = s[s.length - 1];
  if (contiguous) {
    if (a.slice(0, 7) === z.slice(0, 7)) return (+a.slice(5, 7)) + '月' + (+a.slice(8)) + '–' + (+z.slice(8)) + '日';
    return mdLabel(a) + '–' + mdLabel(z);
  }
  if (s.length <= 3 && s.every(d => d.slice(0, 7) === a.slice(0, 7))) {
    return (+a.slice(5, 7)) + '月' + s.map(d => +d.slice(8)).join('、') + '日';
  }
  return mdLabel(a) + '–' + mdLabel(z);
}
// A 16- or 19-digit number is read off a statement in chunks, so the digits
// are grouped for the eye. Presentation only: data-copy and the clipboard
// carry the raw id. A number carrying letters is a code rather than a run of
// digits to be chunked, and a short one needs no help, so both stay as they
// are.
export function groupId(id) {
  const s = String(id);
  return /^\d{9,}$/.test(s) ? s.replace(/(\d{4})(?=\d)/g, '$1\u2009') : s;
}
// A batch's dates are not assumed contiguous: the platform holds a problem leg
// back and settles it with a later statement. The latest run is the statement's
// own days; every earlier run is a leg it picked up.
export function runsOf(dates) {
  const runs = [];
  for (const d of [...dates].sort()) {
    const last = runs[runs.length - 1];
    if (last && d === addDays(last[last.length - 1], 1)) last.push(d);
    else runs.push([d]);
  }
  return runs;
}
