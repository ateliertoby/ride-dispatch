// Every request the app makes to its own server goes through here, so one
// place decides what an expired login looks like.

export class AuthExpired extends Error {
  constructor() { super('auth expired'); this.name = 'AuthExpired'; }
}

let expired = false;
const listeners = new Set();

export function onAuthExpired(fn) { listeners.add(fn); }
export function isAuthExpired() { return expired; }

function markExpired() {
  if (!expired) {
    expired = true;
    listeners.forEach(fn => fn());
  }
  throw new AuthExpired();
}

// The access proxy in front of the server answers a request that carries no
// session in one of two ways: a 302 to its login on another origin, or, when
// the request declares itself scripted, a 401 HTML page. The server itself
// never redirects an API call and never answers one with 401 or 403.
// redirect:'manual' is what makes the first visible; followed, a cross-origin
// redirect is indistinguishable from being offline. HTML on a successful
// answer is that login having been followed anyway. HTML on any other status
// is not a sign of anything: the server's own error pages and the tunnel's
// are HTML too, and the login is intact behind them.
export async function apiFetch(path, init = {}) {
  const res = await fetch(path, { ...init, redirect: 'manual', cache: 'no-store',
                                  credentials: 'same-origin' });
  if (res.type === 'opaqueredirect') markExpired();
  const type = res.headers.get('content-type') || '';
  if (type.startsWith('text/html') && (res.ok || res.status === 401 || res.status === 403)) {
    markExpired();
  }
  return res;
}

export async function getJson(path) {
  const res = await apiFetch(path);
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

export async function apiWrite(method, path, body) {
  const res = await apiFetch(path, {
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
