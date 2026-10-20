// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { simDistanceWeight, simSpeedWeight } from './perilsTornadoSim.ts';
import { crossfade, LAYER_TABLE } from './perilsLayers.ts';

test('the simulation is opaque near the tornado, absent far from it, and smooth in between', () => {
  assert.equal(simDistanceWeight(0.08), 1);
  assert.equal(simDistanceWeight(1.8), 1);
  assert.equal(simDistanceWeight(4.5), 0);
  assert.equal(simDistanceWeight(30), 0);
  let prev = 1;
  for (let d = 1.8; d <= 4.5; d += 0.05) {
    const w = simDistanceWeight(d);
    assert.ok(w <= prev + 1e-12 && w >= 0 && w <= 1);
    prev = w;
  }
  assert.ok(Math.abs(simDistanceWeight(3.15) - 0.5) < 1e-9);
});

const shots = [
  { name: 'establishing', t0: 0, t1: 15 }, { name: 'flyby', t0: 15, t1: 46 }, { name: 'tornado', t0: 46, t1: 70 },
  { name: 'window', t0: 70, t1: 90 }, { name: 'rise', t0: 90, t1: 105 }, { name: 'topdown', t0: 105, t1: 120 },
];

test('the simulation belongs to the tornado shot; the particle funnel to the wide shots', () => {
  assert.equal(crossfade(shots, LAYER_TABLE.sim, 58), 1);
  for (const t of [5, 30, 80, 100, 115]) assert.equal(crossfade(shots, LAYER_TABLE.sim, t), 0);
  assert.equal(crossfade(shots, LAYER_TABLE.funnel, 58), 1); // drawn only where the simulation is not (opacity 1 - simW)
  assert.ok(crossfade(shots, LAYER_TABLE.funnel, 30) > 0.5);
  assert.equal(crossfade(shots, LAYER_TABLE.hail, 58), 0);
});

test('the simulation is on in real time and in a modest time-lapse, and fades out as the data clock speeds up', () => {
  assert.equal(simSpeedWeight(1), 1);
  assert.equal(simSpeedWeight(6), 1);
  assert.equal(simSpeedWeight(30), 0);
  assert.equal(simSpeedWeight(43), 0);
  let prev = 1;
  for (let s = 6; s <= 30; s += 0.5) {
    const w = simSpeedWeight(s);
    assert.ok(w <= prev + 1e-12 && w >= 0);
    prev = w;
  }
});

const focusShots = [
  { name: 'establishing', t0: 0, t1: 9 }, { name: 'flyby', t0: 9, t1: 21 },
  { name: 'focus_wide', t0: 21, t1: 26, overlap: 5 }, { name: 'focus_chaser', t0: 26, t1: 45.5, overlap: 5 },
  { name: 'focus_close', t0: 45.5, t1: 56.5, overlap: 3 }, { name: 'focus_low', t0: 56.5, t1: 63, overlap: 3 },
  { name: 'focus_swath', t0: 63, t1: 71, overlap: 3.5 }, { name: 'focus_top', t0: 71, t1: 77, overlap: 4 },
  { name: 'window', t0: 77, t1: 85 }, { name: 'rise', t0: 85, t1: 91 }, { name: 'topdown', t0: 91, t1: 120 },
];

test('the close angles of the tornado focus use the simulation and the focus inset, and not the old plan view', () => {
  for (const t of [35, 50, 60, 66]) {
    assert.equal(crossfade(focusShots, LAYER_TABLE.sim, t), 1, `sim at ${t}`);
    assert.equal(crossfade(focusShots, LAYER_TABLE.focusInset, t), 1);
    assert.equal(crossfade(focusShots, LAYER_TABLE.minimap, t), 0);
    assert.equal(crossfade(focusShots, LAYER_TABLE.immersion, t), 1);
    assert.equal(crossfade(focusShots, LAYER_TABLE.captions, t), 0); // one lower third, no science captions on top of it
  }
  for (const t of [5, 15, 80, 100]) assert.equal(crossfade(focusShots, LAYER_TABLE.focusInset, t), 0);
  assert.equal(crossfade(focusShots, LAYER_TABLE.immersion, 23), 0); // the wide and the top angles are not immersive
  assert.ok(crossfade(focusShots, LAYER_TABLE.funnel, 33) > 0.9 && crossfade(focusShots, LAYER_TABLE.funnel, 74) < 0.2);
});

test('the glide into a shot follows the shot\'s own overlap', () => {
  // 'focus_close' glides over 3 s centred on 45.5: the previous shot's value before 44, its own after 47
  const before = crossfade(focusShots, LAYER_TABLE.rotSeries, 43.9);
  const after = crossfade(focusShots, LAYER_TABLE.rotSeries, 47.1);
  assert.ok(Math.abs(before - 0.85) < 1e-9 && Math.abs(after - 0.7) < 1e-9, `${before} ${after}`);
  const mid = crossfade(focusShots, LAYER_TABLE.rotSeries, 45.5);
  assert.ok(mid < 0.85 && mid > 0.7);
});
