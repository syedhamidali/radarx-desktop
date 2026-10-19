// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  P_BOTTOM,
  P_TOP,
  makeFrame,
  project,
  pressureAtY,
  pathOf,
  yFrac,
  xFrac,
  dryAdiabatC,
  mixingRatioLineC,
  pseudoAdiabat,
  satVapourHpa,
  heightAtPressure,
  hodographFrame,
  hodoPoint,
  hodographPaths,
  layerOf,
  aglOf,
  HODO_LAYERS,
} from './skewT.ts';

const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, `${msg ?? ''} ${a} vs ${b} (tol ${tol})`);

test('pressure is a log axis: 1050 hPa at the bottom, 100 hPa at the top, equal ratios equal heights', () => {
  assert.equal(yFrac(P_BOTTOM), 0);
  near(yFrac(P_TOP), 1, 1e-12);
  near(yFrac(500) - yFrac(1000), yFrac(250) - yFrac(500), 1e-12, 'log spacing');
  const f = makeFrame(600, 500);
  const [, yb] = project(f, 0, P_BOTTOM);
  const [, yt] = project(f, 0, P_TOP);
  near(yb, f.bottom, 1e-9);
  near(yt, f.top, 1e-9);
  for (const p of [1000, 700, 500, 300, 150]) near(pressureAtY(f, project(f, 0, p)[1]), p, 1e-6, `p ${p}`);
});

test('isotherms lean to the right with height; colder is to the left', () => {
  const f = makeFrame(600, 500);
  const x = (t, p) => project(f, t, p)[0];
  assert.ok(x(0, 500) > x(0, 1000), 'skew');
  assert.ok(x(-10, 700) < x(0, 700), 'colder left');
  near(x(10, 700) - x(0, 700), x(10, 300) - x(0, 300), 1e-9, 'isotherms are parallel');
  // the diagram holds a normal sounding: surface 30 degC, 500 hPa -15 degC, 200 hPa -55 degC
  for (const [t, p] of [[30, 1000], [-15, 500], [-55, 200], [-60, 150]]) {
    const xf = xFrac(t, p);
    assert.ok(xf > 0 && xf < 1, `(${t}, ${p}) inside: ${xf}`);
  }
});

test('dry adiabats: theta is conserved, 300 K passes 300 K at 1000 hPa', () => {
  near(dryAdiabatC(300, 1000), 300 - 273.15, 1e-9);
  // 500 hPa on the 300 K adiabat: 300 * 0.5^0.2854 = 246.1 K
  near(dryAdiabatC(300, 500) + 273.15, 300 * Math.pow(0.5, 0.2854), 1e-9);
  assert.ok(dryAdiabatC(300, 700) < dryAdiabatC(300, 850));
});

test('saturation mixing-ratio lines: 10 g/kg at 1000 hPa has a dew point near 14 degC (Bolton)', () => {
  near(mixingRatioLineC(10, 1000), 14.0, 0.3);
  // e = w p / (0.622 + w); consistency with the Bolton saturation vapour pressure
  const td = mixingRatioLineC(8, 850);
  const w = 0.008;
  near(satVapourHpa(td), (w * 850) / (0.622 + w), 1e-6);
  assert.ok(mixingRatioLineC(4, 500) < mixingRatioLineC(4, 1000), 'colder aloft');
});

test('pseudo-adiabat: cooler than the dry adiabat, ordered by start temperature, tends to dry aloft', () => {
  const pa = pseudoAdiabat(20);
  assert.equal(pa[0][0], 1000);
  near(pa[0][1], 20, 1e-12);
  assert.ok(pa.every(([p], i) => i === 0 || p < pa[i - 1][0]), 'goes up');
  const at = (arr, p) => arr.find(([q]) => q === p)[1];
  const dry = (p) => dryAdiabatC(20 + 273.15, p);
  assert.ok(at(pa, 500) > dry(500), 'condensation warms the parcel: the moist adiabat is warmer than the dry one');
  // a 20 degC pseudo-adiabat is about -2 to -8 degC at 500 hPa (textbook skew-T charts)
  assert.ok(at(pa, 500) > -9 && at(pa, 500) < 0, `500 hPa: ${at(pa, 500)}`);
  assert.ok(at(pseudoAdiabat(26), 500) > at(pa, 500), 'warmer start, warmer aloft');
  assert.ok(at(pa, 200) < at(pa, 500) && at(pa, 100) < at(pa, 200));
});

test('pathOf breaks the line at missing values and outside the diagram', () => {
  const f = makeFrame(600, 500);
  const d = pathOf(f, [20, null, 10, 5, NaN, 0], [1000, 900, 800, 700, 600, 2000]);
  assert.equal((d.match(/M/g) || []).length, 2, d); // [1000] alone, then [800, 700]; the rest is skipped
  assert.ok(d.includes('L'));
  assert.equal(pathOf(f, [], []), '');
});

test('height at a pressure level is interpolated in ln(p) and null outside the profile', () => {
  const p = [1000, 850, 700, 500];
  const z = [100, 1500, 3000, 5600];
  assert.equal(heightAtPressure(p, z, 850), 1500);
  const mid = heightAtPressure(p, z, 775);
  assert.ok(mid > 1500 && mid < 3000);
  assert.equal(heightAtPressure(p, z, 300), null);
  assert.equal(heightAtPressure(p, z, 1050), null);
  assert.equal(heightAtPressure([1000, null, 500], [0, 1, 2], 700), null);
});

test('hodograph: the outer ring holds every point, north is up', () => {
  const u = [0, 5, 12, 22];
  const v = [0, 8, 6, -3];
  const f = hodographFrame(240, u, v);
  assert.equal(f.max, 30);
  const [x, y] = hodoPoint(f, 0, 10);
  assert.equal(x, f.cx);
  assert.ok(y < f.cy, 'north is up');
  const [xe] = hodoPoint(f, 10, 0);
  assert.ok(xe > f.cx, 'east is right');
  assert.equal(hodographFrame(240, [], [], [[40, 0]]).max, 50); // storm motion counts
  assert.equal(hodographFrame(240, [1], [1]).max, 20); // never smaller than 20 m/s
});

test('hodograph trace is split by height class and stays joined', () => {
  const f = hodographFrame(240, [0, 5, 10, 15], [0, 5, 10, 15]);
  const z = [0, 500, 2000, 7000];
  const paths = hodographPaths(f, [0, 5, 10, 15], [0, 5, 10, 15], z);
  assert.deepEqual(paths.map((p) => p.layer), [0, 1, 3]);
  assert.ok(paths[1].d.startsWith('M') && paths[1].d.includes('L'), 'joined to the previous class');
  assert.equal(hodographPaths(f, [1, null], [1, 2], [0, 100]).length, 1); // a missing wind breaks it
  assert.equal(layerOf(0), 0);
  assert.equal(layerOf(999), 0);
  assert.equal(layerOf(1000), 1);
  assert.equal(layerOf(20000), HODO_LAYERS.length - 1);
  assert.deepEqual(hodographPaths(f, [5], [5], [13000]), []); // above 12 km AGL: not drawn
});

test('heights above ground', () => {
  assert.deepEqual(aglOf([300, 800, null], 300), [0, 500, null]);
});
