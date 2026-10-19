// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  hermite, cameraAt, stormAt, dataTimeAt, locate, morphShifts, intervalMotion, utcLabel, stormChoreography, nearTo, trackFrame, lagTarget,
} from './clipCamera.ts';

test('hermite passes through the keyframes, eases at both ends and has no jumps', () => {
  const ts = [0, 5, 12, 20];
  const vs = [0, 10, 4, 30];
  for (let i = 0; i < ts.length; i++) assert.ok(Math.abs(hermite(ts, vs, ts[i]) - vs[i]) < 1e-9);
  // zero speed at both ends
  const eps = 1e-4;
  assert.ok(Math.abs(hermite(ts, vs, eps) - vs[0]) < 1e-6);
  assert.ok(Math.abs(hermite(ts, vs, 20 - eps) - vs[3]) < 1e-6);
  // continuous velocity: the numerical derivative does not jump across a keyframe
  const d = (t) => (hermite(ts, vs, t + 1e-3) - hermite(ts, vs, t - 1e-3)) / 2e-3;
  for (const k of [5, 12]) assert.ok(Math.abs(d(k - 1e-2) - d(k + 1e-2)) < 0.05, `velocity jump at ${k}`);
  // clamped outside the range
  assert.equal(hermite(ts, vs, -3), 0);
  assert.equal(hermite(ts, vs, 99), 30);
});

test('a smooth path stays inside the keyframe range when the keyframes are monotone', () => {
  const ts = [0, 10, 20, 30];
  const vs = [0, 1, 2, 10];
  let prev = -Infinity;
  for (let t = 0; t <= 30; t += 0.25) {
    const v = hermite(ts, vs, t);
    assert.ok(v >= prev - 1e-9, 'monotone');
    prev = v;
  }
});

test('the storm choreography is smooth: no frame-to-frame jumps over the whole clip', () => {
  const keys = stormChoreography(36, 56);
  assert.equal(keys[0].t, 0);
  assert.equal(keys[keys.length - 1].t, 36);
  const fps = 30;
  let prev = cameraAt(keys, 0);
  let maxTheta = 0, maxPhi = 0, maxDist = 0, accel = 0, prevD = null;
  for (let f = 1; f <= 36 * fps; f++) {
    const c = cameraAt(keys, f / fps);
    const d = [c.theta - prev.theta, c.phi - prev.phi, c.dist - prev.dist];
    maxTheta = Math.max(maxTheta, Math.abs(d[0]));
    maxPhi = Math.max(maxPhi, Math.abs(d[1]));
    maxDist = Math.max(maxDist, Math.abs(d[2]));
    if (prevD) accel = Math.max(accel, Math.abs(d[0] - prevD[0]) + Math.abs(d[1] - prevD[1]) + Math.abs(d[2] - prevD[2]) / 10);
    prevD = d;
    prev = c;
    assert.ok(c.phi >= 4 && c.phi <= 88, `phi ${c.phi} at frame ${f}`);
    assert.ok(c.dist > 60 && c.dist < 240, `dist ${c.dist} at frame ${f}`);
  }
  assert.ok(maxTheta < 3.5, `orbit speed ${maxTheta} deg/frame`);
  assert.ok(maxPhi < 1.5 && maxDist < 3, `phi ${maxPhi} dist ${maxDist} per frame`);
  assert.ok(accel < 1.0, `angular acceleration ${accel}`);
});

test('the choreography ends where it started (back to the oblique shot) and passes a low and a top-down view', () => {
  const keys = stormChoreography(36, 56);
  const start = cameraAt(keys, 0);
  const end = cameraAt(keys, 36);
  assert.ok(Math.abs((((end.theta - start.theta) % 360) + 360) % 360) < 1e-6);
  assert.ok(Math.abs(end.phi - start.phi) < 1e-9 && Math.abs(end.dist - start.dist) < 1e-9);
  const phis = Array.from({ length: 361 }, (_, i) => cameraAt(keys, i / 10).phi);
  assert.ok(Math.min(...phis) <= 7, 'a low-angle view');
  assert.ok(Math.max(...phis) >= 84, 'a top-down view');
  // the low-angle view is taken from behind the storm (camera opposite to its heading, within 60 degrees)
  const low = keys.find((k) => k.phi === 6);
  const behind = (56 + 180) % 360;
  const diff = Math.abs((((low.theta - behind) % 360) + 540) % 360 - 180);
  assert.ok(diff < 60, `low view azimuth ${low.theta} vs behind ${behind}`);
  // a clip of another length keeps its shape
  const k2 = stormChoreography(18, 56);
  assert.equal(k2[k2.length - 1].t, 18);
  assert.ok(Math.abs(cameraAt(k2, 9).theta - cameraAt(keys, 18).theta) < 1e-6);
});

test('nearTo keeps turns short', () => {
  assert.equal(nearTo(10, 370), 370);
  assert.equal(nearTo(350, 10), -10);
  assert.equal(nearTo(180, 560), 540);
});

test('the storm centre follows the tracked positions and keeps moving at the ends', () => {
  const track = [{ t: 0, x: 0, y: 0 }, { t: 100, x: 10, y: 5 }, { t: 200, x: 30, y: 15 }];
  for (const p of track) {
    const [x, y] = stormAt(track, p.t);
    assert.ok(Math.abs(x - p.x) < 1e-9 && Math.abs(y - p.y) < 1e-9);
  }
  const [xm] = stormAt(track, 50);
  assert.ok(xm > 0 && xm < 10);
  // before the first and after the last observation it continues at the end velocity
  const [x0] = stormAt(track, -10);
  assert.ok(Math.abs(x0 - -1) < 1e-9);
  const [x1] = stormAt(track, 210);
  assert.ok(Math.abs(x1 - 32) < 1e-9);
  // a single point stays
  assert.deepEqual(stormAt([{ t: 0, x: 3, y: 4 }], 99), [3, 4]);
});

test('data time: holds on the first and last observation, runs between', () => {
  const t0 = 1000, t1 = 1660;
  assert.equal(dataTimeAt(0, 36, t0, t1), t0);
  assert.equal(dataTimeAt(1.4, 36, t0, t1), t0);
  assert.equal(dataTimeAt(34.6, 36, t0, t1), t1);
  assert.equal(dataTimeAt(36, 36, t0, t1), t1);
  let prev = t0;
  for (let t = 0; t <= 36; t += 0.1) {
    const v = dataTimeAt(t, 36, t0, t1);
    assert.ok(v >= prev - 1e-9, 'never runs backwards');
    prev = v;
  }
  const mid = dataTimeAt(18, 36, t0, t1);
  assert.ok(Math.abs(mid - (t0 + t1) / 2) < 1, 'symmetric');
});

test('locating the interval between observations', () => {
  const ts = [0, 330, 660];
  assert.deepEqual(locate(ts, -5), { i: 0, s: 0 });
  assert.deepEqual(locate(ts, 165), { i: 0, s: 0.5 });
  assert.deepEqual(locate(ts, 330), { i: 0, s: 1 });
  const b = locate(ts, 495);
  assert.equal(b.i, 1);
  assert.equal(b.s, 0.5);
  assert.deepEqual(locate(ts, 700), { i: 1, s: 1 });
});

test('time interpolation: A is carried forward, B back, so a moving echo stays on the true path', () => {
  // an echo at x = 0 in volume A (t = 0) and at x = 10 km in volume B (t = 100 s): speed 0.1 km/s
  const m = [0.1, 0];
  const dt = 100;
  for (const s of [0, 0.25, 0.5, 1]) {
    const { shiftA, shiftB } = morphShifts(m, dt, s);
    const truePos = 10 * s;
    // the position at which A shows its echo: pos - shiftA = 0  =>  pos = shiftA
    assert.ok(Math.abs(shiftA[0] - truePos) < 1e-9);
    // and where B shows its echo: pos - shiftB = 10  =>  pos = 10 + shiftB
    assert.ok(Math.abs(10 + shiftB[0] - truePos) < 1e-9);
  }
  assert.deepEqual(morphShifts(m, dt, 0).shiftA, [0, 0, 0]);
  const motion = intervalMotion([{ t: 0, x: 0, y: 0 }, { t: 100, x: 10, y: -5 }], 0);
  assert.deepEqual(motion, [0.1, -0.05]);
});

test('UTC label', () => {
  assert.equal(utcLabel('2021-12-11T01:53:57+00:00', 0), '01:53:57 UTC');
  assert.equal(utcLabel('2021-12-11T01:53:57+00:00', 363), '02:00:00 UTC');
  assert.equal(utcLabel('2021-12-11T01:53:57+00:00', 363, false), '02:00 UTC');
});

test('the shown box is fixed around the track and the camera target follows only a fraction of the storm motion', () => {
  // a storm moving 21 km NE in 18 min (the Mayfield track)
  const track = [{ x: -9.2, y: -5.7 }, { x: -3.5, y: -1.3 }, { x: 1.9, y: 2.7 }, { x: 2.8, y: 3.3 }, { x: 7.8, y: 6.4 }];
  const { anchor, cropHalf, extent } = trackFrame(track, 60);
  assert.ok(Math.abs(anchor[0] - (-0.7)) < 1e-9 && Math.abs(anchor[1] - 0.35) < 1e-9);
  assert.ok(Math.abs(extent - 17.0) < 1e-9);
  assert.ok(Math.abs(cropHalf - 68.5) < 1e-9);
  // every track point stays well inside the fixed box
  for (const p of track) assert.ok(Math.abs(p.x - anchor[0]) < cropHalf - 50 && Math.abs(p.y - anchor[1]) < cropHalf - 50);
  // lag 0 = fixed target, lag 1 = locked on the storm, 0.3 = the storm travels 70 % of its path across the view
  assert.deepEqual(lagTarget(anchor, [7.8, 6.4], 0), anchor);
  const locked = lagTarget(anchor, [7.8, 6.4], 1);
  assert.ok(Math.abs(locked[0] - 7.8) < 1e-9 && Math.abs(locked[1] - 6.4) < 1e-9);
  const t = lagTarget(anchor, [7.8, 6.4], 0.3);
  const first = lagTarget(anchor, [-9.2, -5.7], 0.3);
  const onScreen = Math.hypot(7.8 - t[0] - (-9.2 - first[0]), 6.4 - t[1] - (-5.7 - first[1]));
  const path = Math.hypot(7.8 + 9.2, 6.4 + 5.7);
  assert.ok(Math.abs(onScreen / path - 0.7) < 1e-9);
});
