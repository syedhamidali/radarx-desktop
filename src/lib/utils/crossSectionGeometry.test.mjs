// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  niceStep,
  niceTicks,
  formatTick,
  buildCells,
  cellAt,
  columnAt,
  autoHeightTop,
  computeLayout,
  effectiveExaggeration,
  zoomView,
  panView,
  isFullView,
  azRangeToXY,
  xyToAzRange,
  wrapAzimuth,
  haversineKm,
  sectionPointCount,
  describeSection,
  safeStem,
  fmtLatLon,
} from './crossSectionGeometry.ts';

const near = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) <= eps, `${a} != ${b}`);

test('ticks: nice steps, no float drift, inside the range', () => {
  assert.equal(niceStep(100, 10), 10);
  assert.equal(niceStep(20, 10), 2);
  assert.equal(niceStep(1, 10), 0.1);
  assert.deepEqual(niceTicks(0, 20, 10), [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20]);
  assert.deepEqual(niceTicks(0, 1, 5), [0, 0.2, 0.4, 0.6, 0.8, 1]);
  assert.deepEqual(niceTicks(3, 7, 4), [3, 4, 5, 6, 7]);
  assert.deepEqual(niceTicks(5, 5, 4), []);
  for (const t of niceTicks(0.1, 0.9, 8)) assert.ok(t >= 0.1 && t <= 0.9);
});

test('tick labels: decimals follow the step, no negative zero', () => {
  assert.equal(formatTick(40, 10), '40');
  assert.equal(formatTick(0.5, 0.5), '0.5');
  assert.equal(formatTick(0.25, 0.05), '0.25');
  assert.equal(formatTick(-0, 1), '0');
  assert.equal(formatTick(47.5, 2.5), '47.5');
  assert.equal(formatTick(5, 2.5), '5.0');
});

/** Two columns, three sweeps; the third sweep has no echo in column 0 and the top is beyond range in column 1. */
const section = () => ({
  distance_km: [0, 10, 20],
  ground_range_km: [30, 40, 50],
  n_sweeps: 3,
  height_km: [
    [0.5, 0.7, 0.9],
    [1.5, 1.9, 2.3],
    [3.0, 3.6, null],
  ],
  values: [
    [10, 20, 30],
    [11, null, 31],
    [null, 22, 32],
  ],
});

test('cells: one per measured gate, columns split half way to the neighbours', () => {
  const sc = buildCells(section(), { fill: true });
  assert.equal(sc.cells.length, 3 + 2 + 1); // sweep0: 3, sweep1: 2 (one null), sweep2: 1 (null value, no height)
  const c = sc.columns[1].find((x) => x.sweep === 0);
  near(c.x0, 5);
  near(c.x1, 15);
  const first = sc.columns[0][0];
  near(first.x0, -5); // half a step beyond the first sample
  const last = sc.columns[2][0];
  near(last.x1, 25);
});

test('cells: fill closes the gap between two beams, beam-only does not', () => {
  const filled = buildCells(section(), { fill: true });
  const beam = buildCells(section(), { fill: false });
  const lo = filled.columns[0].find((x) => x.sweep === 0);
  const hi = filled.columns[0].find((x) => x.sweep === 1);
  near(lo.y1, (0.5 + 1.5) / 2);
  near(hi.y0, (0.5 + 1.5) / 2);
  const blo = beam.columns[0].find((x) => x.sweep === 0);
  const bhi = beam.columns[0].find((x) => x.sweep === 1);
  assert.ok(blo.y1 < bhi.y0, 'a gap stays open between the beams');
  assert.ok(blo.y1 - 0.5 < 0.5, 'a beam is under a kilometre tall at 30 km');
});

test('cells: masked values get no cell, and the beams around them keep their size', () => {
  const plain = buildCells(section(), { fill: true });
  const masked = buildCells(section(), { fill: true, masked: (v) => v < 15 });
  assert.equal(masked.cells.length, plain.cells.length - plain.cells.filter((c) => c.v < 15).length);
  assert.ok(masked.cells.every((c) => c.v >= 15));
  // the neighbours keep the extent they had: nothing grows into the hole
  const before = plain.columns[0].find((x) => x.sweep === 1); // 11: masked
  const after = masked.columns[0].find((x) => x.sweep === 1);
  assert.ok(before && !after);
  const c1 = plain.columns[1].find((x) => x.sweep === 1);
  assert.equal(c1, undefined); // that one was a null in the data
  const keep = masked.columns[2].find((x) => x.sweep === 1);
  const same = plain.columns[2].find((x) => x.sweep === 1);
  near(keep.y0, same.y0);
  near(keep.y1, same.y1);
  // a mask that hides everything leaves an empty (but well-formed) section
  const none = buildCells(section(), { fill: true, masked: () => true });
  assert.equal(none.cells.length, 0);
  assert.equal(none.columns.length, 3);
});

test('cells: nothing below the radar level, a null value or height makes no cell', () => {
  const sc = buildCells(section(), { fill: false });
  for (const c of sc.cells) assert.ok(c.y0 >= 0);
  assert.equal(sc.columns[0].some((x) => x.sweep === 2), false);
  assert.equal(sc.columns[2].some((x) => x.sweep === 2), false);
});

test('cells: beams sharing a height do not make zero-height cells', () => {
  const d = {
    distance_km: [0, 1],
    n_sweeps: 2,
    height_km: [[1, 1], [1, 1]],
    values: [[5, 5], [6, 6]],
  };
  for (const c of buildCells(d, { fill: true }).cells) assert.ok(c.y1 > c.y0);
});

test('cells: an empty or single-column section does not throw', () => {
  assert.deepEqual(buildCells({ distance_km: [], height_km: [], values: [], n_sweeps: 0 }, { fill: true }).cells, []);
  const one = buildCells({ distance_km: [3], height_km: [[1]], values: [[4]], n_sweeps: 1 }, { fill: true });
  assert.equal(one.cells.length, 1);
  near(one.cells[0].x1 - one.cells[0].x0, 1);
});

test('hit test: column and cell under a point', () => {
  const d = section();
  const sc = buildCells(d, { fill: true });
  assert.equal(columnAt(d.distance_km, 4.9), 0);
  assert.equal(columnAt(d.distance_km, 5.1), 1);
  assert.equal(columnAt(d.distance_km, -6), -1);
  assert.equal(columnAt(d.distance_km, 26), -1);
  assert.equal(cellAt(sc, d.distance_km, 10, 0.7).v, 20);
  assert.equal(cellAt(sc, d.distance_km, 0, 1.4).v, 11);
  assert.equal(cellAt(sc, d.distance_km, 0, 19), null); // above everything
  assert.equal(cellAt(sc, d.distance_km, 99, 1), null);
});

test('auto height: a nice number just above the top echo', () => {
  const sc = buildCells(section(), { fill: true });
  const top = autoHeightTop(sc.cells);
  assert.ok(top >= Math.max(...sc.cells.map((c) => c.y1)));
  assert.ok(top <= 5);
  assert.equal(autoHeightTop([]), 15);
  // a few noisy gates far above the storm do not stretch the axis past the cap
  assert.equal(autoHeightTop([{ y1: 43 }, { y1: 3 }]), 20);
  assert.equal(autoHeightTop([{ y1: 43 }], 15, 30), 30);
});

test('layout: vertical exaggeration shapes the plot, fit fills the area', () => {
  const fit = computeLayout(1200, 700, 100, 15, null);
  near(fit.plot.w, 1200 - 64 - 104);
  near(fit.plot.h, 700 - 62 - 54);
  const one = computeLayout(1200, 700, 100, 15, 1);
  near(one.plot.h / one.plot.w, 0.15); // 15 km tall over 100 km wide
  const five = computeLayout(1200, 700, 100, 15, 5);
  near(five.plot.h / five.plot.w, 0.75);
  // too tall for the window: width gives way, the picture stays in proportion
  const tall = computeLayout(1200, 400, 100, 15, 5);
  near(tall.plot.h, 400 - 62 - 54);
  near(effectiveExaggeration(tall.plot, 100, 15), 5, 1e-9);
  near(effectiveExaggeration(fit.plot, 100, 15), (fit.plot.h / 15) / (fit.plot.w / 100));
  // always inside the canvas, colour bar beside the plot
  for (const l of [fit, one, five, tall]) {
    assert.ok(l.plot.x >= 0 && l.plot.x + l.plot.w <= 1200);
    assert.ok(l.plot.y >= 0 && l.plot.y + l.plot.h <= 700);
    assert.ok(l.colorbar.x > l.plot.x + l.plot.w);
  }
});

test('layout survives a tiny canvas and an empty extent', () => {
  const l = computeLayout(50, 50, 0, 0, 3);
  assert.ok(l.plot.w > 0 && l.plot.h > 0);
});

test('zoom keeps the point under the cursor and stays inside the section', () => {
  const full = { d0: 0, d1: 100, h0: 0, h1: 20 };
  const z = zoomView(full, full, 0.25, 0.5, 2);
  near(z.d1 - z.d0, 50);
  near(z.h1 - z.h0, 10);
  near(z.d0 + 0.25 * 50, 25); // the point at 25 % of the width is still at 25 km
  near(z.h0 + 0.5 * 10, 10);
  assert.ok(isFullView(zoomView(full, full, 0.5, 0.5, 0.5), full), 'cannot zoom out past the whole section');
  const corner = zoomView(full, full, 0, 0, 4);
  near(corner.d0, 0);
  near(corner.h0, 0);
  const deep = zoomView(full, full, 0.5, 0.5, 1e6);
  near(deep.d1 - deep.d0, 100 / 40);
});

test('pan moves the window and stops at the edges', () => {
  const full = { d0: 0, d1: 100, h0: 0, h1: 20 };
  const z = zoomView(full, full, 0.5, 0.5, 4);
  const p = panView(z, full, 0.5, 0);
  near(p.d0 - z.d0, 12.5);
  near(p.h0, z.h0);
  const far = panView(z, full, 50, -50);
  near(far.d1, 100);
  near(far.h0, 0);
  near(far.d1 - far.d0, z.d1 - z.d0);
});

test('azimuth / range <-> radar-centred metres round-trips', () => {
  for (const az of [0, 37.5, 90, 181, 270, 359.9]) {
    const [x, y] = azRangeToXY(az, 123.4);
    const back = xyToAzRange(x, y);
    near(back.azimuth, az, 1e-9);
    near(back.rangeKm, 123.4, 1e-9);
  }
  const [x, y] = azRangeToXY(90, 10);
  near(x, 10000, 1e-6);
  near(y, 0, 1e-6);
  assert.deepEqual(xyToAzRange(0, 0), { azimuth: 0, rangeKm: 0 });
  assert.equal(wrapAzimuth(-10), 350);
  assert.equal(wrapAzimuth(725), 5);
});

test('great-circle length and the sampling density', () => {
  near(haversineKm(0, 0, 0, 1), 111.195, 0.01);
  near(haversineKm(35, -97, 35, -97), 0);
  assert.equal(sectionPointCount(10), 200);
  assert.equal(sectionPointCount(150), 600);
  assert.equal(sectionPointCount(1000), 1200);
  assert.equal(sectionPointCount(NaN), 200);
});

test('title and subtitle say what and where', () => {
  const free = describeSection({
    variable: 'DBZH',
    instrument: 'KTLX',
    time: '2013-05-20 20:16:43',
    start: [35.5, -97.9],
    end: [35.1, -96.8],
    lengthKm: 112.34,
  });
  assert.equal(free.title, 'DBZH cross-section  —  KTLX');
  assert.match(free.subtitle, /2013-05-20 20:16:43 UTC/);
  assert.match(free.subtitle, /A 35\.50°N 97\.90°W → B 35\.10°N 96\.80°W/);
  assert.match(free.subtitle, /112\.3 km/);
  const rhi = describeSection({ variable: 'VRADH', fileName: 'x.nc', rhiAzimuth: 365, maxRangeKm: 150 });
  assert.equal(rhi.title, 'VRADH cross-section');
  assert.match(rhi.subtitle, /x\.nc/);
  assert.match(rhi.subtitle, /RHI-style cut, azimuth 5\.0°, 0–150 km/);
  assert.equal(fmtLatLon(-3.456, 12.3), '3.46°S 12.30°E');
});

test('file names are safe', () => {
  assert.equal(safeStem('KTLX 2013/05:20'), 'KTLX_2013_05_20');
  assert.equal(safeStem('///'), 'cross_section');
});
