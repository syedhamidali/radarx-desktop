/**
 * Noise handling for the RHI view: pure functions on ray x gate grids (no DOM, no Svelte).
 *
 * Raw RHI data is mostly noise above the echo (CPOL: half of all gates), so the view cleans it by
 * default and says so in a caption. What can be applied, in this order:
 *
 *  1. quality masks: hide gates whose SQI (or NCP) / SNR / RHOHV is below a limit, using the
 *     companion variables of the same sweep when the file has them;
 *  2. the per-variable value mask (stores/valueMask: "hide below 0 dBZ");
 *  3. despeckle: remove isolated gates and tiny connected groups.
 *
 * Smoothing (interpolation, NaN-aware Gaussian) is a drawing step and lives in crossSectionRaster.ts.
 * "Raw data" switches everything off. Hidden gates become the no-data sentinel (-9999), like the
 * wire format, so the rasteriser and the hover lookup treat them as missing.
 */
import { maskDescription, maskTest, isMaskActive, type ValueMask } from '../stores/valueMask.ts';
import { smoothingLabel, normalizeSmoothing, type Smoothing } from './crossSectionRaster.ts';

export const NODATA = -9999;
const NODATA_LIMIT = -9998;

// ─── Settings ───────────────────────────────────────────────────────────────

export type QualityKind = 'sqi' | 'snr' | 'rhohv';

export interface Threshold {
  on: boolean;
  /** Gates with a quality value below this are hidden. */
  min: number;
}

export interface RhiNoiseSettings {
  /** Show the data exactly as measured: no masking, no despeckling, no smoothing. */
  raw: boolean;
  sqi: Threshold;
  snr: Threshold;
  rhohv: Threshold;
  despeckle: { on: boolean; minGates: number };
  smoothing: Smoothing;
}

/** What the first picture uses: clean, lightly smoothed. */
export const DEFAULT_NOISE: RhiNoiseSettings = {
  raw: false,
  sqi: { on: true, min: 0.4 },
  snr: { on: true, min: 3 },
  rhohv: { on: true, min: 0.7 },
  despeckle: { on: true, minGates: 8 },
  smoothing: { interp: 'bilinear', sigma: 1, upsample: 1 },
};

export const QUALITY_LABEL: Record<QualityKind, string> = { sqi: 'SQI', snr: 'SNR', rhohv: 'RHOHV' };
export const QUALITY_UNIT: Record<QualityKind, string> = { sqi: '', snr: ' dB', rhohv: '' };
export const QUALITY_STEP: Record<QualityKind, number> = { sqi: 0.05, snr: 1, rhohv: 0.05 };

const num = (v: unknown, fallback: number, lo: number, hi: number): number => {
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : fallback;
};

/** Validate settings read from storage or a form; anything unusable falls back to the default. */
export function normalizeNoise(x: unknown): RhiNoiseSettings {
  const o = (x && typeof x === 'object' ? x : {}) as Record<string, any>;
  const th = (k: QualityKind, lo: number, hi: number): Threshold => {
    const t = (o[k] && typeof o[k] === 'object' ? o[k] : {}) as Record<string, unknown>;
    return {
      on: typeof t.on === 'boolean' ? t.on : DEFAULT_NOISE[k].on,
      min: num(t.min, DEFAULT_NOISE[k].min, lo, hi),
    };
  };
  const d = (o.despeckle && typeof o.despeckle === 'object' ? o.despeckle : {}) as Record<string, unknown>;
  return {
    raw: o.raw === true,
    sqi: th('sqi', 0, 1),
    snr: th('snr', -30, 60),
    rhohv: th('rhohv', 0, 1),
    despeckle: {
      on: typeof d.on === 'boolean' ? d.on : DEFAULT_NOISE.despeckle.on,
      minGates: Math.round(num(d.minGates, DEFAULT_NOISE.despeckle.minGates, 2, 200)),
    },
    smoothing: o.smoothing ? normalizeSmoothing(o.smoothing) : { ...DEFAULT_NOISE.smoothing },
  };
}

// ─── Companion variables ────────────────────────────────────────────────────

const PATTERNS: Record<QualityKind, RegExp> = {
  sqi: /^(sqi|ncp|signal_?quality|normalized_coherent_power)/i,
  snr: /^(snr|signal_to_noise)/i,
  rhohv: /(^|_)rhohv|^rho_?hv|cross_correlation|correlation_coeff/i,
};

/** Which kind of quality field a variable name is, or null. */
export function qualityKind(name: string): QualityKind | null {
  for (const k of ['sqi', 'snr', 'rhohv'] as QualityKind[]) if (PATTERNS[k].test(name)) return k;
  return null;
}

/** The variable of the sweep to use for each quality kind (first match; absent = not in the sweep). */
export function findCompanions(variables: string[]): Partial<Record<QualityKind, string>> {
  const out: Partial<Record<QualityKind, string>> = {};
  for (const v of variables) {
    const k = qualityKind(v);
    if (k && !out[k]) out[k] = v;
  }
  return out;
}

// ─── Cleaning ───────────────────────────────────────────────────────────────

export interface CleanInput {
  /** nRays x nRange, row-major; no-data is anything <= -9998 or NaN. Not modified. */
  values: ArrayLike<number>;
  nRays: number;
  nRange: number;
  /** Quality fields on the same grid, with the name of the variable each came from. */
  quality: Partial<Record<QualityKind, { name: string; values: ArrayLike<number> }>>;
  /** Value mask of the displayed variable, if any. */
  mask?: ValueMask | null;
}

export interface CleanResult {
  values: Float32Array;
  /** What was applied, in order, as short phrases ("SQI ≥ 0.4", "despeckled (< 8 gates)"). */
  applied: string[];
  /** Quality kinds that were switched on but have no field in this sweep. */
  missing: QualityKind[];
  /** How many quality masks (SQI / SNR / RHOHV) were applied. */
  qualityApplied: number;
  validBefore: number;
  validAfter: number;
}

const isValid = (v: number) => v > NODATA_LIMIT;

/**
 * Remove connected groups (8-neighbourhood in ray/gate space) of fewer than `minGates` valid gates.
 * Operates in place on `values`; returns the number of gates removed.
 */
export function despeckle(values: Float32Array, nRays: number, nRange: number, minGates: number): number {
  const n = nRays * nRange;
  const seen = new Uint8Array(n);
  const stack = new Int32Array(n);
  const group = new Int32Array(Math.max(1, Math.min(n, minGates)));
  let removed = 0;
  for (let start = 0; start < n; start++) {
    if (seen[start] || !isValid(values[start])) continue;
    // flood fill; remember only the first `minGates` members (enough to blank a small group)
    let sp = 0;
    let size = 0;
    stack[sp++] = start;
    seen[start] = 1;
    while (sp > 0) {
      const i = stack[--sp];
      if (size < group.length) group[size] = i;
      size++;
      const r = (i / nRange) | 0;
      const g = i - r * nRange;
      for (let dr = -1; dr <= 1; dr++) {
        const rr = r + dr;
        if (rr < 0 || rr >= nRays) continue;
        for (let dg = -1; dg <= 1; dg++) {
          if (dr === 0 && dg === 0) continue;
          const gg = g + dg;
          if (gg < 0 || gg >= nRange) continue;
          const j = rr * nRange + gg;
          if (seen[j] || !isValid(values[j])) continue;
          seen[j] = 1;
          stack[sp++] = j;
        }
      }
    }
    if (size < minGates) {
      for (let k = 0; k < size; k++) values[group[k]] = NODATA;
      removed += size;
    }
  }
  return removed;
}

/**
 * The settings as the engine's `rhi_clean` export option (see engine/rhi.py `parse_clean`), so a
 * saved figure of an RHI is cleaned the way the viewer shows it. Smoothing is a drawing step of the
 * viewer and is not part of it: exported figures show the cleaned gates as measured.
 */
export function engineCleanOptions(s: RhiNoiseSettings, mask?: ValueMask | null): Record<string, unknown> {
  if (s.raw) return { raw: true };
  return {
    sqi: s.sqi.on ? s.sqi.min : null,
    snr: s.snr.on ? s.snr.min : null,
    rhohv: s.rhohv.on ? s.rhohv.min : null,
    despeckle: s.despeckle.on ? s.despeckle.minGates : 0,
    mask: mask && isMaskActive(mask) ? mask : null,
  };
}

const fmt = (v: number) => String(Number(v.toFixed(3)));

/** Apply the noise settings to a sweep. `raw` returns a plain copy and an empty `applied`. */
export function cleanField(input: CleanInput, s: RhiNoiseSettings, units = ''): CleanResult {
  const { nRays, nRange } = input;
  const n = nRays * nRange;
  const out = new Float32Array(n);
  let validBefore = 0;
  for (let i = 0; i < n; i++) {
    const v = input.values[i];
    if (isValid(v)) {
      out[i] = v;
      validBefore++;
    } else out[i] = NODATA;
  }
  const result: CleanResult = {
    values: out,
    applied: [],
    missing: [],
    qualityApplied: 0,
    validBefore,
    validAfter: validBefore,
  };
  if (s.raw) return result;

  for (const kind of ['sqi', 'snr', 'rhohv'] as QualityKind[]) {
    const t = s[kind];
    if (!t.on) continue;
    const q = input.quality[kind];
    if (!q || q.values.length < n) {
      result.missing.push(kind);
      continue;
    }
    const qv = q.values;
    for (let i = 0; i < n; i++) {
      // a gate without a quality value is kept: nothing says it is bad
      if (out[i] > NODATA_LIMIT && qv[i] > NODATA_LIMIT && qv[i] < t.min) out[i] = NODATA;
    }
    result.applied.push(`${q.name} ≥ ${fmt(t.min)}${QUALITY_UNIT[kind]}`);
    result.qualityApplied++;
  }

  if (input.mask && isMaskActive(input.mask)) {
    for (let i = 0; i < n; i++) if (out[i] > NODATA_LIMIT && maskTest(out[i], input.mask)) out[i] = NODATA;
    result.applied.push(maskDescription(input.mask, units) ?? 'value mask');
  }

  if (s.despeckle.on) {
    despeckle(out, nRays, nRange, s.despeckle.minGates);
    result.applied.push(`despeckled (< ${s.despeckle.minGates} gates)`);
  }

  let after = 0;
  for (let i = 0; i < n; i++) if (out[i] > NODATA_LIMIT) after++;
  result.validAfter = after;
  return result;
}

/**
 * One-line caption of what the picture shows, for the viewer and the exported image:
 * "Cleaned: SQI ≥ 0.4, despeckled (< 8 gates) · hid 47% of gates · Smoothed: bilinear ..." or
 * "Raw data: no masking, despeckling or smoothing."
 */
export function noiseCaption(
  s: RhiNoiseSettings,
  r: Pick<CleanResult, 'applied' | 'missing' | 'qualityApplied' | 'validBefore' | 'validAfter'> | null,
  gateKm?: number | null,
): string {
  if (s.raw) return 'Raw data: no masking, despeckling or smoothing.';
  const parts: string[] = [];
  if (r && r.applied.length) {
    let text = `Cleaned: ${r.applied.join(', ')}`;
    const pct = r.validBefore > 0 ? Math.round((100 * (r.validBefore - r.validAfter)) / r.validBefore) : 0;
    if (pct > 0) text += ` (hid ${pct}% of gates)`;
    parts.push(text);
  }
  const smooth = smoothingLabel(s.smoothing, gateKm);
  if (smooth) parts.push(smooth);
  if (r && r.qualityApplied === 0 && r.missing.length) {
    parts.push(`no ${r.missing.map((k) => QUALITY_LABEL[k]).join('/')} field in this sweep`);
  }
  return parts.length ? parts.join('  ·  ') : 'No masking or smoothing applied.';
}
