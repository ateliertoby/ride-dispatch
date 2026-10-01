// What the server last said, by key, so a view can paint before the network
// answers. Held in memory only, and every read asks the server again: an
// answer here is good for an instant paint and never for the truth.

export function createStore(getJson) {
  const held = new Map();      // key -> { data, text }
  const seq = new Map();       // key -> number of the latest request started
  const applied = new Map();   // key -> number of the latest answer applied

  function start(key) {
    const n = (seq.get(key) || 0) + 1;
    seq.set(key, n);
    return n;
  }
  // Answers can land out of order; one older than what is already applied
  // would put stale data back.
  function overtaken(key, n) { return n < (applied.get(key) || 0); }
  function land(key, n, data) {
    if (overtaken(key, n)) return false;
    applied.set(key, n);
    const text = JSON.stringify(data);
    const prev = held.get(key);
    if (!prev || prev.text !== text) held.set(key, { data, text });
    return true;
  }

  return {
    peek(key) { const h = held.get(key); return h ? h.data : undefined; },

    // Paints what is held at once, then asks the server and paints again only
    // if the answer differs. Resolves true once an answer has landed, false
    // when the request failed but the caller is not left without data;
    // rejects when it failed with nothing to show, and always on an expired
    // login.
    async read(key, url, paint) {
      const had = held.get(key);
      if (had) paint(had.data);
      const n = start(key);
      let data;
      try {
        data = await getJson(url);
      } catch (e) {
        if (e.name === 'AuthExpired') throw e;
        // A request a newer one has overtaken would have been dropped had it
        // succeeded, so its failure says nothing about what is on screen.
        if (had || overtaken(key, n)) return false;
        throw e;
      }
      if (land(key, n, data)) {
        // Compared with what this call painted, not with what was held when
        // the answer landed: a prefetch can fill the key in between, and the
        // caller has still been shown nothing.
        const now = held.get(key);
        if (!had || had.text !== now.text) paint(now.data);
      }
      return true;
    },

    async prefetch(key, url) {
      const n = start(key);
      try { land(key, n, await getJson(url)); } catch (e) { /* a warm-up only */ }
    },
  };
}
