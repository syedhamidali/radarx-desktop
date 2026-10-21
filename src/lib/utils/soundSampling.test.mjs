// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  lowestEcho, rainAtListener, rainLevelOfDbz, thunderArrivalFilm, realDelayS, flashCount, RAIN_SEARCH_KM,
} from './soundSampling.ts';
import { thunderOf } from './soundscape.ts';
import { makeFlash, SPEED_OF_SOUND_MS } from './lightning.ts';
import { dataTimeAt, clipTimeOf } from './clipCamera.ts';

/** A 5 x 5 grid, 1 km cells centred on 0, levels 0.5..10 km every 0.5 km, echo `dbz` only at the levels in `withEcho` (km). */
function grid(withEcho, dbz = 40) {
  const nx = 5, ny = 5, z = Array.from({ length: 20 }, (_, k) => 0.5 + 0.5 * k);
  const nz = z.length, vmin = -10, vmax = 70;
  const data = new Uint8Array(nx * ny * nz);
  const code = Math.round(1 + ((dbz - vmin) / (vmax - vmin)) * 254);
  for (let k = 0; k < nz; k++) {
    if (!withEcho.some((h) => Math.abs(h - z[k]) < 1e-9)) continue;
    for (let i = 0; i < nx * ny; i++) data[k * nx * ny + i] = code;
  }
  return { nx, ny, nz, x0: -2, y0: -2, dx: 1, dy: 1, z: Float64Array.from(z), data, vmin, vmax };
}

test('rain is read from the lowest level that has echo: echo only at 1.5 km is heard', () => {
  const g = grid([1.5, 2.0, 3.0], 40);
  const r = rainAtListener(g, 0, 0, 0.1);
  assert.equal(r.zKm, 1.5);
  // 40 dBZ: (40 - 20) / 35 of the full rain (within the 8-bit coding)
  assert.ok(Math.abs(r.level - 20 / 35) < 0.01, `level ${r.level}`);
  assert.ok(r.level > 0);
});

test('echo only above the search height (3.5 km and up) gives no rain', () => {
  assert.ok(RAIN_SEARCH_KM < 3.5);
  const r = rainAtListener(grid([3.5, 4, 6], 50), 0, 0, 0.1);
  assert.equal(r.level, 0);
  assert.equal(r.zKm, null);
});

test('the lowest echo wins over a stronger one above it, and the ground height shifts the search', () => {
  const nx = 5, ny = 5;
  const g = grid([1.0, 2.0], 30);
  // a stronger echo at 2.0 km: still the 1.0 km level is used
  const code = Math.round(1 + ((55 + 10) / 80) * 254);
  for (let i = 0; i < nx * ny; i++) g.data[3 * nx * ny + i] = code; // level index 3 = 2.0 km
  assert.equal(lowestEcho(g, 0, 0, 0, 2.5).zKm, 1.0);
  // a listener on ground 0.8 km up still finds the 1.0 km level; one at 2.6 km finds nothing below 5.1 km
  assert.equal(rainAtListener(g, 0, 0, 0.8).zKm, 1.0);
  assert.equal(rainAtListener(g, 0, 0, 2.6).zKm, null);
});

test('outside the grid or without a grid there is no rain', () => {
  const g = grid([1.0]);
  assert.equal(rainAtListener(g, 40, 0, 0).level, 0);
  assert.equal(rainAtListener(null, 0, 0, 0).level, 0);
});

test('rain level: 20 dBZ none, 55 dBZ full, linear between, NaN none', () => {
  assert.equal(rainLevelOfDbz(10), 0);
  assert.equal(rainLevelOfDbz(20), 0);
  assert.equal(rainLevelOfDbz(37.5), 0.5);
  assert.equal(rainLevelOfDbz(55), 1);
  assert.equal(rainLevelOfDbz(70), 1);
  assert.equal(rainLevelOfDbz(NaN), 0);
});

test('thunder arrival through a known film clock (data time = film time squared)', () => {
  const clock = { dataAt: (t) => t * t, filmAt: (td) => Math.sqrt(td) };
  // flash at film 2 s = data 4 s; 3 km: 3000 / 343 = 8.746 s real; data 12.746 s = film 3.570 s
  const want = Math.sqrt(4 + 3000 / SPEED_OF_SOUND_MS);
  assert.ok(Math.abs(thunderArrivalFilm(2, 3, clock) - want) < 1e-9);
  assert.ok(Math.abs(want - 3.5702) < 1e-3);
  // the same delay is shorter in film time where the clock runs faster
  const later = thunderArrivalFilm(10, 3, clock) - 10;
  assert.ok(later < thunderArrivalFilm(2, 3, clock) - 2);
});

test('with the eased film clock the thunder lands one real delay later in DATA time', () => {
  const dur = 36, t0 = 0, t1 = 3600;
  const clock = { dataAt: (t) => dataTimeAt(t, dur, t0, t1), filmAt: (td) => clipTimeOf(td, dur, t0, t1), dataEnd: t1, rateAfter: 3600 / 33 };
  const mean = (t1 - t0) / (dur - 3);
  const delays = [];
  for (const tf of [3, 10, 18, 26, 32]) {
    const a = thunderArrivalFilm(tf, 20, clock);
    const dData = clock.dataAt(a) - clock.dataAt(tf);
    assert.ok(Math.abs(dData - realDelayS(20)) < 0.05, `at ${tf} s: ${dData} s data vs ${realDelayS(20)} s`);
    delays.push(a - tf);
  }
  // the film delay changes with the clock speed: a fixed division would give the same number everywhere
  const fixed = realDelayS(20) / mean;
  assert.ok(Math.max(...delays) / Math.min(...delays) > 1.3, `film delays ${delays}`);
  assert.ok(delays.some((d) => Math.abs(d - fixed) / fixed > 0.1));
  // past the last data time the clock runs on at the given rate
  const end = clock.filmAt(t1);
  const a = thunderArrivalFilm(33.4, 30, clock);
  assert.ok(Math.abs(a - (end + (clock.dataAt(33.4) + realDelayS(30) - t1) / (3600 / 33))) < 1e-6);
});

test('thunderOf uses the clock when it is given', () => {
  const f = makeFlash({ id: 1, t: 10, kind: 'CG', source: 'observed', origin: [10, 0, 7], end: [10, 0, 0.1], seed: 4, energy: 1 });
  const clock = { dataAt: (t) => t * t, filmAt: (td) => Math.sqrt(td) };
  const th = thunderOf(f, [0, 0, 0], 50, 200, 3, clock);
  assert.ok(Math.abs(th.arrival - thunderArrivalFilm(10, th.distKm, clock)) < 1e-9);
  const fixed = thunderOf(f, [0, 0, 0], 50);
  assert.ok(Math.abs(fixed.arrival - (10 + realDelayS(fixed.distKm) / 50)) < 1e-9);
});

test('the flash count says flashes (and flash for one)', () => {
  assert.equal(flashCount(33), '33 flashes');
  assert.equal(flashCount(1), '1 flash');
  assert.equal(flashCount(0), '0 flashes');
});
