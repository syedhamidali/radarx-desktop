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
    c.drawImage(this.mrms, 0, 0, spec.size, spec.size);
    return c.getImageData(0, 0, spec.size, spec.size).data;
  }
}
