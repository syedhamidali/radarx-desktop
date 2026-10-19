// @ts-nocheck - plain node:test file, not part of the app bundle
import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveSeed, axisAt } from './seed.ts';
import { DebrisSim, DEBRIS_CLASSES, windField, terminalSpeed } from './debris.ts';
import { omega, windAt } from './vortexField.ts';

const SEED = {
  width: 366, ef: 'EF2',
  track: [{ t: 240, lat: 33.1008, lon: -88.4691 }, { t: 960, lat: 33.2105, lon: -88.3316 }],
  meso: { vrot: 11 },
  thermo: { t0: 15.7, td0: 15.4, p0: 994.4 },
};
const R = resolveSeed(SEED);

test('tracers advance around the axis at omega = v / r, per frame, within 5 %', () => {
  const V = R.vortex;
  const fps = 30, dt = 1 / fps;
  const f = (p) => windAt(V, p[0], p[1], p[2]);
  const rk4 = (p, h) => {
    const k1 = f(p);
    const k2 = f([p[0] + 0.5 * h * k1[0], p[1] + 0.5 * h * k1[1], p[2] + 0.5 * h * k1[2]]);
    const k3 = f([p[0] + 0.5 * h * k2[0], p[1] + 0.5 * h * k2[1], p[2] + 0.5 * h * k2[2]]);
    const k4 = f([p[0] + h * k3[0], p[1] + h * k3[1], p[2] + h * k3[2]]);
    return [p[0] + (h / 6) * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0]), p[1] + (h / 6) * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1]), p[2] + (h / 6) * (k1[2] + 2 * k2[2] + 2 * k3[2] + k4[2])];
  };
  let worst = 0;
  for (const [r0, z0] of [[0.5 * R.rm, 60], [R.rm, 100], [2 * R.rm, 150], [4 * R.rm, 300], [R.rm, 400]]) {
    let p = [r0, 0, z0];
    let phi = 0;
    for (let fr = 0; fr < 12; fr++) {
      const rA = Math.hypot(p[0], p[1]), zA = p[2];
      const a0 = Math.atan2(p[1], p[0]);
      for (let s = 0; s < 10; s++) p = rk4(p, dt / 10);
      let da = Math.atan2(p[1], p[0]) - a0;
      da = Math.atan2(Math.sin(da), Math.cos(da));
      const expect = omega(V, 0.5 * (rA + Math.hypot(p[0], p[1])), 0.5 * (zA + p[2])) * dt;
      const err = Math.abs(da - expect) / expect;
      worst = Math.max(worst, err);
      assert.ok(err < 0.05, `r=${r0} z=${z0} frame ${fr}: ${da} vs ${expect}`);
      phi += da;
    }
    assert.ok(phi > 0, 'cyclonic = counter-clockwise');
  }
  console.log(`# worst per-frame angular error over the tracers: ${(worst * 100).toFixed(3)} %`);
});

test('the debris pool is deterministic: the same time gives the same state', () => {
  const a = new DebrisSim(R, { count: 600, seed: 9 });
  const b = new DebrisSim(R, { count: 600, seed: 9 });
  a.runTo(440, 452);
  b.reset(440);
  for (let t = 441; t <= 452; t += 1) b.stepTo(t);
  b.stepTo(452);
  assert.equal(a.steps, b.steps);
  assert.deepEqual(Array.from(a.pos), Array.from(b.pos));
  a.runTo(440, 452);
  assert.deepEqual(Array.from(a.pos), Array.from(b.pos));
  const c = new DebrisSim(R, { count: 600, seed: 10 });
  c.runTo(440, 452);
  assert.notDeepEqual(Array.from(a.pos), Array.from(c.pos));
});

test('inertia: light objects are lofted higher than boards, terminal speeds are ordered', () => {
  const s = new DebrisSim(R, { count: 5000, seed: 3 });
  s.runTo(440, 462);
  const stat = DEBRIS_CLASSES.map(() => ({ n: 0, z: 0 }));
  for (let i = 0; i < s.n; i++) {
    if (s.onGround[i]) continue;
    stat[s.cls[i]].n++;
    stat[s.cls[i]].z += s.pos[i * 3 + 2];
  }
  for (const c of stat) c.z /= Math.max(1, c.n);
  assert.ok(s.airborne() > 500, `airborne ${s.airborne()}`);
  assert.ok(stat[0].n > 50 && stat[2].n > 5, 'dust and boards are both in the air');
  assert.ok(stat[0].z > stat[2].z, `dust ${stat[0].z.toFixed(1)} m above boards ${stat[2].z.toFixed(1)} m`);
  assert.ok(terminalSpeed(DEBRIS_CLASSES[0]) < terminalSpeed(DEBRIS_CLASSES[2]));
  assert.ok(terminalSpeed(DEBRIS_CLASSES[2]) < terminalSpeed(DEBRIS_CLASSES[5]));
});

test('a heavy object is flung out of the core while its flow-following twin stays close (same start)', () => {
  const V = R.vortex;
  const run = (k) => {
    let x = R.rm * 0.6, y = 0, z = 5, vx = 0, vy = 0, vz = 0;
    const dt = 1 / 120;
    for (let i = 0; i < 120 * 8; i++) {
      const w = windAt(V, x, y, z);
      const dx = w[0] - vx, dy = w[1] - vy, dz = w[2] - vz, dm = Math.hypot(dx, dy, dz);
      const e = Math.exp(-k * dm * dt);
      vx = w[0] - dx * e; vy = w[1] - dy * e; vz = w[2] - dz * e - 9.80665 * dt;
      x += vx * dt; y += vy * dt; z = Math.max(0.05, z + vz * dt);
    }
    return Math.hypot(x, y);
  };
  assert.ok(run(0.02) > run(5) * 1.2, 'inertial object ends farther from the axis (centrifuged)');
});

test('wind field: the storm translation is carried far away, the vortex acts only inside the survey span', () => {
  const w = windField(R, 5000, 5000, 100, 600);
  assert.ok(Math.hypot(w[0], w[1]) > 10);
  const ax = axisAt(R, 600);
  const wCore = windField(R, ax.x + 70, ax.y, 50, 600);
  assert.ok(Math.hypot(wCore[0], wCore[1]) > 40, `core wind ${Math.hypot(wCore[0], wCore[1])}`);
  const a0 = axisAt(R, 100);
  const wBefore = windField(R, a0.x + 70, a0.y, 50, 100);
  assert.ok(Math.hypot(wBefore[0], wBefore[1]) < 40, 'no tornado wind before the survey start');
});
