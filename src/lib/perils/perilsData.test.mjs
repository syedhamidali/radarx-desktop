// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { seriesAt, stationReadout, stationHasData, nearestSounding, balloonAt, tornadoAt, rotationAt, interpWeights, observedStepAt, clockUtc, fmtOffset, compass16 } from './perilsData.ts';

test('a series is interpolated between samples but never across a gap or outside its span', () => {
  const t = [0, 10, 20, 400];
  const v = [1, 3, null, 5];
  assert.equal(seriesAt(t, v, 5), 2);
  assert.equal(seriesAt(t, v, -1), null);
  assert.equal(seriesAt(t, v, 401), null);
  assert.equal(seriesAt(t, v, 10), 3);
  // a missing neighbour: nothing is made up for the half of the interval next to it
  assert.equal(seriesAt(t, v, 12), 3);
  assert.equal(seriesAt(t, v, 18), null);
  // a long gap holds the nearer sample instead of drawing a line across it
  assert.equal(seriesAt([0, 1000], [2, 4], 100, 120), 2);
  assert.equal(seriesAt([0, 1000], [2, 4], 900, 120), 4);
  assert.equal(seriesAt([], [], 1), null);
});

test('station read-out and whether the series covers the time', () => {
  const st = { id: 's', series: { t_s: [0, 60], rain_rate_mmh: [0, 60], dm_mm: [null, 2], wind_speed_ms: [4, 8] } };
  const r = stationReadout(st, 30);
  assert.equal(r.rain, 30);
  assert.equal(r.windMs, 6);
  assert.equal(r.dbz, null);
  assert.equal(stationHasData(st, 30), true);
  assert.equal(stationHasData(st, 90), false);
});

test('the sounding nearest in time and its offset from the clip time', () => {
  const s = [{ id: 'a', launch: -1800 }, { id: 'b', launch: 400 }, { id: 'c', launch: 3000 }];
  assert.deepEqual({ id: nearestSounding(s, 100).snd.id, off: nearestSounding(s, 100).offsetS }, { id: 'b', off: 300 });
  assert.equal(nearestSounding(s, -1000).snd.id, 'a');
  assert.equal(nearestSounding([], 0), null);
});

test('the balloon exists only during its flight and moves along the drift track', () => {
  const sn = { drift: [[0, 0, 0, 0.1], [100, 10, 20, 5]] };
  assert.ok(balloonAt(sn, 50).every((v, i) => Math.abs(v - [5, 10, 2.55][i]) < 1e-9));
  assert.equal(balloonAt(sn, -1), null);
  assert.equal(balloonAt(sn, 101), null);
  assert.equal(balloonAt({ drift: [] }, 0), null);
});

test('tornado position only inside its surveyed span; between survey points it is interpolated', () => {
  const tor = { path: [{ t: 100, x: 0, y: 0 }, { t: 200, x: 10, y: 0 }, { t: 300, x: 10, y: 10 }] };
  assert.equal(tornadoAt(tor, 50), null);
  assert.equal(tornadoAt(tor, 301), null);
  const a = tornadoAt(tor, 150);
  assert.deepEqual([a.x, a.y, a.interpolated], [5, 0, true]);
  const b = tornadoAt(tor, 200);
  assert.deepEqual([b.x, b.y, b.interpolated], [10, 0, false]);
});

test('rotation track point within a tolerance in time', () => {
  const tr = { points: [{ t: 0, x: 0, y: 0, vrot_ms: 20 }, { t: 60, x: 1, y: 1, vrot_ms: 30 }] };
  assert.equal(rotationAt(tr, 55, 10).vrot_ms, 30);
  assert.equal(rotationAt(tr, 30, 10), null);
});

test('interpolation weights between observed steps sum to one and follow the position in the interval', () => {
  const steps = [0, 120, 240];
  const w = interpWeights(steps, 150);
  assert.equal(w.k, 1);
  assert.ok(Math.abs(w.s - 0.25) < 1e-12);
  assert.ok(Math.abs(w.wA + w.wB - 1) < 1e-12);
  assert.equal(interpWeights(steps, 0).wB, 0);
  assert.equal(interpWeights(steps, 999).wA, 0);
  assert.equal(interpWeights([5], 3).wA, 1);
});

test('an observed frame is one within the tolerance of a step; anything else is interpolated', () => {
  const steps = [0, 120, 240];
  assert.equal(observedStepAt(steps, 121, 6), 1);
  assert.equal(observedStepAt(steps, 130, 6), -1);
  assert.equal(observedStepAt(steps, 240, 6), 2);
});

test('labels', () => {
  assert.equal(clockUtc('2022-03-30T22:00:00Z', 125), '22:02:05');
  assert.equal(fmtOffset(12), '+12 s');
  assert.equal(fmtOffset(-90), '-90 s');
  assert.equal(fmtOffset(-300), '-5 min');
  assert.equal(compass16(0), 'N');
  assert.equal(compass16(255), 'WSW');
  assert.equal(compass16(-10), 'N');
});
