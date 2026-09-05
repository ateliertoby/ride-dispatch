// Shared by dashboard.html and settle.html; included inside each page's
// <script>. Leaf helpers only: anything that assumes a page's own DOM shape
// (the view stacks, the calendar, the row renderers) stays in that page.

const PLATFORMS = [
  { key: 'ride',      label: '接送' },
  { key: 'didi',      label: '滴滴' },
  { key: 'uber',      label: 'Uber' },
  { key: 'foodpanda', label: '熊貓' },
];

// ---- utils ----
function fmtDate(d) {
  return d.getFullYear() + '-' +
    String(d.getMonth() + 1).padStart(2, '0') + '-' +
    String(d.getDate()).padStart(2, '0');
}
function $(n) { return n % 1 ? n.toFixed(2) : n.toFixed(0); }
// JS twin of statement.py:money_str. A 判罰賠款 can outweigh what a leg or a
// day earned, and "$-97.38" reads as a mangled figure where "−$97.38" reads as
// money taken off, so the sign goes outside the symbol.
function money(n) { return (n < 0 ? '−$' : '$') + $(Math.abs(n)); }
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
// JS port of phone.py:format_phone_e164 — display-time only.
// _KNOWN_CC mirrors phone.py:KNOWN_CC — keep both lists in sync.
const _KNOWN_CC = new Set(['1','7','44','61','62','63','65','66','81','82','86','852','853','886','971']);
function formatPhoneE164(raw) {
  const s = (raw || '').trim();
  if (!s) return raw;
  // Drop the trunk zero some channels leave between CC and subscriber
  // (+81 0 80... won't dial IDD). Longest CC match first so 852/853/886
  // beat the 1-digit codes.
  if (s.startsWith('+')) {
    const d = s.slice(1).replace(/[\s\-]/g, '');
    for (const n of [3, 2, 1]) {
      const cc = d.slice(0, n);
      if (_KNOWN_CC.has(cc)) {
        let sub = d.slice(n);
        if (sub.startsWith('0')) sub = sub.slice(1);
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
    if (_KNOWN_CC.has(cc)) {
      if (sub.startsWith('0')) sub = sub.slice(1);
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
function collectContactLines(o) {
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
function platform(o) {
  if (o.service_type === '滴滴') return 'didi';
  if (o.service_type === 'Uber') return 'uber';
  if (o.service_type === 'foodpanda') return 'foodpanda';
  return 'ride';
}
// JS twin of service.py:expected_of — keep in sync. Null fees count as 0, and
// a recorded 判罰賠款 is money the platform takes back out of whatever it pays,
// so it nets off on every platform.
function expectedOf(o) {
  const p = platform(o);
  const pen = o.penalty_fee || 0;
  if (p === 'ride') return (o.price || 0) + (o.banner_fee || 0) - pen;
  if (p === 'foodpanda') return (o.price || 0) - pen;
  return (o.price || 0) + (o.tunnel_fee || 0) - pen;   // didi, uber: the toll is reimbursed
}
// JS port of service.py — display-time classification only.  Keep the service
// types listed here in step with that module.
const _QUICK_TYPES = new Set(['滴滴', 'Uber', 'foodpanda']);
function svcLabel(st) {
  if (st === '接机') return '接機';
  if (st === '送机') return '送機';
  if (st === '接站') return '接站';
  if (_QUICK_TYPES.has(st)) return st;
  return '單程';
}
function orderTime(o) {
  const t = (o.scheduled_time || '').split(' ')[1];
  return t ? t.slice(0, 5) : '';
}
function weekday(dateStr) {
  return ['日', '一', '二', '三', '四', '五', '六'][new Date(dateStr + 'T00:00:00').getDay()];
}
let toastTimer;
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2400);
}

// ---- api ----
async function apiWrite(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || ('HTTP ' + res.status));
  }
  return res.json();
}
