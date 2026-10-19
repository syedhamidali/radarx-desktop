/**
 * Debris: inertial particles in the vortex field (pure, deterministic, unit-tested in debris.test.mjs).
 *
 * Each particle obeys  dv/dt = k |u - v| (u - v) - g z^   with k = rho Cd A / (2 m)  [1/m]  (quadratic
 * drag, integrated with an exponential relaxation so the stiff light classes stay stable), so heavy
 * objects (small k) keep their momentum and are flung out of the core while light ones follow the flow
 * and are lofted by the updraft. A particle on the ground is picked up where the surface wind exceeds its
 * pick-up speed. The state at time t is a pure function of the seed and of the start time (fixed 1/60 s
 * steps from `t0`, a hash-seeded random stream per particle), so a frame rendered twice is identical.
 *
 * The objects are stylised (dust grains, leaves and insulation, boards, sheet metal, branches, rare large
 * objects); the motion is physical. Which object is where is MODELLED, not observed.
 */

import { windAt, horizontalSpeed, GRAVITY, type Vortex } from './vortexField.ts';
import { axisAt, intensityAt, type ResolvedSeed } from './seed.ts';

export interface DebrisClass {
  name: string;
  /** Drag parameter k = rho Cd A / (2 m), 1/m: terminal speed sqrt(g / k). */
  k: number;
  /** Half extents of the sprite (m): a along its length, b across. */
  a: number;
  b: number;
  /** Surface wind (m/s) needed to pick the object up from the ground. */
  pick: number;
  /** Share of the particle pool. */
  share: number;
  /** Tumbling rate (rad/s) scale. */
  spin: number;
  /** Mean albedo (grey-brown) of the sprite colour. */
  tone: number;
}

export const DEBRIS_CLASSES: DebrisClass[] = [
  { name: 'dust and soil', k: 12, a: 0.05, b: 0.05, pick: 18, share: 0.38, spin: 2, tone: 0.34 },
  { name: 'leaves and insulation', k: 1.2, a: 0.16, b: 0.12, pick: 24, share: 0.3, spin: 9, tone: 0.3 },
  { name: 'shingles and boards', k: 0.07, a: 0.9, b: 0.09, pick: 34, share: 0.14, spin: 5, tone: 0.28 },
  { name: 'sheet metal and panels', k: 0.035, a: 0.9, b: 0.7, pick: 38, share: 0.08, spin: 3.5, tone: 0.55 },
  { name: 'branches and timber', k: 0.012, a: 1.8, b: 0.12, pick: 40, share: 0.08, spin: 2.2, tone: 0.2 },
  { name: 'large objects', k: 0.003, a: 2.2, b: 1.1, pick: 48, share: 0.02, spin: 0.9, tone: 0.24 },
];

function mulberry(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Ground-frame wind (m/s) at (x, y, z) and time t: tornado vortex + couplet + the air carried along with the storm. */
export function windField(r: ResolvedSeed, x: number, y: number, z: number, t: number, out: Float64Array | number[] = [0, 0, 0]): Float64Array | number[] {
  const inten = intensityAt(r, t);
  const ax = axisAt(r, t);
  const rx = x - ax.x;
  const ry = y - ax.y;
  // the environment is carried with the storm: weaker at the surface (friction), the translation speed aloft
  const zz = Math.max(0, z);
  const carry = 0.5 + 0.5 * Math.min(1, zz / 300);
  let u = r.translation[0] * carry;
  let v = r.translation[1] * carry;
  let w = 0;
  if (inten > 0) {
    const f = windAt(r.vortex, rx, ry, z);
    u += f[0] * inten;
    v += f[1] * inten;
    w += f[2] * inten;
  }
  if (r.meso) {
    const m = windAt(r.meso, rx, ry, z);
    u += m[0];
    v += m[1];
  }
  out[0] = u;
  out[1] = v;
  out[2] = w;
  return out;
}

export interface DebrisOptions {
  count?: number;
  seed?: number;
  /** Fixed step (s). */
  dt?: number;
  /** Particles farther than this from the axis are recycled (m). */
  radius?: number;
  maxAge?: number;
}

export class DebrisSim {
  readonly n: number;
  readonly dt: number;
  readonly radius: number;
  readonly maxAge: number;
  pos: Float32Array;
  vel: Float32Array;
  cls: Uint8Array;
  /** Per particle constants for the sprite: spin axis (3), phase, spin rate, size scale, tone variation. */
  attr: Float32Array;
  age: Float32Array;
  life: Float32Array;
  onGround: Uint8Array;
  private rng: (() => number)[];
  private classCum: number[] = [];
  t0 = 0;
  steps = 0;
  private r: ResolvedSeed;
  private seed: number;
  private tmp = new Float64Array(3);

  constructor(r: ResolvedSeed, o: DebrisOptions = {}) {
    this.r = r;
    this.n = o.count ?? 6000;
    this.dt = o.dt ?? 1 / 60;
    this.radius = o.radius ?? 1500;
    this.maxAge = o.maxAge ?? 30;
    this.seed = o.seed ?? 12345;
    this.pos = new Float32Array(this.n * 3);
    this.vel = new Float32Array(this.n * 3);
    this.cls = new Uint8Array(this.n);
    this.attr = new Float32Array(this.n * 6);
    this.age = new Float32Array(this.n);
    this.life = new Float32Array(this.n);
    this.onGround = new Uint8Array(this.n);
    this.rng = new Array(this.n);
    let c = 0;
    for (const k of DEBRIS_CLASSES) { c += k.share; this.classCum.push(c); }
    this.reset(0);
  }

  /** Restart at time t0: particles are placed (aged randomly) so the pool is in a steady state after a short warm-up. */
  reset(t0: number): void {
    this.t0 = t0;
    this.steps = 0;
    for (let i = 0; i < this.n; i++) {
      this.rng[i] = mulberry(this.seed * 2654435761 + i * 40503 + 7);
      this.assignClass(i);
      this.spawn(i, t0, true);
    }
  }

  get time(): number {
    return this.t0 + this.steps * this.dt;
  }

  private assignClass(i: number): void {
    const u = this.rng[i]() * this.classCum[this.classCum.length - 1];
    let c = 0;
    while (c < this.classCum.length - 1 && u > this.classCum[c]) c++;
    this.cls[i] = c;
    const R = this.rng[i];
    const ax = R() * 2 - 1, ay = R() * 2 - 1, az = R() * 2 - 1;
    const l = Math.hypot(ax, ay, az) || 1;
    const a = this.attr;
    a[i * 6] = ax / l;
    a[i * 6 + 1] = ay / l;
    a[i * 6 + 2] = az / l;
    a[i * 6 + 3] = R() * Math.PI * 2;
    a[i * 6 + 4] = (0.5 + R()) * DEBRIS_CLASSES[c].spin * (R() < 0.5 ? -1 : 1);
    a[i * 6 + 5] = 0.75 + 0.5 * R(); // size scale
  }

  private spawn(i: number, t: number, warm: boolean): void {
    const R = this.rng[i];
    const r = this.r;
    const ax = axisAt(r, t);
    const cl = DEBRIS_CLASSES[this.cls[i]];
    const rm = r.rm;
    // sample a radius where the surface wind exceeds the pick-up speed (rejection sampling on a log-uniform radius)
    let rad = 2 * rm, ok = false;
    for (let tries = 0; tries < 24 && !ok; tries++) {
      const cand = rm * Math.exp(Math.log(0.25) + R() * (Math.log(10) - Math.log(0.25)));
      const spd = horizontalSpeed(r.vortex, cand, 2) * intensityAt(r, t);
      const w = Math.min(1, Math.max(0, (spd - cl.pick * 0.55) / (0.5 * r.vt)));
      if (R() < w) { rad = cand; ok = true; }
    }
    const phi = R() * Math.PI * 2;
    const z = R() * 3;
    const p = this.pos;
    p[i * 3] = ax.x + rad * Math.cos(phi);
    p[i * 3 + 1] = ax.y + rad * Math.sin(phi);
    p[i * 3 + 2] = z;
    const w = windField(r, p[i * 3], p[i * 3 + 1], 1.5, t, this.tmp);
    const v = this.vel;
    v[i * 3] = w[0] * 0.6;
    v[i * 3 + 1] = w[1] * 0.6;
    v[i * 3 + 2] = 0;
    this.age[i] = warm ? R() * this.maxAge * 0.5 : 0;
    this.life[i] = (6 + R() * (this.maxAge - 6));
    this.onGround[i] = 1;
    if (warm) {
      // warm start: spread the pool through the first seconds of flight by pre-aging (the first steps do the rest)
      this.onGround[i] = 1;
    }
  }

  /** Advance the particles to time t (never backwards: call reset first). */
  stepTo(t: number): void {
    const dt = this.dt;
    const r = this.r;
    const w = this.tmp;
    while (this.time + dt <= t + 1e-9) {
      const tt = this.time;
      for (let i = 0; i < this.n; i++) {
        const o = i * 3;
        const cl = DEBRIS_CLASSES[this.cls[i]];
        const x = this.pos[o], y = this.pos[o + 1], z = this.pos[o + 2];
        windField(r, x, y, z, tt, w);
        let vx = this.vel[o], vy = this.vel[o + 1], vz = this.vel[o + 2];
        if (this.onGround[i]) {
          const sp = Math.hypot(w[0], w[1]);
          if (sp > cl.pick) {
            // saltation / pick-up: a kick proportional to the excess wind, plus the updraft above
            this.onGround[i] = 0;
            vz = 0.25 * (sp - cl.pick) + 1.5 + 3 * this.rng[i]();
          } else {
            // sliding / resting: relax toward zero and wait; recycle when waited too long near nothing
            vx *= 0.8; vy *= 0.8;
          }
        }
        const dx = w[0] - vx, dy = w[1] - vy, dz = w[2] - vz;
        const dm = Math.hypot(dx, dy, dz);
        // exponential relaxation: v -> u - (u - v) exp(-k |u-v| dt)
        const e = Math.exp(-cl.k * dm * dt);
        vx = w[0] - dx * e;
        vy = w[1] - dy * e;
        vz = w[2] - dz * e - GRAVITY * dt;
        let nx = x + vx * dt, ny = y + vy * dt, nz = z + vz * dt;
        if (nz < 0.05) {
          nz = 0.05;
          vz = -vz * 0.25;
          vx *= 0.75; vy *= 0.75;
          if (Math.abs(vz) < 1.5) this.onGround[i] = 1;
        }
        this.pos[o] = nx; this.pos[o + 1] = ny; this.pos[o + 2] = nz;
        this.vel[o] = vx; this.vel[o + 1] = vy; this.vel[o + 2] = vz;
        this.age[i] += dt;
        // recycle
        const ax = axisAt(r, tt);
        const d2 = (nx - ax.x) * (nx - ax.x) + (ny - ax.y) * (ny - ax.y);
        if (this.age[i] > this.life[i] || d2 > this.radius * this.radius || nz > 1400) {
          this.assignClass(i);
          this.spawn(i, tt, false);
        }
      }
      this.steps++;
    }
  }

  /** Run from t0 to t deterministically (reset + steps). */
  runTo(t0: number, t: number): void {
    this.reset(t0);
    this.stepTo(t);
  }

  /** Number of particles currently above ground (airborne). */
  airborne(): number {
    let c = 0;
    for (let i = 0; i < this.n; i++) if (!this.onGround[i]) c++;
    return c;
  }
}

/** Terminal fall speed of a class (m/s). */
export function terminalSpeed(c: DebrisClass): number {
  return Math.sqrt(GRAVITY / c.k);
}

export type { Vortex };
