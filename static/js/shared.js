// Shared by the day view, the settle view and the order sheet, which import
// what they use. Leaf helpers only: anything that assumes a view's own DOM
// shape (the view stacks, the calendar, the row renderers) stays in that view.

export const PLATFORMS = [
  { key: 'ride',      label: '接送' },
  { key: 'didi',      label: '滴滴' },
  { key: 'uber',      label: 'Uber' },
  { key: 'foodpanda', label: '熊貓' },
];

// ---- utils ----
export function fmtDate(d) {
  return d.getFullYear() + '-' +
    String(d.getMonth() + 1).padStart(2, '0') + '-' +
    String(d.getDate()).padStart(2, '0');
}
export function $(n) { return n % 1 ? n.toFixed(2) : n.toFixed(0); }
// JS twin of statement.py:money_str. A 判罰賠款 can outweigh what a leg or a
// day earned, and "$-97.38" reads as a mangled figure where "−$97.38" reads as
// money taken off, so the sign goes outside the symbol.
export function money(n) { return (n < 0 ? '−$' : '$') + $(Math.abs(n)); }
// A figure set in the mono face gives its colon, point, comma or middle dot a
// whole cell; each is wrapped so the stylesheet (.p) can pull it in. Takes
// text that is already escaped and returns markup: tags are passed over, so
// the result can be given back to it unchanged, and it must never be handed
// raw user text.
export function tight(html) {
  return String(html ?? '').replace(/<[^>]*>[:.,·]<\/span>|<[^>]*>|[:.,·]/g,
    m => m.length === 1 ? '<span class="p">' + m + '</span>' : m);
}
export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
// JS port of phone.py:format_phone_e164 — display-time only.
// _E164_CC mirrors phone.py:E164_CC and _TRUNK_ZERO_CC mirrors
// phone.py:TRUNK_ZERO_CC — keep both lists in sync.
// Every country code currently assigned in ITU-T E.164, one zone per line.
// The assignment is prefix-free — no assigned code is a prefix of another —
// which is what makes the longest-match-first scan below unambiguous. Codes
// withdrawn from service are deliberately absent (37 East Germany, 38
// Yugoslavia, 42 Czechoslovakia, 878 UPT, 888 disaster relief, 997
// Kazakhstan): such a number cannot be dialled, so leaving it unrecognised is
// the honest result.
const _E164_CC = new Set(`
1
20 27 211 212 213 216 218 220 221 222 223 224 225 226 227 228 229 230 231 232
233 234 235 236 237 238 239 240 241 242 243 244 245 246 247 248 249 250 251
252 253 254 255 256 257 258 260 261 262 263 264 265 266 267 268 269 290 291
297 298 299
30 31 32 33 34 36 39 350 351 352 353 354 355 356 357 358 359 370 371 372 373
374 375 376 377 378 379 380 381 382 383 385 386 387 389
40 41 43 44 45 46 47 48 49 420 421 423
51 52 53 54 55 56 57 58 500 501 502 503 504 505 506 507 508 509 590 591 592
593 594 595 596 597 598 599
60 61 62 63 64 65 66 670 672 673 674 675 676 677 678 679 680 681 682 683 685
686 687 688 689 690 691 692
7
81 82 84 86 800 808 850 852 853 855 856 870 880 881 882 883 886
90 91 92 93 94 95 98 960 961 962 963 964 965 966 967 968 970 971 972 973 974
975 976 977 979 992 993 994 995 996 998
`.trim().split(/\s+/));
// The country codes whose subscriber part is written with a national trunk
// zero that has to come off before the number can be dialled internationally
// (+81 0 80... won't dial IDD). Every other code keeps all of its digits: an
// Italian number (+39) carries the 0 as part of the subscriber number, so
// stripping it there dials someone else. Only extend this set with a code
// whose trunk prefix is known to be 0.
const _TRUNK_ZERO_CC = new Set(['1','7','44','61','62','63','65','66','81','82','86','852','853','886','971']);
export function formatPhoneE164(raw) {
  const s = (raw || '').trim();
  if (!s) return raw;
  // Already has +: the leading + is the caller declaring an international
  // number, so any assigned code can be taken at face value. Longest CC match
  // first so 852/853/886 beat the 1-digit codes.
  if (s.startsWith('+')) {
    const d = s.slice(1).replace(/[\s\-]/g, '');
    for (const n of [3, 2, 1]) {
      const cc = d.slice(0, n);
      if (_E164_CC.has(cc)) {
        let sub = d.slice(n);
        if (_TRUNK_ZERO_CC.has(cc) && sub.startsWith('0')) sub = sub.slice(1);
        return '+' + cc + sub;
      }
    }
    return '+' + d;
  }
  const hasSep = /[\s\-]/.test(s);
  if (hasSep) {
    const i = s.search(/[\s\-]/);
    const cc = s.slice(0, i);
    let sub = s.slice(i).replace(/[\s\-]/g, '');
    // Without a + there is nothing declaring the number international, and
    // "AAA BBB-BBBB" is how a NANP number is written: a 3-digit area code that
    // may equal a 3-digit country code (212 Morocco, 226 Burkina Faso, 254
    // Kenya...) followed by exactly 7 digits. That exact shape is
    // unresolvable, and a wrong guess dials a wrong number, so leave it alone.
    // A 2-digit code carries no such collision.
    const ambiguousNanp = cc.length === 3 && sub.length === 7;
    if (_E164_CC.has(cc) && !ambiguousNanp) {
      if (_TRUNK_ZERO_CC.has(cc) && sub.startsWith('0')) sub = sub.slice(1);
      return '+' + cc + sub;
    }
    return raw;
  }
  const d = s.replace(/[\s\-]/g, '');
  if (!/^\d+$/.test(d)) return raw;
  if (d.length === 11 && /^1[3-9]/.test(d)) return '+86' + d;
  if (d.length === 8 && /^[2-9]/.test(d)) return '+852' + d;
  return raw;
}
// Collect distinct labelled phone entries from all four contact fields.
// Parallel to collect_contact_lines() in bot.py — keep shapes in sync.
const _bracketRe = /【(.+?)】\s*(.*)/;
export function collectContactLines(o) {
  const seen = new Set();
  const result = [];
  function digits(s) { return s.replace(/\D/g, ''); }
  function add(label, raw, fmt) {
    const key = digits(raw);
    if (key && seen.has(key)) return;
    if (key) seen.add(key);
    result.push([label, fmt ? formatPhoneE164(raw) : raw.trim()]);
  }
  if ((o.passenger_phone || '').trim()) add('電話', o.passenger_phone, true);
  if ((o.overseas_phone || '').trim()) add('境外', o.overseas_phone, true);
  if ((o.third_party_contact || '').trim()) {
    const m = o.third_party_contact.trim().match(_bracketRe);
    if (m) add(m[1], m[2].trim(), true);
    else add('聯絡', o.third_party_contact, false);
  }
  if ((o.more_contacts || '').trim()) {
    const m = o.more_contacts.trim().match(_bracketRe);
    if (m) add(m[1], m[2].trim(), true);
    else add('更多', o.more_contacts, true);
  }
  return result;
}
// JS twin of service.py:platform_of — the settlement counterparty.
export function platform(o) {
  if (o.service_type === '滴滴') return 'didi';
  if (o.service_type === 'Uber') return 'uber';
  if (o.service_type === 'foodpanda') return 'foodpanda';
  return 'ride';
}
// JS twin of service.py:expected_of — keep in sync. Null fees count as 0, and
// a recorded 判罰賠款 is money the platform takes back out of whatever it pays,
// so it nets off on every platform.
export function expectedOf(o) {
  const p = platform(o);
  const pen = o.penalty_fee || 0;
  if (p === 'ride') return (o.price || 0) + (o.banner_fee || 0) - pen;
  if (p === 'foodpanda') return (o.price || 0) - pen;
  return (o.price || 0) + (o.tunnel_fee || 0) - pen;   // didi, uber: the toll is reimbursed
}
// JS twin of service.py:owed_of — keep in sync. What the batch that takes this
// leg's trip is owed for it: the 舉牌 other batches carry, paid ahead of a trip
// the platform held back, comes off. Only the settle payload carries paid_ahead.
export function owedOf(o) {
  return expectedOf(o) - (o.paid_ahead || 0);
}
// JS port of service.py — display-time classification only.  Keep the service
// types listed here in step with that module.
export const _QUICK_TYPES = new Set(['滴滴', 'Uber', 'foodpanda']);
export function svcLabel(st) {
  if (st === '接机') return '接機';
  if (st === '送机') return '送機';
  if (st === '接站') return '接站';
  if (_QUICK_TYPES.has(st)) return st;
  return '單程';
}
export function isFlightPickup(st) { return st === '接机'; }
// The tail an order is known by on screen: a quick order's own suffix, else
// the last six digits.
export function shortId(id) {
  return id.includes('_') ? id.split('_').pop() : id.slice(-6);
}
export function orderTime(o) {
  const t = (o.scheduled_time || '').split(' ')[1];
  return t ? t.slice(0, 5) : '';
}
export function weekday(dateStr) {
  return ['日', '一', '二', '三', '四', '五', '六'][new Date(dateStr + 'T00:00:00').getDay()];
}
let toastTimer;
export function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2400);
}

// ---- api ----
export { apiWrite } from './api.js';
