// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { crossfade, LayerVis, LAYER_TABLE } from './perilsLayers.ts';

const shots = [
  { name: 'establishing', t0: 0, t1: 15 },
  { name: 'flyby', t0: 15, t1: 46 },
  { name: 'tornado', t0: 46, t1: 70 },
  { name: 'window', t0: 70, t1: 90 },
  { name: 'rise', t0: 90, t1: 105 },
  { name: 'topdown', t0: 105, t1: 120 },
];

test('a layer has the value of its shot in the middle of the shot', () => {
  assert.equal(crossfade(shots, LAYER_TABLE.sim, 58), 1);
  assert.equal(crossfade(shots, LAYER_TABLE.convStrat, 115), 1);
  assert.equal(crossfade(shots, LAYER_TABLE.skewt, 5), 0.95);
});

test('values change smoothly across a shot change: no pop, within the range of the two shots', () => {
  let prev = crossfade(shots, LAYER_TABLE.funnel, 0);
  for (let t = 0; t <= 120; t += 1 / 30) {
    const v = crossfade(shots, LAYER_TABLE.funnel, t);
    assert.ok(Math.abs(v - prev) < 0.03, `step at ${t}`);
    assert.ok(v >= -1e-9 && v <= 1 + 1e-9);
    prev = v;
  }
});

test('all layer lists cover the six shots and stay within 0..1', () => {
  for (const [k, v] of Object.entries(LAYER_TABLE)) {
    assert.equal(v.length, 6, k);
    assert.ok(v.every((x) => x >= 0 && x <= 1), k);
  }
});

test('an override is a switch for the whole film', () => {
  const lv = new LayerVis(shots);
  assert.equal(lv.at('lma', 10), 1);
  lv.set('lma', 0);
  assert.equal(lv.at('lma', 10), 0);
  lv.set('lma', undefined);
  assert.equal(lv.at('lma', 10), 1);
});

test('without shots the establishing value is used', () => {
  assert.equal(crossfade([], LAYER_TABLE.radars, 5), 1);
});
