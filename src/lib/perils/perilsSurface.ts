/**
 * Surface thermodynamics and rotation on the ground map of the PERiLS movie.
 *
 *  - The objective surface analysis (a Barnes analysis of the station observations at the step times,
 *    class ANALYSIS: an interpolation of observations, smooth, defined only where stations are near)
 *    gives temperature and friends on a 2 km grid. Temperature is drawn as a translucent ground layer
 *    (cool blue to warm orange) so that the cold pool and the warm sector are visible, and the cold-pool
 *    edge is the contour 2 K below the warm sector (the 90th percentile of the first analysis).
 *  - The MRMS azimuthal shear gives the rotation swath: the maximum so far, RETRIEVED (a product made
 *    from radar velocities), over the ground.
 *
 * Pure maths (decode, sampling, colour, contour level, swath): unit-tested in perilsSurface.test.mjs.
 */
import type { RGB } from './perilsStyle.ts';

export interface SurfaceMeta {
  bin: string;
  x0: number;
  y0: number;
  dx: number;
  nx: number;
  ny: number;
  fields: { name: string; min: number; max: number }[];
  nStations?: number | null;
}

/** Decode a surface file: one float array per field (NaN where the analysis is not defined). */
export function decodeSurface(meta: SurfaceMeta, bytes: Uint8Array): Record<string, Float32Array> {
  const n = meta.nx * meta.ny;
  const out: Record<string, Float32Array> = {};
  meta.fields.forEach((f, i) => {
    const a = new Float32Array(n);
    for (let k = 0; k < n; k++) {
      const c = bytes[i * n + k];
      a[k] = c === 0 ? NaN : f.min + ((c - 1) / 254) * (f.max - f.min);
    }
    out[f.name] = a;
  });
  return out;
}

/** Bilinear value of a gridded field at (x, y) km; NaN outside it or where a corner is undefined. */
export function sampleGrid(f: Float32Array, m: { x0: number; y0: number; dx: number; nx: number; ny: number }, x: number, y: number): number {
  const fx = (x - m.x0) / m.dx;
  const fy = (y - m.y0) / m.dx;
  if (fx < 0 || fy < 0 || fx > m.nx - 1 || fy > m.ny - 1) return NaN;
  const i = Math.min(m.nx - 2, Math.floor(fx));
  const j = Math.min(m.ny - 2, Math.floor(fy));
  const ax = fx - i;
  const ay = fy - j;
  const a = f[j * m.nx + i];
  const b = f[j * m.nx + i + 1];
  const c = f[(j + 1) * m.nx + i];
  const d = f[(j + 1) * m.nx + i + 1];
  return (a * (1 - ax) + b * ax) * (1 - ay) + (c * (1 - ax) + d * ax) * ay;
}

/** Cross-fade of two fields (either may be null): `s` = position in the interval; a missing one gives the other. */
export function blendFields(a: Float32Array | null, b: Float32Array | null, s: number): Float32Array | null {
  if (!a && !b) return null;
  if (!a) return b;
  if (!b) return a;
  const out = new Float32Array(a.length);
  for (let i = 0; i < a.length; i++) {
    const x = a[i];
    const y = b[i];
    out[i] = Number.isFinite(x) && Number.isFinite(y) ? x * (1 - s) + y * s : Number.isFinite(x) ? (s < 0.5 ? x : NaN) : Number.isFinite(y) ? (s >= 0.5 ? y : NaN) : NaN;
  }
  return out;
}

/** Colour of a temperature on the ground: blue-violet at `lo` through grey to warm orange at `hi`. */
export function tempColour(v: number, lo: number, hi: number): RGB {
  const f = Math.max(0, Math.min(1, (v - lo) / Math.max(1e-6, hi - lo)));
  const stops: [number, RGB][] = [[0, [70, 90, 255]], [0.45, [90, 190, 230]], [0.7, [235, 200, 120]], [1, [255, 120, 50]]];
  for (let i = 1; i < stops.length; i++) {
    if (f <= stops[i][0]) {
      const [a0, ca] = stops[i - 1];
      const [a1, cb] = stops[i];
      const t = (f - a0) / (a1 - a0);
      return [0, 1, 2].map((k) => Math.round(ca[k] + (cb[k] - ca[k]) * t)) as RGB;
    }
  }
  return stops[stops.length - 1][1];
}

/** The `q`-quantile (0..1) of the finite values of a field. */
export function quantile(f: ArrayLike<number>, q: number): number {
  const v: number[] = [];
  for (let i = 0; i < f.length; i++) if (Number.isFinite(f[i])) v.push(f[i]);
  if (!v.length) return NaN;
  v.sort((a, b) => a - b);
  return v[Math.min(v.length - 1, Math.max(0, Math.floor(q * (v.length - 1))))];
}

/**
 * The temperature of the cold-pool edge: `dropK` below the warm sector (the 90th percentile of `field`), or
 * NaN when the field has no cell that cold (then no cold pool is drawn: nothing is invented).
 */
export function coldPoolLevel(field: ArrayLike<number>, dropK = 2.0): number {
  const warm = quantile(field, 0.9);
  const cold = quantile(field, 0.02);
  if (!Number.isFinite(warm) || !Number.isFinite(cold) || cold > warm - dropK - 0.5) return NaN;
  return warm - dropK;
}

/** The running maximum of 8-bit swath layers up to and including step `k` (missing layers are skipped). */
export function swathUpTo(layers: (Uint8Array | null)[], k: number): Uint8Array | null {
  let out: Uint8Array | null = null;
  for (let i = 0; i <= Math.min(k, layers.length - 1); i++) {
    const a = layers[i];
    if (!a) continue;
    if (!out) out = new Uint8Array(a);
    else for (let j = 0; j < out.length; j++) if (a[j] > out[j]) out[j] = a[j];
  }
  return out;
}
