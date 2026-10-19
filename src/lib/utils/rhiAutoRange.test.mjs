// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { autoExtent, beamPoint, buildLookup, niceCeil } from './rhiGeometry.ts';

/** 100 rays 0.5..50 deg, 120 gates to 120 km: echo (30 dBZ) below `topKm`, `noise` everywhere else. */
function noisyScan(topKm, noise) {
  const nRays = 100;
  const nRange = 120;
  const elevation = Float32Array.from({ length: nRays }, (_, k) => 0.5 + 0.5 * k);
  const range = Float32Array.from({ length: nRange }, (_, g) => (g + 1) * 1000);
  const values = new Float32Array(nRays * nRange);
  for (let k = 0; k < nRays; k++) {
    for (let g = 0; g < nRange; g++) {
      const h = beamPoint(range[g], elevation[k]).heightM / 1000;
      values[k * nRange + g] = h < topKm ? 30 : noise;
    }
  }
  return buildLookup({ elevation, range, values, nRays, nRange });
}

test('niceCeil rounds up to a readable number', () => {
  assert.equal(niceCeil(13.3), 14);
  assert.equal(niceCeil(77.5), 80);
  assert.equal(niceCeil(9.2), 10);
  assert.equal(niceCeil(0), 1);
});

test('echo to 12 km with the noise masked: the height axis is about 12-14 km, not the ~95 km the beams reach', () => {
  for (const noise of [-9999, NaN]) {
    const e = autoExtent(noisyScan(12, noise));
    assert.ok(e.geometric.yMaxKm > 80, `beams reach ${e.geometric.yMaxKm} km`);
    assert.ok(e.yMaxKm >= 12 && e.yMaxKm <= 14.5, `height axis to ${e.yMaxKm} km`);
    assert.equal(e.capped, false);
    assert.ok(e.xMaxKm > 20 && e.xMaxKm < 125);
    assert.equal(e.yMinKm, 0);
    assert.equal(e.xMinKm, 0);
  }
});

test('noise below the colour range does not count either', () => {
  const lk = noisyScan(12, -30);
  assert.ok(autoExtent(lk, -20).yMaxKm <= 14.5);
  assert.equal(autoExtent(lk, -100).yMaxKm, 25); // counted as data: cut at the cap
});

test('valid noise everywhere is cut at the 25 km cap; the cap can be raised or lifted', () => {
  const lk = noisyScan(12, 5);
  const e = autoExtent(lk);
  assert.equal(e.yMaxKm, 25);
  assert.equal(e.capped, true);
  assert.equal(autoExtent(lk, -100, { capKm: 40 }).yMaxKm, 40);
  assert.equal(autoExtent(lk, -100, { capKm: 10 }).yMaxKm, 10);
  const full = autoExtent(lk, -100, { full: true });
  assert.ok(full.yMaxKm > 80 && full.capped === false);
});

test('stray gates above the echo do not stretch the axes', () => {
  const lk = noisyScan(8, -9999);
  const { field } = lk;
  for (let g = 100; g < 102; g++) field.values[90 * field.nRange + g] = 10; // a few isolated gates high up
  const e = autoExtent(lk);
  assert.ok(e.yMaxKm <= 10, `got ${e.yMaxKm}`);
});

test('nothing valid falls back to the capped scan extent', () => {
  const e = autoExtent(noisyScan(0, -9999));
  assert.equal(e.yMaxKm, 25);
  assert.ok(e.xMaxKm > 100);
});
