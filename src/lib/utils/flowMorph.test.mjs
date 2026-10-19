// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  decodeWindStep, stormRelativeField, traceBack, traceForward, morphCell, solidBodyRotation,
} from './flowMorph.ts';

const blob = (cx, cy, sig) => (x, y) => Math.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * sig * sig));
const sampler = (f) => (x, y) => ({ val: f(x, y), cov: 1 });

function angleOf(x, y, cx, cy) { return Math.atan2(y - cy, x - cx); }

/** Intensity-weighted peak position of the interpolated field on a grid around the centre. */
function peak(fieldAt, cx, cy, R) {
  let best = -1, bx = 0, by = 0;
  for (let y = cy - R; y <= cy + R; y += 0.5) {
    for (let x = cx - R; x <= cx + R; x += 0.5) {
      const r = fieldAt(x, y);
      const v = r ? r.val * r.cov : 0;
      if (v > best) { best = v; bx = x; by = y; }
    }
  }
  return { v: best, x: bx, y: by };
}

test('tracing along a solid-body rotation turns a point by omega * t (back and forward)', () => {
  const omega = 2e-3; // rad/s: an eyewall at 30 km radius turns at 60 m/s
  const vel = solidBodyRotation(0, 0, omega);
  const p = [30, 0, 2];
  const f = traceForward(p, vel, 300);
  assert.ok(Math.abs(angleOf(f[0], f[1], 0, 0) - omega * 300) < 0.01, `angle ${angleOf(f[0], f[1], 0, 0)}`);
  assert.ok(Math.abs(Math.hypot(f[0], f[1]) - 30) < 0.3, 'radius is kept');
  const b = traceBack(p, vel, 300);
  assert.ok(Math.abs(angleOf(b[0], b[1], 0, 0) + omega * 300) < 0.01);
});

test('interpolating a rotating vortex: the echo turns at the rate of the observed flow (a cross-fade would not)', () => {
  const omega = 2e-3, dt = 300; // 0.6 rad between the two scans
  const R = 30, sig = 5;
  const th0 = 0.2;
  const A = blob(R * Math.cos(th0), R * Math.sin(th0), sig);
  const B = blob(R * Math.cos(th0 + omega * dt), R * Math.sin(th0 + omega * dt), sig);
  const vel = solidBodyRotation(0, 0, omega);
  for (const s of [0, 0.25, 0.5, 0.75, 1]) {
    const at = (x, y) => morphCell(sampler(A), sampler(B), [x, y, 2], s, dt, vel);
    const pk = peak(at, 0, 0, 45);
    const want = th0 + omega * dt * s;
    const got = angleOf(pk.x, pk.y, 0, 0);
    assert.ok(Math.abs(got - want) < 0.03, `s=${s}: angle ${got.toFixed(3)} vs ${want.toFixed(3)}`);
    assert.ok(Math.abs(Math.hypot(pk.x, pk.y) - R) < 1.0, 'stays on its ring');
    assert.ok(pk.v > 0.9, `one sharp echo, not a double image (peak ${pk.v.toFixed(2)})`);
  }
  // the plain cross-fade of the two stationary pictures has two half-strength echoes at s = 0.5
  const cross = (x, y) => ({ val: 0.5 * A(x, y) + 0.5 * B(x, y), cov: 1 });
  assert.ok(peak(cross, 0, 0, 45).v < 0.6);
});

test('translation and rotation together: the vortex moves with the track and turns with the flow', () => {
  const omega = 1.5e-3, dt = 400;
  const track = [0.01, 0.006]; // km/s, about 11.6 m/s
  const R = 25, sig = 5, th0 = 1.0;
  const c0 = [0, 0];
  const c1 = [track[0] * dt, track[1] * dt];
  const pos = (c, th) => [c[0] + R * Math.cos(th), c[1] + R * Math.sin(th)];
  const pa = pos(c0, th0), pb = pos(c1, th0 + omega * dt);
  const A = blob(pa[0], pa[1], sig), B = blob(pb[0], pb[1], sig);
  // velocity of the field: translation + rotation about the (moving) centre; the centre at time s*dt is c0 + track * s * dt
  const velAt = (t) => (x, y) => {
    const cx = c0[0] + track[0] * t, cy = c0[1] + track[1] * t;
    return [track[0] - omega * (y - cy), track[1] + omega * (x - cx)];
  };
  // the shader uses one velocity field for both traces: the one at the interpolation time
  const s = 0.5;
  const vel = velAt(s * dt);
  const at = (x, y) => morphCell(sampler(A), sampler(B), [x, y, 2], s, dt, vel);
  const pk = peak(at, track[0] * s * dt, track[1] * s * dt, 40);
  const cm = [track[0] * s * dt, track[1] * s * dt];
  const want = pos(cm, th0 + omega * dt * s);
  assert.ok(Math.hypot(pk.x - want[0], pk.y - want[1]) < 1.5, `peak (${pk.x}, ${pk.y}) vs (${want[0].toFixed(1)}, ${want[1].toFixed(1)})`);
});

test('missing data in one volume: the other one is used (coverage weighted)', () => {
  const A = () => null;
  const B = () => ({ val: 0.7, cov: 1 });
  const r = morphCell(A, B, [0, 0, 1], 0.4, 300, () => [0, 0]);
  assert.ok(r && Math.abs(r.val - 0.7) < 1e-12);
  assert.equal(morphCell(A, A, [0, 0, 1], 0.4, 300, () => [0, 0]), null);
});

test('storm-relative flow: the echo mean is removed, so a rotation keeps its turning and loses the translation', () => {
  const nx = 21, ny = 21, nz = 2, dx = 2;
  const x0 = -20, y0 = -20;
  const u = new Float32Array(nx * ny * nz), v = new Float32Array(nx * ny * nz);
  const omega = 1.5e-3; // rad/s -> m/s per km: omega * 1000
  for (let k = 0; k < nz; k++) for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const x = x0 + i * dx, y = y0 + j * dy_(dx);
    const idx = (k * ny + j) * nx + i;
    u[idx] = 12 - omega * 1000 * y; // a 12 m/s eastward environment plus the rotation
    v[idx] = 5 + omega * 1000 * x;
  }
  function dy_(d) { return d; }
  u[3] = NaN; // a hole
  const w = { nx, ny, nz, x0, y0, z0: 1, dx, dy: dx, dz: 1, u, v, originLat: 36, originLon: -90 };
  const f = stormRelativeField(w, () => true);
  assert.ok(Math.abs(f.mean[0][0] - 12) < 0.2 && Math.abs(f.mean[0][1] - 5) < 0.2, 'the mean is the environment');
  // at (x=10, y=0) the anomaly is the rotation: v' = omega * x * 1000 = 15 m/s, u' ~ 0
  const i = Math.round((10 - x0) / dx), j = Math.round((0 - y0) / dx);
  const o = ((0 * ny + j) * nx + i) * 4;
  assert.ok(Math.abs(f.data[o] - 0) < 0.3 && Math.abs(f.data[o + 1] - 15) < 0.3, `anomaly ${f.data[o]}, ${f.data[o + 1]}`);
  assert.equal(f.data[o + 3], 1);
  // the hole has no confidence and no flow
  assert.equal(f.data[3 * 4 + 3], 0);
  assert.equal(f.data[3 * 4], 0);
  assert.equal(f.validCells, nx * ny * nz - 1);
});

test('wind steps decode from the bundle layout (u block then v block) and reject a wrong size', () => {
  const header = { nx: 2, ny: 2, nz: 1, x0_km: -1, y0_km: -1, z0_km: 0.5, dx_km: 2, dy_km: 2, dz_km: 0.5, origin: { lat: 36, lon: -90 }, n_radars: 3 };
  const f = new Float32Array([1, 2, 3, 4, -1, -2, -3, NaN]);
  const w = decodeWindStep(header, new Uint8Array(f.buffer));
  assert.ok(w && w.nx === 2 && w.u[3] === 4 && w.v[2] === -3 && Number.isNaN(w.v[3]) && w.nRadars === 3);
  assert.equal(decodeWindStep(header, new Uint8Array(12)), null);
});
