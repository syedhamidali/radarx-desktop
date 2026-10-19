/**
 * The value mask (stores/valueMask) for the GPU: the same rules as `maskTest`, packed into one
 * `vec4` uniform so the 2D PPI, the polar 3D volume and the Cartesian (mosaic / wind) volumes hide
 * exactly the same values without re-uploading any data.
 *
 *   u = (mode, lower, upper, inclusive)       mode: 0 off, 1 below, 2 above, 3 between, 4 outside
 *
 * `below` reads only `lower`, `above` only `upper`; `between` / `outside` read both, in either
 * order. The limits are float32 (the shader's own precision, and the data's), which is also how
 * the engine compares, so all places agree on a value that is the float32 form of a limit.
 */
import { float32Mask, isMaskActive, type ValueMask } from '../stores/valueMask.ts';

export type MaskVec4 = [number, number, number, number];

export const MASK_OFF: MaskVec4 = [0, 0, 0, 0];

const CODE = { below: 1, above: 2, between: 3, outside: 4 } as const;

/** The mask in data units (a PPI shader compares gate values directly). */
export function maskVec(mask: ValueMask | null | undefined): MaskVec4 {
  if (!mask || !isMaskActive(mask)) return MASK_OFF;
  const m = float32Mask(mask);
  const code = CODE[m.mode as keyof typeof CODE];
  return [code, m.min ?? 0, m.max ?? 0, m.inclusive ? 1 : 0];
}

/**
 * The mask for a volume whose stored value is normalised over [vmin, vmax] (0..1): the limits are
 * mapped the same way. A limit outside the range still works for what it can: `below` a limit
 * above vmax hides everything, `below` a limit under vmin hides nothing. The volume does not hold
 * values outside [vmin, vmax], so a limit there cannot hide them.
 */
export function maskVecNorm(mask: ValueMask | null | undefined, vmin: number, vmax: number): MaskVec4 {
  if (!mask || !isMaskActive(mask) || !(vmax > vmin)) return MASK_OFF;
  const span = vmax - vmin;
  const norm = (x: number | null): number => (x === null ? 0 : Math.fround((x - vmin) / span));
  const code = CODE[mask.mode as keyof typeof CODE];
  return [code, norm(mask.min), norm(mask.max), mask.inclusive ? 1 : 0];
}

/** Same test as the shader below (and as `maskTest`), for tests and CPU paths: true = hidden. */
export function hiddenByVec(v: number, m: MaskVec4): boolean {
  const [mode, lo, hi, inc] = m;
  if (mode < 0.5 || !Number.isFinite(v)) return false;
  const i = inc > 0.5;
  if (mode < 1.5) return i ? v <= lo : v < lo;
  if (mode < 2.5) return i ? v >= hi : v > hi;
  const a = Math.min(lo, hi);
  const b = Math.max(lo, hi);
  if (mode < 3.5) return i ? v >= a && v <= b : v > a && v < b;
  return i ? v <= a || v >= b : v < a || v > b;
}

/** GLSL: `bool valueHidden(float v, vec4 m)` for the packed mask. Valid in GLSL ES 1.00 and 3.00. */
export const GLSL_VALUE_MASK = `
    // True when the value mask m = (mode, lower, upper, inclusive) hides v.
    bool valueHidden(float v, vec4 m) {
      if (m.x < 0.5) return false;
      bool inc = m.w > 0.5;
      if (m.x < 1.5) return inc ? (v <= m.y) : (v < m.y);
      if (m.x < 2.5) return inc ? (v >= m.z) : (v > m.z);
      float a = min(m.y, m.z);
      float b = max(m.y, m.z);
      if (m.x < 3.5) return inc ? (v >= a && v <= b) : (v > a && v < b);
      return inc ? (v <= a || v >= b) : (v < a || v > b);
    }
`;
