// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  NO_SMOOTHING,
  isPlain,
  normalizeSmoothing,
  smoothingLabel,
  gridSize,
  rasterizeSection,
  gaussianSmoothNaN,
  resampleGrid,
  buildSectionGrid,
  addPngText,
} from './crossSectionRaster.ts';

const near = (a, b, eps = 1e-5) => assert.ok(Math.abs(a - b) <= eps, `${a} != ${b}`);
const nan = (v) => v !== v;

const grid = (rows) => {
  const ny = rows.length;
  const nx = rows[0].length;
  return { nx, ny, d0: 0, d1: nx, h0: 0, h1: ny, data: Float32Array.from(rows.flat()) };
};

test('settings: validation, plain detection and caption', () => {
  assert.ok(isPlain(NO_SMOOTHING));
  assert.deepEqual(normalizeSmoothing(null), NO_SMOOTHING);
  assert.deepEqual(normalizeSmoothing({ interp: 'cubic!', sigma: 'x', upsample: 99 }), { interp: 'nearest', sigma: 0, upsample: 4 });
  assert.deepEqual(normalizeSmoothing({ interp: 'bicubic', sigma: 1.26, upsample: 2.4 }), { interp: 'bicubic', sigma: 1.5, upsample: 2 });
  assert.equal(normalizeSmoothing({ sigma: -3 }).sigma, 0);
  assert.equal(normalizeSmoothing({ sigma: 40 }).sigma, 4);
  assert.ok(!isPlain({ interp: 'nearest', sigma: 0.5, upsample: 1 }));
  assert.ok(!isPlain({ interp: 'nearest', sigma: 0, upsample: 2 }));
  assert.equal(smoothingLabel(NO_SMOOTHING), null);
  assert.equal(
    smoothingLabel({ interp: 'bilinear', sigma: 1, upsample: 2 }, 0.25),
    'Smoothed: bilinear interpolation, Gaussian σ 1 gate (0.25 km), 2× finer grid',
  );
  assert.match(smoothingLabel({ interp: 'bicubic', sigma: 0, upsample: 1 }), /bicubic interpolation$/);
});

test('gaussian: a field with missing data keeps its level (no pull towards zero)', () => {
  const nx = 20;
  const ny = 12;
  const src = new Float32Array(nx * ny).fill(40);
  for (const [i, j] of [[0, 0], [19, 11], [5, 5], [6, 5], [7, 5], [10, 2], [3, 9]]) src[j * nx + i] = NaN;
  const out = gaussianSmoothNaN(src, nx, ny, 2, 2);
  for (let k = 0; k < src.length; k++) {
    if (nan(src[k])) assert.ok(nan(out[k]), 'a missing sample stays missing');
    else near(out[k], 40, 1e-4);
  }
});

test('gaussian: the echo footprint is unchanged and values do not leak into the empty area', () => {
  const nx = 30;
  const ny = 3;
  const src = new Float32Array(nx * ny).fill(NaN);
  for (let j = 0; j < ny; j++) for (let i = 0; i < 10; i++) src[j * nx + i] = 50; // echo on the left
  for (let j = 0; j < ny; j++) for (let i = 20; i < 30; i++) src[j * nx + i] = 10; // weak echo on the right
  const out = gaussianSmoothNaN(src, nx, ny, 3, 0);
  for (let i = 10; i < 20; i++) assert.ok(nan(out[i]), 'the gap stays empty');
  for (let i = 0; i < 10; i++) near(out[i], 50, 1e-4); // the gap is out of reach for the left echo's own value
  for (let i = 20; i < 30; i++) near(out[i], 10, 1e-4);
});

test('gaussian: it smooths a step and conserves what it should', () => {
  const nx = 40;
  const src = new Float32Array(nx);
  for (let i = 0; i < nx; i++) src[i] = i < 20 ? 0 : 10;
  const out = gaussianSmoothNaN(src, nx, 1, 2, 0);
  assert.ok(out[19] > 0 && out[19] < 5);
  assert.ok(out[20] > 5 && out[20] < 10);
  near(out[19] + out[20], 10, 1e-4); // symmetric about the step
  for (let i = 1; i < nx; i++) assert.ok(out[i] >= out[i - 1] - 1e-6, 'monotone');
  // sigma 0 is the identity
  assert.deepEqual(Array.from(gaussianSmoothNaN(src, nx, 1, 0, 0)), Array.from(src));
  // a lone echo spreads but its weighted mean stays put
  const spike = new Float32Array(41).fill(NaN);
  spike[20] = 30;
  assert.equal(gaussianSmoothNaN(spike, 41, 1, 2, 0)[20], 30); // alone, normalised: still 30
});

test('gaussian: the wide (box-blur) path agrees with the exact kernel and keeps the same guarantees', () => {
  const nx = 200;
  const ny = 4;
  const src = new Float32Array(nx * ny);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) src[j * nx + i] = i < 100 ? 0 : 10;
  const exact = gaussianSmoothNaN(src, nx, ny, 12, 0, Infinity);
  const boxed = gaussianSmoothNaN(src, nx, ny, 12, 0, 4);
  let worst = 0;
  for (let i = 0; i < src.length; i++) worst = Math.max(worst, Math.abs(exact[i] - boxed[i]));
  assert.ok(worst < 0.25, `box blur within 2.5 % of the exact Gaussian (${worst})`);
  // missing data: level kept, footprint kept
  const holes = new Float32Array(nx * ny).fill(40);
  for (const [i, j] of [[0, 0], [199, 3], [50, 1], [51, 1], [120, 2], [121, 2], [122, 2]]) holes[j * nx + i] = NaN;
  const out = gaussianSmoothNaN(holes, nx, ny, 9, 9);
  for (let k = 0; k < holes.length; k++) {
    if (nan(holes[k])) assert.ok(nan(out[k]));
    else near(out[k], 40, 1e-3);
  }
});

test('gaussian: works along height as well', () => {
  const nx = 2;
  const ny = 30;
  const src = new Float32Array(nx * ny);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) src[j * nx + i] = j < 15 ? 0 : 20;
  const out = gaussianSmoothNaN(src, nx, ny, 0, 2);
  assert.ok(out[14 * nx] > 0 && out[15 * nx] < 20);
  near(out[14 * nx] + out[15 * nx], 20, 1e-4);
});

test('resample: nearest keeps measured values and missing data', () => {
  const g = grid([[1, NaN, 3], [4, 5, 6]]);
  const out = resampleGrid(g, { d0: 0, d1: 3, h0: 0, h1: 2 }, 6, 4, 'nearest');
  assert.equal(out[0], 1);
  assert.ok(nan(out[2]) && nan(out[3]));
  assert.equal(out[5], 3);
  assert.equal(out[3 * 6 + 3], 5);
});

test('resample: bilinear reproduces a linear ramp and bicubic a constant', () => {
  const rows = [];
  for (let j = 0; j < 8; j++) rows.push(Array.from({ length: 8 }, (_, i) => 10 * i + j));
  const g = grid(rows);
  const out = resampleGrid(g, { d0: 0, d1: 8, h0: 0, h1: 8 }, 32, 32, 'bilinear');
  // inside the sample centres (not the half-cell border) the ramp is exact
  const x = 12;
  const y = 16;
  const d = 0 + ((x + 0.5) / 32) * 8;
  const h = 8 - ((y + 0.5) / 32) * 8;
  const gx = d - 0.5;
  const gy = 8 - h - 0.5;
  near(out[y * 32 + x], 10 * gx + gy, 1e-4);
  const flat = grid(Array.from({ length: 6 }, () => Array(6).fill(7)));
  for (const v of resampleGrid(flat, { d0: 0, d1: 6, h0: 0, h1: 6 }, 24, 24, 'bicubic')) near(v, 7, 1e-5);
});

test('resample: never smears into empty cells, never fades towards zero next to them', () => {
  // echo of 40 with a hole: every interpolation keeps the hole empty and the rest at 40
  const rows = Array.from({ length: 8 }, () => Array(8).fill(40));
  for (const [i, j] of [[3, 3], [4, 3], [3, 4], [4, 4]]) rows[j][i] = NaN;
  const g = grid(rows);
  for (const mode of ['nearest', 'bilinear', 'bicubic']) {
    const out = resampleGrid(g, { d0: 0, d1: 8, h0: 0, h1: 8 }, 64, 64, mode);
    let valid = 0;
    for (let y = 0; y < 64; y++) {
      for (let x = 0; x < 64; x++) {
        const v = out[y * 64 + x];
        const inHole = x >= 26 && x < 38 && y >= 26 && y < 38; // pixels well inside the 4-cell hole
        if (inHole) assert.ok(nan(v), `${mode}: hole pixel (${x},${y}) must stay empty`);
        if (!nan(v)) {
          valid++;
          near(v, 40, 1e-4);
        }
      }
    }
    assert.ok(valid > 64 * 64 * 0.7);
  }
});

test('resample: bicubic stays inside the range of its neighbours (no overshoot at an edge)', () => {
  const g = grid([[0, 0, 100, 100], [0, 0, 100, 100], [0, 0, 100, 100], [0, 0, 100, 100]]);
  const out = resampleGrid(g, { d0: 0, d1: 4, h0: 0, h1: 4 }, 64, 64, 'bicubic');
  for (const v of out) if (!nan(v)) assert.ok(v >= 0 && v <= 100);
});

test('resample: outside the grid is empty, and a zoomed window samples the right part', () => {
  const g = grid([[1, 2, 3, 4]]);
  const out = resampleGrid(g, { d0: -1, d1: 5, h0: -1, h1: 2 }, 12, 6, 'nearest');
  assert.ok(nan(out[0]));
  const zoom = resampleGrid(g, { d0: 2, d1: 4, h0: 0, h1: 1 }, 4, 1, 'nearest');
  assert.deepEqual(Array.from(zoom), [3, 3, 4, 4]);
});

const cell = (y0, y1, v) => ({ y0, y1, yc: (y0 + y1) / 2, v });

test('rasterise: blocky fills each cell, interpolation blends touching beams only', () => {
  // one data column, two touching beams (centres 1 and 3) and a third beam far above (centre 9)
  const columns = [[cell(0, 2, 10), cell(2, 4, 30), cell(8.5, 9.5, 50)]];
  const full = { d0: 0, d1: 1, h0: 0, h1: 10 };
  const blocky = rasterizeSection(columns, [0.5], full, 1, 10, false);
  const smooth = rasterizeSection(columns, [0.5], full, 1, 10, true);
  const col = (g) => Array.from(g.data);
  // rows are 1 km tall, row 0 is the top (9-10 km)
  assert.equal(col(blocky)[9], 10); // 0-1 km
  assert.equal(col(blocky)[8], 10);
  assert.equal(col(blocky)[7], 30);
  assert.ok(nan(col(blocky)[5])); // the unfilled gap
  assert.equal(col(blocky)[0], 50);
  const s = col(smooth);
  near(s[9], 10); // the lowest beam, constant below its centre
  near(s[8], 15); // 1.5 km: a quarter of the way from the beam at 1 km (10) to the one at 3 km (30)
  near(s[7], 25); // 2.5 km: three quarters of the way
  near(s[6], 30); // above its beam centre the upper beam keeps its own value
  assert.ok(nan(s[5]) && nan(s[4]) && nan(s[2])); // no echo invented across the unfilled gap
  assert.equal(s[0], 50);
});

test('rasterise: data columns are chosen by distance and missing columns stay empty', () => {
  const columns = [[cell(0, 2, 5)], [], [cell(0, 2, 9)]];
  const g = rasterizeSection(columns, [0, 1, 2], { d0: -0.5, d1: 2.5, h0: 0, h1: 2 }, 3, 2, false);
  assert.deepEqual(Array.from(g.data.slice(0, 3)), [5, NaN, 9].map(Math.fround));
  // a finer grid repeats each column
  const fine = rasterizeSection(columns, [0, 1, 2], { d0: -0.5, d1: 2.5, h0: 0, h1: 2 }, 6, 2, false);
  assert.deepEqual(Array.from(fine.data.slice(0, 6)), [5, 5, NaN, NaN, 9, 9].map(Math.fround));
});

test('grid size follows the upsample factor within bounds', () => {
  const full = { d0: 0, d1: 150, h0: 0, h1: 20 };
  const a = gridSize(full, 600, 1);
  const b = gridSize(full, 600, 4);
  assert.equal(a.nx, 600);
  assert.equal(b.nx, 2400);
  assert.ok(b.ny >= a.ny);
  assert.ok(a.ny >= 20 / 0.04 - 1, 'rows are no taller than 40 m'); 
  assert.ok(gridSize(full, 5000, 4).nx <= 4000);
  assert.ok(gridSize(full, 600, 4).ny <= 1200);
});

test('pipeline: smoothing keeps the footprint of the rasterised section', () => {
  const columns = [[cell(0, 2, 10)], [cell(0, 2, 30)], [], [cell(0, 2, 50)], [cell(0, 2, 50)]];
  const dist = [0, 1, 2, 3, 4];
  const full = { d0: -0.5, d1: 4.5, h0: 0, h1: 4 };
  const base = buildSectionGrid(columns, dist, full, { interp: 'bilinear', sigma: 0, upsample: 2 });
  const smooth = buildSectionGrid(columns, dist, full, { interp: 'bilinear', sigma: 2, upsample: 2 });
  assert.equal(base.nx, 10);
  for (let k = 0; k < base.data.length; k++) assert.equal(nan(base.data[k]), nan(smooth.data[k]));
  for (const v of smooth.data) if (!nan(v)) assert.ok(v >= 10 && v <= 50);
});

test('PNG text chunk: placed after IHDR with a valid CRC, other bytes intact', () => {
  // smallest valid PNG: 1x1 grey pixel
  const png = Uint8Array.from(Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg==', 'base64'));
  const out = addPngText(png, 'Description', 'bilinear — σ 1 → 中');
  assert.equal(out.length, png.length + 12 + 'Description'.length + 1 + 'bilinear - sigma 1 -> ?'.length);
  // chunk right after IHDR (8 + 25 bytes)
  const dv = new DataView(out.buffer);
  const len = dv.getUint32(33);
  assert.equal(String.fromCharCode(...out.subarray(37, 41)), 'tEXt');
  const text = String.fromCharCode(...out.subarray(41, 41 + len));
  assert.equal(text, 'Description\0bilinear - sigma 1 -> ?');
  // the CRC checks out
  let c = 0xffffffff;
  for (const b of out.subarray(37, 41 + len)) {
    c ^= b;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  }
  assert.equal(dv.getUint32(41 + len), (c ^ 0xffffffff) >>> 0);
  // everything else is byte for byte what it was
  assert.deepEqual(Array.from(out.subarray(0, 33)), Array.from(png.subarray(0, 33)));
  assert.deepEqual(Array.from(out.subarray(33 + 12 + len)), Array.from(png.subarray(33)));
  // not a PNG: untouched
  const junk = Uint8Array.from([1, 2, 3]);
  assert.equal(addPngText(junk, 'a', 'b'), junk);
});
