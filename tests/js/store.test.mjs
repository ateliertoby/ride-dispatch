import test from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from '../../static/js/store.js';

function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}

class AuthExpired extends Error { constructor() { super(); this.name = 'AuthExpired'; } }

test('first read paints once, after the fetch', async () => {
  const store = createStore(async () => ({ n: 1 }));
  const seen = [];
  assert.equal(await store.read('k', '/u', d => seen.push(d)), true);
  assert.deepEqual(seen, [{ n: 1 }]);
});

test('second read paints held data at once and stays quiet if nothing changed', async () => {
  const store = createStore(async () => ({ n: 1 }));
  await store.read('k', '/u', () => {});
  const seen = [];
  const p = store.read('k', '/u', d => seen.push(d));
  assert.deepEqual(seen, [{ n: 1 }]);          // synchronous
  await p;
  assert.deepEqual(seen, [{ n: 1 }]);          // no repaint
});

test('a changed answer repaints', async () => {
  let n = 1;
  const store = createStore(async () => ({ n: n++ }));
  await store.read('k', '/u', () => {});
  const seen = [];
  await store.read('k', '/u', d => seen.push(d));
  assert.deepEqual(seen, [{ n: 1 }, { n: 2 }]);
});

test('keys are held apart and fetched by their own url', async () => {
  const store = createStore(async u => ({ u }));
  await store.read('a', '/a', () => {});
  await store.read('b', '/b', () => {});
  assert.deepEqual(store.peek('a'), { u: '/a' });
  assert.deepEqual(store.peek('b'), { u: '/b' });
  assert.equal(store.peek('c'), undefined);
});

test('an answer that arrives after a newer one is dropped', async () => {
  const slow = deferred(), fast = deferred();
  const queue = [slow, fast];
  const store = createStore(() => queue.shift().promise);
  const seen = [];
  const a = store.read('k', '/u', d => seen.push(d));
  const b = store.read('k', '/u', d => seen.push(d));
  fast.resolve({ n: 2 });
  await b;
  slow.resolve({ n: 1 });
  assert.equal(await a, true);
  assert.deepEqual(seen, [{ n: 2 }]);
  assert.deepEqual(store.peek('k'), { n: 2 });
});

test('answers that arrive in the order they were asked both apply', async () => {
  const first = deferred(), second = deferred();
  const queue = [first, second];
  const store = createStore(() => queue.shift().promise);
  const seen = [];
  const a = store.read('k', '/u', d => seen.push(d));
  const b = store.read('k', '/u', d => seen.push(d));
  first.resolve({ n: 1 });
  assert.equal(await a, true);
  assert.deepEqual(store.peek('k'), { n: 1 });
  second.resolve({ n: 2 });
  assert.equal(await b, true);
  assert.deepEqual(seen, [{ n: 1 }, { n: 2 }]);
  assert.deepEqual(store.peek('k'), { n: 2 });
});

test('an older request failing after a newer one landed is not a failure', async () => {
  const slow = deferred(), fast = deferred();
  const queue = [slow, fast];
  const store = createStore(() => queue.shift().promise);
  const seen = [];
  const a = store.read('k', '/u', d => seen.push(d));
  const b = store.read('k', '/u', d => seen.push(d));
  fast.resolve({ n: 2 });
  assert.equal(await b, true);
  slow.reject(new Error('HTTP 500'));
  assert.equal(await a, false);
  assert.deepEqual(seen, [{ n: 2 }]);
  assert.deepEqual(store.peek('k'), { n: 2 });
});

test('a newer request failing leaves the older answer to apply', async () => {
  const slow = deferred(), fast = deferred();
  const queue = [slow, fast];
  const store = createStore(() => queue.shift().promise);
  const seen = [];
  const a = store.read('k', '/u', d => seen.push(d));
  const b = store.read('k', '/u', d => seen.push(d));
  fast.reject(new Error('HTTP 500'));
  await assert.rejects(b, /HTTP 500/);
  slow.resolve({ n: 1 });
  assert.equal(await a, true);
  assert.deepEqual(seen, [{ n: 1 }]);
});

test('a failed fetch with nothing held rejects', async () => {
  const store = createStore(async () => { throw new Error('HTTP 500'); });
  await assert.rejects(store.read('k', '/u', () => {}), /HTTP 500/);
});

test('a failed revalidation keeps what is held and resolves false', async () => {
  let fail = false;
  const store = createStore(async () => { if (fail) throw new Error('offline'); return { n: 1 }; });
  await store.read('k', '/u', () => {});
  fail = true;
  const seen = [];
  assert.equal(await store.read('k', '/u', d => seen.push(d)), false);
  assert.deepEqual(seen, [{ n: 1 }]);
  assert.deepEqual(store.peek('k'), { n: 1 });
});

test('an expired login always rejects, held data or not', async () => {
  let expire = false;
  const store = createStore(async () => { if (expire) throw new AuthExpired(); return { n: 1 }; });
  await store.read('k', '/u', () => {});
  expire = true;
  await assert.rejects(store.read('k', '/u', () => {}), e => e.name === 'AuthExpired');
});

test('an expired login rejects even on a request a newer one has overtaken', async () => {
  const slow = deferred(), fast = deferred();
  const queue = [slow, fast];
  const store = createStore(() => queue.shift().promise);
  const a = store.read('k', '/u', () => {});
  const b = store.read('k', '/u', () => {});
  fast.resolve({ n: 2 });
  await b;
  slow.reject(new AuthExpired());
  await assert.rejects(a, e => e.name === 'AuthExpired');
});

test('prefetch fills a key without painting and never throws', async () => {
  const store = createStore(async u => { if (u === '/bad') throw new Error('x'); return { n: 1 }; });
  await store.prefetch('k', '/u');
  assert.deepEqual(store.peek('k'), { n: 1 });
  await store.prefetch('bad', '/bad');
  assert.equal(store.peek('bad'), undefined);
});

test('a read after a prefetch paints the prefetched data at once', async () => {
  const store = createStore(async () => ({ n: 1 }));
  await store.prefetch('k', '/u');
  const seen = [];
  const p = store.read('k', '/u', d => seen.push(d));
  assert.deepEqual(seen, [{ n: 1 }]);
  assert.equal(await p, true);
  assert.deepEqual(seen, [{ n: 1 }]);
});

test('a read that a prefetch of the same data beats still paints', async () => {
  const warm = deferred(), asked = deferred();
  const queue = [warm, asked];
  const store = createStore(() => queue.shift().promise);
  const pre = store.prefetch('k', '/u');
  const seen = [];
  const p = store.read('k', '/u', d => seen.push(d));
  assert.deepEqual(seen, []);                  // nothing held when it was asked
  warm.resolve({ n: 1 });
  await pre;
  asked.resolve({ n: 1 });
  assert.equal(await p, true);
  assert.deepEqual(seen, [{ n: 1 }]);
});

test('an error thrown while repainting is the caller\'s, not a failed revalidation', async () => {
  let n = 1;
  const store = createStore(async () => ({ n: n++ }));
  await store.read('k', '/u', () => {});
  const p = store.read('k', '/u', d => { if (d.n === 2) throw new Error('paint broke'); });
  await assert.rejects(p, /paint broke/);
});
