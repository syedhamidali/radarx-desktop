// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  sunPosition, sunsetAzimuth, sunLight, sunVector, julianDay, REAL_SUN_MIN_ELEVATION, DUSK_ELEVATION,
  daylightAssumed, DAYLIGHT_MIN_ELEVATION, DAYLIGHT_MAX_ELEVATION,
} from './sunPosition.ts';

test('julian day of J2000', () => {
  assert.ok(Math.abs(julianDay(new Date(Date.UTC(2000, 0, 1, 12, 0, 0))) - 2451545.0) < 1e-9);
});

test('sun position: solar noon at the equinox on the equator and at the June solstice at Greenwich', () => {
  const eq = sunPosition(new Date(Date.UTC(2021, 2, 20, 12, 0, 0)), 0, 0);
  assert.ok(eq.elevationDeg > 88, `equinox noon elevation ${eq.elevationDeg}`);
  assert.ok(Math.abs(eq.declinationDeg) < 0.6);
  // 2021-06-21 solar noon at Greenwich is about 12:01 UTC: 90 - 51.48 + 23.44 = 61.96 degrees, due south
  const sol = sunPosition(new Date(Date.UTC(2021, 5, 21, 12, 1, 0)), 51.48, 0);
  assert.ok(Math.abs(sol.elevationDeg - 61.95) < 0.3, `solstice noon elevation ${sol.elevationDeg}`);
  assert.ok(Math.abs(sol.azimuthDeg - 180) < 3, `azimuth ${sol.azimuthDeg}`);
  assert.ok(Math.abs(sol.declinationDeg - 23.44) < 0.05);
  // morning: east of south, afternoon: west of south
  const am = sunPosition(new Date(Date.UTC(2021, 5, 21, 8, 0, 0)), 51.48, 0);
  const pm = sunPosition(new Date(Date.UTC(2021, 5, 21, 16, 0, 0)), 51.48, 0);
  assert.ok(am.azimuthDeg > 60 && am.azimuthDeg < 140);
  assert.ok(pm.azimuthDeg > 220 && pm.azimuthDeg < 300);
});

test('Mayfield, 11 Dec 2021 01:50 UTC (19:50 local) is night: the sun is far below the horizon', () => {
  const p = sunPosition(new Date(Date.UTC(2021, 11, 11, 1, 50, 0)), 36.15, -89.9);
  assert.ok(p.elevationDeg < -30 && p.elevationDeg > -45, `elevation ${p.elevationDeg}`);
  assert.ok(Math.abs(p.declinationDeg + 23.1) < 0.3);
  // the same place at local noon (18:00 UTC) is a low winter sun: about 90 - 36.15 - 23.1 = 30.7 degrees
  const noon = sunPosition(new Date(Date.UTC(2021, 11, 11, 18, 0, 0)), 36.15, -89.9);
  assert.ok(Math.abs(noon.elevationDeg - 30.7) < 1.2, `noon elevation ${noon.elevationDeg}`);
});

test('sunset azimuth: south-west in December, north-west in June (mid latitudes)', () => {
  const dec = sunsetAzimuth(new Date(Date.UTC(2021, 11, 11, 22, 0, 0)), 36.15);
  assert.ok(dec > 238 && dec < 244, `December sunset azimuth ${dec}`);
  const jun = sunsetAzimuth(new Date(Date.UTC(2021, 5, 21, 22, 0, 0)), 36.15);
  assert.ok(jun > 296 && jun < 304, `June sunset azimuth ${jun}`);
});

test('light: the real sun by day, the documented dusk light when the sun is below the horizon', () => {
  const day = sunLight(new Date(Date.UTC(2021, 11, 11, 18, 0, 0)), 36.15, -89.9);
  assert.equal(day.stylised, false);
  assert.ok(day.elevationDeg >= REAL_SUN_MIN_ELEVATION && Math.abs(day.elevationDeg - day.realElevationDeg) < 1e-9);
  const night = sunLight(new Date(Date.UTC(2021, 11, 11, 1, 50, 0)), 36.15, -89.9);
  assert.equal(night.stylised, true);
  assert.equal(night.elevationDeg, DUSK_ELEVATION);
  assert.ok(night.realElevationDeg < -30);
  assert.ok(night.azimuthDeg > 238 && night.azimuthDeg < 244);
  assert.match(night.label, /stylised dusk light/);
  assert.match(night.label, /below the horizon/);
});

test('light vector: unit length, heights stretched like the scene', () => {
  const v = sunVector(270, 30, 1);
  assert.ok(Math.abs(Math.hypot(...v) - 1) < 1e-12);
  assert.ok(v[0] < -0.8 && Math.abs(v[1]) < 1e-9 && Math.abs(v[2] - 0.5) < 1e-9); // from the west, 30 degrees up
  const s = sunVector(270, 30, 4);
  assert.ok(Math.abs(Math.hypot(...s) - 1) < 1e-12);
  assert.ok(s[2] > v[2], 'a stretched scene sees the light steeper');
  // undoing the stretch gives the true elevation back
  const el = (Math.atan2(s[2] / 4, Math.hypot(s[0], s[1])) * 180) / Math.PI;
  assert.ok(Math.abs(el - 30) < 1e-9);
});

test('assumed daylight: the afternoon azimuth of that date and place, elevation raised into 36-50 degrees, labelled', () => {
  const night = new Date(Date.UTC(2021, 11, 11, 1, 50, 0));
  const d = daylightAssumed(night, 36.15, -89.9);
  assert.equal(d.stylised, true);
  assert.equal(d.daylightAssumed, true);
  assert.ok(d.elevationDeg >= DAYLIGHT_MIN_ELEVATION && d.elevationDeg <= DAYLIGHT_MAX_ELEVATION);
  // a December afternoon sun at 36 N is in the south-west
  assert.ok(d.azimuthDeg > 200 && d.azimuthDeg < 245, `azimuth ${d.azimuthDeg}`);
  assert.ok(d.realElevationDeg < -30, 'the label says what the real sun did');
  assert.match(d.label, /stylised daylight; the real sun was below the horizon at this time/);
  // a summer afternoon is higher than the minimum and stays below the maximum
  const summer = daylightAssumed(new Date(Date.UTC(2021, 5, 21, 3, 0, 0)), 36.15, -89.9);
  assert.ok(summer.elevationDeg <= DAYLIGHT_MAX_ELEVATION);
  // the same date gives the same light whatever the hour of the scan
  const later = daylightAssumed(new Date(Date.UTC(2021, 11, 11, 2, 8, 0)), 36.15, -89.9);
  assert.equal(later.azimuthDeg, d.azimuthDeg);
});
