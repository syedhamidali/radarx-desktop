// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { routeDrop, AUTOPILOT_DROP_MIN } from './dropRoute.ts';

test('one dropped folder opens the Autopilot (the engine expands it)', () => {
  assert.equal(routeDrop(['/data/mayfield'], ['folder']), 'autopilot');
});

test('a folder among one or two paths opens the Autopilot', () => {
  assert.equal(routeDrop(['/data/KHPX_V06', '/data/KNQA'], ['file', 'folder']), 'autopilot');
});

test('one or two plain files open as before', () => {
  assert.equal(routeDrop(['/data/KHPX20211211_015357_V06'], ['file']), 'open');
  assert.equal(routeDrop(['/a/x.nc', '/a/y.nc'], ['file', 'file']), 'open');
});

test('a Zarr store is a file to the viewer, not a folder to expand', () => {
  assert.equal(routeDrop(['/a/vol.zarr'], ['zarr']), 'open');
});

test('many paths open the Autopilot whatever they are', () => {
  const many = Array.from({ length: AUTOPILOT_DROP_MIN }, (_, i) => `/a/f${i}`);
  assert.equal(routeDrop(many, null), 'autopilot');
  assert.equal(routeDrop(many, many.map(() => 'file')), 'autopilot');
});

test('without kinds (older shell) the count decides; nothing dropped is nothing to do', () => {
  assert.equal(routeDrop(['/data/mayfield'], undefined), 'open');
  assert.equal(routeDrop(['/data/mayfield'], ['folder', 'file']), 'open', 'mismatched kinds are ignored');
  assert.equal(routeDrop([], []), 'none');
  assert.equal(routeDrop([''], null), 'none');
});
