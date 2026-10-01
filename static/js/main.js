// Boot: the views, the router that shows one of them, and the change stream.

import { createStore } from './store.js';
import { openStream } from './stream.js';
import { getJson } from './api.js';
import { createRouter } from './router.js';
import { dayView } from './day/index.js';
import { settleView } from './settle/index.js';

const store = createStore(getJson);
// For the timing readout: when the tap that switched views was made.
const navAt = () => router.navAt();
const views = {
  day: { path: '/', title: 'Ride Dispatch', view: dayView,
         root: document.getElementById('view-day'), deps: { store, navAt } },
  settle: { path: '/settle', title: '埋數 · Ride Dispatch', view: settleView,
            root: document.getElementById('view-settle'), deps: { navAt } },
};
const router = createRouter(views);
router.start();

// A change on the server refreshes what is on screen; a hidden view catches up
// when it is shown, which is the only time it can be painted correctly.
openStream(
  () => views[router.current()].view.refresh(),
  () => { getJson('/api/ping').catch(() => {}); },   // tells an expired login from a dropped line
);
