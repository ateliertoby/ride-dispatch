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
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
// JS twin of service.py:platform_of — the settlement counterparty.
function platform(o) {
  if (o.service_type === '滴滴') return 'didi';
  if (o.service_type === 'Uber') return 'uber';
  if (o.service_type === 'foodpanda') return 'foodpanda';
  return 'ride';
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
