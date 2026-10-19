/**
 * The ground of the PERiLS movie, composed per frame: a schematic base map (county and state
 * lines, water, cities; drawn by tools/perils_movie/render/make_ground.py from the Census cartographic
 * boundary files and Natural Earth, no terrain and no imagery), the MRMS composite reflectivity as a 2-D layer
 * OUTSIDE the radar volume's box (inside it the 3-D volume is the truth), optionally the convective /
 * stratiform classification inside the box, all in one RGBA image that the renderer drapes on the
 * ground. MRMS between two analyses is carried along the line motion and cross-faded, like the
 * volume (labelled INTERPOLATED on screen).
 *
 * The pure maths (sampling, weights, feathering) is exported and unit-tested in perilsGround.test.mjs.
 */
import type { MrmsMeta } from './perilsData.ts';

export function smooth01(x: number): number {
  const c = Math.max(0, Math.min(1, x));
  return c * c * (3 - 2 * c);
}

/** Nearest-cell code of a gridded uint8 field at (x, y) km; 0 outside. */
export function codeAt(codes: Uint8Array, m: { x0: number; y0: number; dx: number; nx: number; ny: number }, x: number, y: number): number {
  const i = Math.round((x - m.x0) / m.dx);
  const j = Math.round((y - m.y0) / m.dx);
  if (i < 0 || j < 0 || i >= m.nx || j >= m.ny) return 0;
  return codes[j * m.nx + i];
}

/**
 * Opacity of the 2-D layer at (x, y): 0 inside the volume's box (the 3-D volume is the truth there),
 * rising smoothly to 1 over `featherKm` outside its edge.
 */
export function outsideBoxAlpha(x: number, y: number, box: { xMin: number; xMax: number; yMin: number; yMax: number }, featherKm = 8): number {
  const dx = Math.max(box.xMin - x, x - box.xMax, 0);
  const dy = Math.max(box.yMin - y, y - box.yMax, 0);
  const inside = x >= box.xMin && x <= box.xMax && y >= box.yMin && y <= box.yMax;
  if (inside) return 0;
  return smooth01(Math.hypot(dx, dy) / featherKm);
}

/** Opacity of a reflectivity colour on the ground: nothing below 10 dBZ, 0.35 at 10, 0.85 from 45. */
export function reflAlpha(dbz: number): number {
  if (!(dbz >= 10)) return 0;
  return Math.min(0.85, 0.35 + ((dbz - 10) / 35) * 0.5);
}

/** A ground layer given as a function of the position (km): RGBA with alpha 0..1, or null for nothing. */
export type PixelFn = (x: number, y: number) => [number, number, number, number] | null;

/** Marching squares: the segments [x0, y0, x1, y1] of the contour `level` of a regular grid (row-major, x fastest). */
export function contourSegments(f: ArrayLike<number>, nx: number, ny: number, x0: number, y0: number, dx: number, level: number): number[][] {
  const out: number[][] = [];
  const v = (i: number, j: number) => f[j * nx + i];
  const lerp = (a: number, b: number) => (a === b ? 0.5 : (level - a) / (b - a));
  for (let j = 0; j < ny - 1; j++) {
    for (let i = 0; i < nx - 1; i++) {
      const a = v(i, j);
      const b = v(i + 1, j);
      const c = v(i + 1, j + 1);
      const d = v(i, j + 1);
      if (![a, b, c, d].every(Number.isFinite)) continue;
      const code = (a > level ? 1 : 0) | (b > level ? 2 : 0) | (c > level ? 4 : 0) | (d > level ? 8 : 0);
      if (code === 0 || code === 15) continue;
      const X = x0 + i * dx;
      const Y = y0 + j * dx;
      // edge crossings: bottom (a-b), right (b-c), top (d-c), left (a-d)
      const e = {
        bottom: [X + lerp(a, b) * dx, Y],
        right: [X + dx, Y + lerp(b, c) * dx],
        top: [X + lerp(d, c) * dx, Y + dx],
        left: [X, Y + lerp(a, d) * dx],
      };
      const segs: [number[], number[]][] = [];
      switch (code) {
        case 1: case 14: segs.push([e.left, e.bottom]); break;
        case 2: case 13: segs.push([e.bottom, e.right]); break;
        case 3: case 12: segs.push([e.left, e.right]); break;
        case 4: case 11: segs.push([e.right, e.top]); break;
        case 6: case 9: segs.push([e.bottom, e.top]); break;
        case 7: case 8: segs.push([e.left, e.top]); break;
        case 5: segs.push([e.left, e.top], [e.bottom, e.right]); break;
        case 10: segs.push([e.left, e.bottom], [e.top, e.right]); break;
      }
      for (const [p, q] of segs) out.push([p[0], p[1], q[0], q[1]]);
    }
  }
  return out;
}

export interface GroundSpec {
  size: number;
  half: number;
  cx: number;
  cy: number;
}

/** The ground compositor (browser only: canvases). */
export class GroundComposer {
  private base: HTMLCanvasElement;
  private mrms: HTMLCanvasElement;
  private mrmsCtx: CanvasRenderingContext2D;
  private out: HTMLCanvasElement;
  private outCtx: CanvasRenderingContext2D;
  private img: ImageData;
  private under: HTMLCanvasElement;
  private underCtx: CanvasRenderingContext2D;
  private over: HTMLCanvasElement;
  private overCtx: CanvasRenderingContext2D;
  private M: number;
  mrmsCodes: (Uint8Array | null)[] = [];

  readonly spec: GroundSpec;

  constructor(spec: GroundSpec, baseImage: ImageBitmap | null, mrmsSize = 512) {
    this.spec = spec;
    const S = spec.size;
    this.base = document.createElement('canvas');
    this.base.width = this.base.height = S;
    const bctx = this.base.getContext('2d')!;
    bctx.fillStyle = 'rgb(20,24,34)';
    bctx.fillRect(0, 0, S, S);
    if (baseImage) bctx.drawImage(baseImage, 0, 0, S, S);
    this.M = mrmsSize;
    this.mrms = document.createElement('canvas');
    this.mrms.width = this.mrms.height = mrmsSize;
    this.mrmsCtx = this.mrms.getContext('2d')!;
    this.out = document.createElement('canvas');
    this.out.width = this.out.height = S;
    this.outCtx = this.out.getContext('2d', { willReadFrequently: true })!;
    this.img = this.mrmsCtx.createImageData(mrmsSize, mrmsSize);
    this.under = document.createElement('canvas');
    this.under.width = this.under.height = mrmsSize;
    this.underCtx = this.under.getContext('2d')!;
    this.over = document.createElement('canvas');
    this.over.width = this.over.height = mrmsSize;
    this.overCtx = this.over.getContext('2d')!;
  }

  /** Draw a pixel function into a small canvas covering the ground square. */
  private paint(ctx: CanvasRenderingContext2D, fn: PixelFn | null): boolean {
    const { spec, M } = this;
    if (!fn) {
      ctx.clearRect(0, 0, M, M);
      return false;
    }
    const img = ctx.createImageData(M, M);
    const d = img.data;
    let any = false;
    for (let j = 0; j < M; j++) {
      const y = spec.cy + spec.half - ((j + 0.5) / M) * 2 * spec.half;
      for (let i = 0; i < M; i++) {
        const x = spec.cx - spec.half + ((i + 0.5) / M) * 2 * spec.half;
        const c = fn(x, y);
        if (!c || c[3] <= 0) continue;
        const o = (j * M + i) * 4;
        d[o] = c[0];
        d[o + 1] = c[1];
        d[o + 2] = c[2];
        d[o + 3] = Math.round(255 * Math.min(1, c[3]));
        any = true;
      }
    }
    ctx.putImageData(img, 0, 0);
    return any;
  }

  /**
   * One frame of the ground: the base map, the MRMS composite of the interval (A carried forward by
   * `s * dt` along `motion` km/s, B back by `(1 - s) * dt`, cross-faded with weight `s`) outside `box`.
   * Returns the RGBA bytes to hand to the renderer.
   */
  compose(opts: {
    metaA: MrmsMeta | null;
    metaB: MrmsMeta | null;
    codesA: Uint8Array | null;
    codesB: Uint8Array | null;
    s: number;
    dt: number;
    motion: [number, number];
    box: { xMin: number; xMax: number; yMin: number; yMax: number };
    lut: Uint8Array;
    vmin: number;
    vmax: number;
    gain?: number;
    /** A layer under the MRMS (the surface analysis) and one over it (the rotation swath). */
    under?: PixelFn | null;
    over?: PixelFn | null;
  }): Uint8ClampedArray {
    const { spec, M } = this;
    const d = this.img.data;
    d.fill(0);
    const { codesA, codesB, metaA, metaB, s, dt, motion } = opts;
    if ((codesA && metaA) || (codesB && metaB)) {
      for (let j = 0; j < M; j++) {
        const y = spec.cy + spec.half - ((j + 0.5) / M) * 2 * spec.half;
        for (let i = 0; i < M; i++) {
          const x = spec.cx - spec.half + ((i + 0.5) / M) * 2 * spec.half;
          const fa = outsideBoxAlpha(x, y, opts.box);
          if (fa <= 0) continue;
          const ca = codesA && metaA ? codeAt(codesA, metaA, x - motion[0] * s * dt, y - motion[1] * s * dt) : 0;
          const cb = codesB && metaB ? codeAt(codesB, metaB, x + motion[0] * (1 - s) * dt, y + motion[1] * (1 - s) * dt) : 0;
          const wa = ca ? 1 - s : 0;
          const wb = cb ? s : 0;
          if (wa + wb <= 0) continue;
          const code = (wa * ca + wb * cb) / (wa + wb);
          const dbz = opts.vmin + ((code - 1) / 254) * (opts.vmax - opts.vmin);
          const a = reflAlpha(dbz) * fa * Math.min(1, wa + wb) * (opts.gain ?? 1);
          if (a <= 0) continue;
          const idx = Math.max(0, Math.min(255, Math.round(((dbz - opts.vmin) / (opts.vmax - opts.vmin)) * 255)));
          const o = (j * M + i) * 4;
          d[o] = opts.lut[idx * 3];
          d[o + 1] = opts.lut[idx * 3 + 1];
          d[o + 2] = opts.lut[idx * 3 + 2];
          d[o + 3] = Math.round(255 * a);
        }
      }
    }
    this.mrmsCtx.putImageData(this.img, 0, 0);
    const c = this.outCtx;
    c.globalCompositeOperation = 'source-over';
    c.drawImage(this.base, 0, 0);
    c.imageSmoothingEnabled = true;
    c.imageSmoothingQuality = 'high';
    if (this.paint(this.underCtx, opts.under ?? null)) c.drawImage(this.under, 0, 0, spec.size, spec.size);
    c.drawImage(this.mrms, 0, 0, spec.size, spec.size);
    if (this.paint(this.overCtx, opts.over ?? null)) c.drawImage(this.over, 0, 0, spec.size, spec.size);
    return c.getImageData(0, 0, spec.size, spec.size).data;
  }
}
