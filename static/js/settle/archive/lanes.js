// ARCHIVE. Only ./strip-events.js, itself unused, names this file.
//
// Lane packing of the settle calendar as it was when batches and credits were
// drawn in it: which row of a week each bar and chip went on. An event is
// anything carrying `rs`, the first column its label reserves, and `reach`,
// how many columns it reserves; the view measured both against the live grid
// before packing.

export function packLanes(events) {
  const lanes = [];
  for (const e of events) {
    let li = lanes.findIndex(lane => lane.every(x =>
      e.rs >= x.rs + x.reach || e.rs + e.reach <= x.rs));
    if (li < 0) { lanes.push([]); li = lanes.length - 1; }
    lanes[li].push(e);
  }
  return lanes;
}
