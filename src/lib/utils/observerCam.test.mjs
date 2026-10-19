// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  bearingDeg, bearingDiff, placeObserver, observerPose, horizontalFovDeg, elevationDeg, compassLabel, OBSERVER_DEFAULTS,
} from './observerCam.ts';

test('bearings: clockwise from north, wrapped differences', () => {
  assert.equal(bearingDeg(0, 1), 0);
  assert.equal(bearingDeg(1, 0), 90);
  assert.equal(bearingDeg(0, -1), 180);
  assert.equal(bearingDeg(-1, 0), 270);
  assert.ok(Math.abs(bearingDiff(350, 10) - 20) < 1e-9);
  assert.ok(Math.abs(bearingDiff(10, 350) + 20) < 1e-9);
  assert.equal(compassLabel(0), 'N');
  assert.equal(compassLabel(55), 'NE');
  assert.equal(compassLabel(359), 'N');
  assert.equal(compassLabel(270), 'W');
});

test('the observer stands beside the track, upwind, and looks at it', () => {
  // a storm moving east: the right-hand side is south; upwind is west
  const obs = placeObserver([0, 0], 90, { distKm: 14, upwindKm: 6, side: 'right' });
  assert.ok(Math.abs(obs[0] + 6) < 1e-9 && Math.abs(obs[1] + 14) < 1e-9, `${obs}`);
  const left = placeObserver([0, 0], 90, { distKm: 14, upwindKm: 6, side: 'left' });
  assert.ok(Math.abs(left[1] - 14) < 1e-9);
  // for a storm moving north-east the observer is south-east of the track (right-hand side)
  const ne = placeObserver([0, 0], 45, { distKm: 10, upwindKm: 0 });
  assert.ok(ne[0] > 0 && ne[1] < 0 && Math.abs(Math.hypot(ne[0], ne[1]) - 10) < 1e-9);
  // an explicit place wins
  assert.deepEqual(placeObserver([0, 0], 45, { x: 3, y: 4 }), [3, 4]);
  // defaults are the documented ones
  assert.equal(OBSERVER_DEFAULTS.distKm, 10);
  const def = placeObserver([0, 0], 55);
  assert.ok(Math.abs(Math.hypot(def[0] - 0, def[1] - 0)) > 10);
});

test('the camera turns with the storm by the pan fraction', () => {
  const obs = [0, -10];
  const anchor = [0, 0];
  // the storm is where the camera looks: no turn
  assert.ok(Math.abs(observerPose(obs, anchor, anchor, { pan: 0.5 }).yawDeg - 0) < 1e-9);
  // the storm moved 10 km east: bearing 45 degrees, half of it with pan 0.5
  const p = observerPose(obs, [10, 0], anchor, { pan: 0.5 });
  const b1 = bearingDeg(10, 10);
  assert.ok(Math.abs(p.yawDeg - 0.5 * b1) < 1e-9, `yaw ${p.yawDeg}`);
  assert.equal(observerPose(obs, [10, 0], anchor, { pan: 0 }).yawDeg, 0);
  assert.ok(Math.abs(observerPose(obs, [10, 0], anchor, { pan: 1 }).yawDeg - b1) < 1e-9);
  assert.equal(observerPose(obs, anchor, anchor, { pitchDeg: 12 }).pitchDeg, 12);
  // wrap-around: looking north, the storm moves west of north
  const w = observerPose([0, -10], [-10, 0], anchor, { pan: 1 });
  assert.ok(Math.abs(w.yawDeg - 315) < 1e-9);
});

test('field of view and the elevation of a cloud top', () => {
  const tanY = Math.tan(Math.PI / 8); // 45 degrees vertical
  assert.ok(Math.abs(horizontalFovDeg(tanY, 16 / 9) - 72.7) < 0.2);
  assert.ok(Math.abs(elevationDeg(10, 10) - 45) < 1e-9);
  assert.ok(Math.abs(elevationDeg(20, 14, 0.01) - Math.atan2(13.99, 20) * 180 / Math.PI) < 1e-9);
});
