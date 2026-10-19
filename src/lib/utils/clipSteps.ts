/**
 * Time steps of a storm clip, one at a time.
 *
 * A clip can have many steps (a tropical cyclone over an hour is a dozen volumes or more); only the
 * two the current frame needs stay in memory (plus the one before, for stepping back), the others are
 * read again from the bundle when needed. What the whole clip needs from every step (the column
 * maximum for the inset map, the ice-phase volume and the lightning candidates, the histogram that
 * sets the opacity) is computed once while the step is streamed past, and only that small summary is
 * kept. Pure helpers (no DOM, no GL); unit-tested in clipSteps.test.mjs.
 */
import type { CartVolume } from './cartesianVolume.ts';
import { iceVolumeKm3, type FlashCandidate } from './lightning.ts';

/** A cache of at most `max` loaded items, least recently used out first; one load per missing key at a time. */
export class LruLoader<T> {
  private items = new Map<number, T>();
  private pending = new Map<number, Promise<T>>();
  loads = 0;

  private max: number;
  private loader: (k: number) => Promise<T>;

  constructor(max: number, loader: (k: number) => Promise<T>) {
    this.max = max;
    this.loader = loader;
  }

  get size(): number {
    return this.items.size;
  }

  has(k: number): boolean {
    return this.items.has(k);
  }

  async get(k: number): Promise<T> {
    const hit = this.items.get(k);
    if (hit !== undefined) {
      this.items.delete(k);
      this.items.set(k, hit);
      return hit;
    }
    let p = this.pending.get(k);
    if (!p) {
      p = this.loader(k).then((v) => {
        this.loads++;
        this.items.set(k, v);
        while (this.items.size > this.max) {
          const oldest = this.items.keys().next().value;
          if (oldest === undefined) break;
          this.items.delete(oldest);
        }
        this.pending.delete(k);
        return v;
      }, (e) => {
        this.pending.delete(k);
        throw e;
      });
      this.pending.set(k, p);
    }
    return p;
  }
}

/** Column maximum of the codes of a volume (0 = no echo anywhere in the column). */
export function columnMax(v: CartVolume): Uint8Array {
  const n = v.nx * v.ny;
  const out = new Uint8Array(n);
  for (let k = 0; k < v.nz; k++) {
    const o = k * n;
    for (let i = 0; i < n; i++) {
      const c = v.data[o + i];
      if (c > out[i]) out[i] = c;
    }
  }
  return out;
}

/** Code of a reflectivity (dBZ) in the volume's 8-bit scale. */
export function codeOfDbz(v: { vmin: number; vmax: number }, dbz: number): number {
  return Math.max(1, Math.min(255, 1 + Math.ceil(((dbz - v.vmin) / (v.vmax - v.vmin)) * 254)));
}

export function dbzOfCode(v: { vmin: number; vmax: number }, code: number): number {
  return v.vmin + ((code - 1) / 254) * (v.vmax - v.vmin);
}

/**
 * Columns of strong echo above the -10 C level (`t10Km`): the places a modelled flash may start. Weight
 * grows with how far the echo exceeds `dbzMin`; at most `maxN` columns (the strongest) are kept.
 */
export function lightningCandidates(v: CartVolume, t10Km: number, dbzMin = 40, maxN = 300): FlashCandidate[] {
  const need = codeOfDbz(v, dbzMin);
  const low = codeOfDbz(v, 15);
  const out: FlashCandidate[] = [];
  const n = v.nx * v.ny;
  for (let j = 0; j < v.ny; j += 2) {
    for (let i = 0; i < v.nx; i += 2) {
      const col = j * v.nx + i;
      let best = 0;
      let base = Infinity;
      let top = 0;
      for (let k = 0; k < v.nz; k++) {
        const c = v.data[k * n + col];
        if (c >= low) top = Math.max(top, v.z[k]);
        if (v.z[k] >= t10Km && c >= need) {
          best = Math.max(best, c);
          base = Math.min(base, v.z[k]);
        }
      }
      if (best >= need && top > 0) out.push({ x: v.x0 + i * v.dx, y: v.y0 + j * v.dy, topKm: top, baseKm: base, weight: 1 + (best - need) / 8 });
    }
  }
  out.sort((a, b) => b.weight - a.weight);
  return out.slice(0, maxN);
}

/** What the whole clip needs of one step, kept after the volume itself is released. */
export interface StepSummary {
  composite: Uint8Array;
  hist: Float64Array;
  iceKm3: number;
  candidates: FlashCandidate[];
}

export function summarise(v: CartVolume, t10Km: number): StepSummary {
  const hist = new Float64Array(256);
  for (let i = 0; i < v.data.length; i += 3) hist[v.data[i]]++;
  return {
    composite: columnMax(v),
    hist,
    iceKm3: iceVolumeKm3(v.data, v.nx, v.ny, v.nz, v.z, v.dx, v.dy, v.vmin, v.vmax, 35, t10Km),
    candidates: lightningCandidates(v, t10Km),
  };
}

/** dBZ of the volume at a position (nearest cell; NaN where there is no echo or outside). */
export function dbzAt(v: CartVolume, x: number, y: number, zKm: number): number {
  const i = Math.round((x - v.x0) / v.dx), j = Math.round((y - v.y0) / v.dy);
  if (i < 0 || j < 0 || i >= v.nx || j >= v.ny) return NaN;
  let k = 0, best = Infinity;
  for (let q = 0; q < v.nz; q++) {
    const d = Math.abs(v.z[q] - zKm);
    if (d < best) { best = d; k = q; }
  }
  const c = v.data[(k * v.ny + j) * v.nx + i];
  return c === 0 ? NaN : dbzOfCode(v, c);
}
