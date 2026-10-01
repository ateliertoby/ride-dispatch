// Boot: the views, the router that shows one of them, the change stream, the
// worker that caches the shell, and the banner that offers a new version.

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

// A reload loses whatever is open: a sheet, a statement being read, unsaved
// ticks. So a new version is never taken behind the operator's back. It waits,
// the banner offers it, and the page reloads only on the tap that asked.
async function registerWorker() {
  if (!('serviceWorker' in navigator)) return;
  const sw = navigator.serviceWorker;
  const updateBanner = document.getElementById('banner-update');
  let controlled = !!sw.controller;
  let asked = false;
  let reloading = false;
  const reload = () => {
    if (reloading) return;
    reloading = true;
    location.reload();
  };
  sw.addEventListener('controllerchange', () => {
    const first = !controlled;
    controlled = true;
    if (asked) reload();
    // Another window of the app took the new version. This one still runs the
    // old code, complete in memory; it offers the reload instead of making it.
    // The first worker taking control of a page is not an update at all.
    else if (!first) updateBanner.hidden = false;
  });
  const reg = await sw.register('/sw.js');
  const take = w => { asked = true; w.postMessage('activate'); };
  const watch = w => w.addEventListener('statechange', () => {
    // reg.active tells an update from the first install, which passes through
    // the same state on its way to taking charge.
    if (w.state === 'installed' && reg.active) updateBanner.hidden = false;
  });
  // A version that finished installing before this load: nothing is open yet,
  // so take it now.
  if (reg.waiting && reg.active) take(reg.waiting);
  if (reg.installing) watch(reg.installing);
  reg.addEventListener('updatefound', () => watch(reg.installing));
  updateBanner.addEventListener('click', () => {
    if (reg.waiting) take(reg.waiting); else reload();
  });
  // An installed app is rarely navigated, so nothing else would make the
  // browser look for a new worker.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') reg.update().catch(() => {});
  });
}
// A browser that refuses the worker (no secure context, an expired login in
// front of its script) still runs the app, from the network.
registerWorker().catch(() => {});
