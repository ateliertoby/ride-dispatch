import test from 'node:test';
import assert from 'node:assert/strict';
import { REOPEN_MAX_MS, REOPEN_MIN_MS, openStream, reopenDelay } from '../../static/js/stream.js';

const CONNECTING = 0, OPEN = 1, CLOSED = 2;

// The browser's side of the stream, driven by hand: every source ever made,
// and the one timer the stream may have pending.
function harness({ stopped = () => false } = {}) {
  const h = { sources: [], changes: 0, errors: 0, pending: null, cleared: 0 };
  const source = url => {
    const es = { url, readyState: CONNECTING, onmessage: null, onerror: null };
    h.sources.push(es);
    return es;
  };
  const timers = {
    set(fn, ms) { h.pending = { fn, ms }; return h.pending; },
    clear(id) { if (h.pending === id) { h.pending = null; h.cleared += 1; } },
  };
  h.stream = openStream(() => { h.changes += 1; }, () => { h.errors += 1; },
                        { stopped, source, timers });
  h.last = () => h.sources[h.sources.length - 1];
  h.message = () => { h.last().readyState = OPEN; h.last().onmessage({ data: 'x' }); };
  // A dropped line: the browser retries on the same object.
  h.drop = () => { h.last().readyState = CONNECTING; h.last().onerror({}); };
  // An answer that is not an event stream: the browser gives the object up.
  h.refuse = () => { h.last().readyState = CLOSED; h.last().onerror({}); };
  h.fire = () => { const t = h.pending; h.pending = null; t.fn(); };
  return h;
}

test('the delay doubles from its floor and stops at its ceiling', () => {
  assert.equal(REOPEN_MIN_MS, 2000);
  assert.equal(REOPEN_MAX_MS, 30000);
  assert.deepEqual([0, 1, 2, 3, 4, 5, 20].map(reopenDelay),
                   [2000, 4000, 8000, 16000, 30000, 30000, 30000]);
});

test('one stream is opened, on the events address', () => {
  const h = harness();
  assert.deepEqual(h.sources.map(s => s.url), ['/api/events']);
  assert.equal(h.pending, null);
});

test('the first greeting is not a change; every later message is', () => {
  const h = harness();
  h.message();
  assert.equal(h.changes, 0);
  h.message();
  h.message();
  assert.equal(h.changes, 2);
});

test('the greeting after a dropped line is a change, on the same stream', () => {
  const h = harness();
  h.message();
  h.drop();
  assert.equal(h.errors, 1);
  assert.equal(h.pending, null, 'the browser retries by itself; no second stream');
  h.message();
  assert.equal(h.changes, 1);
  assert.equal(h.sources.length, 1);
});

test('a first greeting that follows a failure is a change', () => {
  const h = harness();
  h.drop();
  h.message();
  assert.equal(h.changes, 1);
});

test('a closed stream is opened again after the delay, and its greeting is a change', () => {
  const h = harness();
  h.message();
  h.refuse();
  assert.equal(h.errors, 1);
  assert.equal(h.pending.ms, 2000);
  assert.equal(h.sources.length, 1, 'not before the delay');
  h.fire();
  assert.equal(h.sources.length, 2);
  h.message();
  assert.equal(h.changes, 1);
});

test('the delay grows while the stream keeps being refused and starts over after a message', () => {
  const h = harness();
  const waits = [];
  for (let i = 0; i < 6; i++) { h.refuse(); waits.push(h.pending.ms); h.fire(); }
  assert.deepEqual(waits, [2000, 4000, 8000, 16000, 30000, 30000]);
  assert.equal(h.errors, 6);
  h.message();
  h.refuse();
  assert.equal(h.pending.ms, 2000);
});

test('a second error on a closed stream does not start a second wait', () => {
  const h = harness();
  h.refuse();
  const first = h.pending;
  h.last().onerror({});
  assert.equal(h.pending, first);
  h.fire();
  assert.equal(h.sources.length, 2);
});

test('nothing is opened again once the stream is stopped', () => {
  let expired = false;
  const h = harness({ stopped: () => expired });
  h.message();
  h.refuse();
  expired = true;
  h.fire();
  assert.equal(h.sources.length, 1);
  assert.equal(h.pending, null);
  h.stream.wake();
  assert.equal(h.sources.length, 1);
});

test('wake opens a waiting stream at once and starts the delay over', () => {
  const h = harness();
  h.refuse(); h.fire();
  h.refuse(); h.fire();
  h.refuse();
  assert.equal(h.pending.ms, 8000);
  h.stream.wake();
  assert.equal(h.cleared, 1);
  assert.equal(h.pending, null);
  assert.equal(h.sources.length, 4);
  h.refuse();
  assert.equal(h.pending.ms, 2000);
});

test('wake leaves a stream that is not waiting alone', () => {
  const h = harness();
  h.message();
  h.stream.wake();
  h.drop();
  h.stream.wake();
  assert.equal(h.sources.length, 1);
});
