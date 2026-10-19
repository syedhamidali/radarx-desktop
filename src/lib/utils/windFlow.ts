/**
 * Wind flow: the pure (CPU) side of the animated 3D streamlines of a multi-Doppler analysis.
 *
 * The GPU layer (flowRenderer.ts) advects particles with the same rules as the functions here,
 * which are the reference that the tests pin down. The flow is a faithful picture of the
 * retrieved field and nothing else:
 *
 *  - a cell has wind only where u and v are finite (and w, when the analysis retrieved w), at least
 *    two radars saw it, its beam-crossing angle is at least the minimum and its uncertainty is at
 *    most the limit; the same mask is used for the arrows;
 *  - the field is sampled by trilinear interpolation; if ANY of the 8 surrounding cells has no wind
 *    the sample is invalid. Nothing is extrapolated, filled or held, and no noise, turbulence or
 *    smoothing is added;
 *  - particles move in physical space (km, m/s, using u, v and w) with a midpoint (RK2) integrator,
 *    time step in seconds times ONE explicit "visual speed" factor; a particle that reaches an
 *    invalid sample stops, fades out and is respawned inside a valid cell;
 *  - the vertical exaggeration is a display transform only (applied when drawing).
 *
 * Units: positions in km (x east, y north, z above sea level), winds in m/s, times in seconds.
 */

import type { WindVolume } from './cartesianVolume';

// ─── Masks and the flow field ───────────────────────────────────────────────

export interface MaskOptions {
  /** Cells whose beam-crossing angle is below this (degrees) have no wind. */
  minCrossing: number;
  /** Cells whose wind error is above this (m/s) have no wind; null = no limit. */
  maxUncertainty: number | null;
  /** Cells seen by fewer radars have no wind. */
  minRadars: number;
}

export const DEFAULT_MASK: MaskOptions = { minCrossing: 30, maxUncertainty: null, minRadars: 2 };

/** Crossing angle (degrees) over which the confidence rises from its floor to 1. */
const CONF_CROSSING_SPAN = 30;
/** Uncertainty (m/s) at which the confidence reaches its floor when there is no explicit limit. */
const CONF_UNCERTAINTY_REF = 10;
const CONF_FLOOR = 0.35;

/** Whether any cell of the analysis carries a retrieved w. */
export function hasRetrievedW(w: WindVolume): boolean {
  const ww = w.fields.w;
  if (!ww) return false;
  for (let i = 0; i < ww.length; i++) if (Number.isFinite(ww[i])) return true;
  return false;
}

/**
 * Confidence (0..1) of the wind at flat cell index `q`; 0 = no wind there. `wStrict`: w must be
 * finite too (an analysis that retrieved w); otherwise the flow is horizontal only.
 */
export function cellConfidence(w: WindVolume, o: MaskOptions, q: number, wStrict: boolean): number {
  const f = w.fields;
  if (!Number.isFinite(f.u[q]) || !Number.isFinite(f.v[q])) return 0;
  if (wStrict && !(f.w && Number.isFinite(f.w[q]))) return 0;
  if (f.n_radars && !(f.n_radars[q] >= o.minRadars)) return 0;
  let conf = 1;
  const cr = f.crossing_angle;
  if (cr) {
    const c = cr[q];
    if (o.minCrossing > 0 && !(c >= o.minCrossing)) return 0;
    if (Number.isFinite(c)) {
      const t = Math.max(0, Math.min(1, (c - o.minCrossing) / CONF_CROSSING_SPAN));
      conf *= CONF_FLOOR + (1 - CONF_FLOOR) * t;
    }
  }
  const un = f.uncertainty;
  if (un && Number.isFinite(un[q])) {
    if (o.maxUncertainty !== null && !(un[q] <= o.maxUncertainty)) return 0;
    const ref = o.maxUncertainty ?? CONF_UNCERTAINTY_REF;
    conf *= 1 - (1 - CONF_FLOOR) * Math.max(0, Math.min(1, un[q] / ref));
  }
  return conf;
}

/** The wind on its grid, ready for sampling and for upload as an RGBA32F 3D texture. */
export interface FlowField {
  nx: number;
  ny: number;
  nz: number;
  /** Position of cell (0, 0, 0) and the cell size, km. */
  x0: number;
  y0: number;
  z0: number;
  dx: number;
  dy: number;
  dz: number;
  /** 4 floats per cell, x fastest: u, v, w (m/s; 0 where unknown) and the confidence (0 = no wind). */
  data: Float32Array;
  validCells: number;
  totalCells: number;
  /** w was retrieved: the flow is 3D. Otherwise it is horizontal and w plays no role. */
  wRetrieved: boolean;
}

/** Whether the levels are evenly spaced (the sampler assumes it). */
export function levelsUniform(z: ArrayLike<number>): boolean {
  if (z.length < 2) return false;
  const d = (z[z.length - 1] - z[0]) / (z.length - 1);
  if (!(d > 0)) return false;
  for (let k = 1; k < z.length; k++) if (Math.abs(z[k] - z[k - 1] - d) > 1e-3 * d) return false;
  return true;
}

export function buildFlowField(w: WindVolume, o: MaskOptions): FlowField {
  const n = w.nx * w.ny * w.nz;
  const wRet = hasRetrievedW(w);
  const data = new Float32Array(n * 4);
  let valid = 0;
  for (let q = 0; q < n; q++) {
    const c = cellConfidence(w, o, q, wRet);
    if (c <= 0) continue;
    data[q * 4] = w.fields.u[q];
    data[q * 4 + 1] = w.fields.v[q];
    data[q * 4 + 2] = wRet ? w.fields.w[q] : 0;
    data[q * 4 + 3] = c;
    valid++;
  }
  const dz = w.nz > 1 ? (w.z[w.nz - 1] - w.z[0]) / (w.nz - 1) : 1;
  return {
    nx: w.nx, ny: w.ny, nz: w.nz,
    x0: w.x0, y0: w.y0, z0: w.z[0], dx: w.dx, dy: w.dy, dz,
    data, validCells: valid, totalCells: n, wRetrieved: wRet,
  };
}

// ─── Sampling and integration (reference of the GPU code) ───────────────────

export type Vec3 = [number, number, number];
/** u, v, w (m/s) and the interpolated confidence. */
export type FlowSample = [number, number, number, number];

/**
 * Trilinear sample at (x, y, z) km. Null when the point is outside the grid or when ANY of the 8
 * surrounding cells has no wind (never extrapolates, never fills).
 */
export function sampleFlow(f: FlowField, x: number, y: number, z: number): FlowSample | null {
  const fx = (x - f.x0) / f.dx;
  const fy = (y - f.y0) / f.dy;
  const fz = (z - f.z0) / f.dz;
  if (!(fx >= 0 && fy >= 0 && fz >= 0 && fx <= f.nx - 1 && fy <= f.ny - 1 && fz <= f.nz - 1)) return null;
  const i0 = Math.min(Math.floor(fx), f.nx - 2);
  const j0 = Math.min(Math.floor(fy), f.ny - 2);
  const k0 = Math.min(Math.floor(fz), f.nz - 2);
  const tx = fx - i0;
  const ty = fy - j0;
  const tz = fz - k0;
  let u = 0, v = 0, w = 0, c = 0;
  for (let corner = 0; corner < 8; corner++) {
    const ox = corner & 1, oy = (corner >> 1) & 1, oz = (corner >> 2) & 1;
    const q = ((k0 + oz) * f.ny + (j0 + oy)) * f.nx + (i0 + ox);
    const conf = f.data[q * 4 + 3];
    if (!(conf > 0)) return null;
    const wt = (ox ? tx : 1 - tx) * (oy ? ty : 1 - ty) * (oz ? tz : 1 - tz);
    u += wt * f.data[q * 4];
    v += wt * f.data[q * 4 + 1];
    w += wt * f.data[q * 4 + 2];
    c += wt * conf;
  }
  return [u, v, w, c];
}

/** Smallest cell size in metres (limits the integration sub-step). */
export function minCellMetres(f: FlowField): number {
  return 1000 * Math.min(f.dx, f.dy, f.dz);
}

/** Number of sub-steps for a step of `dt` s at speed `spd` m/s (at most `cap`). */
export function subSteps(spd: number, dt: number, cellM: number, frac: number, cap: number): number {
  return Math.max(1, Math.min(cap, Math.ceil((spd * dt) / (frac * cellM))));
}

/**
 * One midpoint (RK2) step of `dt` seconds, in physical space (km; winds in m/s). Null if the wind
 * at the start, at the midpoint or at the end of the step is invalid.
 */
export function rk2Step(f: FlowField, p: Vec3, dt: number): Vec3 | null {
  const a = sampleFlow(f, p[0], p[1], p[2]);
  if (!a) return null;
  const k = dt / 1000;
  const b = sampleFlow(f, p[0] + 0.5 * k * a[0], p[1] + 0.5 * k * a[1], p[2] + 0.5 * k * a[2]);
  if (!b) return null;
  const n: Vec3 = [p[0] + k * b[0], p[1] + k * b[1], p[2] + k * b[2]];
  return sampleFlow(f, n[0], n[1], n[2]) ? n : null;
}

export interface AdvectResult {
  pos: Vec3;
  /** false: the particle reached a sample without wind and stopped at `pos`. */
  ok: boolean;
}

/** Advect `p` for `dt` seconds (sign = direction) with sub-steps of at most half a cell. */
export function advect(f: FlowField, p: Vec3, dt: number): AdvectResult {
  const s0 = sampleFlow(f, p[0], p[1], p[2]);
  if (!s0) return { pos: p, ok: false };
  const spd = Math.hypot(s0[0], s0[1], s0[2]);
  const n = subSteps(spd, Math.abs(dt), minCellMetres(f), 0.5, 8);
  const h = dt / n;
  let q = p;
  for (let i = 0; i < n; i++) {
    const nx = rk2Step(f, q, h);
    if (!nx) return { pos: q, ok: false };
    q = nx;
  }
  return { pos: q, ok: true };
}

/**
 * The points of a trail: the head and `nPts - 1` points reached by integrating the field BACKWARDS
 * in steps of `h` seconds (a piece of the streamline through the head). Each point carries the
 * confidence there; once the path leaves the wind the remaining points have confidence 0 and
 * stay at the last valid position, so the trail is cut instead of extended.
 */
export function trailPoints(f: FlowField, head: Vec3, nPts: number, h: number): { p: Vec3; conf: number }[] {
  const s0 = sampleFlow(f, head[0], head[1], head[2]);
  const out: { p: Vec3; conf: number }[] = [{ p: head, conf: s0 ? s0[3] : 0 }];
  let q = head;
  let ok = !!s0;
  for (let k = 1; k < nPts; k++) {
    if (ok) {
      const s = sampleFlow(f, q[0], q[1], q[2]);
      const spd = s ? Math.hypot(s[0], s[1], s[2]) : 0;
      const ns = subSteps(spd, h, minCellMetres(f), 0.75, 3);
      let c: Vec3 | null = q;
      for (let i = 0; i < ns && c; i++) c = rk2Step(f, c, -h / ns);
      if (c) q = c;
      else ok = false;
    }
    const s = ok ? sampleFlow(f, q[0], q[1], q[2]) : null;
    out.push({ p: q, conf: s ? s[3] : 0 });
    if (!s) ok = false;
  }
  return out;
}

// ─── Seeding and lifetime ───────────────────────────────────────────────────

/** Deterministic PRNG (mulberry32): one float in [0, 1) per call. */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Cells a particle may be born in: all 8 corners have wind (so every point of the cell is a valid
 * sample) and the cell centre lies in [zLoKm, zHiKm]. Index = i + (nx - 1) * (j + (ny - 1) * k).
 */
export function seedCells(f: FlowField, zLoKm: number, zHiKm: number): Uint32Array {
  const out: number[] = [];
  const cx = f.nx - 1;
  const cy = f.ny - 1;
  for (let k = 0; k < f.nz - 1; k++) {
    const zc = f.z0 + (k + 0.5) * f.dz;
    if (zc < zLoKm || zc > zHiKm) continue;
    for (let j = 0; j < cy; j++) {
      for (let i = 0; i < cx; i++) {
        let all = true;
        for (let c = 0; c < 8 && all; c++) {
          const q = ((k + ((c >> 2) & 1)) * f.ny + (j + ((c >> 1) & 1))) * f.nx + (i + (c & 1));
          if (!(f.data[q * 4 + 3] > 0)) all = false;
        }
        if (all) out.push(i + cx * (j + cy * k));
      }
    }
  }
  return Uint32Array.from(out);
}

/** A position inside seed cell `idx`; r1..r3 in [0, 1). */
export function positionInCell(f: FlowField, idx: number, r1: number, r2: number, r3: number): Vec3 {
  const cx = f.nx - 1;
  const cy = f.ny - 1;
  const i = idx % cx;
  const j = Math.floor(idx / cx) % cy;
  const k = Math.floor(idx / (cx * cy));
  return [f.x0 + (i + r1) * f.dx, f.y0 + (j + r2) * f.dy, f.z0 + (k + r3) * f.dz];
}

/** Lifetime (visual seconds) of a particle for the random number r in [0, 1). */
export function lifetimeFor(base: number, r: number): number {
  return base * (0.6 + 0.8 * r);
}

export interface ParticleInit {
  /** x, y, z (km), age (visual s) per particle. */
  state: Float32Array;
  /** life (visual s), frozen (-1 = flowing), colour value, respawn count per particle. */
  aux: Float32Array;
}

/** Initial particles: positions inside valid cells, ages spread over the life so the field is full at once. */
export function seedParticles(f: FlowField, cells: Uint32Array, n: number, lifeBase: number, seed = 1): ParticleInit {
  const rnd = mulberry32(seed);
  const state = new Float32Array(n * 4);
  const aux = new Float32Array(n * 4);
  for (let i = 0; i < n; i++) {
    const life = lifetimeFor(lifeBase, rnd());
    aux[i * 4] = life;
    aux[i * 4 + 1] = -1;
    if (cells.length === 0) {
      // nothing to seed: park the particle outside its life so that it never draws
      aux[i * 4] = 0;
      continue;
    }
    const idx = cells[Math.min(cells.length - 1, Math.floor(rnd() * cells.length))];
    const p = positionInCell(f, idx, rnd(), rnd(), rnd());
    state[i * 4] = p[0];
    state[i * 4 + 1] = p[1];
    state[i * 4 + 2] = p[2];
    state[i * 4 + 3] = rnd() * life;
  }
  return { state, aux };
}

/** Opacity factor of a particle from its age / life and, once stopped, from the time since it stopped. */
export function particleFade(age: number, life: number, frozen: number, fadeIn: number, fadeOut: number): number {
  const sm = (a: number, b: number, x: number) => {
    const t = Math.max(0, Math.min(1, (x - a) / (b - a || 1)));
    return t * t * (3 - 2 * t);
  };
  let a = sm(0, fadeIn, age) * (1 - sm(life - fadeOut, life, age));
  if (frozen >= 0) a *= 1 - sm(0, fadeOut, frozen);
  return a;
}

// ─── Colour, speed, quality ─────────────────────────────────────────────────

export type FlowColor = 'none' | 'speed' | 'w';

/** 0..1 position in the colour table of a wind vector (0 for 'none'). */
export function colorValue(mode: FlowColor, u: number, v: number, w: number, colorMax: number): number {
  if (mode === 'speed') return Math.max(0, Math.min(1, Math.hypot(u, v) / colorMax));
  if (mode === 'w') return Math.max(0, Math.min(1, 0.5 + w / (2 * colorMax)));
  return 0;
}

/**
 * Visual speed: simulated seconds per animation second. The default makes a wind of `refSpeed`
 * m/s cross the domain (`extentKm` wide) in `seconds` animation seconds.
 */
export function defaultVisualSpeed(extentKm: number, refSpeed = 30, seconds = 10): number {
  const v = (extentKm * 1000) / (refSpeed * seconds);
  return Math.max(1, Math.round(v));
}

/** "1 s = 11 min of wind motion". */
export function describeVisualSpeed(factor: number): string {
  const s = factor;
  if (s < 90) return `1 s = ${Math.round(s)} s of wind motion`;
  if (s < 5400) return `1 s = ${Math.round(s / 60)} min of wind motion`;
  return `1 s = ${(s / 3600).toFixed(1)} h of wind motion`;
}

/** Share of the particles that are drawn at quality level index 0 (minimal) .. 4 (ultra). */
export const PARTICLE_LEVEL_FRACTION = [0.2, 0.35, 0.55, 0.8, 1];

export function activeParticles(requested: number, levelIndex: number): number {
  const f = PARTICLE_LEVEL_FRACTION[Math.max(0, Math.min(PARTICLE_LEVEL_FRACTION.length - 1, Math.round(levelIndex)))];
  return Math.max(0, Math.round(requested * f));
}

// ─── Field animation (drift of the echo along the wind) ─────────────────────

/**
 * Reference of the shader's two-phase cross-fade: the echo is sampled where each cell was
 * `phase * T` seconds ago (T = the drift at the end of a cycle) in two phases half a cycle apart;
 * a phase's weight is 0 at its reset (phase 0 or 1) and 1 half-way, and the two weights sum to 1,
 * so the loop has no visible seam. Returns [phase1, phase2, weight1, weight2].
 */
export function animPhases(phase: number): [number, number, number, number] {
  const p1 = ((phase % 1) + 1) % 1;
  const p2 = (p1 + 0.5) % 1;
  const w1 = 1 - Math.abs(2 * p1 - 1);
  return [p1, p2, w1, 1 - w1];
}

/** Where a cell's echo is taken from: `pos` minus the displacement (km) of the wind (m/s) over `seconds`. */
export function driftSource(pos: Vec3, wind: Vec3, seconds: number, vexag = 1): Vec3 {
  const k = seconds / 1000;
  return [pos[0] - wind[0] * k, pos[1] - wind[1] * k, pos[2] - wind[2] * k * vexag];
}

// ─── The physics read-out ───────────────────────────────────────────────────

export interface PhysicsCheck {
  maxSpeed: number;
  maxAbsW: number | null;
  /** Share (%) of the grid cells that carry wind under the current mask. */
  validShare: number;
  /** Mean horizontal divergence over those cells, 1e-3 1/s (null without the field). */
  meanDivergence: number | null;
  warnings: string[];
}

export function physicsCheck(w: WindVolume, f: FlowField, qc: any): PhysicsCheck {
  const n = f.totalCells;
  let maxSpeed = 0;
  let maxAbsW = 0;
  let divSum = 0;
  let divN = 0;
  let strong = 0;
  const div = w.fields.divergence;
  for (let q = 0; q < n; q++) {
    if (!(f.data[q * 4 + 3] > 0)) continue;
    const sp = Math.hypot(f.data[q * 4], f.data[q * 4 + 1]);
    if (sp > maxSpeed) maxSpeed = sp;
    if (sp > 60) strong++;
    const aw = Math.abs(f.data[q * 4 + 2]);
    if (aw > maxAbsW) maxAbsW = aw;
    if (div && Number.isFinite(div[q])) { divSum += div[q]; divN++; }
  }
  const warnings: string[] = [];
  const maxQcSpeed = typeof qc?.max_speed === 'number' ? qc.max_speed : null;
  const maxQcW = typeof qc?.max_abs_w === 'number' ? qc.max_abs_w : null;
  if (qc && qc.masked_cells > 0) {
    warnings.push(`QC removed ${qc.masked_cells} cells (wind faster than ${maxQcSpeed ?? '?'} m/s or too uncertain)`);
  }
  if (maxQcSpeed !== null && maxSpeed >= 0.9 * maxQcSpeed) warnings.push(`fastest wind is at the QC limit of ${maxQcSpeed} m/s`);
  if (maxQcW !== null && f.wRetrieved && maxAbsW >= 0.9 * maxQcW) warnings.push(`|w| is at the QC limit of ${maxQcW} m/s`);
  if (f.validCells > 0 && (strong / f.validCells > 0.02 || maxAbsW > 25)) {
    warnings.push('the field looks noisy (very strong winds): the velocities may be aliased');
  }
  if (!f.wRetrieved) warnings.push('w was not retrieved: the flow is horizontal only');
  return {
    maxSpeed,
    maxAbsW: f.wRetrieved ? maxAbsW : null,
    validShare: n ? (100 * f.validCells) / n : 0,
    meanDivergence: divN ? divSum / divN : null,
    warnings,
  };
}

/** "02:04 UTC" from an ISO time; null when unknown. */
export function formatUtc(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + 'Z');
  if (Number.isNaN(d.getTime())) return null;
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')} UTC`;
}
