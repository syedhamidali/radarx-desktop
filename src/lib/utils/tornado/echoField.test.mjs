// @ts-nocheck - plain node:test file, not part of the app bundle
import test from 'node:test';
import assert from 'node:assert/strict';
import { rainRate, makeEchoSeries, echoRainAt } from './echoField.ts';

const grid = (t, blobX, n = 40, dx = 500) => {
  // a 50 dBZ blob (3 cells wide) on a 20 dBZ background, its centre at column `blobX`
  const dbz = [];
  for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) dbz.push(Math.abs(i - blobX) <= 1 && Math.abs(j - 20) <= 1 ? 50 : 20);
  return { x0: 0, y0: 0, dx, nx: n, ny: n, dbz, t };
};

const centroid = (R, n = 40) => {
  let sx = 0, s = 0;
  for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
    const v = Math.max(0, R[j * n + i] - rainRate(20) * 1.01);
    sx += v * (i + 0.5);
    s += v;
  }
  return sx / s;
};

test('rain rate follows Marshall-Palmer, is zero for no echo and capped', () => {
  assert.equal(rainRate(null), 0);
  assert.equal(rainRate(NaN), 0);
  assert.equal(rainRate(3), 0);
  assert.ok(Math.abs(rainRate(40) - Math.pow(10000 / 200, 1 / 1.6)) < 1e-9);
  assert.equal(rainRate(75), 120);
});

test('between two volumes the echo is carried along the motion, not cross-faded', () => {
  // the blob moves from column 10 to column 16 in 60 s: 6 cells = 3 km, 50 m/s
  const s = makeEchoSeries([grid(0, 10), grid(60, 16)], [50, 0]);
  const R0 = echoRainAt(s, 0), R30 = echoRainAt(s, 30), R60 = echoRainAt(s, 60);
  assert.ok(Math.abs(centroid(R0) - 10.5) < 0.2);
  assert.ok(Math.abs(centroid(R60) - 16.5) < 0.2);
  assert.ok(Math.abs(centroid(R30) - 13.5) < 0.3, `centroid ${centroid(R30)}`);
  // one blob in the middle, not two half-strength ones
  const peak = Math.max(...R30);
  assert.ok(peak > 0.8 * rainRate(50), `peak ${peak}`);
});

test('before the first and after the last volume the nearest one is carried on; a single volume works', () => {
  const s = makeEchoSeries([grid(0, 10), grid(60, 16)], [50, 0]);
  assert.ok(Math.abs(centroid(echoRainAt(s, -20)) - 8.5) < 0.3); // 20 s * 50 m/s = 2 cells back
  assert.ok(Math.abs(centroid(echoRainAt(s, 80)) - 18.5) < 0.3);
  const one = makeEchoSeries([grid(0, 10)], [0, 0]);
  assert.ok(Math.abs(centroid(echoRainAt(one, 500)) - 10.5) < 0.2);
  assert.equal(makeEchoSeries([], [0, 0]), null);
});

test('missing values are no rain and the buffer is reused', () => {
  const g = grid(0, 10);
  g.dbz[5] = null;
  const s = makeEchoSeries([g], [0, 0]);
  const out = new Float32Array(g.nx * g.ny);
  const R = echoRainAt(s, 0, out);
  assert.equal(R, out);
  assert.equal(R.length, 1600);
  assert.ok(R.every((v) => Number.isFinite(v) && v >= 0));
});
