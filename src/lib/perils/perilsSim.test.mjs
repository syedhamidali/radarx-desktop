// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { simDistanceWeight } from './perilsTornadoSim.ts';
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
