/**
 * 3D wind glyphs: arrow mesh and the instance buffer built from a multi-Doppler wind volume.
 *
 * Pure (no GL), unit-tested. The renderer (cartesianRenderer.ts) draws the mesh instanced: every
 * instance is 8 floats
 *
 *   [ px, py, pz, length,  dx, dy, dz,  value ]
 *
 * = centre of the arrow in scene km (z already exaggerated), its length in km, the unit direction
 * in scene space and a 0..1 value for the colour table.
 *
 * Direction: the physical vector (u, v, w) drawn in the vertically exaggerated scene, i.e.
 * (u, v, w * vexag) normalised, so an updraft tilts the arrow the way the stretched scene shows
 * the air actually moving. The length is proportional to the 3D speed.
 */

import type { WindVolume } from './cartesianVolume';
import { cellConfidence, hasRetrievedW } from './windFlow.ts';

// ─── Mesh ───────────────────────────────────────────────────────────────────

export interface ArrowMesh {
  /** Interleaved per vertex: x, cos, sin (local: x along the arrow, [-0.5, 0.5]), nx, ny, nz, radius multiplier. */
  vertices: Float32Array;
  indices: Uint16Array;
  floatsPerVertex: number;
}

/** Fraction of the length taken by the head, and radius multipliers (relative to the shaft). */
export const HEAD_FRACTION = 0.36;
export const HEAD_RADIUS_MUL = 2.6;

/**
 * Closed arrow: a prism for the shaft (with a back cap) and a cone for the head (with its base).
 * Outward triangles are counter-clockwise, so the same mesh serves the fill (cull back faces) and
 * the outline (inflated, cull front faces = "inverted hull").
 */
export function buildArrowMesh(sides = 6): ArrowMesh {
  const v: number[] = [];
  const idx: number[] = [];
  const push = (x: number, c: number, s: number, nx: number, ny: number, nz: number, r: number) => {
    v.push(x, c, s, nx, ny, nz, r);
    return v.length / 7 - 1;
  };
  const ang = (j: number) => (j / sides) * Math.PI * 2;
  const xA = -0.5;
  const xB = 0.5 - HEAD_FRACTION;
  const xT = 0.5;

  // shaft sides (smooth radial normals)
  const sa: number[] = [];
  const sb: number[] = [];
  for (let j = 0; j < sides; j++) {
    const c = Math.cos(ang(j));
    const s = Math.sin(ang(j));
    sa.push(push(xA, c, s, 0, c, s, 1));
    sb.push(push(xB, c, s, 0, c, s, 1));
  }
  for (let j = 0; j < sides; j++) {
    const k = (j + 1) % sides;
    idx.push(sa[j], sa[k], sb[k], sa[j], sb[k], sb[j]);
  }
  // tail cap
  const tc = push(xA, 0, 0, -1, 0, 0, 0);
  const tr: number[] = [];
  for (let j = 0; j < sides; j++) tr.push(push(xA, Math.cos(ang(j)), Math.sin(ang(j)), -1, 0, 0, 1));
  for (let j = 0; j < sides; j++) idx.push(tc, tr[(j + 1) % sides], tr[j]);
  // head base (annulus facing backwards)
  const pb: number[] = [];
  const qb: number[] = [];
  for (let j = 0; j < sides; j++) {
    const c = Math.cos(ang(j));
    const s = Math.sin(ang(j));
    pb.push(push(xB, c, s, -1, 0, 0, 1));
    qb.push(push(xB, c, s, -1, 0, 0, HEAD_RADIUS_MUL));
  }
  for (let j = 0; j < sides; j++) {
    const k = (j + 1) % sides;
    idx.push(pb[j], qb[k], qb[j], pb[j], pb[k], qb[k]);
  }
  // cone
  const ring: number[] = [];
  const tips: number[] = [];
  for (let j = 0; j < sides; j++) {
    const c = Math.cos(ang(j));
    const s = Math.sin(ang(j));
    ring.push(push(xB, c, s, 0.45, c * 0.89, s * 0.89, HEAD_RADIUS_MUL));
    const am = ang(j) + Math.PI / sides;
    tips.push(push(xT, Math.cos(am), Math.sin(am), 0.8, Math.cos(am) * 0.6, Math.sin(am) * 0.6, 0));
  }
  for (let j = 0; j < sides; j++) {
    const k = (j + 1) % sides;
    idx.push(ring[j], ring[k], tips[j]);
  }
  return { vertices: new Float32Array(v), indices: new Uint16Array(idx), floatsPerVertex: 7 };
}

// ─── Instances ──────────────────────────────────────────────────────────────

export type GlyphColorBy = 'none' | 'speed' | 'w';

export interface GlyphOptions {
  /** Level range to draw (km ASL); levels outside are skipped. */
  zMinKm: number;
  zMaxKm: number;
  /** Take every n-th cell horizontally / every n-th level. */
  strideH: number;
  strideV: number;
  /** Hide cells whose beam-crossing angle is below this (degrees). */
  minCrossing: number;
  /** Hide cells whose wind error is above this (m/s); null = no limit. */
  maxUncertainty: number | null;
  /** Hide arrows slower than this (m/s). */
  minSpeed: number;
  /** User multiplier of the automatic arrow length. */
  lengthScale: number;
  vexag: number;
  colorBy: GlyphColorBy;
  /** Frame translation (km) from the wind grid's frame into the scene frame. */
  offsetX: number;
  offsetY: number;
  /** Upper bound of the number of arrows (strides grow until it fits). */
  maxGlyphs: number;
}

export const DEFAULT_GLYPH_OPTIONS: GlyphOptions = {
  zMinKm: 0,
  zMaxKm: 100,
  strideH: 3,
  strideV: 2,
  minCrossing: 30,
  maxUncertainty: null,
  minSpeed: 0,
  lengthScale: 1,
  vexag: 6,
  colorBy: 'none',
  offsetX: 0,
  offsetY: 0,
  maxGlyphs: 80000,
};

export interface GlyphSet {
  /** 8 floats per arrow. */
  data: Float32Array;
  count: number;
  /** Strides actually used (grown if the cap was hit). */
  strideH: number;
  strideV: number;
  levels: number[];
  /** km per m/s used for the lengths (x lengthScale already included). */
  kmPerMs: number;
  /** Speed (m/s) that the colour table spans: [0, speedMax] (speed) or [-speedMax, speedMax] (w). */
  colorMax: number;
}

/** q-quantile of the finite positive values of `a` (sampled when large). */
export function percentile(a: ArrayLike<number>, q: number): number {
  const step = Math.max(1, Math.floor(a.length / 200000));
  const vals: number[] = [];
  for (let i = 0; i < a.length; i += step) {
    const x = a[i];
    if (Number.isFinite(x)) vals.push(x);
  }
  if (vals.length === 0) return 0;
  vals.sort((p, r) => p - r);
  return vals[Math.min(vals.length - 1, Math.max(0, Math.floor(q * (vals.length - 1))))];
}

/** Statistics used to scale arrows and colours; computed once per wind volume. */
export interface WindStats {
  speedP95: number;
  speedP98: number;
  absWP98: number;
}

export function windStats(w: WindVolume): WindStats {
  const sp = w.fields.speed ?? hypotField(w.fields.u, w.fields.v);
  const aw = w.fields.w ? Float32Array.from(w.fields.w, Math.abs) : new Float32Array(0);
  return {
    speedP95: Math.max(1, percentile(sp, 0.95)),
    speedP98: Math.max(1, percentile(sp, 0.98)),
    absWP98: Math.max(1, percentile(aw, 0.98)),
  };
}

function hypotField(u: Float32Array, v: Float32Array): Float32Array {
  const out = new Float32Array(u.length);
  for (let i = 0; i < u.length; i++) out[i] = Math.hypot(u[i], v[i]);
  return out;
}

/** Indices of the levels to draw. */
export function selectLevels(z: ArrayLike<number>, zMinKm: number, zMaxKm: number, stride: number): number[] {
  const inRange: number[] = [];
  for (let k = 0; k < z.length; k++) if (z[k] >= zMinKm - 1e-9 && z[k] <= zMaxKm + 1e-9) inRange.push(k);
  const s = Math.max(1, Math.round(stride));
  const out: number[] = [];
  for (let i = Math.floor(s / 2) % Math.max(1, inRange.length); i < inRange.length; i += s) out.push(inRange[i]);
  if (out.length === 0 && inRange.length) out.push(inRange[0]);
  return out;
}

/**
 * Whether the cell at flat index `q` gets an arrow under `o`: the same mask as the flow animation
 * (finite u / v (/ w), at least two radars, crossing angle, uncertainty), plus a minimum speed.
 */
function cellVisible(w: WindVolume, o: GlyphOptions, q: number, wRet: boolean): boolean {
  if (!(cellConfidence(w, { minCrossing: o.minCrossing, maxUncertainty: o.maxUncertainty, minRadars: 2 }, q, wRet) > 0)) return false;
  const ww = w.fields.w;
  const wv = wRet && ww ? ww[q] : 0;
  return Math.sqrt(w.fields.u[q] ** 2 + w.fields.v[q] ** 2 + wv * wv) >= Math.max(o.minSpeed, 1e-3);
}

function countGlyphs(w: WindVolume, o: GlyphOptions, levels: number[], sh: number, wRet: boolean): number {
  const nxy = w.nx * w.ny;
  const half = Math.floor(sh / 2);
  let n = 0;
  for (const k of levels) {
    for (let j = half; j < w.ny; j += sh) {
      for (let i = half; i < w.nx; i += sh) if (cellVisible(w, o, k * nxy + j * w.nx + i, wRet)) n++;
    }
  }
  return n;
}

export function buildGlyphs(w: WindVolume, o: GlyphOptions, stats: WindStats = windStats(w)): GlyphSet {
  const u = w.fields.u;
  const v = w.fields.v;
  const ww = w.fields.w;
  const nxy = w.nx * w.ny;
  const sv = Math.max(1, Math.round(o.strideV));
  let sh = Math.max(1, Math.round(o.strideH));
  const levels = selectLevels(w.z, o.zMinKm, o.zMaxKm, sv);
  // grow the horizontal stride until the arrows that would really be drawn fit the cap
  const wRet = hasRetrievedW(w);
  let total = countGlyphs(w, o, levels, sh, wRet);
  while (total > o.maxGlyphs && sh < 64) total = countGlyphs(w, o, levels, ++sh, wRet);
  const spacing = sh * Math.max(w.dx, w.dy);
  const kmPerMs = (0.9 * spacing * Math.max(0.05, o.lengthScale)) / stats.speedP95;
  const colorMax = o.colorBy === 'w' ? stats.absWP98 : stats.speedP98;
  const data = new Float32Array(total * 8);
  let n = 0;
  const half = Math.floor(sh / 2);
  for (const k of levels) {
    const zs = w.z[k] * o.vexag;
    const base = k * nxy;
    for (let j = half; j < w.ny; j += sh) {
      for (let i = half; i < w.nx; i += sh) {
        const q = base + j * w.nx + i;
        if (!cellVisible(w, o, q, wRet)) continue;
        const uu = u[q];
        const vv = v[q];
        const wv = wRet && ww ? ww[q] : 0;
        const spd3 = Math.sqrt(uu * uu + vv * vv + wv * wv);
        const dx = uu;
        const dy = vv;
        const dz = wv * o.vexag;
        const dl = Math.sqrt(dx * dx + dy * dy + dz * dz);
        let val = 0;
        if (o.colorBy === 'speed') val = Math.min(1, Math.sqrt(uu * uu + vv * vv) / colorMax);
        else if (o.colorBy === 'w') val = Math.max(0, Math.min(1, 0.5 + wv / (2 * colorMax)));
        const o8 = n * 8;
        data[o8] = w.x0 + i * w.dx + o.offsetX;
        data[o8 + 1] = w.y0 + j * w.dy + o.offsetY;
        data[o8 + 2] = zs;
        data[o8 + 3] = spd3 * kmPerMs;
        data[o8 + 4] = dx / dl;
        data[o8 + 5] = dy / dl;
        data[o8 + 6] = dz / dl;
        data[o8 + 7] = val;
        n++;
      }
    }
  }
  return { data: data.subarray(0, n * 8), count: n, strideH: sh, strideV: sv, levels, kmPerMs, colorMax };
}

/**
 * Default strides: about `target` arrows per level where there is wind (a retrieval often covers
 * only a few percent of the grid, so the density follows the valid cells, not the grid size), and
 * about six levels.
 */
export function autoStrides(w: WindVolume, target = 450): { strideH: number; strideV: number } {
  const k = Math.min(w.nz - 1, Math.floor(w.nz / 2));
  const nxy = w.nx * w.ny;
  let valid = 0;
  for (let q = 0; q < nxy; q++) {
    if (Number.isFinite(w.fields.u[k * nxy + q]) && Number.isFinite(w.fields.v[k * nxy + q])) valid++;
  }
  const strideH = Math.max(1, Math.min(12, Math.ceil(Math.sqrt(valid / Math.max(1, target)))));
  return { strideH, strideV: Math.max(1, Math.round(w.nz / 6)) };
}

// ─── Colours ────────────────────────────────────────────────────────────────

/** Arrow colours: white fill / dark outline on a dark scene, the opposite on a light scene. */
export interface ArrowPalette {
  fill: [number, number, number];
  outline: [number, number, number];
}

export function arrowPalette(lightScene: boolean): ArrowPalette {
  return lightScene
    ? { fill: [0.07, 0.08, 0.1], outline: [0.97, 0.97, 0.98] }
    : { fill: [0.98, 0.98, 0.99], outline: [0.04, 0.05, 0.07] };
}

/** Parse the CSS "rgb(r,g,b)" strings of wind.ts' scaleLut into a 256x4 RGBA table. */
export function lutFromCss(css: string[]): Uint8Array {
  const out = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    const m = /(\d+)\D+(\d+)\D+(\d+)/.exec(css[Math.min(css.length - 1, i)] ?? '');
    out[i * 4] = m ? +m[1] : 200;
    out[i * 4 + 1] = m ? +m[2] : 200;
    out[i * 4 + 2] = m ? +m[3] : 200;
    out[i * 4 + 3] = 255;
  }
  return out;
}

/**
 * Colour table of the flow trails, 256 RGBA entries over the speed (or w) range. Thin translucent
 * trails need light colours on a dark scene (they are blended additively, so they glow), and
 * saturated mid-dark ones on a light scene (alpha blended). Light ramps in the manner of windy /
 * zoom.earth / earth.nullschool: calm air is pale blue, strong wind warm pink.
 */
export function flowRamp(kind: 'speed' | 'w', lightScene: boolean): Uint8Array {
  const stops: [number, [number, number, number]][] =
    kind === 'speed'
      ? lightScene
        ? [[0, [35, 75, 205]], [0.25, [0, 135, 215]], [0.5, [0, 160, 130]], [0.75, [235, 115, 25]], [1, [210, 25, 90]]]
        : [[0, [165, 210, 255]], [0.2, [110, 235, 235]], [0.4, [175, 250, 170]], [0.62, [255, 245, 130]], [0.82, [255, 190, 110]], [1, [255, 135, 155]]]
      : lightScene
        ? [[0, [30, 80, 200]], [0.5, [80, 90, 110]], [1, [200, 40, 40]]]
        : [[0, [120, 185, 255]], [0.5, [235, 240, 255]], [1, [255, 145, 125]]];
  const out = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let k = 0;
    while (k < stops.length - 2 && t > stops[k + 1][0]) k++;
    const [t0, c0] = stops[k];
    const [t1, c1] = stops[k + 1];
    const f = Math.max(0, Math.min(1, (t - t0) / (t1 - t0)));
    for (let c = 0; c < 3; c++) out[i * 4 + c] = Math.round(c0[c] + (c1[c] - c0[c]) * f);
    out[i * 4 + 3] = 255;
  }
  return out;
}
