// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { lcl, surfaceParcel, pseudoLabel } from './perilsThermo.ts';

test('the LCL of a saturated parcel is at the surface; a drier parcel lifts higher (about 125 m per degree of dew point depression)', () => {
  const sat = lcl(20, 20, 1000);
  assert.ok(Math.abs(sat.pHpa - 1000) < 0.5);
  const dry = lcl(30, 10, 1000);
  // 20 K depression: roughly 2.5 km, i.e. around 760 hPa
  assert.ok(dry.pHpa > 700 && dry.pHpa < 820, `LCL ${dry.pHpa}`);
  assert.ok(dry.tC < 10 && dry.tC > -5);
});

test('the parcel path cools along the dry adiabat then follows the moist one; pressure falls monotonically', () => {
  const lv = {
    pressure_hpa: [1000, 925, 850, 700], height_msl_m: [100, 780, 1500, 3100], temperature_c: [24, 20, 17, 8], dewpoint_c: [19, 16, 13, 2],
    u_ms: [0, 0, 0, 0], v_ms: [0, 0, 0, 0],
  };
  const pp = surfaceParcel(lv);
  assert.ok(pp);
  assert.equal(pp.path[0][0], 1000);
  assert.equal(pp.path[0][1], 24);
  for (let i = 1; i < pp.path.length; i++) assert.ok(pp.path[i][0] < pp.path[i - 1][0]);
  // above the LCL the parcel cools more slowly than on the dry adiabat
  const i = pp.path.findIndex((q) => q[0] <= pp.lclHpa);
  const a = pp.path[i];
  const b = pp.path[Math.min(pp.path.length - 1, i + 8)];
  const dry = (a[1] + 273.15) * Math.pow(b[0] / a[0], 0.2854) - 273.15;
  assert.ok(b[1] > dry, 'the moist ascent is warmer than the dry one at the same pressure');
});

test('no path without a temperature and a dew point', () => {
  assert.equal(surfaceParcel(null), null);
  assert.equal(surfaceParcel({ pressure_hpa: [1000], height_msl_m: [0], temperature_c: [20], dewpoint_c: [null], u_ms: [0], v_ms: [0] }), null);
});

test('the pseudo-adiabat label reproduces a point on it', () => {
  const lab = pseudoLabel(10, 800);
  assert.ok(lab > 10 && lab < 40);
});
