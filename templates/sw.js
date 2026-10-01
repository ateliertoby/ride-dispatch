// Caches the shell and its assets, and nothing else. Requests for data are
// not intercepted at all: a figure on screen must always have come from the
// server on this load.
const VERSION = {{ version|tojson }};
const CACHE = 'shell-' + VERSION;
const SHELL_KEY = '/__shell__';
const ASSETS = {{ assets|tojson }};
const SHELL_PATHS = ['/', '/settle'];

// A redirect is the access proxy's login standing where the file should be,
// and a response that was redirected cannot be handed to a navigation later.
async function take(url, init) {
  const res = await fetch(url, { credentials: 'same-origin', ...init });
  if (!res.ok || res.redirected) throw new Error('not available: ' + url);
  return res;
}

// Installed whole or not at all. The server answers an asset address of any
// version but its own with 404, so a deploy that lands part-way through fails
// the install here; the worker in charge stays in charge, and the browser
// tries again the next time it looks for a new one. A cache left part-filled
// is never read: only an installed worker reads its cache.
self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    const shell = await take('/', { cache: 'no-store' });
    // A document of another version must not be stored as this version's.
    if (shell.headers.get('X-Asset-Version') !== VERSION) {
      throw new Error('shell not available at version ' + VERSION);
    }
    await Promise.all(ASSETS.map(async url => cache.put(url, await take(url))));
    await cache.put(SHELL_KEY, shell);
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith('shell-') && key !== CACHE) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

// A new version waits until the page asks for it, so code is never swapped
// under a sheet the operator has open.
self.addEventListener('message', event => {
  if (event.data === 'activate') self.skipWaiting();
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // An address this version does not hold belongs to a document that came
  // from the network, and goes to the network with it.
  if (url.pathname.startsWith('/assets/')) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE);
      return (await cache.match(url.pathname)) || fetch(req);
    })());
    return;
  }
  // ?login asks for the network on purpose: that is how an expired session
  // reaches the access proxy's login instead of the cached document.
  if (req.mode === 'navigate' && SHELL_PATHS.includes(url.pathname) && !url.searchParams.has('login')) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE);
      return (await cache.match(SHELL_KEY)) || fetch(req);
    })());
  }
});
