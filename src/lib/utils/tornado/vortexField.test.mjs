// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  swirlShape, pressureShape, makeVortex, tangential, omega, pressureDeficit, radialWind, verticalWind, windAt,
  coreRadius, heightFactor, coreRadiusFromSwath, radiusOfSpeed, efPeakWind, AIR_DENSITY, makeMesoVortex, LO_B,
} from './vortexField.ts';

const V = makeVortex({ vt: 55, rm: 70 });

test('swirl profile peaks at exactly Vt at the core radius (any height of the maximum)', () => {
  // radial profile at the height of the swirl maximum
  let zBest = 0, gBest = 0;
  for (let z = 0; z < 600; z += 1) { const g = heightFactor(V, z); if (g > gBest) { gBest = g; zBest = z; } }
  assert.ok(Math.abs(gBest - 1) < 1e-9);
  const rc = coreRadius(V, zBest);
  let rBest = 0, vBest = 0;
  for (let r = 1; r < 400; r += 0.05) { const v = tangential(V, r, zBest); if (v > vBest) { vBest = v; rBest = r; } }
  assert.ok(Math.abs(vBest - 55) / 55 < 1e-4, `peak ${vBest}`);
  assert.ok(Math.abs(rBest - rc) / rc < 0.005, `peak radius ${rBest} vs ${rc}`);
  // nowhere in the field exceeds Vt
  for (let z = 0; z < 3000; z += 50) for (let r = 1; r < 1500; r += 25) assert.ok(tangential(V, r, z) <= 55 + 1e-9);
});

test('solid-body core, potential vortex outside', () => {
  assert.ok(Math.abs(swirlShape(1) - 1) < 1e-12);
  const z = 100, rc = coreRadius(V, z);
  const o1 = omega(V, 0.01 * rc, z), o2 = omega(V, 0.1 * rc, z);
  assert.ok(Math.abs(o1 - o2) / o1 < 0.02, 'omega is uniform in the core');
  // outside: v * r constant (circulation) beyond 4 core radii
  const c1 = tangential(V, 5 * rc, z) * 5 * rc, c2 = tangential(V, 10 * rc, z) * 10 * rc;
  assert.ok(Math.abs(c1 - c2) / c1 < 0.01);
});

test('angular velocity is v / r (omega = Vt / rm at the core radius) and signed by the sense', () => {
  const z = 60, rc = coreRadius(V, z);
  const vt = tangential(V, rc, z);
  assert.ok(Math.abs(omega(V, rc, z) - vt / rc) < 1e-12);
  for (const r of [10, 35, 70, 150, 300]) assert.ok(Math.abs(omega(V, r, z) - tangential(V, r, z) / r) < 1e-9 * 55);
  const anti = makeVortex({ vt: 55, rm: 70, sense: -1 });
  assert.ok(omega(V, rc, z) > 0 && omega(anti, rc, z) < 0);
});

test('cyclonic rotation is counter-clockwise seen from above', () => {
  const [u, v] = windAt(V, 100, 0, 50); // east of the axis: counter-clockwise flow points north
  assert.ok(v > 20 && Math.abs(u) < Math.abs(v), `u=${u} v=${v}`);
  const [u2, v2] = windAt(V, 0, 100, 50); // north of the axis: counter-clockwise flow points west
  assert.ok(u2 < -20, `u2=${u2}`);
  assert.ok(windAt(makeVortex({ vt: 55, rm: 70, sense: -1 }), 100, 0, 50)[1] < -20);
});

test('pressure deficit satisfies cyclostrophic balance dp/dr = rho v^2 / r', () => {
  for (const z of [0, 40, 150, 600]) {
    for (const r of [15, 40, 70, 120, 300, 800]) {
      const h = r * 1e-3;
      const dpdr = (pressureDeficit(V, r - h, z) - pressureDeficit(V, r + h, z)) / (2 * h); // the deficit falls with r, so p rises: dp/dr = +rho v^2 / r
      const rhs = AIR_DENSITY * tangential(V, r, z) ** 2 / r;
      assert.ok(Math.abs(dpdr - rhs) / rhs < 0.01, `z=${z} r=${r}: ${dpdr} vs ${rhs}`);
    }
  }
  // far field -> 0, centre is the minimum
  assert.ok(pressureDeficit(V, 5000, 50) < 0.01 * pressureDeficit(V, 0, 50));
  assert.ok(pressureDeficit(V, 0, 50) > pressureDeficit(V, 70, 50));
});

test('central deficit is of the right size for an EF2 vortex (tens of hPa, order rho Vt^2)', () => {
  const dp0 = pressureDeficit(V, 0, 100);
  assert.ok(dp0 > AIR_DENSITY * 55 * 55 && dp0 < 2.2 * AIR_DENSITY * 55 * 55, `${dp0} Pa`);
  assert.ok(dp0 > 3500 && dp0 < 8000, 'tens of hPa');
  assert.ok(Math.abs(pressureShape(1e-3) - pressureShape(0)) < 1e-9);
  assert.ok(LO_B > 1.25 && LO_B < 1.26);
});

test('mass continuity: div(u) is ~0 for the axisymmetric inflow and updraft', () => {
  const err = [];
  for (const [r, z] of [[20, 10], [50, 25], [70, 60], [120, 100], [200, 250], [40, 400]]) {
    const hr = 0.01 * r, hz = 0.1;
    const dr = ((r + hr) * radialWind(V, r + hr, z) - (r - hr) * radialWind(V, r - hr, z)) / (2 * hr) / r;
    const dz = (verticalWind(V, r, z + hz) - verticalWind(V, r, z - hz)) / (2 * hz);
    const scale = Math.max(Math.abs(dr), Math.abs(dz), 1e-9);
    err.push(Math.abs(dr + dz) / scale);
  }
  // the swirl-height variation only enters the swirl, the inflow terms are exact: < 1 %
  assert.ok(Math.max(...err) < 0.01, `max rel. divergence ${Math.max(...err)}`);
});

test('inflow at the ground, updraft in the core, no flow through the ground', () => {
  assert.ok(radialWind(V, 70, 1) < -10, 'strong surface inflow near the core');
  assert.ok(Math.abs(radialWind(V, 70, 600)) < 4, 'weak inflow aloft');
  assert.ok(verticalWind(V, 0, 0) === 0, 'w = 0 on the ground');
  assert.ok(verticalWind(V, 0, 500) > 10, 'updraft in the core');
});

test('core radius from the survey: the swath edge sits at the damage-onset speed', () => {
  const rm = coreRadiusFromSwath(55, 366, 29);
  assert.ok(Math.abs(radiusOfSpeed(55, rm, 29) - 183) < 0.5, 'half width of 366 m is where the wind falls to 29 m/s');
  assert.ok(rm > 40 && rm < 120, `rm = ${rm}`);
  assert.ok(efPeakWind('EF2') > 49.6 && efPeakWind('EF2') < 60.3);
  assert.ok(efPeakWind('EF3') > efPeakWind('EF2'));
});

test('mesoscale (radar couplet) vortex: Vrot at its core radius, weak pressure drop', () => {
  const M = makeMesoVortex(29, 1800);
  let best = 0;
  for (let z = 0; z < 6000; z += 100) for (let r = 100; r < 8000; r += 20) best = Math.max(best, tangential(M, r, z));
  assert.ok(Math.abs(best - 29) / 29 < 0.02, `${best}`);
  assert.ok(pressureDeficit(M, 0, 400) < 0.4 * pressureDeficit(V, 0, 100), 'the couplet is a much weaker low than the tornado');
});
