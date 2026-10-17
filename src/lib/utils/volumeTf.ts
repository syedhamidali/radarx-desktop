/**
 * Default transfer functions for the 3D volume views.
 *
 * A transfer function (TF) is a 256-entry RGBA table over [vmin, vmax]. The colours always come
 * from the variable's colormap (the same one the 2D view uses); what differs between variables is
 * the OPACITY curve, because a volume is only readable when the weak/uninteresting part of the
 * data is transparent and the strong part is clearly the most visible:
 *
 *  - reflectivity: transparent below ~10-15 dBZ, opacity rising steeply with dBZ so convective
 *    cores (> 40 dBZ) dominate and light rain is only a faint hint;
 *  - velocity: transparent near zero, strong at large |V| (the signal is the magnitude);
 *  - anything else: transparent at the low end, rising towards the top of the range;
 *  - categorical: every class equally visible (flat alpha).
 *
 * The same module defines the "Hide below X" threshold semantics and the presets of the editor.
 */

export type VolumeVarKind = 'reflectivity' | 'velocity' | 'categorical' | 'other';

/** Classify a variable by name (the same rules the 2D view uses to pick a colormap). */
export function variableKind(variable: string | null | undefined, categorical = false): VolumeVarKind {
  if (categorical) return 'categorical';
  const v = (variable ?? '').toLowerCase();
  if (!v) return 'other';
  if (v.includes('dbz') || v.includes('reflectivity') || v.includes('total_power') || v === 'refl' || v === 'ref') {
    return 'reflectivity';
  }
  if (v.includes('vel') || v.includes('vrad')) return 'velocity';
  return 'other';
}

const smoothstep = (a: number, b: number, x: number) => {
  const t = Math.max(0, Math.min(1, (x - a) / (b - a || 1)));
  return t * t * (3 - 2 * t);
};

/** Piecewise-linear lookup over sorted (x, y) knots; clamps outside. */
function pwl(knots: ReadonlyArray<readonly [number, number]>, x: number): number {
  if (x <= knots[0][0]) return knots[0][1];
  for (let i = 1; i < knots.length; i++) {
    if (x <= knots[i][0]) {
      const [x0, y0] = knots[i - 1];
      const [x1, y1] = knots[i];
      return y0 + ((x - x0) / (x1 - x0 || 1)) * (y1 - y0);
    }
  }
  return knots[knots.length - 1][1];
}

/** Reflectivity opacity per ray step (dBZ -> 0..1). Weak echoes stay a faint hint; cores dominate. */
const REFL_KNOTS: ReadonlyArray<readonly [number, number]> = [
  [5, 0], [10, 0.001], [15, 0.003], [20, 0.005], [25, 0.01], [30, 0.02], [35, 0.04],
  [40, 0.09], [45, 0.22], [50, 0.45], [55, 0.75], [60, 0.93], [75, 1],
];
/** The 'Cores' preset: nothing below 30 dBZ, only the strong convective cores. */
const CORE_KNOTS: ReadonlyArray<readonly [number, number]> = [
  [28, 0], [34, 0.015], [40, 0.12], [45, 0.38], [50, 0.7], [55, 0.92], [60, 1], [75, 1],
];

export type TfPreset = 'default' | 'cores' | 'full' | 'transparent-core';

/** Opacity (0..1) of normalised value t in [0,1] for `kind` over [vmin, vmax]. */
function alphaAt(kind: VolumeVarKind, t: number, vmin: number, vmax: number, preset: TfPreset, shift: number): number {
  const phys = vmin + t * (vmax - vmin);
  if (preset === 'full') return t < 0.04 ? 0 : 1;
  if (preset === 'transparent-core') return t < 0.04 ? 0 : 1 - (t - 0.04) / 0.96;
  switch (kind) {
    case 'categorical':
      return 0.85;
    case 'reflectivity':
      // `shift` slides the opacity curve towards weaker values for weak scenes (see adaptiveShiftDb)
      return pwl(preset === 'cores' ? CORE_KNOTS : REFL_KNOTS, phys + shift);
    case 'velocity': {
      const m = Math.abs(t * 2 - 1); // 0 at zero velocity, 1 at +-vmax
      if (preset === 'cores') return 0.9 * smoothstep(0.45, 0.85, m);
      return 0.7 * smoothstep(0.08, 0.7, m);
    }
    default:
      if (preset === 'cores') return 0.85 * smoothstep(0.55, 0.95, t);
      return 0.55 * Math.pow(smoothstep(0.08, 0.95, t), 1.6);
  }
}

/** 256 opacity values (0..255) for the given kind / preset. */
export function alphaCurve(kind: VolumeVarKind, vmin: number, vmax: number, preset: TfPreset = 'default', shift = 0): Uint8Array {
  const a = new Uint8Array(256);
  for (let i = 0; i < 256; i++) a[i] = Math.round(Math.max(0, Math.min(1, alphaAt(kind, i / 255, vmin, vmax, preset, shift))) * 255);
  return a;
}

/**
 * How far (dB) to slide the reflectivity opacity curve towards weaker echoes so that a weak scene
 * (light rain: the strongest 0.5% of gates is only ~25 dBZ) still shows its structure, and a
 * little towards stronger ones for intense convection (p99.5 above ~48 dBZ), so that in a big
 * storm the cores stand out from the surrounding rain. `p995` = 99.5th percentile of reflectivity.
 */
export function adaptiveShiftDb(p995: number): number {
  return Math.max(-6, Math.min(18, Math.round(48 - p995)));
}

/** RGBA table: colours from `rgb` (256x3, a colormap), opacity from the default curve of `kind`. */
export function buildTf(
  kind: VolumeVarKind,
  rgb: ArrayLike<number>,
  vmin: number,
  vmax: number,
  preset: TfPreset = 'default',
  shift = 0,
): Uint8Array {
  const alpha = alphaCurve(kind, vmin, vmax, preset, shift);
  const tf = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    tf[i * 4] = rgb[i * 3];
    tf[i * 4 + 1] = rgb[i * 3 + 1];
    tf[i * 4 + 2] = rgb[i * 3 + 2];
    tf[i * 4 + 3] = alpha[i];
  }
  return tf;
}

/** Same colours as `tf`, opacity replaced by a preset curve. */
export function withPresetAlpha(tf: Uint8Array, kind: VolumeVarKind, vmin: number, vmax: number, preset: TfPreset, shift = 0): Uint8Array {
  const out = new Uint8Array(tf);
  const a = alphaCurve(kind, vmin, vmax, preset, shift);
  for (let i = 0; i < 256; i++) out[i * 4 + 3] = a[i];
  return out;
}

/**
 * Reduce a 256-sample opacity curve to a handful of draggable control points (for the editor).
 * Greedy: keep adding the sample that is furthest from the current piecewise-linear fit.
 */
export function curvePointsFromAlpha(alpha: ArrayLike<number>, maxPoints = 9, tol = 6): { x: number; y: number }[] {
  const n = 256;
  const keep = new Set<number>([0, n - 1]);
  for (let iter = 0; iter < maxPoints - 2; iter++) {
    const xs = [...keep].sort((a, b) => a - b);
    let worst = -1;
    let worstErr = tol;
    for (let k = 0; k < xs.length - 1; k++) {
      const x0 = xs[k];
      const x1 = xs[k + 1];
      for (let x = x0 + 1; x < x1; x++) {
        const fit = alpha[x0] + ((x - x0) / (x1 - x0)) * (alpha[x1] - alpha[x0]);
        const err = Math.abs(fit - alpha[x]);
        if (err > worstErr) { worstErr = err; worst = x; }
      }
    }
    if (worst < 0) break;
    keep.add(worst);
  }
  return [...keep].sort((a, b) => a - b).map((x) => ({ x, y: alpha[x] }));
}

// ─── "Hide below" threshold ────────────────────────────────────────────────

export interface ThresholdSpec {
  /** Slider bounds / step / default in the variable's own units. */
  min: number;
  max: number;
  step: number;
  value: number;
  /** Compared against |value| (velocity) instead of value. */
  symmetric: boolean;
  /** UI label, e.g. "Hide below" / "Hide |V| below". */
  label: string;
  unit: string;
}

export function thresholdSpec(kind: VolumeVarKind, vmin: number, vmax: number, units = ''): ThresholdSpec | null {
  const span = vmax - vmin;
  switch (kind) {
    case 'categorical':
      return null;
    case 'reflectivity':
      return { min: Math.max(vmin, -10), max: Math.min(vmax, 70), step: 1, value: 15, symmetric: false, label: 'Hide below', unit: units || 'dBZ' };
    case 'velocity': {
      const m = Math.max(Math.abs(vmin), Math.abs(vmax));
      return { min: 0, max: Math.round(m * 0.8), step: 1, value: Math.min(3, Math.round(m * 0.1)), symmetric: true, label: 'Hide |V| below', unit: units || 'm/s' };
    }
    default: {
      const step = span >= 20 ? 1 : span >= 2 ? 0.1 : 0.01;
      const dec = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
      return { min: vmin, max: vmax, step, value: +(vmin + span * 0.1).toFixed(dec), symmetric: false, label: 'Hide below', unit: units };
    }
  }
}

/** Threshold in normalised units for the shader: (value - vmin) / (vmax - vmin), or |v|/vmax for symmetric. */
export function thresholdToShader(spec: ThresholdSpec, thr: number, vmin: number, vmax: number): { thr: number; soft: number; mode: 0 | 1 } {
  if (spec.symmetric) {
    const m = Math.max(Math.abs(vmin), Math.abs(vmax)) || 1;
    return { thr: Math.max(0, thr / m), soft: Math.max(0.02, 1.5 / m), mode: 1 };
  }
  const span = vmax - vmin || 1;
  const soft = spec.unit === 'dBZ' ? 3 / span : 0.03;
  return { thr: Math.max(0, (thr - vmin) / span), soft, mode: 0 };
}

/**
 * Value (normalised 0..1 over [vmin, vmax]) above which a gate counts as a "significant" echo when
 * estimating how far out the storm extends. null = no meaningful echo-based range (velocity, ...).
 */
export function significantNorm(kind: VolumeVarKind, vmin: number, vmax: number): number | null {
  if (kind === 'reflectivity') return Math.max(0, Math.min(1, (30 - vmin) / (vmax - vmin || 1)));
  if (kind === 'other') return 0.5;
  return null;
}
