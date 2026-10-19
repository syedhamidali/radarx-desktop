// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { rayLayout, rayAtAzimuth } from './meshBuilder.ts';
import {
  angleLayout,
  angleHalfWidths,
  angleIndexAt,
  beamPoint,
  beamInverse,
  buildLookup,
  buildRhiLayout,
  buildRangeAxis,
  dataExtent,
  EFFECTIVE_RADIUS_M,
  formatTick,
  gateAtRange,
  gridLines,
  niceTicks,
  rasterizeRhi,
  rayAtElevation,
  sampleAt,
  sweepLabel,
} from './rhiGeometry.ts';

// ─── beam geometry ─────────────────────────────────────────────────────────

test('beamPoint: horizontal beam rises by r^2 / 2Re; vertical beam has no ground distance', () => {
  const flat = beamPoint(100_000, 0);
  assert.ok(Math.abs(flat.heightM - 100_000 ** 2 / (2 * EFFECTIVE_RADIUS_M)) < 1);
  assert.ok(Math.abs(flat.groundM - 100_000) < 50);
  const up = beamPoint(10_000, 90);
  assert.ok(Math.abs(up.groundM) < 1e-6);
  assert.ok(Math.abs(up.heightM - 10_000) < 1e-6);
  const diag = beamPoint(10_000, 30);
  assert.ok(Math.abs(diag.heightM - 5000) < 20 && Math.abs(diag.groundM - 8660) < 20);
});

test('beamPoint agrees with radarx.fundamentals.geometry (beam_center_height / ground_range)', () => {
  // radarx: beam_center_height(100000, 0.5) = 1461.1325028166175, ground_range(100000, 0.5) = 99981.30372405128
  const p = beamPoint(100_000, 0.5);
  assert.ok(Math.abs(p.heightM - 1461.1325028166175) < 1e-6);
  assert.ok(Math.abs(p.groundM - 99981.30372405128) < 1e-6);
});

test('beamInverse undoes beamPoint', () => {
  for (const r of [1000, 25_000, 120_000, 300_000]) {
    for (const el of [-1, 0.5, 5, 30, 60, 89]) {
      const p = beamPoint(r, el);
      const q = beamInverse(p.groundM, p.heightM);
      assert.ok(Math.abs(q.rangeM - r) < 0.5, `range ${r}@${el}: ${q.rangeM}`);
      assert.ok(Math.abs(q.elDeg - el) < 1e-6, `el ${r}@${el}: ${q.elDeg}`);
    }
  }
});

// ─── ray layout generalisation ─────────────────────────────────────────────

test('angleLayout with a 360 period matches meshBuilder.rayLayout', () => {
  const sets = [
    Array.from({ length: 360 }, (_, i) => i), // full circle, closed
    Array.from({ length: 131 }, (_, i) => 50 + i), // sector, open
    Array.from({ length: 91 }, (_, i) => 180 - i), // descending
    [10, 11, 12, 13, 40, 41, 42], // big hole
  ];
  for (const s of sets) {
    const az = Float32Array.from(s);
    assert.deepEqual(angleLayout(az, az.length, 360), rayLayout(az, az.length));
  }
  const sector = Float32Array.from(sets[1]);
  const holey = Float32Array.from(sets[3]);
  const full = Float32Array.from(sets[0]);
  for (const [az, probes] of [
    [sector, [100.2, 49.8, 10, 250, 181.0]],
    [holey, [25, 41.2, 14.4]],
    [full, [359.7, 180.4, 0.2]],
  ]) {
    for (const a of probes) assert.equal(angleIndexAt(az, az.length, a, 360), rayAtAzimuth(az, az.length, a));
  }
});

test('without a period an elevation list never wraps around', () => {
  const el = Float32Array.from([1, 2, 3, 4, 5, 6]);
  assert.equal(angleLayout(el, 6, 0).closed, false);
  // 359 deg is not "next to" 1 deg for an elevation list
  assert.equal(angleIndexAt(el, 6, -0.3, 0), -1);
  const hw = angleHalfWidths(el, 6, 0, angleLayout(el, 6, 0), 0);
  assert.equal(hw.halfPrev, 0.5); // half a normal step at the end of the scan
  assert.equal(hw.halfNext, 0.5);
});

test('RHI layout: cells reach half-way to a neighbour, sector ends and big holes stay empty', () => {
  const el = Float32Array.from([1, 2, 3, 4, 30, 31, 32]);
  const lay = buildRhiLayout(el, el.length);
  assert.equal(rayAtElevation(lay, 0.6), 0);
  assert.equal(rayAtElevation(lay, 0.4), -1); // below the first cell (half a step under ray 0)
  assert.equal(rayAtElevation(lay, 3.4), 2);
  assert.equal(rayAtElevation(lay, 3.6), 3);
  assert.equal(rayAtElevation(lay, 4.4), 3); // end of the sector below the hole
  assert.equal(rayAtElevation(lay, 15), -1); // the 26 degree hole is not smeared
  assert.equal(rayAtElevation(lay, 29.6), 4);
  assert.equal(rayAtElevation(lay, 32.4), 6);
  assert.equal(rayAtElevation(lay, 33), -1); // above the scan
});

test('RHI layout: a moderate hole is split between its neighbours', () => {
  const el = Float32Array.from([10, 11, 12, 13, 17.5, 18.5, 19.5]);
  const lay = buildRhiLayout(el, el.length);
  assert.equal(rayAtElevation(lay, 14), 3);
  assert.equal(rayAtElevation(lay, 15.2), 3);
  assert.equal(rayAtElevation(lay, 15.3), 4);
  assert.equal(rayAtElevation(lay, 17), 4);
});

test('RHI layout: irregular real-world spacing resolves to the nearest ray', () => {
  const el = Float32Array.from([0.53, 0.92, 1.36, 1.85, 2.42, 3.16, 4.0]);
  const lay = buildRhiLayout(el, el.length);
  assert.equal(rayAtElevation(lay, 0.5), 0);
  assert.equal(rayAtElevation(lay, 1.1), 1);
  assert.equal(rayAtElevation(lay, 1.15), 2);
  assert.equal(rayAtElevation(lay, 2.75), 4);
  assert.equal(rayAtElevation(lay, 2.9), 5);
  assert.equal(rayAtElevation(lay, 4.2), 6);
});

// ─── gates ──────────────────────────────────────────────────────────────────

test('gateAtRange: uniform and irregular gate spacing', () => {
  const ax = buildRangeAxis(Float32Array.from([1000, 1250, 1500, 1750]), 4);
  assert.equal(ax.uniform, true);
  assert.equal(gateAtRange(ax, 1000), 0);
  assert.equal(gateAtRange(ax, 1370), 1);
  assert.equal(gateAtRange(ax, 1380), 2);
  assert.equal(gateAtRange(ax, 1874), 3);
  assert.equal(gateAtRange(ax, 1876), -1);
  assert.equal(gateAtRange(ax, 800), -1);
  const irr = buildRangeAxis(Float32Array.from([100, 200, 400, 800]), 4);
  assert.equal(irr.uniform, false);
  assert.equal(gateAtRange(irr, 290), 1);
  assert.equal(gateAtRange(irr, 310), 2);
  assert.equal(gateAtRange(irr, 700), 3);
});

// ─── sampling + rasterising ────────────────────────────────────────────────

/** A small RHI: ray k at elevation 2 + 2k (0..9), gate g at 5 + 5g km; value = 10 * ray + gate. */
function fixture(extra = {}) {
  const nRays = 10;
  const nRange = 20;
  const elevation = Float32Array.from({ length: nRays }, (_, k) => 2 + 2 * k);
  const range = Float32Array.from({ length: nRange }, (_, g) => (5 + 5 * g) * 1000);
  const values = new Float32Array(nRays * nRange);
  for (let k = 0; k < nRays; k++) for (let g = 0; g < nRange; g++) values[k * nRange + g] = 10 * k + g;
  return { elevation, range, values, nRays, nRange, ...extra };
}

const lut = Uint8ClampedArray.from({ length: 768 }, (_, i) => (i % 3 === 0 ? Math.floor(i / 3) : 0)); // red = index

test('sampleAt finds the ray and gate under a point', () => {
  const lk = buildLookup(fixture());
  const p = beamPoint(52_000, 10); // ray 4 (el 10), gate 9 (r 50 km spans 47.5..52.5)
  const s = sampleAt(lk, p.groundM, p.heightM);
  assert.equal(s.ray, 4);
  assert.equal(s.gate, 9);
  assert.equal(s.value, 49);
  assert.ok(Math.abs(s.rangeM - 52_000) < 1 && Math.abs(s.elDeg - 10) < 1e-6);
  assert.equal(sampleAt(lk, 200_000, 0), null); // beyond the last gate
  assert.equal(sampleAt(lk, 30_000, -3_000), null); // below the lowest ray
});

test('rasterizeRhi paints exactly the pixels sampleAt says have data', () => {
  const lk = buildLookup(fixture());
  const vp = { xLeftKm: -5, yTopKm: 40, kmPerPxX: 0.5, kmPerPxY: 0.25, width: 240, height: 180 };
  const out = new Uint8ClampedArray(vp.width * vp.height * 4);
  const painted = rasterizeRhi(lk, vp, lut, 0, 255, out);
  assert.ok(painted > 1000);
  let mismatches = 0;
  let count = 0;
  for (let py = 0; py < vp.height; py++) {
    for (let px = 0; px < vp.width; px++) {
      const g = (vp.xLeftKm + (px + 0.5) * vp.kmPerPxX) * 1000;
      const h = (vp.yTopKm - (py + 0.5) * vp.kmPerPxY) * 1000;
      const s = sampleAt(lk, g, h);
      const a = out[(py * vp.width + px) * 4 + 3];
      const expect = s && Number.isFinite(s.value) ? 255 : 0;
      if (a !== expect) mismatches++;
      if (a) {
        count++;
        assert.equal(out[(py * vp.width + px) * 4], s.value | 0, 'colour index = value');
      }
    }
  }
  assert.equal(mismatches, 0);
  assert.equal(count, painted);
});

test('rasterizeRhi: no-data gates and values below vmin stay transparent', () => {
  const f = fixture();
  f.values[4 * f.nRange + 9] = -9999; // the gate we probe
  f.values[4 * f.nRange + 10] = NaN;
  f.values[4 * f.nRange + 11] = 1; // below vmin
  const lk = buildLookup(f);
  const vp = { xLeftKm: 0, yTopKm: 20, kmPerPxX: 0.25, kmPerPxY: 0.25, width: 400, height: 120 };
  const out = new Uint8ClampedArray(vp.width * vp.height * 4);
  rasterizeRhi(lk, vp, lut, 20, 255, out);
  const pixel = (r, el) => {
    const p = beamPoint(r, el);
    const px = Math.floor((p.groundM / 1000 - vp.xLeftKm) / vp.kmPerPxX);
    const py = Math.floor((vp.yTopKm - p.heightM / 1000) / vp.kmPerPxY);
    return out[(py * vp.width + px) * 4 + 3];
  };
  assert.equal(pixel(50_000, 10), 0); // sentinel
  assert.equal(pixel(55_000, 10), 0); // NaN
  assert.equal(pixel(60_000, 10), 0); // value 1 < vmin 20
  assert.equal(pixel(30_000, 10), 255); // 40 + 5 = 45 >= 20
  assert.equal(pixel(30_000, 2), 0); // ray 0, gate 5 = 5 < vmin
});

test('a gap between sectors stays empty in the picture', () => {
  const f = fixture();
  // rays at elevations 2,4,6,8 | 30,32,34,36,38,40: the 8..30 hole is not filled
  f.elevation = Float32Array.from([2, 4, 6, 8, 30, 32, 34, 36, 38, 40]);
  const lk = buildLookup(f);
  assert.equal(sampleAt(lk, ...Object.values(beamPoint(40_000, 18))), null);
  assert.ok(sampleAt(lk, ...Object.values(beamPoint(40_000, 7))) !== null);
  assert.ok(sampleAt(lk, ...Object.values(beamPoint(40_000, 31))) !== null);
});

test('dataExtent bounds the gates with data and always includes the radar', () => {
  const f = fixture();
  for (let k = 0; k < f.nRays; k++) for (let g = 10; g < f.nRange; g++) f.values[k * f.nRange + g] = -9999;
  const e = dataExtent(buildLookup(f));
  assert.equal(e.xMinKm, 0);
  assert.equal(e.yMinKm, 0);
  const last = beamPoint(55_000 + 2_500, 21); // last valid gate (r 50 km) edge, top ray edge (el 20 + 1)
  assert.ok(e.xMaxKm > 40 && e.xMaxKm < 58);
  assert.ok(e.yMaxKm >= beamPoint(55_000, 21).heightM / 1000 - 1);
  assert.ok(e.yMaxKm < last.heightM / 1000 + 1);
  // gates below the colour range's lower end are not drawn, so they do not stretch the extent
  const faint = fixture();
  for (let k = 0; k < faint.nRays; k++) for (let g = 10; g < faint.nRange; g++) faint.values[k * faint.nRange + g] = -30;
  const full = dataExtent(buildLookup(faint));
  const shown = dataExtent(buildLookup(faint), -20);
  assert.ok(full.xMaxKm > 90 && shown.xMaxKm < 58);
  // nothing with data: falls back to the scan extent instead of an empty box
  const empty = fixture();
  empty.values.fill(-9999);
  const e2 = dataExtent(buildLookup(empty));
  assert.ok(e2.xMaxKm > 50 && e2.yMaxKm > 20);
});

// ─── axes / labels ──────────────────────────────────────────────────────────

test('niceTicks / formatTick', () => {
  assert.deepEqual(niceTicks(0, 100, 5), [0, 20, 40, 60, 80, 100]);
  assert.deepEqual(niceTicks(0, 1, 5), [0, 0.2, 0.4, 0.6, 0.8, 1]);
  assert.deepEqual(niceTicks(-3, 3, 6), [-3, -2, -1, 0, 1, 2, 3]);
  assert.deepEqual(niceTicks(5, 5), []);
  assert.equal(formatTick(20), '20');
  assert.equal(formatTick(0.4, 0.2), '0.4');
  assert.equal(formatTick(2.5, 0.5), '2.5');
});

test('gridLines: constant-range arcs and constant-elevation rays', () => {
  const lines = gridLines(1, 50, 120_000);
  const arcs = lines.filter((l) => l.kind === 'range');
  const rays = lines.filter((l) => l.kind === 'elevation');
  assert.ok(arcs.length >= 3 && rays.length >= 3);
  for (const a of arcs) {
    for (const [x, y] of a.points) {
      const q = beamInverse(x * 1000, y * 1000);
      assert.ok(Math.abs(q.rangeM / 1000 - a.value) < 0.01);
    }
  }
  for (const r of rays) {
    for (const [x, y] of r.points.slice(1)) {
      const q = beamInverse(x * 1000, y * 1000);
      assert.ok(Math.abs(q.elDeg - r.value) < 1e-6);
    }
  }
  assert.deepEqual(gridLines(0, 10, 0), []);
});

test('sweepLabel lists RHIs by azimuth', () => {
  assert.equal(sweepLabel({ index: 0, elevation: 135, kind: 'rhi', azimuth: 135 }), 'RHI 135.0° (sweep 0)');
  assert.equal(sweepLabel({ index: 3, elevation: 0.5, kind: 'ppi' }), 'Sweep 3 (0.5°)');
  assert.equal(sweepLabel({ index: 2, elevation: null }), 'Sweep 2 (?°)');
});
