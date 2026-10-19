// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { Director, bracket, lerpAt, angleDiff, lerpAngle, nearestStation } from './perilsDirector.ts';

/** A small dense timeline like the one choreography.py writes: 10 s at 10 fps, speed 1 then 0.5 then 1. */
function fixture() {
  const fps = 10;
  const n = 100;
  const t = Array.from({ length: n + 1 }, (_, i) => i / fps);
  const sp = t.map((v) => (v > 4 && v < 6 ? 0.5 : 1));
  const td = [0];
  for (let i = 1; i <= n; i++) td.push(td[i - 1] + ((sp[i] + sp[i - 1]) / 2) / fps);
  const k = 60 / td[n];
  const tdn = td.map((v) => v * k);
  const tl = {
    data_start: '2022-03-30T22:00:00Z', data_end: '2022-03-30T22:01:00Z', data_window_s: 60, clip_duration_s: 10, fps, frames: n,
    speedup_mean: 6, speedup_min: 3, speedup_max: 6, tornado_slowdown: null, mapping: { t_clip_s: t, t_data_s: tdn }, speedup: sp.map((v) => v * k),
    shots: [{ name: 'a', label: 'A', t0: 0, t1: 5 }, { name: 'b', label: 'B', t0: 5, t1: 10 }],
  };
  const cam = t.map((v, i) => ({ t_clip_s: v, lat: 0, lon: 0, alt_m: 1000, heading_deg: (350 + 2 * v) % 360, pitch_deg: -10 + v, x_km: v * 2, y_km: -v, z_km: 1 + v, vexag: 2, shot: v < 5 ? 'a' : 'b' }));
  return new Director(tl, cam);
}

test('data time is monotone, starts at 0 and ends at the window', () => {
  const d = fixture();
  assert.equal(d.dataTime(0), 0);
  assert.ok(Math.abs(d.dataTime(10) - 60) < 1e-9);
  let prev = -1;
  for (let i = 0; i <= 1000; i++) {
    const v = d.dataTime(i / 100);
    assert.ok(v >= prev, 'never backwards');
    prev = v;
  }
});

test('the slow-down shows as a lower speed-up and clip time is the inverse of data time', () => {
  const d = fixture();
  assert.ok(d.speedup(5) < d.speedup(2));
  for (const t of [0.3, 2.5, 4.2, 5.5, 7.77, 9.9]) assert.ok(Math.abs(d.clipTimeOfData(d.dataTime(t)) - t) < 1e-6);
});

test('lookups clamp outside the clip', () => {
  const d = fixture();
  assert.equal(d.dataTime(-3), 0);
  assert.ok(Math.abs(d.dataTime(99) - 60) < 1e-9);
  assert.equal(d.pose(-1).x, 0);
});

test('pose is interpolated between frames and takes the short way round the compass', () => {
  const d = fixture();
  const p = d.pose(2.55);
  assert.ok(Math.abs(p.x - 5.1) < 1e-9);
  assert.ok(Math.abs(p.pitchDeg - (-10 + 2.55)) < 1e-9);
  // heading wraps from 359.x to 0.x without a swing through 180
  const w = d.pose(4.95);
  assert.ok(w.yawDeg > 355 || w.yawDeg < 5);
  assert.equal(d.pose(7).shot, 'b');
});

test('no jump in position or heading between neighbouring frames', () => {
  const d = fixture();
  for (let i = 0; i < 1000; i++) {
    const a = d.pose(i / 100);
    const b = d.pose((i + 1) / 100);
    assert.ok(Math.hypot(b.x - a.x, b.y - a.y) < 0.5);
    assert.ok(Math.abs(angleDiff(a.yawDeg, b.yawDeg)) < 1);
  }
});

test('shot at a clip time', () => {
  const d = fixture();
  assert.equal(d.shotAt(1).name, 'a');
  assert.equal(d.shotAt(5).name, 'b');
  assert.equal(d.shotAt(9.99).label, 'B');
});

test('angle helpers', () => {
  assert.equal(angleDiff(350, 10), 20);
  assert.equal(angleDiff(10, 350), -20);
  assert.ok(Math.abs(lerpAngle(350, 10, 0.5) - 0) < 1e-9 || Math.abs(lerpAngle(350, 10, 0.5) - 360) < 1e-9);
  assert.deepEqual(bracket([0, 1, 2], 5), { i: 1, f: 1 });
  assert.equal(lerpAt([0, 10], [0, 100], 2.5), 25);
});

test('the station nearest the camera, from the 3-D distance (height counts)', () => {
  const st = [{ id: 'A', x: 0, y: 0 }, { id: 'B', x: 10, y: 0 }, { id: 'C', x: 3, y: 4 }];
  assert.equal(nearestStation(st, [1, 0, 0.002]).id, 'A');
  assert.equal(nearestStation(st, [9, 1, 0.5]).id, 'B');
  // high above C but A and B further away horizontally: C is nearest
  assert.equal(nearestStation(st, [3, 4, 20]).id, 'C');
  assert.equal(nearestStation([], [0, 0, 0]), null);
  // ties keep the first
  assert.equal(nearestStation([{ id: 'X', x: 1, y: 0 }, { id: 'Y', x: -1, y: 0 }], [0, 0, 0]).id, 'X');
});
