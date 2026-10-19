/**
 * Value masks: hide the values of a variable below / above / between / outside some limits
 * ("mask reflectivity below 0 dBZ"). One setting per variable name, remembered across sessions.
 *
 * This module is the shared piece: the pure helpers (`maskTest`, `maskDescription`, `maskPresets`)
 * work on plain numbers, so any view can apply a mask the same way, and the store keeps the
 * per-variable settings. The cross-section views use it today; the 2D / 3D views, mosaics and
 * exports can read the same settings later.
 *
 * Semantics: the mask says which values are HIDDEN. `min` / `max` are the lower / upper limit:
 *   below    hides  v < min          (inclusive: v <= min)
 *   above    hides  v > max          (inclusive: v >= max)
 *   between  hides  min < v < max    (inclusive: min <= v <= max)
 *   outside  hides  v < min or v > max (inclusive: v <= min or v >= max)
 * Missing values (NaN) are never "masked": they are simply missing already.
 */
import { writable, get } from 'svelte/store';

export type MaskMode = 'off' | 'below' | 'above' | 'between' | 'outside';

export interface ValueMask {
  mode: MaskMode;
  min: number | null;
  max: number | null;
  /** The limit values themselves are hidden too. */
  inclusive: boolean;
}

export const NO_MASK: ValueMask = { mode: 'off', min: null, max: null, inclusive: false };

export const MASK_MODES: { id: MaskMode; label: string }[] = [
  { id: 'off', label: 'Off' },
  { id: 'below', label: 'Below' },
  { id: 'above', label: 'Above' },
  { id: 'between', label: 'Between' },
  { id: 'outside', label: 'Outside' },
];

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

/** Validate a mask read from storage or a form; anything unusable becomes "off". */
export function normalizeMask(x: unknown): ValueMask {
  const o = (x && typeof x === 'object' ? x : {}) as Record<string, unknown>;
  const mode: MaskMode =
    o.mode === 'below' || o.mode === 'above' || o.mode === 'between' || o.mode === 'outside' ? o.mode : 'off';
  const num = (v: unknown): number | null => {
    if (v === null || v === '' || v === undefined) return null;
    const n = typeof v === 'number' ? v : Number(v);
    return Number.isFinite(n) ? n : null;
  };
  return { mode, min: num(o.min), max: num(o.max), inclusive: o.inclusive === true };
}

/** The limits a mode needs are all set. */
export function isMaskActive(mask: ValueMask | null | undefined): boolean {
  if (!mask) return false;
  switch (mask.mode) {
    case 'below':
      return finite(mask.min);
    case 'above':
      return finite(mask.max);
    case 'between':
    case 'outside':
      return finite(mask.min) && finite(mask.max);
    default:
      return false;
  }
}

/** True when `value` is HIDDEN by `mask`. NaN / null / undefined are never masked. */
export function maskTest(value: number | null | undefined, mask: ValueMask | null | undefined): boolean {
  if (!mask || !finite(value) || !isMaskActive(mask)) return false;
  const inc = mask.inclusive;
  switch (mask.mode) {
    case 'below':
      return inc ? value <= mask.min! : value < mask.min!;
    case 'above':
      return inc ? value >= mask.max! : value > mask.max!;
    case 'between': {
      const lo = Math.min(mask.min!, mask.max!);
      const hi = Math.max(mask.min!, mask.max!);
      return inc ? value >= lo && value <= hi : value > lo && value < hi;
    }
    case 'outside': {
      const lo = Math.min(mask.min!, mask.max!);
      const hi = Math.max(mask.min!, mask.max!);
      return inc ? value <= lo || value >= hi : value < lo || value > hi;
    }
    default:
      return false;
  }
}

/**
 * The same mask with its limits rounded to float32. Radar data is float32 (in files, textures and
 * the engine's arrays), and the engine and the shaders compare in float32, so a gate that holds
 * the float32 form of a limit (0.7 stored as 0.699999988) is treated alike everywhere.
 */
export function float32Mask(mask: ValueMask | null | undefined): ValueMask {
  if (!mask) return NO_MASK;
  return {
    ...mask,
    min: mask.min === null ? null : Math.fround(mask.min),
    max: mask.max === null ? null : Math.fround(mask.max),
  };
}

/**
 * A predicate for `mask` (null when it is off), for loops that test many values. Pass
 * `{ f32: true }` when the values come from float32 data (see `float32Mask`).
 */
export function maskPredicate(
  mask: ValueMask | null | undefined,
  opts: { f32?: boolean } = {},
): ((v: number) => boolean) | null {
  if (!isMaskActive(mask)) return null;
  const m = opts.f32 ? float32Mask(mask) : mask;
  return (v) => maskTest(v, m);
}

const fmtNum = (n: number): string => (Number.isInteger(n) ? String(n) : String(Number(n.toFixed(3))));

/** "hide below 0 dBZ" ... or null when the mask is off / incomplete. */
export function maskDescription(mask: ValueMask | null | undefined, units = ''): string | null {
  if (!mask || !isMaskActive(mask)) return null;
  const u = units ? ` ${units}` : '';
  const inc = mask.inclusive;
  switch (mask.mode) {
    case 'below':
      return inc ? `hide ${fmtNum(mask.min!)}${u} and below` : `hide below ${fmtNum(mask.min!)}${u}`;
    case 'above':
      return inc ? `hide ${fmtNum(mask.max!)}${u} and above` : `hide above ${fmtNum(mask.max!)}${u}`;
    case 'between': {
      const lo = Math.min(mask.min!, mask.max!);
      const hi = Math.max(mask.min!, mask.max!);
      return inc ? `hide ${fmtNum(lo)} to ${fmtNum(hi)}${u}` : `hide between ${fmtNum(lo)} and ${fmtNum(hi)}${u}`;
    }
    case 'outside': {
      const lo = Math.min(mask.min!, mask.max!);
      const hi = Math.max(mask.min!, mask.max!);
      return inc
        ? `hide outside ${fmtNum(lo)} to ${fmtNum(hi)}${u}, and those limits`
        : `hide outside ${fmtNum(lo)} to ${fmtNum(hi)}${u}`;
    }
    default:
      return null;
  }
}

// ---------------------------------------------------------------------------------------------
// Presets
// ---------------------------------------------------------------------------------------------

export interface MaskPreset {
  label: string;
  mask: ValueMask;
}

export type VariableKind = 'reflectivity' | 'velocity' | 'width' | 'zdr' | 'rhohv' | 'phidp' | 'kdp' | 'other';

/** What kind of moment a variable is, from its name (same spirit as the default colormaps). */
export function variableKind(variable: string): VariableKind {
  const v = (variable || '').toLowerCase();
  if (/zdr|differential_reflectivity/.test(v)) return 'zdr';
  if (/rhohv|correlation/.test(v)) return 'rhohv';
  if (/width|wrad|spec/.test(v)) return 'width';
  if (/(^|_)(dbz|dz|th|tv|zh|zv|refl)|reflectivity|power/.test(v)) return 'reflectivity';
  if (/vel|vrad|^vt$|^vr/.test(v)) return 'velocity';
  if (/phidp|phase|psi/.test(v)) return 'phidp';
  if (/kdp/.test(v)) return 'kdp';
  return 'other';
}

const m = (mode: MaskMode, min: number | null, max: number | null, inclusive = false): ValueMask => ({
  mode,
  min,
  max,
  inclusive,
});

/** One-click masks that make sense for this kind of variable. */
export function maskPresets(variable: string, units = ''): MaskPreset[] {
  const u = units ? ` ${units}` : '';
  switch (variableKind(variable)) {
    case 'reflectivity':
      return [
        { label: `< 0${u}`, mask: m('below', 0, null) },
        { label: `< 10${u}`, mask: m('below', 10, null) },
        { label: `< 20${u}`, mask: m('below', 20, null) },
      ];
    case 'velocity':
      return [
        { label: `|V| < 2${u}`, mask: m('between', -2, 2) },
        { label: `|V| < 5${u}`, mask: m('between', -5, 5) },
      ];
    case 'width':
      return [{ label: `> 8${u}`, mask: m('above', null, 8) }];
    case 'zdr':
      return [
        { label: `< -2${u}`, mask: m('below', -2, null) },
        { label: `outside -2 … 6${u}`, mask: m('outside', -2, 6) },
      ];
    case 'rhohv':
      return [
        { label: '< 0.7', mask: m('below', 0.7, null) },
        { label: '< 0.9', mask: m('below', 0.9, null) },
      ];
    case 'kdp':
      return [{ label: `< 0${u}`, mask: m('below', 0, null) }];
    case 'phidp':
      return [{ label: `< 0${u}`, mask: m('below', 0, null) }];
    default:
      return [];
  }
}

// ---------------------------------------------------------------------------------------------
// Store (per variable name, remembered)
// ---------------------------------------------------------------------------------------------

const KEY = 'xradar.valueMask.v1';

function load(): Record<string, ValueMask> {
  try {
    const raw = typeof localStorage === 'undefined' ? null : localStorage.getItem(KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    const out: Record<string, ValueMask> = {};
    if (parsed && typeof parsed === 'object') {
      for (const [name, mask] of Object.entries(parsed)) {
        const n = normalizeMask(mask);
        if (n.mode !== 'off' || n.min !== null || n.max !== null) out[name] = n;
      }
    }
    return out;
  } catch {
    return {};
  }
}

/** Mask settings by variable name (absent = off). */
export const valueMasks = writable<Record<string, ValueMask>>(load());

valueMasks.subscribe((all) => {
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(KEY, JSON.stringify(all));
  } catch {
    /* storage unavailable: ignore */
  }
});

/** The mask of `variable` in a snapshot of the store (`$valueMasks`). */
export function maskOf(all: Record<string, ValueMask>, variable: string | null | undefined): ValueMask {
  return (variable ? all[variable] : null) ?? NO_MASK;
}

export function getMask(variable: string): ValueMask {
  return maskOf(get(valueMasks), variable);
}

export function setMask(variable: string, patch: Partial<ValueMask>): void {
  if (!variable) return;
  valueMasks.update((all) => ({ ...all, [variable]: normalizeMask({ ...maskOf(all, variable), ...patch }) }));
}

export function resetMask(variable: string): void {
  valueMasks.update((all) => {
    const next = { ...all };
    delete next[variable];
    return next;
  });
}
