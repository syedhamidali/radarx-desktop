// @ts-nocheck - plain node:test file, not part of the app bundle
import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveSeed, axisAt, intensityAt, rotationPeriod, toLocal, fromLocal } from './seed.ts';
import { omega, windAt, coreRadius, tangential, radiusOfSpeed } from './vortexField.ts';
import { vortexOnlyRadius } from './funnel.ts';

// The PERiLS IOP2 EF2 tornado (NWS event 1035605): begin 33.1008 N 88.4691 W at t = 240 s, end 33.2105 N 88.3316 W at t = 960 s
const SEED = {
  width: 366, ef: 'EF2',
  track: [{ t: 240, lat: 33.1008, lon: -88.4691 }, { t: 960, lat: 33.2105, lon: -88.3316 }],
  sense: 'cyclonic',
  meso: { vrot: 11, radar: 'NOXP', radiusM: 900 },
  thermo: { t0: 15.7, td0: 15.4, p0: 994.4, source: 'StickNet 0102A' },
  inflowThermo: { t0: 20.4, td0: 18.3, p0: 995.6, source: 'StickNet 0216A' },
  lightningFlashes: [{ t: 300, lat: 33.12, lon: -88.43, altM: 8000 }],
};

test('the seed resolves to survey-implied numbers', () => {
  const r = resolveSeed(SEED);
  assert.ok(r.vt > 49.6 && r.vt < 60.3, `Vt ${r.vt}`);
  assert.equal(r.coreSource, 'survey-swath');
  // the swath edge (183 m) is where the swirl has fallen to the damage-onset speed
  assert.ok(Math.abs(radiusOfSpeed(r.vt, r.rm, 29) - 183) < 0.5);
  assert.ok(r.rm > 50 && r.rm < 100, `rm ${r.rm}`);
  // translation: 17.7 km in 720 s toward the north-east
  assert.ok(Math.abs(r.speed - 24.6) < 0.6, `speed ${r.speed}`);
  assert.ok(Math.abs(r.headingDeg - 46) < 3, `heading ${r.headingDeg}`);
  assert.equal(r.vortex.sense, 1);
  assert.ok(Math.abs(r.nearCondition.lcl - 125 * 0.3) < 10, `station LCL ${r.nearCondition.lcl}`);
  assert.ok(Math.abs(r.condition.lcl - 125 * 2.1) / (125 * 2.1) < 0.08, `inflow LCL ${r.condition.lcl}`);
  assert.equal(r.cloudCondition.lcl, r.condition.lcl);
  assert.equal(r.flashes.length, 1);
  assert.ok(r.summary.observed.length >= 4 && r.summary.modelled.length >= 2);
  assert.ok(!r.summary.observed.join(' ').includes('29 m/s'), 'the superseded Vrot 29 m/s is not presented');
});

test('spin periods: the core turns in ~8 s, the funnel surface slower; the survey-scale estimate (19-23 s) lies between', () => {
  const r = resolveSeed(SEED);
  const z = 60;
  const rf = vortexOnlyRadius(r.condition, r.vortex, z);
  assert.ok(rf > 40 && rf < 200, `funnel radius at 60 m ${rf} m`);
  const periodSurface = (2 * Math.PI) / omega(r.vortex, rf, z);
  const periodSwathEdge = (2 * Math.PI) / omega(r.vortex, 183, z);
  assert.ok(periodSurface > 5 && periodSurface < 60 && periodSwathEdge > 19 && periodSwathEdge < 60 && 2 * Math.PI * r.rm / r.vt < 19, `period at the funnel surface ${periodSurface} s`);
  const rc = coreRadius(r.vortex, z);
  assert.ok(Math.abs(omega(r.vortex, rc, z) - tangential(r.vortex, rc, z) / rc) < 1e-12);
  const T = rotationPeriod(r);
  assert.ok(Math.abs(T - (2 * Math.PI * r.rm) / r.vt) < 1e-9 && T > 4 && T < 15, `core period ${T}`);
});

test('anticyclonic sense reverses the rotation', () => {
  const r = resolveSeed({ ...SEED, sense: 'anticyclonic' });
  assert.equal(r.vortex.sense, -1);
  assert.ok(windAt(r.vortex, 100, 0, 50)[1] < 0);
});

test('the tornado exists only inside the survey span (no evidence, no tornado)', () => {
  const r = resolveSeed(SEED);
  assert.equal(intensityAt(r, 100), 0);
  assert.equal(intensityAt(r, 239.9), 0);
  assert.equal(intensityAt(r, 961), 0);
  assert.ok(intensityAt(r, 480) === 1);
  assert.ok(intensityAt(r, 250) > 0 && intensityAt(r, 250) < 1, 'forming ramp');
  assert.ok(intensityAt(r, 950) > 0 && intensityAt(r, 950) < 1, 'decay ramp');
});

test('the axis follows the survey track and the local frame is invertible', () => {
  const r = resolveSeed(SEED);
  const a0 = axisAt(r, 240), a1 = axisAt(r, 960), am = axisAt(r, 600);
  assert.ok(Math.hypot(a0.x, a0.y) < 1e-6);
  assert.ok(Math.abs(am.x - a1.x / 2) < 1 && Math.abs(am.y - a1.y / 2) < 1);
  const [x, y] = toLocal(r.origin, 33.15, -88.4);
  const [la, lo] = fromLocal(r.origin, x, y);
  assert.ok(Math.abs(la - 33.15) < 1e-9 && Math.abs(lo + 88.4) < 1e-9);
  assert.throws(() => resolveSeed({ ...SEED, track: [SEED.track[0]] }));
});
