/**
 * Real lightning mapping array (LMA) flashes on the movie: the sources at their measured position (x, y, height)
 * and time, mapped through the movie's timeline. This file holds the light that the sources give to the cloud
 * (a source flares when it appears, then fades over `glowS` clip seconds) and the flash rates. The lightning
 * itself is drawn as lines by lmaChannel.ts (no dots at the sources).
 *
 * Pure part: unit-tested in lmaFx.test.mjs.
 */
import type { LmaFlash } from './perilsData.ts';

export interface LmaParams {
  /** Afterglow, clip seconds (0.3 to 0.8). */
  glowS: number;
  /** Flare of a source that has just appeared, clip seconds. */
  flareS: number;
}

export const LMA_DEFAULTS: LmaParams = { glowS: 0.55, flareS: 0.06 };

export interface CompiledFlash {
  id: number;
  /** Clip time of the first and the last source. */
  tc0: number;
  tc1: number;
  td0: number;
  td1: number;
  n: number;
  /** Per source: clip time, data time, x, y, height (km), power (dBW). */
  tc: Float64Array;
  td: Float64Array;
  x: Float32Array;
  y: Float32Array;
  z: Float32Array;
  p: Float32Array;
}

/** Precompute each flash's sources in clip time (`toClip` = the timeline's data -> clip mapping). Sorted by first source. */
export function compileFlashes(flashes: LmaFlash[], toClip: (td: number) => number): CompiledFlash[] {
  const out: CompiledFlash[] = [];
  for (const f of flashes) {
    const rows = [...f.s].sort((a, b) => a[0] - b[0]);
    if (!rows.length) continue;
    const n = rows.length;
    const c: CompiledFlash = {
      id: f.id, tc0: 0, tc1: 0, td0: rows[0][0], td1: rows[n - 1][0], n,
      tc: new Float64Array(n), td: new Float64Array(n), x: new Float32Array(n), y: new Float32Array(n), z: new Float32Array(n), p: new Float32Array(n),
    };
    rows.forEach((r, i) => {
      c.td[i] = r[0];
      c.tc[i] = toClip(r[0]);
      c.x[i] = r[1];
      c.y[i] = r[2];
      c.z[i] = r[3];
      c.p[i] = r[4];
    });
    c.tc0 = c.tc[0];
    c.tc1 = c.tc[n - 1];
    out.push(c);
  }
  return out.sort((a, b) => a.tc0 - b.tc0);
}

/** A source as a light for the cloud (never drawn as a mark on the film). */
export interface SourceLight {
  flash: number;
  x: number;
  y: number;
  z: number;
  /** 0..1 */
  bright: number;
  /** Position in the flash (0 first source .. 1 last), by time. */
  phase: number;
  powerDbw: number;
}

export interface FlashFrame {
  sources: SourceLight[];
  /** Flashes with at least one source visible. */
  active: number;
}

/** Brightness of a source that appeared `age` clip seconds ago (0 before it appears). */
export function sourceBrightness(age: number, p: LmaParams = LMA_DEFAULTS): number {
  if (age < 0 || age >= p.glowS) return 0;
  const flare = Math.exp(-age / p.flareS);
  const glow = Math.pow(1 - age / p.glowS, 1.6);
  return Math.min(1, 0.8 * glow + flare);
}

/** Index of the first flash whose first source is at or after clip time `t` (flashes sorted by `tc0`). */
function firstAfter(fl: CompiledFlash[], t: number): number {
  let lo = 0;
  let hi = fl.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (fl[m].tc0 < t) lo = m + 1;
    else hi = m;
  }
  return lo;
}

/**
 * The sources that still give light at clip time `tc`. A flash can last longer than the afterglow, so
 * every flash that starts up to `maxDur + glowS` before `tc` is scanned (maxDur = the longest flash).
 */
export function flashFrame(fl: CompiledFlash[], tc: number, maxDur: number, p: LmaParams = LMA_DEFAULTS, minBright = 0.02): FlashFrame {
  const sources: SourceLight[] = [];
  let active = 0;
  const lo = firstAfter(fl, tc - maxDur - p.glowS);
  for (let k = lo; k < fl.length && fl[k].tc0 <= tc; k++) {
    const f = fl[k];
    if (f.tc1 + p.glowS < tc) continue;
    let any = false;
    const span = Math.max(1e-6, f.td1 - f.td0);
    for (let i = 0; i < f.n; i++) {
      const b = sourceBrightness(tc - f.tc[i], p);
      if (b >= minBright) {
        any = true;
        const phase = (f.td[i] - f.td0) / span;
        sources.push({ flash: f.id, x: f.x[i], y: f.y[i], z: f.z[i], bright: b, phase, powerDbw: f.p[i] });
      }
    }
    if (any) active++;
  }
  return { sources, active };
}

/** The longest flash, in clip seconds (to bound the scan of `flashFrame`). */
export function longestFlash(fl: CompiledFlash[]): number {
  let m = 0;
  for (const f of fl) m = Math.max(m, f.tc1 - f.tc0);
  return m;
}

/** Light points for the 3-D scene: the brightest flashes' centres (weighted by brightness) with a strength. z is multiplied by `vexag`. */
export function lightFrom(frame: FlashFrame, vexag: number, maxPoints = 4): { points: { x: number; y: number; z: number; i: number }[]; sky: number } {
  const by = new Map<number, { w: number; x: number; y: number; z: number; peak: number; n: number }>();
  for (const s of frame.sources) {
    const a = by.get(s.flash) ?? { w: 0, x: 0, y: 0, z: 0, peak: 0, n: 0 };
    a.w += s.bright;
    a.x += s.x * s.bright;
    a.y += s.y * s.bright;
    a.z += s.z * s.bright;
    a.peak = Math.max(a.peak, s.bright);
    a.n++;
    by.set(s.flash, a);
  }
  const arr = [...by.values()].map((a) => ({ x: a.x / a.w, y: a.y / a.w, z: (a.z / a.w) * vexag, i: Math.min(1, 0.3 + 0.1 * Math.sqrt(a.n)) * a.peak * Math.min(1, a.w / 3) }));
  arr.sort((a, b) => b.i - a.i);
  const points = arr.slice(0, maxPoints);
  const sky = Math.min(0.7, arr.reduce((t, a) => t + 0.12 * a.i, 0));
  return { points, sky };
}

/**
 * The scene light of the flashes averaged over the last `windowS` seconds of clip time (`n` samples): the light points of the samples
 * that are within `mergeKm` of each other are one point (position by intensity, intensities added and divided by `n`), the strongest
 * `maxPoints` are kept, the sky is the mean. Pure, deterministic; a flash that lasts less than the window is dimmer and longer.
 */
export function smoothedFlashLight(
  frameAt: (t: number) => FlashFrame, t: number, vexag: number, windowS = 0.25, n = 6, mergeKm = 3, maxPoints = 4,
): { points: { x: number; y: number; z: number; i: number }[]; sky: number } {
  const acc: { x: number; y: number; z: number; i: number }[] = [];
  let sky = 0;
  for (let k = 0; k < n; k++) {
    const l = lightFrom(frameAt(t - (k * windowS) / Math.max(1, n - 1)), vexag, 8);
    sky += l.sky / n;
    for (const p of l.points) {
      const w = p.i / n;
      const hit = acc.find((q) => Math.hypot(q.x - p.x, q.y - p.y) <= mergeKm);
      if (hit) {
        const tot = hit.i + w;
        hit.x = (hit.x * hit.i + p.x * w) / tot;
        hit.y = (hit.y * hit.i + p.y * w) / tot;
        hit.z = (hit.z * hit.i + p.z * w) / tot;
        hit.i = tot;
      } else acc.push({ x: p.x, y: p.y, z: p.z, i: w });
    }
  }
  acc.sort((a, b) => b.i - a.i);
  return { points: acc.slice(0, maxPoints), sky };
}

/** The sources of the flashes that lie in the data window [lo, hi] (data s); flashes without a source in it are dropped. */
export function flashesInWindowOf(flashes: LmaFlash[], lo: number, hi: number): LmaFlash[] {
  return flashes.map((f) => ({ ...f, s: f.s.filter((r) => r[0] >= lo - 1e-6 && r[0] <= hi + 1e-6) })).filter((f) => f.s.length);
}

// ─── rates ──────────────────────────────────────────────────────────────────

/** Flashes whose first source lies in (t - windowS, t] (data seconds): the flash rate per `windowS`. */
export function flashesInWindow(firstTimes: number[], t: number, windowS: number): number {
  let n = 0;
  for (const f of firstTimes) if (f > t - windowS && f <= t) n++;
  return n;
}

/** Histogram of flash start times: `bins` counts per `binS` seconds from `t0` to `t1`. */
export function rateSeries(firstTimes: number[], t0: number, t1: number, binS: number): number[] {
  const nb = Math.max(1, Math.ceil((t1 - t0) / binS));
  const out = new Array<number>(nb).fill(0);
  for (const f of firstTimes) {
    if (f < t0 || f > t1) continue;
    out[Math.min(nb - 1, Math.floor((f - t0) / binS))]++;
  }
  return out;
}
