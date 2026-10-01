// The one live channel: the server says "something changed" and nothing more.
// The first message of a connection is its greeting. The greeting of the first
// connection is not a change, provided nothing failed before it; the greeting
// of any later connection stands for whatever was missed while the stream was
// down.
//
// The document lives for hours, so the stream has to outlive what would end
// it. A browser retries a connection that dropped, but an answer that is not
// a 200 event stream (the tunnel's 502 while the server restarts, a redirect
// to a login) closes an EventSource for good. A closed stream is opened again
// here, after a delay that grows while it keeps failing.

const CLOSED = 2;             // EventSource.CLOSED
export const REOPEN_MIN_MS = 2000;
export const REOPEN_MAX_MS = 30000;

// How long to wait before opening the stream again, given how many times in a
// row it has been closed without a message in between.
export function reopenDelay(failures) {
  return Math.min(REOPEN_MAX_MS, REOPEN_MIN_MS * 2 ** failures);
}

// onChange() for every change, onError() for every failure of the stream.
// stopped() says the stream must not be opened again (the login has expired,
// and nothing but a reload brings it back). source and timers are the
// browser's, replaceable for a test.
//
// Returns { wake() }: open a stream that is waiting out its delay now, for a
// caller that knows the wait has lost its point (the app came back to the
// foreground).
export function openStream(onChange, onError, {
  stopped = () => false,
  source = url => new EventSource(url),
  timers = { set: (fn, ms) => setTimeout(fn, ms), clear: id => clearTimeout(id) },
} = {}) {
  let es = null;
  let untouched = true;         // no message yet, and no failure either
  let failures = 0;
  let timer = null;

  function connect() {
    timer = null;
    if (stopped()) return;
    es = source('/api/events');
    es.onmessage = () => {
      failures = 0;
      if (untouched) { untouched = false; return; }
      onChange();
    };
    es.onerror = () => {
      untouched = false;
      onError();
      if (es.readyState !== CLOSED || timer !== null) return;
      timer = timers.set(connect, reopenDelay(failures));
      failures += 1;
    };
  }
  connect();

  return {
    wake() {
      if (timer === null) return;
      timers.clear(timer);
      failures = 0;
      connect();
    },
  };
}
