import test from 'node:test';
import assert from 'node:assert/strict';

async function fresh() {
  // A new module instance per test: the expired flag is module state.
  return import('../../static/js/api.js?' + Math.random());
}
const hdr = ct => ({ get: k => (k.toLowerCase() === 'content-type' ? ct : null) });
const okJson = () => ({ ok: true, status: 200, type: 'basic', headers: hdr('application/json'),
                        json: async () => ({ a: 1 }) });
function stub(r) { globalThis.fetch = async () => r; }

test('each import with a new query string is a separate module instance', async () => {
  const a = await fresh();
  const b = await fresh();
  assert.notEqual(a, b);
  stub({ ok: false, status: 0, type: 'opaqueredirect', headers: hdr(null) });
  await assert.rejects(a.getJson('/api/x'), a.AuthExpired);
  assert.equal(a.isAuthExpired(), true);
  assert.equal(b.isAuthExpired(), false);
});

test('getJson returns the parsed body', async () => {
  const api = await fresh();
  stub(okJson());
  assert.deepEqual(await api.getJson('/api/x'), { a: 1 });
  assert.equal(api.isAuthExpired(), false);
});

test('a redirect is an expired session, not a failure to load', async () => {
  const api = await fresh();
  let told = 0;
  api.onAuthExpired(() => told++);
  stub({ ok: false, status: 0, type: 'opaqueredirect', headers: hdr(null) });
  await assert.rejects(api.getJson('/api/x'), api.AuthExpired);
  assert.equal(told, 1);
  assert.equal(api.isAuthExpired(), true);
});

test('a login page where JSON was expected is an expired session', async () => {
  const api = await fresh();
  stub({ ok: true, status: 200, type: 'basic', headers: hdr('text/html; charset=utf-8') });
  await assert.rejects(api.getJson('/api/x'), api.AuthExpired);
});

// What the access proxy answers when the request declares itself scripted
// (X-Requested-With) instead of redirecting it to the login.
test('the access proxy\'s 401 page is an expired session', async () => {
  const api = await fresh();
  let told = 0;
  api.onAuthExpired(() => told++);
  stub({ ok: false, status: 401, type: 'basic', headers: hdr('text/html; charset=UTF-8') });
  await assert.rejects(api.getJson('/api/x'), api.AuthExpired);
  assert.equal(told, 1);
});

test('the access proxy\'s 403 page is an expired session', async () => {
  const api = await fresh();
  stub({ ok: false, status: 403, type: 'basic', headers: hdr('text/html; charset=UTF-8') });
  await assert.rejects(api.apiWrite('PATCH', '/api/orders/X', {}), api.AuthExpired);
});

test('listeners are told once however many calls fail', async () => {
  const api = await fresh();
  let told = 0;
  api.onAuthExpired(() => told++);
  stub({ ok: false, status: 0, type: 'opaqueredirect', headers: hdr(null) });
  await assert.rejects(api.getJson('/api/x'));
  await assert.rejects(api.getJson('/api/y'));
  assert.equal(told, 1);
});

test('a server error is an ordinary failure', async () => {
  const api = await fresh();
  stub({ ok: false, status: 500, type: 'basic', headers: hdr('application/json') });
  await assert.rejects(api.getJson('/api/x'), /HTTP 500/);
  assert.equal(api.isAuthExpired(), false);
});

// The server's own error pages (an unhandled exception, an unknown route) and
// the tunnel's when the server is down are HTML too, with the login intact.
test('an HTML error page is an ordinary failure', async () => {
  for (const status of [404, 405, 500, 502, 530]) {
    const api = await fresh();
    stub({ ok: false, status, type: 'basic', headers: hdr('text/html; charset=utf-8'),
           json: async () => { throw new SyntaxError('not JSON'); } });
    await assert.rejects(api.getJson('/api/x'), new RegExp('HTTP ' + status));
    await assert.rejects(api.apiWrite('POST', '/api/x', {}), new RegExp('HTTP ' + status));
    assert.equal(api.isAuthExpired(), false);
  }
});

test('a network failure is passed on as it is', async () => {
  const api = await fresh();
  globalThis.fetch = async () => { throw new TypeError('Load failed'); };
  await assert.rejects(api.getJson('/api/x'), TypeError);
  assert.equal(api.isAuthExpired(), false);
});

test('apiWrite surfaces the server\'s own message', async () => {
  const api = await fresh();
  stub({ ok: false, status: 409, type: 'basic', headers: hdr('application/json'),
         json: async () => ({ error: '已結算嘅單要先撤銷結算' }) });
  await assert.rejects(api.apiWrite('PATCH', '/api/orders/X', {}), /已結算/);
});

test('apiWrite sends the body as JSON and returns the parsed answer', async () => {
  const api = await fresh();
  let seen;
  globalThis.fetch = async (p, init) => { seen = { p, init }; return okJson(); };
  assert.deepEqual(await api.apiWrite('PATCH', '/api/orders/X', { price: 120 }), { a: 1 });
  assert.equal(seen.p, '/api/orders/X');
  assert.equal(seen.init.method, 'PATCH');
  assert.equal(seen.init.headers['Content-Type'], 'application/json');
  assert.equal(seen.init.body, '{"price":120}');
  assert.equal(seen.init.redirect, 'manual');
});

test('requests never follow a redirect and never use a cache', async () => {
  const api = await fresh();
  let seen;
  globalThis.fetch = async (_p, init) => { seen = init; return okJson(); };
  await api.getJson('/api/x');
  assert.equal(seen.redirect, 'manual');
  assert.equal(seen.cache, 'no-store');
});

test('a caller cannot turn redirect following or the cache back on', async () => {
  const api = await fresh();
  let seen;
  globalThis.fetch = async (_p, init) => { seen = init; return okJson(); };
  await api.apiFetch('/api/x', { method: 'POST', redirect: 'follow', cache: 'default' });
  assert.equal(seen.method, 'POST');
  assert.equal(seen.redirect, 'manual');
  assert.equal(seen.cache, 'no-store');
});
