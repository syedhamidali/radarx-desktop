/**
 * Smoothing for the cross-section: the section is rasterised onto a regular (distance, height)
 * grid, optionally smoothed with a NaN-aware Gaussian, and resampled to the screen with nearest /
 * bilinear / bicubic interpolation. Missing data is NaN throughout and stays missing: it never
 * pulls values towards zero and echo is never smeared over empty areas or unscanned sectors.
 *
 * Pure functions on typed arrays and no imports, so they run under `node --test`
 * (crossSectionRaster.test.mjs).
 */

export type Interp = 'nearest' | 'bilinear' | 'bicubic';

export interface Smoothing {
  /** How the grid is sampled for the screen. */
  interp: Interp;
  /** Gaussian strength in gate lengths (sample spacing along the line); 0 = off. */
  sigma: number;
  /** The grid is this many times finer than the radar gates (1 - 4). */
  upsample: number;
}

export const NO_SMOOTHING: Smoothing = { interp: 'nearest', sigma: 0, upsample: 1 };
export const MAX_SIGMA = 4;
export const MAX_UPSAMPLE = 4;

/** True when the section can be drawn as plain cells (nothing to smooth). */
export function isPlain(s: Smoothing): boolean {
  return s.interp === 'nearest' && !(s.sigma > 0) && !(s.upsample > 1);
}

/** Validate settings read from storage or a UI; anything unusable falls back to "off". */
export function normalizeSmoothing(x: unknown): Smoothing {
  const o = (x && typeof x === 'object' ? x : {}) as Record<string, unknown>;
  const interp: Interp = o.interp === 'bilinear' || o.interp === 'bicubic' ? o.interp : 'nearest';
  const sigmaRaw = Number(o.sigma);
  const sigma = Number.isFinite(sigmaRaw) ? Math.min(MAX_SIGMA, Math.max(0, Math.round(sigmaRaw * 2) / 2)) : 0;
  const upRaw = Number(o.upsample);
  const upsample = Number.isFinite(upRaw) ? Math.min(MAX_UPSAMPLE, Math.max(1, Math.round(upRaw))) : 1;
  return { interp, sigma, upsample };
}

const INTERP_NAME: Record<Interp, string> = {
  nearest: 'nearest (as measured)',
  bilinear: 'bilinear',
  bicubic: 'bicubic',
};

/** Caption text for the figure, or null when nothing is smoothed. */
export function smoothingLabel(s: Smoothing, gateKm?: number | null): string | null {
  if (isPlain(s)) return null;
  const parts = [`${INTERP_NAME[s.interp]} interpolation`];
  if (s.sigma > 0) {
    const km = gateKm && gateKm > 0 ? ` (${(s.sigma * gateKm).toFixed(2)} km)` : '';
    parts.push(`Gaussian σ ${s.sigma} gate${s.sigma === 1 ? '' : 's'}${km}`);
  }
  if (s.upsample > 1) parts.push(`${s.upsample}× finer grid`);
  return `Smoothed: ${parts.join(', ')}`;
}

// ---------------------------------------------------------------------------------------------
// Grid
// ---------------------------------------------------------------------------------------------

export interface Extent {
  d0: number;
  d1: number;
  h0: number;
  h1: number;
}

/** Samples are cell-centred: sample (i, j) covers [d0 + i dx, d0 + (i+1) dx] x [h1 - (j+1) dy, h1 - j dy]. */
export interface Grid extends Extent {
  nx: number;
  ny: number;
  data: Float32Array;
}

export const MAX_NX = 4000;
export const MAX_NY = 1200;
const MIN_DY_KM = 0.04;

/** Grid size for `columns` samples along the line, `upsample` times finer, with square-ish samples. */
export function gridSize(full: Extent, columns: number, upsample: number): { nx: number; ny: number } {
  const dSpan = Math.max(full.d1 - full.d0, 1e-9);
  const hSpan = Math.max(full.h1 - full.h0, 1e-9);
  const up = Math.max(1, Math.round(upsample));
  const nx = Math.min(MAX_NX, Math.max(2, Math.round(Math.max(columns, 2) * up)));
  // Rows are at most MIN_DY tall so the lower and upper edges of the echo are not visibly stepped.
  const dy = Math.min(dSpan / nx, MIN_DY_KM);
  const ny = Math.min(MAX_NY, Math.max(8, Math.round(hSpan / dy)));
  return { nx, ny };
}

/** A cell as the section geometry makes them (see crossSectionGeometry.buildCells). */
export interface RasterCell {
  y0: number;
  y1: number;
  yc: number;
  v: number;
}

/**
 * Rasterise a section onto a grid. `columns[c]` are the cells of data column c (any order),
 * `distance` the distance of each column. Each grid column takes the data column nearest to it.
 *
 * Without `interpolate` a cell fills its whole height (blocky, as measured). With it, rows between
 * two beam centres whose cells touch (the elevation gap was filled) are linearly interpolated in
 * height, and rows only reached by one beam keep its value: no echo is invented where the
 * neighbouring beam saw nothing, or across a gap that was not filled.
 */
export function rasterizeSection(
  columns: RasterCell[][],
  distance: number[],
  full: Extent,
  nx: number,
  ny: number,
  interpolate: boolean,
): Grid {
  const data = new Float32Array(nx * ny).fill(NaN);
  const dx = (full.d1 - full.d0) / nx;
  const dy = (full.h1 - full.h0) / ny;
  const nCol = distance.length;
  const profiles = new Map<number, Float32Array | null>();

  const profile = (c: number): Float32Array | null => {
    if (profiles.has(c)) return profiles.get(c)!;
    const cells = [...columns[c]].sort((a, b) => a.yc - b.yc || a.y0 - b.y0);
    let out: Float32Array | null = null;
    if (cells.length) {
      out = new Float32Array(ny).fill(NaN);
      const rowOf = (h: number) => (full.h1 - h) / dy - 0.5; // fractional row of height h
      for (const cell of cells) {
        const jTop = Math.max(0, Math.ceil(rowOf(cell.y1)));
        const jBot = Math.min(ny - 1, Math.floor(rowOf(cell.y0)));
        for (let j = jTop; j <= jBot; j++) out[j] = cell.v;
      }
      if (interpolate) {
        for (let k = 0; k + 1 < cells.length; k++) {
          const a = cells[k];
          const b = cells[k + 1];
          if (b.yc - a.yc < 1e-6) continue; // beams sharing a height: nothing to interpolate
          if (b.y0 - a.y1 > 1e-6) continue; // not touching: the gap was not filled
          const jTop = Math.max(0, Math.ceil(rowOf(b.yc)));
          const jBot = Math.min(ny - 1, Math.floor(rowOf(a.yc)));
          for (let j = jTop; j <= jBot; j++) {
            const h = full.h1 - (j + 0.5) * dy;
            const t = (h - a.yc) / (b.yc - a.yc);
            if (t >= 0 && t <= 1) out[j] = a.v + (b.v - a.v) * t;
          }
        }
      }
    }
    profiles.set(c, out);
    return out;
  };

  for (let i = 0; i < nx; i++) {
    const d = full.d0 + (i + 0.5) * dx;
    const c = nearestIndex(distance, d, nCol);
    if (c < 0) continue;
    // Outside the section's own extent (half a step beyond the end samples) there is no column.
    const step = nCol > 1 ? (distance[nCol - 1] - distance[0]) / (nCol - 1) : 1;
    if (d < distance[0] - step / 2 - 1e-9 || d > distance[nCol - 1] + step / 2 + 1e-9) continue;
    const p = profile(c);
    if (!p) continue;
    for (let j = 0; j < ny; j++) data[j * nx + i] = p[j];
  }
  return { nx, ny, data, ...full };
}

function nearestIndex(dist: number[], d: number, n: number): number {
  if (n === 0) return -1;
  let a = 0;
  let b = n - 1;
  while (b - a > 1) {
    const m = (a + b) >> 1;
    if (dist[m] <= d) a = m;
    else b = m;
  }
  return Math.abs(dist[a] - d) <= Math.abs(dist[b] - d) ? a : b;
}

// ---------------------------------------------------------------------------------------------
// NaN-aware Gaussian
// ---------------------------------------------------------------------------------------------

function kernel(sigma: number): Float32Array {
  const r = Math.max(1, Math.ceil(3 * sigma));
  const k = new Float32Array(2 * r + 1);
  let sum = 0;
  for (let i = -r; i <= r; i++) {
    const w = Math.exp(-(i * i) / (2 * sigma * sigma));
    k[i + r] = w;
    sum += w;
  }
  for (let i = 0; i < k.length; i++) k[i] /= sum;
  return k;
}

/** Widths (odd) of three successive box blurs that together approximate a Gaussian of `sigma`. */
function boxWidths(sigma: number, n = 3): number[] {
  const ideal = Math.sqrt((12 * sigma * sigma) / n + 1);
  let wl = Math.floor(ideal);
  if (wl % 2 === 0) wl--;
  const wu = wl + 2;
  const m = Math.round((12 * sigma * sigma - n * wl * wl - 4 * n * wl - 3 * n) / (-4 * wl - 4));
  return Array.from({ length: n }, (_, i) => (i < m ? wl : wu));
}

/** One running-sum box pass of radius `r` along every line (windows are cut at the ends). */
function boxPass(
  src: Float32Array,
  dst: Float32Array,
  len: number,
  lines: number,
  stride: number,
  lineStride: number,
  r: number,
): void {
  for (let l = 0; l < lines; l++) {
    const base = l * lineStride;
    let sum = 0;
    for (let q = 0; q <= Math.min(r, len - 1); q++) sum += src[base + q * stride];
    for (let p = 0; p < len; p++) {
      dst[base + p * stride] = sum;
      const add = p + r + 1;
      const sub = p - r;
      if (add < len) sum += src[base + add * stride];
      if (sub >= 0) sum -= src[base + sub * stride];
    }
  }
}

/**
 * Gaussian smoothing that ignores missing data (normalised convolution): every output is the
 * kernel-weighted mean of the *valid* samples around it, so echo edges and the borders of the grid
 * are not pulled towards zero. A sample that was missing stays missing, so the footprint of the
 * echo (and every empty area, sector gap or unscanned wedge) is unchanged.
 * `sigmaX` / `sigmaY` are in grid samples; 0 leaves that axis alone. A wide Gaussian
 * (`sigma > boxAbove`) is built from three box blurs, which costs the same at any width.
 */
export function gaussianSmoothNaN(
  src: Float32Array,
  nx: number,
  ny: number,
  sigmaX: number,
  sigmaY: number,
  boxAbove = 4,
): Float32Array {
  if (!(sigmaX > 0) && !(sigmaY > 0)) return Float32Array.from(src);
  const n = nx * ny;
  let num = new Float32Array(n);
  let den = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const v = src[i];
    if (v === v) {
      num[i] = v;
      den[i] = 1;
    }
  }
  const pass = (horizontal: boolean, sigma: number) => {
    if (!(sigma > 0)) return;
    if (sigma > boxAbove) {
      const lines = horizontal ? ny : nx;
      const stride = horizontal ? 1 : nx;
      const lineStride = horizontal ? nx : 1;
      const len = horizontal ? nx : ny;
      let tmpN = new Float32Array(n);
      let tmpD = new Float32Array(n);
      for (const w of boxWidths(sigma)) {
        const r = (w - 1) >> 1;
        boxPass(num, tmpN, len, lines, stride, lineStride, r);
        boxPass(den, tmpD, len, lines, stride, lineStride, r);
        [num, tmpN] = [tmpN, num];
        [den, tmpD] = [tmpD, den];
      }
      return;
    }
    const k = kernel(sigma);
    const r = (k.length - 1) >> 1;
    const outN = new Float32Array(n);
    const outD = new Float32Array(n);
    const len = horizontal ? nx : ny;
    const lines = horizontal ? ny : nx;
    const stride = horizontal ? 1 : nx;
    const lineStride = horizontal ? nx : 1;
    for (let l = 0; l < lines; l++) {
      const base = l * lineStride;
      for (let p = 0; p < len; p++) {
        let sn = 0;
        let sd = 0;
        const lo = Math.max(0, p - r);
        const hi = Math.min(len - 1, p + r);
        for (let q = lo; q <= hi; q++) {
          const w = k[q - p + r];
          const idx = base + q * stride;
          sn += w * num[idx];
          sd += w * den[idx];
        }
        outN[base + p * stride] = sn;
        outD[base + p * stride] = sd;
      }
    }
    num = outN;
    den = outD;
  };
  pass(true, sigmaX);
  pass(false, sigmaY);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = src[i] === src[i] && den[i] > 0 ? num[i] / den[i] : NaN;
  return out;
}

// ---------------------------------------------------------------------------------------------
// Resampling to the screen
// ---------------------------------------------------------------------------------------------

/** Catmull-Rom weights for the four taps around a position `t` (0..1) between taps 1 and 2. */
function cubicWeights(t: number, out: Float64Array, o: number): void {
  const t2 = t * t;
  const t3 = t2 * t;
  out[o] = -0.5 * t3 + t2 - 0.5 * t;
  out[o + 1] = 1.5 * t3 - 2.5 * t2 + 1;
  out[o + 2] = -1.5 * t3 + 2 * t2 + 0.5 * t;
  out[o + 3] = 0.5 * t3 - 0.5 * t2;
}

/**
 * Sample `grid` for a `outW` x `outH` picture of the window `view` (row 0 = top). Missing data
 * stays NaN: a pixel is only given a value where the grid sample nearest to it has one, and
 * interpolation uses just the valid samples around it (weights renormalised), so nothing fades
 * into the empty parts and nothing outside the data range is invented.
 */
export function resampleGrid(
  grid: Grid,
  view: Extent,
  outW: number,
  outH: number,
  interp: Interp,
): Float32Array {
  const out = new Float32Array(outW * outH).fill(NaN);
  const { nx, ny, data } = grid;
  const dx = (grid.d1 - grid.d0) / nx;
  const dy = (grid.h1 - grid.h0) / ny;

  // Per output column / row: nearest sample, and for the interpolators the taps and their weights.
  const taps = interp === 'bicubic' ? 4 : 2;
  const colNear = new Int32Array(outW);
  const colIdx = new Int32Array(outW * taps);
  const colW = new Float64Array(outW * taps);
  for (let x = 0; x < outW; x++) {
    const d = view.d0 + ((x + 0.5) / outW) * (view.d1 - view.d0);
    const g = (d - grid.d0) / dx - 0.5;
    colNear[x] = g < -0.5 || g >= nx - 0.5 ? -1 : Math.min(nx - 1, Math.max(0, Math.round(g)));
    fillTaps(g, nx, interp, colIdx, colW, x * taps);
  }
  const rowNear = new Int32Array(outH);
  const rowIdx = new Int32Array(outH * taps);
  const rowW = new Float64Array(outH * taps);
  for (let y = 0; y < outH; y++) {
    const h = view.h1 - ((y + 0.5) / outH) * (view.h1 - view.h0);
    const g = (grid.h1 - h) / dy - 0.5;
    rowNear[y] = g < -0.5 || g >= ny - 0.5 ? -1 : Math.min(ny - 1, Math.max(0, Math.round(g)));
    fillTaps(g, ny, interp, rowIdx, rowW, y * taps);
  }

  for (let y = 0; y < outH; y++) {
    const rn = rowNear[y];
    if (rn < 0) continue;
    for (let x = 0; x < outW; x++) {
      const cn = colNear[x];
      if (cn < 0) continue;
      const centre = data[rn * nx + cn];
      if (centre !== centre) continue; // missing stays missing
      if (interp === 'nearest') {
        out[y * outW + x] = centre;
        continue;
      }
      let sum = 0;
      let wsum = 0;
      let lo = Infinity;
      let hi = -Infinity;
      for (let a = 0; a < taps; a++) {
        const wy = rowW[y * taps + a];
        if (wy === 0) continue;
        const rowBase = rowIdx[y * taps + a] * nx;
        for (let b = 0; b < taps; b++) {
          const wx = colW[x * taps + b];
          if (wx === 0) continue;
          const v = data[rowBase + colIdx[x * taps + b]];
          if (v !== v) continue;
          const w = wx * wy;
          sum += w * v;
          wsum += w;
          if (v < lo) lo = v;
          if (v > hi) hi = v;
        }
      }
      if (!(Math.abs(wsum) > 1e-9)) {
        out[y * outW + x] = centre;
        continue;
      }
      const v = sum / wsum;
      // Catmull-Rom overshoots (and more so with taps missing): stay within the neighbours.
      out[y * outW + x] = v < lo ? lo : v > hi ? hi : v;
    }
  }
  return out;
}

function fillTaps(
  g: number,
  n: number,
  interp: Interp,
  idx: Int32Array,
  w: Float64Array,
  o: number,
): void {
  if (interp === 'nearest') return;
  const i0 = Math.floor(g);
  const t = g - i0;
  const clamp = (i: number) => Math.min(n - 1, Math.max(0, i));
  if (interp === 'bilinear') {
    idx[o] = clamp(i0);
    idx[o + 1] = clamp(i0 + 1);
    w[o] = 1 - t;
    w[o + 1] = t;
    return;
  }
  for (let k = 0; k < 4; k++) idx[o + k] = clamp(i0 - 1 + k);
  cubicWeights(t, w, o);
}

// ---------------------------------------------------------------------------------------------
// The whole pipeline
// ---------------------------------------------------------------------------------------------

/**
 * Grid for a section under the given smoothing: rasterised (vertical interpolation between
 * elevation angles when the interpolation is bilinear / bicubic) and Gaussian-smoothed.
 */
export function buildSectionGrid(
  columns: RasterCell[][],
  distance: number[],
  full: Extent,
  s: Smoothing,
): Grid {
  const { nx, ny } = gridSize(full, distance.length, s.upsample);
  const grid = rasterizeSection(columns, distance, full, nx, ny, s.interp !== 'nearest');
  if (s.sigma > 0) {
    const step = distance.length > 1 ? (distance[distance.length - 1] - distance[0]) / (distance.length - 1) : 0.25;
    const sigmaKm = s.sigma * step;
    const dx = (full.d1 - full.d0) / nx;
    const dy = (full.h1 - full.h0) / ny;
    grid.data = gaussianSmoothNaN(grid.data, nx, ny, sigmaKm / dx, sigmaKm / dy);
  }
  return grid;
}

// ---------------------------------------------------------------------------------------------
// PNG text metadata
// ---------------------------------------------------------------------------------------------

let crcTable: Uint32Array | null = null;
function crc32(bytes: Uint8Array): number {
  if (!crcTable) {
    crcTable = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      crcTable[n] = c >>> 0;
    }
  }
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) c = crcTable[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/**
 * Insert a tEXt chunk (Latin-1 keyword and text) right after IHDR of a PNG. A few common symbols
 * are spelled out (sigma, arrows, dashes); anything else outside Latin-1 becomes '?'. Returns the
 * input unchanged if it is not a PNG.
 */
export function addPngText(png: Uint8Array, keyword: string, text: string): Uint8Array {
  const sig = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
  if (png.length < 33 || sig.some((b, i) => png[i] !== b)) return png;
  const spelled = (s: string) =>
    s.replace(/\u03C3/g, 'sigma').replace(/\u2192/g, '->').replace(/[\u2013\u2014]/g, '-');
  const latin = (s: string) =>
    Uint8Array.from(spelled(s), (ch) => (ch.charCodeAt(0) < 256 ? ch.charCodeAt(0) : 63));
  const kw = latin(keyword.slice(0, 79));
  const body = latin(text.replace(/\0/g, ' '));
  const data = new Uint8Array(kw.length + 1 + body.length);
  data.set(kw, 0);
  data.set(body, kw.length + 1);
  const chunk = new Uint8Array(12 + data.length);
  const view = new DataView(chunk.buffer);
  view.setUint32(0, data.length);
  chunk.set([0x74, 0x45, 0x58, 0x74], 4); // "tEXt"
  chunk.set(data, 8);
  view.setUint32(8 + data.length, crc32(chunk.subarray(4, 8 + data.length)));
  const ihdrEnd = 8 + 12 + 13; // signature + length/type/CRC + 13 bytes of IHDR data
  const out = new Uint8Array(png.length + chunk.length);
  out.set(png.subarray(0, ihdrEnd), 0);
  out.set(chunk, ihdrEnd);
  out.set(png.subarray(ihdrEnd), ihdrEnd + chunk.length);
  return out;
}
