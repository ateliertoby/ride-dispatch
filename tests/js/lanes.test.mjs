import test from 'node:test';
import assert from 'node:assert/strict';

import { packLanes } from '../../static/js/lanes.js';

const ev = (name, rs, reach) => ({ name, rs, reach });
const names = lanes => lanes.map(lane => lane.map(e => e.name));

test('no events need no lane', () => {
  assert.deepEqual(packLanes([]), []);
});

test('events that do not overlap share one lane', () => {
  // The second starts on the column the first ends before: touching is not
  // overlapping.
  assert.deepEqual(names(packLanes([ev('a', 0, 2), ev('b', 2, 3), ev('c', 6, 1)])),
    [['a', 'b', 'c']]);
});

test('two events on the same column take a lane each', () => {
  assert.deepEqual(names(packLanes([ev('a', 0, 3), ev('b', 2, 2)])), [['a'], ['b']]);
});

test('a later event drops back to the first lane it fits', () => {
  assert.deepEqual(names(packLanes([ev('a', 0, 3), ev('b', 2, 2), ev('c', 3, 2)])),
    [['a', 'c'], ['b']]);
});

test('an event that fits no lane opens a third', () => {
  assert.deepEqual(names(packLanes([ev('a', 0, 7), ev('b', 1, 2), ev('c', 2, 1)])),
    [['a'], ['b'], ['c']]);
});

test('lanes follow the order the events are given in, not their columns', () => {
  // An event to the left of one already placed joins its lane when clear of it.
  assert.deepEqual(names(packLanes([ev('late', 4, 3), ev('early', 0, 4)])),
    [['late', 'early']]);
  assert.deepEqual(names(packLanes([ev('late', 4, 3), ev('early', 0, 5)])),
    [['late'], ['early']]);
});

test('the lanes hold the events themselves', () => {
  const a = ev('a', 0, 1);
  assert.equal(packLanes([a])[0][0], a);
});
