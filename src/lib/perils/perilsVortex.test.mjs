// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  vortexParams, parentCirculation, rankineOmega, azimuthAt, hash01, indicatorAzimuth, efRangeMs, vortexFrame, ParticleTornado, RING_FACTOR,
} from './perilsVortex.ts';

const survey = { wind_range_ms: [49.6, 60.4], damage_width_m: 366, angular_velocity_s1_range: [0.271, 0.33], rotation_period_s_range: [19.1, 23.2] };
const vortex = {
  survey_scale_estimate: survey,
  summary: { n_steps: 3, vrot_ms_min_median_max: [7.9, 11.2, 15.2], couplet_diameter_km_min_median_max: [1.1, 1.8, 5.9], omega_s1_median_cyclonic: 0.0128, cyclonic_steps: 2, tornado_resolved_any: false },
  steps: [
    { t: 240, radar: 'NOXP', vrot_ms: 7.9, diameter_km: 2.4, omega: 0.0066, period_s: 953, sense: 'cyclonic', effective_resolution_m: 500, tornado_resolved: false },
    { t: 600, radar: 'NOXP', vrot_ms: 15.2, diameter_km: 1.1, omega: 0.0276, period_s: 228, sense: 'cyclonic', effective_resolution_m: 500, tornado_resolved: false },
  ],
};
const tor = { rating: 'EF2', width_m: 366, path: [{ t: 240, x: 0, y: 0 }, { t: 960, x: 12, y: 12 }] };

test('the spin of the funnel: the middle of the survey wind range over half the damage width (period 19-23 s)', () => {
  const p = vortexParams(tor, vortex, 500);
  assert.equal(p.radiusM, 183);
  assert.ok(Math.abs(p.vtMs - 55.0) < 1e-9);
  assert.ok(Math.abs(p.omega - 55 / 183) < 1e-9);
  assert.ok(Math.abs(p.periodS - (2 * Math.PI * 183) / 55) < 1e-9);
  assert.ok(p.periodS > survey.rotation_period_s_range[0] && p.periodS < survey.rotation_period_s_range[1]);
  assert.equal(p.tornadoResolved, false);
  // the numbers the Python side writes (tools/perils_movie/render/vortex.py): the same radius, speed and period
  assert.ok(Math.abs(p.periodS - 20.9056) < 1e-3);
});

test('without the kinematics file the EF range of the rating gives the same estimate', () => {
  const p = vortexParams(tor, null, 500);
  const r = efRangeMs('EF2');
  assert.ok(Math.abs(p.vtMs - 0.5 * (r[0] + r[1])) < 1e-9);
  assert.equal(p.parent, null);
  assert.equal(vortexParams({ rating: 'unknown', path: [] }, null, 0), null);
});

test('what the radars measured is the parent circulation, taken at the step nearest in time', () => {
  const a = parentCirculation(vortex, 250);
  const b = parentCirculation(vortex, 590);
  assert.equal(a.vrotMs, 7.9);
  assert.equal(b.vrotMs, 15.2);
  assert.equal(a.medianVrotMs, 11.2);
  assert.equal(a.medianDiameterKm, 1.8);
  assert.deepEqual(a.vrotRange, [7.9, 15.2]);
  assert.equal(parentCirculation(null, 0), null);
});

test('a Rankine vortex turns rigidly inside the core and as 1 / r^2 outside; the pattern advances by omega times the data time', () => {
  assert.equal(rankineOmega(0.3, 183, 100), 0.3);
  assert.ok(Math.abs(rankineOmega(0.3, 183, 366) - 0.075) < 1e-12);
  const w = 0.3;
  const a0 = azimuthAt(0.6, w, 100);
  const a1 = azimuthAt(0.6, w, 101);
  assert.ok(Math.abs(a1 - a0 - w) < 1e-12);
  // the indicator arrowhead moves counter-clockwise (increasing azimuth) at omega
  assert.ok(Math.abs(indicatorAzimuth(w, 11) - indicatorAzimuth(w, 10) - w) < 1e-12);
  assert.ok(RING_FACTOR > 1);
});

test('the hash is deterministic and spread over [0, 1)', () => {
  assert.equal(hash01(5, 3), hash01(5, 3));
  let lo = 1;
  let hi = 0;
  for (let i = 0; i < 2000; i++) {
    const v = hash01(i, 9);
    assert.ok(v >= 0 && v < 1);
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  assert.ok(lo < 0.02 && hi > 0.98);
});

test('the vortex exists only while the survey says the tornado does', () => {
  assert.equal(vortexFrame([tor], vortex, 100, 0), null);
  assert.equal(vortexFrame([tor], vortex, 1000, 0), null);
  const f = vortexFrame([tor], vortex, 600, 1);
  assert.ok(f && f.immersion === 1);
  assert.ok(Math.abs(f.x - 6) < 1e-9 && Math.abs(f.y - 6) < 1e-9);
});

test('the funnel is narrow at the ground and wider aloft', () => {
  const R = 0.183;
  assert.ok(ParticleTornado.funnelRadius(R, 0) < ParticleTornado.funnelRadius(R, 0.5));
  assert.ok(ParticleTornado.funnelRadius(R, 0.5) < ParticleTornado.funnelRadius(R, 1));
  assert.ok(ParticleTornado.funnelRadius(R, 0) < R);
});
