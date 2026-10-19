/**
 * Real lightning mapping array (LMA) flashes on the movie: every VHF source is drawn at its measured
 * position (x, y, height) and at its measured time, mapped through the movie's timeline. Nothing is
 * generated: no jitter, no invented branches, no random strokes. The channel is the polyline through
 * the flash's sources in time order, broken wherever two consecutive sources are further apart than
 * `maxLinkKm` or `maxLinkS` data seconds (an LMA locates sources, it does not see the channel between
 * them). A source flares when it appears, then fades over `glowS` clip seconds (the afterglow).
 *
 * Pure part (compile, sprites, light, rates): unit-tested in lmaFx.test.mjs. Drawing at the end.
 */
import type { LmaFlash } from './perilsData.ts';

export interface LmaParams {
  /** Afterglow, clip seconds (0.3 to 0.8). */
  glowS: number;
  /** Flare of a source that has just appeared, clip seconds. */
  flareS: number;
  maxLinkKm: number;
  maxLinkS: number;
  colourBy: 'time' | 'power';
}

export const LMA_DEFAULTS: LmaParams = { glowS: 0.55, flareS: 0.06, maxLinkKm: 3.5, maxLinkS: 0.4, colourBy: 'time' };

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

export interface Sprite {
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

export interface Link {
  ax: number;
  ay: number;
  az: number;
  bx: number;
  by: number;
  bz: number;
  bright: number;
  phase: number;
}

export interface FlashFrame {
  sprites: Sprite[];
  links: Link[];
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
 * Sprites and channel links visible at clip time `tc`. A flash can last longer than the afterglow, so
 * every flash that starts up to `maxDur + glowS` before `tc` is scanned (maxDur = the longest flash).
 */
export function flashFrame(fl: CompiledFlash[], tc: number, maxDur: number, p: LmaParams = LMA_DEFAULTS, minBright = 0.02): FlashFrame {
  const sprites: Sprite[] = [];
  const links: Link[] = [];
  let active = 0;
  const lo = firstAfter(fl, tc - maxDur - p.glowS);
  for (let k = lo; k < fl.length && fl[k].tc0 <= tc; k++) {
    const f = fl[k];
    if (f.tc1 + p.glowS < tc) continue;
    let any = false;
    const span = Math.max(1e-6, f.td1 - f.td0);
    let prevB = 0;
    for (let i = 0; i < f.n; i++) {
      const b = sourceBrightness(tc - f.tc[i], p);
      if (b >= minBright) {
        any = true;
        const phase = (f.td[i] - f.td0) / span;
        sprites.push({ flash: f.id, x: f.x[i], y: f.y[i], z: f.z[i], bright: b, phase, powerDbw: f.p[i] });
        if (i > 0 && prevB >= minBright) {
          const d = Math.hypot(f.x[i] - f.x[i - 1], f.y[i] - f.y[i - 1], f.z[i] - f.z[i - 1]);
          if (d <= p.maxLinkKm && f.td[i] - f.td[i - 1] <= p.maxLinkS) {
            links.push({ ax: f.x[i - 1], ay: f.y[i - 1], az: f.z[i - 1], bx: f.x[i], by: f.y[i], bz: f.z[i], bright: Math.min(b, prevB), phase });
          }
        }
      }
      prevB = b;
    }
    if (any) active++;
  }
  return { sprites, links, active };
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
  for (const s of frame.sprites) {
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

// ─── colour ─────────────────────────────────────────────────────────────────

/** [r, g, b] 0..255 of a source: by position in the flash (white-hot start, cooling to blue) or by power (dBW, violet to white). */
export function sourceColour(s: { phase: number; powerDbw: number }, by: 'time' | 'power'): [number, number, number] {
  if (by === 'power') {
    const f = Math.max(0, Math.min(1, (s.powerDbw + 10) / 40));
    return [Math.round(150 + 105 * f), Math.round(110 + 145 * f), Math.round(255 - 30 * f * f)];
  }
  const f = Math.max(0, Math.min(1, s.phase));
  return [Math.round(255 - 135 * f), Math.round(252 - 90 * f), 255];
}

// ─── drawing ────────────────────────────────────────────────────────────────

interface Cam {
  eye: [number, number, number];
  fwd: [number, number, number];
  right: [number, number, number];
  up: [number, number, number];
  tanX: number;
  tanY: number;
  w: number;
  h: number;
}

let scratch: HTMLCanvasElement | null = null;

function scratchCanvas(w: number, h: number): HTMLCanvasElement {
  if (!scratch) scratch = document.createElement('canvas');
  if (scratch.width !== w || scratch.height !== h) {
    scratch.width = w;
    scratch.height = h;
  }
  return scratch;
}

/** Draw a flash frame on `ctx` (additive blending, one blur pass for the bloom). Returns the number of sprites drawn. */
export function drawLma(
  ctx: CanvasRenderingContext2D,
  cam: Cam,
  frame: FlashFrame,
  project: (cam: Cam, p: [number, number, number]) => [number, number, number] | null,
  vexag: number,
  by: 'time' | 'power',
  gain = 1,
): number {
  if (!frame.sprites.length) return 0;
  const u = cam.h / 1080;
  const sc = scratchCanvas(cam.w, cam.h);
  const sx = sc.getContext('2d')!;
  sx.clearRect(0, 0, sc.width, sc.height);
  sx.lineCap = 'round';
  sx.lineJoin = 'round';
  let drawn = 0;
  const dist = (x: number, y: number, z: number) => Math.hypot(x - cam.eye[0], y - cam.eye[1], (z * vexag - cam.eye[2]) / Math.max(vexag, 1e-3));
  for (const l of frame.links) {
    const a = project(cam, [l.ax, l.ay, l.az * vexag]);
    const b = project(cam, [l.bx, l.by, l.bz * vexag]);
    if (!a || !b) continue;
    const d = Math.max(1.5, dist(l.bx, l.by, l.bz));
    const c = sourceColour({ phase: l.phase, powerDbw: 0 }, 'time');
    const w = Math.max(0.9, Math.min(3.2, 1.8 * Math.pow(15 / d, 0.4))) * u;
    sx.strokeStyle = `rgba(${c[0]},${c[1]},${c[2]},${Math.min(1, l.bright * gain).toFixed(3)})`;
    sx.lineWidth = w;
    sx.beginPath();
    sx.moveTo(a[0], a[1]);
    sx.lineTo(b[0], b[1]);
    sx.stroke();
  }
  for (const s of frame.sprites) {
    const p = project(cam, [s.x, s.y, s.z * vexag]);
    if (!p) continue;
    if (p[0] < -50 || p[0] > cam.w + 50 || p[1] < -50 || p[1] > cam.h + 50) continue;
    const d = Math.max(1.5, dist(s.x, s.y, s.z));
    const r = Math.max(1.4, Math.min(7, 2.6 * Math.pow(15 / d, 0.45))) * u * (0.6 + 0.8 * s.bright);
    const c = sourceColour(s, by);
    const a = Math.min(1, s.bright * gain);
    const g = sx.createRadialGradient(p[0], p[1], 0, p[0], p[1], r * 2.2);
    g.addColorStop(0, `rgba(255,255,255,${a.toFixed(3)})`);
    g.addColorStop(0.35, `rgba(${c[0]},${c[1]},${c[2]},${(a * 0.8).toFixed(3)})`);
    g.addColorStop(1, `rgba(${c[0]},${c[1]},${c[2]},0)`);
    sx.fillStyle = g;
    sx.beginPath();
    sx.arc(p[0], p[1], r * 2.2, 0, Math.PI * 2);
    sx.fill();
    drawn++;
  }
  if (!drawn && !frame.links.length) return 0;
  const peak = frame.sprites.reduce((m, s) => Math.max(m, s.bright), 0);
  ctx.save();
  ctx.globalCompositeOperation = 'lighter';
  // a thin bloom (at most): one wide soft pass, one tight pass, then the channel itself
  ctx.filter = `blur(${Math.round(18 * u)}px)`;
  ctx.globalAlpha = Math.min(1, 0.45 * peak);
  ctx.drawImage(sc, 0, 0);
  ctx.filter = `blur(${Math.round(5 * u)}px)`;
  ctx.globalAlpha = Math.min(1, 0.7 * peak);
  ctx.drawImage(sc, 0, 0);
  ctx.filter = 'none';
  ctx.globalAlpha = 1;
  ctx.drawImage(sc, 0, 0);
  ctx.restore();
  return drawn;
}
