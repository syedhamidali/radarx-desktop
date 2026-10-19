/**
 * Lightning for the storm clip: flash geometry, return-stroke timing, the light a flash throws, and
 * a MODELLED flash source driven by radar fields.
 *
 * Provenance is part of the data: every flash carries `source`, 'observed' (a flash position and
 * time that came from a lightning file: GLM, LMA) or 'modelled' (drawn by a stochastic model from
 * radar echo; deterministic for a given seed). The screen must say which; modelled flashes are never
 * presented as observed. Even for observed flashes only the position and time are data: the branching
 * channel shape is synthetic (no instrument we read resolves it).
 *
 * Pure functions (no DOM, no GL); unit-tested in lightning.test.mjs. Coordinates: km, x east,
 * y north, z = height above sea level (true, not exaggerated).
 */

export type FlashKind = 'CG' | 'IC';
export type FlashSource = 'observed' | 'modelled';

export interface Stroke {
  /** Seconds after the flash start. */
  dt: number;
  /** Relative brightness 0..1. */
  amp: number;
}

export interface Flash {
  id: number;
  /** Clip time (s) of the first stroke. */
  t: number;
  kind: FlashKind;
  source: FlashSource;
  /** Start of the channel (the charge region) and, for cloud-to-ground, the strike point. */
  origin: [number, number, number];
  ground?: [number, number, number];
  /** Flat [x0, y0, z0, x1, y1, z1, ...] channel segments and, per segment, 0 main / 1, 2 branches. */
  segments: Float32Array;
  level: Uint8Array;
  strokes: Stroke[];
  /** 0..1 relative strength (return stroke peak current): scales the light and the thunder. */
  energy: number;
}

/** Deterministic random numbers (mulberry32). */
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

type P3 = [number, number, number];

const sub = (a: P3, b: P3): P3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const add = (a: P3, b: P3): P3 => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const mul = (a: P3, k: number): P3 => [a[0] * k, a[1] * k, a[2] * k];
const len = (a: P3) => Math.hypot(a[0], a[1], a[2]);
const norm = (a: P3): P3 => {
  const l = len(a) || 1;
  return [a[0] / l, a[1] / l, a[2] / l];
};
const cross = (a: P3, b: P3): P3 => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];

/** A jagged path from a to b by recursive midpoint displacement (points including both ends). */
export function jaggedPath(rng: () => number, a: P3, b: P3, depth: number, roughness: number): P3[] {
  if (depth <= 0) return [a, b];
  const d = sub(b, a);
  const l = len(d);
  // random direction perpendicular to the segment
  let r: P3 = [rng() - 0.5, rng() - 0.5, rng() - 0.5];
  const dn = norm(d);
  r = sub(r, mul(dn, r[0] * dn[0] + r[1] * dn[1] + r[2] * dn[2]));
  r = norm(r);
  const m = add(mul(add(a, b), 0.5), mul(r, roughness * l * (rng() * 2 - 1)));
  const left = jaggedPath(rng, a, m, depth - 1, roughness);
  const right = jaggedPath(rng, m, b, depth - 1, roughness);
  return left.concat(right.slice(1));
}

interface Builder {
  seg: number[];
  lvl: number[];
}

function pushPath(b: Builder, path: P3[], level: number): void {
  for (let i = 0; i + 1 < path.length; i++) {
    b.seg.push(path[i][0], path[i][1], path[i][2], path[i + 1][0], path[i + 1][1], path[i + 1][2]);
    b.lvl.push(level);
  }
}

/** Side branches off a path (step-leader forks); `down` biases them toward the ground. */
function grow(rng: () => number, b: Builder, path: P3[], level: number, maxLevel: number, prob: number, down: number): void {
  if (level > maxLevel || path.length < 3) return;
  for (let i = 1; i < path.length - 1; i++) {
    if (rng() > prob) continue;
    const dir = norm(sub(path[i + 1], path[i - 1]));
    const side = norm(cross(dir, [rng() - 0.5, rng() - 0.5, rng() - 0.5]));
    const ang = (0.35 + 0.6 * rng());
    let bd = norm(add(mul(dir, Math.cos(ang)), mul(side, Math.sin(ang))));
    bd = norm(add(bd, [0, 0, -down]));
    const remaining = len(sub(path[path.length - 1], path[i]));
    const bl = remaining * (0.12 + 0.28 * rng()) + 0.3;
    const end = add(path[i], mul(bd, bl));
    const bp = jaggedPath(rng, path[i], end, 3, 0.22);
    pushPath(b, bp, level);
    grow(rng, b, bp, level + 1, maxLevel, prob * 0.55, down * 0.7);
  }
}

export interface ChannelSpec {
  kind: FlashKind;
  origin: P3;
  /** Strike point (cloud-to-ground) or the far end of the horizontal channel (intracloud). */
  end: P3;
  seed: number;
}

/** The branching channel of a flash: segments (km) and their branch level. */
export function buildChannel(spec: ChannelSpec): { segments: Float32Array; level: Uint8Array } {
  const rng = mulberry32(spec.seed);
  const b: Builder = { seg: [], lvl: [] };
  if (spec.kind === 'CG') {
    // the descending channel: a jagged, forked path from the charge region to the ground
    const main = jaggedPath(rng, spec.origin, spec.end, 5, 0.16);
    pushPath(b, main, 0);
    grow(rng, b, main, 1, 2, 0.11, 0.5);
    // the part inside the cloud: a few horizontal channels around the origin
    const nIn = 2 + Math.floor(rng() * 3);
    for (let i = 0; i < nIn; i++) {
      const az = rng() * Math.PI * 2;
      const L = 2 + rng() * 7;
      const e: P3 = [spec.origin[0] + Math.cos(az) * L, spec.origin[1] + Math.sin(az) * L, spec.origin[2] + (rng() - 0.4) * 1.6];
      const p = jaggedPath(rng, spec.origin, e, 4, 0.2);
      pushPath(b, p, 1);
      grow(rng, b, p, 2, 2, 0.12, 0);
    }
  } else {
    const main = jaggedPath(rng, spec.origin, spec.end, 5, 0.18);
    pushPath(b, main, 0);
    grow(rng, b, main, 1, 2, 0.16, 0);
    // a second arm the other way from the origin
    const back = add(spec.origin, mul(sub(spec.origin, spec.end), 0.4 + 0.4 * rng()));
    const p2 = jaggedPath(rng, spec.origin, back, 4, 0.2);
    pushPath(b, p2, 1);
    grow(rng, b, p2, 2, 2, 0.14, 0);
  }
  return { segments: new Float32Array(b.seg), level: new Uint8Array(b.lvl) };
}

/** Return strokes: cloud-to-ground flashes re-strike 2 to 4 times in 0.15 to 0.5 s; intracloud flicker is weaker. */
export function makeStrokes(rng: () => number, kind: FlashKind): Stroke[] {
  const out: Stroke[] = [];
  if (kind === 'CG') {
    const n = 2 + Math.floor(rng() * 3); // 2..4
    let t = 0;
    for (let i = 0; i < n; i++) {
      out.push({ dt: t, amp: i === 0 ? 1 : 0.35 + 0.5 * rng() });
      t += 0.04 + 0.11 * rng();
    }
  } else {
    const n = 2 + Math.floor(rng() * 3);
    let t = 0;
    for (let i = 0; i < n; i++) {
      out.push({ dt: t, amp: 0.25 + 0.4 * rng() });
      t += 0.05 + 0.12 * rng();
    }
  }
  return out;
}

export interface FlashInit {
  id: number;
  t: number;
  kind: FlashKind;
  source: FlashSource;
  origin: P3;
  end: P3;
  energy?: number;
  seed?: number;
}

/** A complete flash (geometry, strokes) from where and when it happens. Same input: same flash. */
export function makeFlash(i: FlashInit): Flash {
  const seed = (i.seed ?? 0) * 7919 + i.id * 104729 + 17;
  const ch = buildChannel({ kind: i.kind, origin: i.origin, end: i.end, seed });
  const rng = mulberry32(seed ^ 0x9e3779b9);
  return {
    id: i.id,
    t: i.t,
    kind: i.kind,
    source: i.source,
    origin: i.origin,
    ground: i.kind === 'CG' ? i.end : undefined,
    segments: ch.segments,
    level: ch.level,
    strokes: makeStrokes(rng, i.kind),
    energy: i.energy ?? 0.5 + 0.5 * rng(),
  };
}

/** Time constants (s): the channel flash fades fast, the light scattered by the cloud a little slower. */
export const CHANNEL_TAU = 0.032;
export const CLOUD_TAU = 0.09;
/** Seconds after the last stroke when a flash can be forgotten. */
export const FLASH_TAIL = 0.6;

export interface FlashLevels {
  /** Brightness of the channel itself, 0..1. */
  channel: number;
  /** Light the cloud, sky and ground receive, 0..1.2. */
  light: number;
}

/** Brightness of a flash at clip time `t` (sum of its strokes, each a fast rise and exponential decay). */
export function flashLevels(f: Flash, t: number): FlashLevels {
  let ch = 0;
  let li = 0;
  for (const s of f.strokes) {
    const x = t - (f.t + s.dt);
    if (x < 0) continue;
    ch += s.amp * Math.exp(-x / CHANNEL_TAU);
    li += s.amp * Math.exp(-x / CLOUD_TAU);
  }
  // a faint continuing glow after the first stroke of a cloud-to-ground flash
  const x0 = t - f.t;
  if (f.kind === 'CG' && x0 >= 0 && x0 < 0.22) {
    ch += 0.12 * (1 - x0 / 0.22);
    li += 0.18 * (1 - x0 / 0.22);
  }
  return { channel: Math.min(1, ch), light: Math.min(1.2, li * (0.6 + 0.6 * f.energy)) };
}

export function flashEnd(f: Flash): number {
  const last = f.strokes[f.strokes.length - 1]?.dt ?? 0;
  return f.t + last + FLASH_TAIL;
}

/** Flashes active (still visible) at clip time `t`. */
export function activeFlashes(flashes: Flash[], t: number): Flash[] {
  return flashes.filter((f) => t >= f.t && t <= flashEnd(f));
}

/** Up to three points that light the surroundings: the start, the middle of the channel and its far end. */
export function lightPoints(f: Flash): P3[] {
  const s = f.segments;
  const n = s.length / 6;
  if (!n) return [f.origin];
  const pt = (i: number): P3 => [s[i * 6], s[i * 6 + 1], s[i * 6 + 2]];
  return [pt(0), pt(Math.floor(n / 2)), [s[(n - 1) * 6 + 3], s[(n - 1) * 6 + 4], s[(n - 1) * 6 + 5]]];
}

/** Sound travels at about 343 m/s; `compress` shortens the delay in a time-lapse clip (1 = real physics). */
export const SPEED_OF_SOUND_MS = 343;
export function thunderDelay(distKm: number, compress = 1): number {
  return (distKm * 1000) / SPEED_OF_SOUND_MS / Math.max(1e-6, compress);
}

// ─── Modelled flashes ───────────────────────────────────────────────────────

/** Where and how a modelled flash may start: a column of strong ice-phase echo. */
export interface FlashCandidate {
  x: number;
  y: number;
  /** Top of the echo (km ASL) and the lowest height of strong echo above the -10 C level. */
  topKm: number;
  baseKm: number;
  weight: number;
}

export interface ModelInput {
  seed: number;
  /** Clip length (s). */
  duration: number;
  /** Flashes per clip second at clip time t (from the radar proxy). */
  rate: (t: number) => number;
  /** Candidate columns at clip time t (the storm moves: the caller shifts them). */
  candidates: (t: number) => FlashCandidate[];
  groundKm: (x: number, y: number) => number;
  /** Heading the anvil spreads toward (degrees clockwise from north). */
  headingDeg: number;
  /** Cloud-to-ground share of the flashes, 0..1. */
  cgFraction?: number;
  maxFlashes?: number;
  /** Shortest gap between two flashes (s). */
  minGap?: number;
}

/**
 * Stochastic flashes whose rate and places are set by radar fields (see ModelInput). It is a
 * model: only the existence of strong ice-phase echo and where it is come from the data; the
 * exact times, places inside the echo and the channel shapes are random draws (deterministic for
 * a seed). Every flash is marked 'modelled'.
 */
export function modelFlashes(m: ModelInput): Flash[] {
  const rng = mulberry32(m.seed);
  const out: Flash[] = [];
  const dt = 1 / 60;
  const minGap = m.minGap ?? 0.35;
  const max = m.maxFlashes ?? 400;
  let last = -1e9;
  let id = 0;
  for (let t = 0; t < m.duration && out.length < max; t += dt) {
    const lambda = Math.max(0, m.rate(t));
    // Poisson process by thinning of small steps
    if (rng() < 1 - Math.exp(-lambda * dt)) {
      // the draws below are consumed in the same order whether or not the flash is kept
      const u1 = rng(), u2 = rng(), u3 = rng(), u4 = rng(), u5 = rng(), u6 = rng(), u7 = rng();
      if (t - last < minGap) continue;
      const cands = m.candidates(t);
      if (!cands.length) continue;
      let tot = 0;
      for (const c of cands) tot += c.weight;
      let pick = u1 * tot;
      let c = cands[cands.length - 1];
      for (const cc of cands) {
        pick -= cc.weight;
        if (pick <= 0) { c = cc; break; }
      }
      const kind: FlashKind = u2 < (m.cgFraction ?? 0.4) ? 'CG' : 'IC';
      const zStart = c.baseKm + (0.25 + 0.5 * u3) * Math.max(0.5, Math.min(c.topKm, c.baseKm + 5) - c.baseKm);
      const origin: P3 = [c.x + (u4 - 0.5) * 3, c.y + (u5 - 0.5) * 3, zStart];
      let end: P3;
      if (kind === 'CG') {
        const gx = origin[0] + (u6 - 0.5) * 8;
        const gy = origin[1] + (u7 - 0.5) * 8;
        end = [gx, gy, m.groundKm(gx, gy)];
      } else {
        const h = (m.headingDeg * Math.PI) / 180;
        const L = 5 + 18 * u6;
        end = [origin[0] + Math.sin(h) * L * (0.5 + u7), origin[1] + Math.cos(h) * L * (0.5 + u7), origin[2] + (u7 - 0.5) * 2];
      }
      out.push(makeFlash({ id: id++, t, kind, source: 'modelled', origin, end, seed: m.seed }));
      last = t;
    }
  }
  return out;
}

/**
 * Flash rate proxy: strong echo (>= `dbzMin`) above the -10 C level, as a volume in km^3. The flash
 * rate of a thunderstorm grows with such an ice-phase volume (graupel and hail aloft); the factor here
 * is a scale for the clip, not a calibrated relation. Returns the volume of one gridded step.
 */
export function iceVolumeKm3(codes: Uint8Array, nx: number, ny: number, nz: number, zKm: ArrayLike<number>, dx: number, dy: number, vmin: number, vmax: number, dbzMin: number, t10Km: number): number {
  const codeMin = 1 + Math.ceil(((dbzMin - vmin) / (vmax - vmin)) * 254);
  let cells = 0;
  const dz = nz > 1 ? (zKm[nz - 1] - zKm[0]) / (nz - 1) : 1;
  for (let k = 0; k < nz; k++) {
    if (zKm[k] < t10Km) continue;
    const o = k * nx * ny;
    for (let i = 0; i < nx * ny; i++) if (codes[o + i] >= codeMin) cells++;
  }
  return cells * dx * dy * dz;
}
