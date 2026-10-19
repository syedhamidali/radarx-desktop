// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_NOISE,
  NODATA,
  cleanField,
  despeckle,
  findCompanions,
  noiseCaption,
  normalizeNoise,
  qualityKind,
} from './rhiClean.ts';
import { buildLookup, colorizeValues, rasterizeRhiGrid, sampleAt, beamPoint } from './rhiGeometry.ts';
import { gaussianSmoothNaN, resampleGrid } from './crossSectionRaster.ts';

const raw = { ...DEFAULT_NOISE, raw: true };
const off = {
  ...DEFAULT_NOISE,
  sqi: { on: false, min: 0.4 },
  snr: { on: false, min: 3 },
  rhohv: { on: false, min: 0.7 },
  despeckle: { on: false, minGates: 8 },
};

test('quality fields are found by name in CPOL, NPOL, NEXRAD and ODIM spellings', () => {
  assert.deepEqual(findCompanions(['DBZ', 'VEL', 'SQI', 'RHOHV', 'ZDR']), { sqi: 'SQI', rhohv: 'RHOHV' });
  assert.deepEqual(
    findCompanions(['corrected_reflectivity', 'normalized_coherent_power', 'cross_correlation_ratio']),
    { sqi: 'normalized_coherent_power', rhohv: 'cross_correlation_ratio' },
  );
  assert.deepEqual(findCompanions(['DBZH', 'SNRH', 'RHOHV', 'NCP']), { snr: 'SNRH', rhohv: 'RHOHV', sqi: 'NCP' });
  assert.deepEqual(findCompanions(['reflectivity', 'velocity', 'spectrum_width']), {});
  assert.equal(qualityKind('differential_reflectivity'), null);
  assert.equal(qualityKind('DBZ_TOT'), null);
});

/** 4 rays x 6 gates of 20 dBZ; quality fields are per-gate. */
function grid(nRays = 4, nRange = 6, v = 20) {
  return { values: new Float32Array(nRays * nRange).fill(v), nRays, nRange };
}

test('raw data is an untouched copy and says so', () => {
  const g = grid();
  g.values[3] = NaN;
  const r = cleanField({ ...g, quality: { sqi: { name: 'SQI', values: new Float32Array(24).fill(0) } } }, raw);
  assert.equal(r.values[0], 20);
  assert.equal(r.values[3], NODATA); // NaN -> the no-data sentinel, like the wire format
  assert.deepEqual(r.applied, []);
  assert.notEqual(r.values, g.values);
  assert.equal(noiseCaption(raw, r), 'Raw data: no masking, despeckling or smoothing.');
});

test('SQI / RHOHV masks hide gates below the limit and keep gates without a quality value', () => {
  const g = grid();
  const sqi = new Float32Array(24).fill(0.9);
  sqi[0] = 0.1; // low quality: hidden
  sqi[1] = 0.4; // exactly the limit: kept
  sqi[2] = NODATA; // no quality value: kept
  const rho = new Float32Array(24).fill(0.95);
  rho[5] = 0.5;
  const r = cleanField(
    { ...g, quality: { sqi: { name: 'SQI', values: sqi }, rhohv: { name: 'RHOHV', values: rho } } },
    { ...off, sqi: { on: true, min: 0.4 }, rhohv: { on: true, min: 0.7 } },
  );
  assert.equal(r.values[0], NODATA);
  assert.equal(r.values[1], 20);
  assert.equal(r.values[2], 20);
  assert.equal(r.values[5], NODATA);
  assert.equal(r.validBefore, 24);
  assert.equal(r.validAfter, 22);
  assert.deepEqual(r.applied, ['SQI ≥ 0.4', 'RHOHV ≥ 0.7']);
  assert.equal(r.qualityApplied, 2);
  assert.match(noiseCaption({ ...off, sqi: { on: true, min: 0.4 } }, r), /^Cleaned: SQI ≥ 0.4, RHOHV ≥ 0.7 \(hid 8% of gates\)/);
});

test('a quality mask whose field is missing is reported, not applied', () => {
  const r = cleanField({ ...grid(), quality: {} }, { ...off, sqi: { on: true, min: 0.4 }, snr: { on: true, min: 3 } });
  assert.deepEqual(r.missing, ['sqi', 'snr']);
  assert.equal(r.validAfter, 24);
  assert.match(noiseCaption(DEFAULT_NOISE, r), /no SQI\/SNR field in this sweep/);
});

test('SNR is in dB', () => {
  const snr = new Float32Array(24).fill(10);
  snr[7] = 1;
  const r = cleanField({ ...grid(), quality: { snr: { name: 'SNRH', values: snr } } }, { ...off, snr: { on: true, min: 3 } });
  assert.equal(r.values[7], NODATA);
  assert.deepEqual(r.applied, ['SNRH ≥ 3 dB']);
});

test('the value mask hides values and is named in the caption', () => {
  const g = grid();
  g.values[0] = 5;
  g.values[1] = 25;
  const r = cleanField({ ...g, quality: {}, mask: { mode: 'below', min: 10, max: null, inclusive: false } }, off, 'dBZ');
  assert.equal(r.values[0], NODATA);
  assert.equal(r.values[1], 25);
  assert.deepEqual(r.applied, ['hide below 10 dBZ']);
  // an incomplete mask does nothing
  const r2 = cleanField({ ...g, quality: {}, mask: { mode: 'below', min: null, max: null, inclusive: false } }, off);
  assert.equal(r2.values[0], 5);
});

test('despeckle removes small groups, keeps big ones and joins diagonal neighbours', () => {
  // 6 rays x 10 gates; a 2x2 blob (size 4), a diagonal pair (size 2), a lone gate, and a 3x4 block (size 12)
  const nRays = 6;
  const nRange = 10;
  const v = new Float32Array(nRays * nRange).fill(NODATA);
  const set = (r, g) => (v[r * nRange + g] = 10);
  set(0, 0); set(0, 1); set(1, 0); set(1, 1); // blob of 4
  set(3, 3); set(4, 4); // diagonal pair (8-connected)
  set(0, 9); // lone
  for (let r = 3; r < 6; r++) for (let g = 6; g < 10; g++) set(r, g); // 12
  const removed = despeckle(v, nRays, nRange, 8);
  assert.equal(removed, 4 + 2 + 1);
  assert.equal(v[0 * nRange + 0], NODATA);
  assert.equal(v[3 * nRange + 3], NODATA);
  assert.equal(v[0 * nRange + 9], NODATA);
  assert.equal(v[4 * nRange + 7], 10);
  // groups of exactly the limit survive
  const w = new Float32Array(nRays * nRange).fill(NODATA);
  for (let g = 0; g < 8; g++) w[g] = 3;
  assert.equal(despeckle(w, nRays, nRange, 8), 0);
  assert.equal(despeckle(w, nRays, nRange, 9), 8);
});

test('cleanField applies the steps in order and counts what was hidden', () => {
  const g = grid(6, 10);
  g.values.fill(NODATA);
  for (let r = 0; r < 6; r++) for (let q = 0; q < 10; q++) g.values[r * 10 + q] = 30; // all valid
  const sqi = new Float32Array(60).fill(0.9);
  for (let q = 0; q < 10; q++) sqi[q] = 0.1; // ray 0 is noise
  sqi[3 * 10 + 4] = 0.9;
  g.values[2 * 10 + 5] = 2; // below the value mask
  const r = cleanField(
    { ...g, quality: { sqi: { name: 'SQI', values: sqi } }, mask: { mode: 'below', min: 5, max: null, inclusive: false } },
    { ...off, sqi: { on: true, min: 0.4 }, despeckle: { on: true, minGates: 8 } },
    'dBZ',
  );
  assert.equal(r.validBefore, 60);
  assert.equal(r.validAfter, 49); // ray 0 (10) and the masked gate (1)
  assert.deepEqual(r.applied, ['SQI ≥ 0.4', 'hide below 5 dBZ', 'despeckled (< 8 gates)']);
});

test('the default settings are on and clean; normalizeNoise repairs anything', () => {
  assert.equal(DEFAULT_NOISE.raw, false);
  assert.ok(DEFAULT_NOISE.sqi.on && DEFAULT_NOISE.snr.on && DEFAULT_NOISE.despeckle.on);
  assert.equal(DEFAULT_NOISE.smoothing.interp, 'bilinear');
  assert.deepEqual(normalizeNoise(undefined), DEFAULT_NOISE);
  assert.deepEqual(normalizeNoise(DEFAULT_NOISE), DEFAULT_NOISE);
  const n = normalizeNoise({ raw: 'yes', sqi: { on: false, min: 9 }, despeckle: { minGates: 'x' }, smoothing: { interp: 'cubic', sigma: 99 } });
  assert.equal(n.raw, false);
  assert.deepEqual(n.sqi, { on: false, min: 1 }); // clamped
  assert.equal(n.despeckle.minGates, 8);
  assert.equal(n.smoothing.interp, 'nearest');
  assert.equal(n.smoothing.sigma, 4);
});

test('caption mentions smoothing and omits it when plain', () => {
  const r = { applied: ['SQI ≥ 0.4'], missing: [], qualityApplied: 1, validBefore: 100, validAfter: 100 };
  const text = noiseCaption(DEFAULT_NOISE, r, 0.075);
  assert.match(text, /^Cleaned: SQI ≥ 0.4 {2}· {2}Smoothed: bilinear interpolation, Gaussian σ 1 gate \(0.07 km\)/);
  assert.doesNotMatch(text, /hid/); // nothing hidden: no "0%"
  assert.equal(noiseCaption({ ...off, smoothing: { interp: 'nearest', sigma: 0, upsample: 1 } }, { ...r, applied: [] }), 'No masking or smoothing applied.');
});

// ─── grid sampling + smoothing glue ─────────────────────────────────────────

function field() {
  const nRays = 10;
  const nRange = 20;
  const elevation = Float32Array.from({ length: nRays }, (_, k) => 2 + 2 * k);
  const range = Float32Array.from({ length: nRange }, (_, g) => (5 + 5 * g) * 1000);
  const values = new Float32Array(nRays * nRange);
  for (let k = 0; k < nRays; k++) for (let g = 0; g < nRange; g++) values[k * nRange + g] = 10 * k + g;
  return { elevation, range, values, nRays, nRange };
}

test('rasterizeRhiGrid holds what sampleAt says, NaN elsewhere', () => {
  const lk = buildLookup(field());
  const full = { d0: -5, d1: 105, h0: -2, h1: 40 };
  const g = rasterizeRhiGrid(lk, full, 220, 84);
  let checked = 0;
  for (let j = 0; j < g.ny; j += 3) {
    for (let i = 0; i < g.nx; i += 3) {
      const d = full.d0 + ((i + 0.5) * (full.d1 - full.d0)) / g.nx;
      const h = full.h1 - ((j + 0.5) * (full.h1 - full.h0)) / g.ny;
      const s = sampleAt(lk, d * 1000, h * 1000);
      const v = g.data[j * g.nx + i];
      if (s && Number.isFinite(s.value)) assert.equal(v, s.value);
      else assert.ok(Number.isNaN(v));
      checked++;
    }
  }
  assert.ok(checked > 1000);
  assert.ok(g.data.some((v) => v === v));
});

test('smoothing a gridded RHI never fills holes or invents echo', () => {
  const f = field();
  for (let g = 8; g < 20; g++) f.values[4 * f.nRange + g] = -9999; // a gap in one ray
  const lk = buildLookup(f);
  const full = { d0: 0, d1: 100, h0: 0, h1: 40 };
  const grid = rasterizeRhiGrid(lk, full, 200, 160);
  const smooth = gaussianSmoothNaN(grid.data, grid.nx, grid.ny, 2, 2);
  for (let i = 0; i < smooth.length; i++) assert.equal(Number.isNaN(smooth[i]), Number.isNaN(grid.data[i]));
  const lo = Math.min(...f.values.filter((v) => v > -9998));
  const hi = Math.max(...f.values);
  for (const v of smooth) if (v === v) assert.ok(v >= lo - 1e-3 && v <= hi + 1e-3);
  const out = resampleGrid({ ...grid, data: smooth }, { d0: 10, d1: 60, h0: 0, h1: 20 }, 120, 60, 'bicubic');
  assert.equal(out.length, 7200);
  assert.ok(out.some((v) => v === v) && out.some((v) => v !== v));
});

test('colorizeValues paints values in range and leaves the rest transparent', () => {
  const lut = Uint8ClampedArray.from({ length: 768 }, (_, i) => (i % 3 === 0 ? Math.floor(i / 3) : 0));
  const out = new Uint8ClampedArray(4 * 4);
  const n = colorizeValues([NaN, -9999, 5, 100], lut, 10, 110, out);
  assert.equal(n, 1);
  assert.deepEqual([...out.filter((_, i) => i % 4 === 3)], [0, 0, 0, 255]); // 5 < vmin is NOT painted; only 100 is
  assert.ok(Math.abs(out[12] - 229.5) <= 1);
  assert.ok(beamPoint(1000, 1).groundM > 0);
});
