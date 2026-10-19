// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { rampColour, recentHail } from './perilsInsets.ts';
import { dsdColumn, raindropFall, codeValue, heightOfTemp } from './perilsData.ts';
import { windColour } from './perilsScene.ts';

test('the colour ramp runs from blue to red without leaving 0..255', () => {
  const lo = rampColour(0);
  const hi = rampColour(1);
  assert.ok(lo[2] > lo[0] && hi[0] > hi[2]);
  for (let f = -0.5; f <= 1.5; f += 0.1) assert.ok(rampColour(f).every((v) => v >= 0 && v <= 255));
});

test('a hail candidate shows for a short time after it was measured, never before', () => {
  const st = { events: [{ kind: 'hail_candidate', t_s: 100, max_d_mm: 12 }, { kind: 'other', t_s: 100 }] };
  assert.equal(recentHail(st, 99), null);
  assert.deepEqual(recentHail(st, 110), { maxD: 12, age: 10 });
  assert.equal(recentHail(st, 130), null);
  assert.equal(recentHail({ events: [] }, 1), null);
});

test('the spectrum column is the one nearest in time, within a tolerance', () => {
  const dsd = { t: [0, 10, 20], d: [1], logN: [[1], [2], [3]] };
  assert.equal(dsdColumn(dsd, 11), 1);
  assert.equal(dsdColumn(dsd, 19), 2);
  assert.equal(dsdColumn(dsd, 100, 30), -1);
  assert.equal(dsdColumn(undefined, 1), -1);
});

test('the raindrop fall speed curve (Atlas et al. 1973)', () => {
  assert.ok(Math.abs(raindropFall(1) - (9.65 - 10.3 * Math.exp(-0.6))) < 1e-12);
  assert.ok(raindropFall(0.5) < raindropFall(2) && raindropFall(2) < raindropFall(6));
  assert.ok(raindropFall(8) < 10);
});

test('time-height codes: 0 is missing, 1 and 255 are the ends of the range', () => {
  assert.equal(codeValue(0, 10, 20), null);
  assert.equal(codeValue(1, 10, 20), 10);
  assert.equal(codeValue(255, 10, 20), 20);
});

test('the height where a sounding crosses a temperature, from the surface up', () => {
  const lv = { height_msl_m: [100, 1000, 3000, 5000], temperature_c: [20, 12, 2, -8], pressure_hpa: [], dewpoint_c: [], u_ms: [], v_ms: [] };
  assert.ok(Math.abs(heightOfTemp(lv, 0) - 3400) < 1e-6);
  assert.ok(Math.abs(heightOfTemp(lv, -4) - 4200) < 1e-6);
  assert.equal(heightOfTemp(lv, -30), null);
  assert.equal(heightOfTemp(null, 0), null);
  // a profile with a gap in the data: the crossing is found between the levels that exist
  const gap = { ...lv, temperature_c: [20, null, 2, -8] };
  assert.ok(heightOfTemp(gap, 10) > 100 && heightOfTemp(gap, 10) < 3000);
});

test('wind arrow colour grows brighter with speed', () => {
  const slow = windColour(2);
  const fast = windColour(25);
  assert.ok(fast[0] > slow[0] && fast[1] > slow[1]);
});
