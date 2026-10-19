// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { fmt, parcelRows, shearRows, srhRows, levelRows, barbPath, launchTimeUtc, validStation, PARCEL_KEYS } from './soundingView.ts';

test('fmt never hides a missing value behind a zero', () => {
  assert.equal(fmt(null, 'J/kg'), 'n/a');
  assert.equal(fmt(undefined), 'n/a');
  assert.equal(fmt(NaN, 'm'), 'n/a');
  assert.equal(fmt(0, 'J/kg'), '0 J/kg');
  assert.equal(fmt(-0.04, 'K', 1), '0.0 K'.replace('0.0', '0.0')); // no "-0.0"
  assert.equal(fmt(1234.56, 'm'), '1235 m');
  assert.equal(fmt(7.46, 'm/s', 1), '7.5 m/s');
  assert.equal(fmt(-3, 'J/kg'), '-3 J/kg');
});

test('parcel rows carry units and the height reference, and say when EL is the top of the sounding', () => {
  assert.deepEqual(PARCEL_KEYS, ['surface-based', 'mixed-layer', 'most-unstable']);
  const rows = parcelRows({ parcel: 'surface-based', label: 'x', cape: 1865, cin: -47, lcl_agl_m: 1004, lfc_agl_m: 1605, el_agl_m: 11463, lcl_hpa: 880, lfc_hpa: 830, el_hpa: 205, lifted_index: -6.8, el_at_top: true });
  const by = Object.fromEntries(rows.map((r) => [r.key, r]));
  assert.equal(by.cape.value, '1865 J/kg');
  assert.equal(by.cin.value, '-47 J/kg');
  assert.equal(by.lcl.value, '1004 m AGL');
  assert.equal(by.lcl.sub, '880 hPa');
  assert.match(by.el.value, /11463 m AGL \(top of sounding\)/);
  assert.equal(by.lifted_index.value, '-6.8 K');
  assert.deepEqual(parcelRows(undefined), []);
  assert.deepEqual(parcelRows({ parcel: 'x', label: '', error: 'bad' }), []);
  const stable = parcelRows({ parcel: 'surface-based', label: '', cape: 0, cin: null, lfc_agl_m: null, el_agl_m: null });
  assert.equal(stable.find((r) => r.key === 'lfc').value, 'n/a');
  assert.equal(stable.find((r) => r.key === 'cape').value, '0 J/kg');
});

test('kinematic and level rows', () => {
  const shear = shearRows({ '0-1km': { speed_ms: 8.6, speed_kt: 17 }, '0-6km': null });
  assert.equal(shear[0].label, 'Bulk shear 0-1 km');
  assert.equal(shear[0].value, '8.6 m/s');
  assert.equal(shear[1].value, 'n/a');
  assert.deepEqual(shearRows(null), []);
  const srh = srhRows({ '0-3km': { right_mover: 247, left_mover: 66 } }, { right_mover: { from_deg: 269, speed_ms: 7.6, speed_kt: 15 } });
  assert.equal(srh[0].value, '247 m²/s²');
  assert.match(srh[0].sub, /left mover 66/);
  assert.match(srh[1].value, /269.*at 7\.6 m\/s/);
  const lv = levelRows({ freezing_agl_m: 3520, wet_bulb_zero_agl_m: null, minus20_agl_m: 6289 }, { precipitable_water_mm: 22, pw_top_hpa: 7 });
  assert.equal(lv[0].value, '3520 m AGL');
  assert.equal(lv[1].value, 'n/a');
  assert.equal(lv[3].value, '22.0 mm');
});

const nums = (d) => (d.match(/-?\d+(\.\d+)?/g) || []).map(Number);

test('wind barb: calm is a circle; the staff points to where the wind comes from', () => {
  assert.match(barbPath(10, 10, 0.5, 0.5), /a2 2/);
  // westerly wind (from the west, u > 0): the staff goes left (x decreases), y unchanged
  const w = nums(barbPath(100, 100, 10, 0, 20));
  assert.ok(w[2] < 100 && Math.abs(w[3] - 100) < 1e-6, `west: ${w}`);
  // northerly wind (v < 0 blows to the south, comes from the north): the staff goes up (y decreases)
  const n = nums(barbPath(100, 100, 0, -10, 20));
  assert.ok(Math.abs(n[2] - 100) < 1e-6 && n[3] < 100, `north: ${n}`);
  // barb count by speed: 5 kt short, 10 kt long, 50 kt pennant, 65 kt = pennant + long + short
  const segs = (kt) => (barbPath(0, 0, -kt * 0.514444, 0, 20).match(/M/g) || []).length;
  assert.equal(segs(5), 2, '5 kt: staff + short barb');
  assert.equal(segs(10), 2, '10 kt: staff + long barb');
  assert.equal(segs(15), 3);
  assert.equal(segs(50), 2, '50 kt: staff + pennant');
  assert.equal(segs(65), 4, '65 kt: staff + pennant + long + short');
});

test('feathers hang off the clockwise side of the staff (Northern Hemisphere)', () => {
  // northerly (staff up): the feather points to the right (east) of the staff
  const d = nums(barbPath(100, 100, 0, -20, 20)); // 38.9 kt -> three long barbs + short
  const feather = d.slice(4, 8);
  assert.ok(feather[2] > feather[0], 'feather tip is east of the staff point');
});

test('archive request: UTC time and station checks', () => {
  assert.equal(launchTimeUtc('2022-03-30T00:00'), '2022-03-30T00:00:00Z');
  assert.equal(launchTimeUtc('2022-03-30 12:00'), '2022-03-30T12:00:00Z');
  assert.equal(launchTimeUtc('2022-03-30T12:00:00'), '2022-03-30T12:00:00Z');
  for (const bad of ['', 'yesterday', '2022-13-01T00:00', '2022-02-30T00:00', '2022-03-30', '2022-03-30T25:00']) assert.equal(launchTimeUtc(bad), null, bad);
  assert.ok(validStation('KJAN') && validStation('72235') && validStation(' BMX '));
  assert.ok(!validStation('') && !validStation('KJ') && !validStation('K J') && !validStation('A'.repeat(13)) && !validStation('../x'));
});
