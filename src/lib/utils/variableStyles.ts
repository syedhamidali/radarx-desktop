/**
 * Display style registry for variables that are not part of the radar file itself
 * (derived variables produced by plugin modules).
 *
 * `getDefaultCmap` / `getDefaultRange` (ppiRenderer.ts) consult this registry first, so
 * a registered name renders correctly in the 2D and 3D viewers without any per-viewer code.
 *
 * Categorical variables get a discrete 256-entry colour table (each category a solid band),
 * the same mechanism module fields use when they are shown on the PPI.
 */

import { writable } from 'svelte/store';
import { COLORMAP_DATA } from './colormaps';

export interface FieldCategory {
  value: number;
  label: string;
  color: string;
}

export interface VariableStyleInput {
  label?: string | null;
  units?: string | null;
  vmin?: number | null;
  vmax?: number | null;
  cmap?: string | null;
  categories?: FieldCategory[] | null;
}

export interface VariableStyle {
  name: string;
  label: string;
  units: string;
  vmin: number;
  vmax: number;
  /** Name of the colour table to draw with (always present in COLORMAP_DATA). */
  cmap: string;
  /** Value-sorted categories, or null for a continuous variable. */
  categories: FieldCategory[] | null;
}

const styles = new Map<string, VariableStyle>();

/** Bumped whenever a style is (re)registered, so views can refresh. */
export const variableStylesRevision = writable(0);

// ── Colour helpers (shared with modules.ts) ──────────────────────────────────

let colorCtx: CanvasRenderingContext2D | null | undefined;

/** Parse '#rgb' / '#rrggbb' / any CSS colour into [r,g,b]; mid-grey when unparseable. */
export function parseColor(color: string): [number, number, number] {
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color.trim());
  if (hex) {
    let h = hex[1];
    if (h.length === 3) h = h.split('').map((c) => c + c).join('');
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  try {
    if (colorCtx === undefined) colorCtx = document.createElement('canvas').getContext('2d');
    if (colorCtx) {
      colorCtx.fillStyle = '#010203';
      colorCtx.fillStyle = color;
      const v = colorCtx.fillStyle;
      const m = /^#([0-9a-f]{6})$/i.exec(v);
      if (m) return parseColor(`#${m[1]}`);
    }
  } catch {
    /* no DOM: fall through */
  }
  return [136, 136, 136];
}

/** Valid, value-sorted categories (or null if the list is empty / not categorical). */
export function cleanCategories(cats: FieldCategory[] | null | undefined): FieldCategory[] | null {
  if (!Array.isArray(cats)) return null;
  const out = cats
    .filter((c) => c && Number.isFinite(Number(c.value)))
    .map((c) => ({
      value: Number(c.value),
      label: String(c.label ?? c.value),
      color: String(c.color ?? '#888888'),
    }))
    .sort((a, b) => a.value - b.value);
  return out.length > 0 ? out : null;
}

/**
 * Register (or refresh) a 256-entry discrete colormap for categorical values.
 * The value axis is vmin = first-0.5 .. vmax = last+0.5, so each category is a solid band.
 */
export function registerCategoricalCmap(
  lutName: string,
  cats: FieldCategory[],
): { vmin: number; vmax: number } {
  const vmin = cats[0].value - 0.5;
  const vmax = cats[cats.length - 1].value + 0.5;
  const span = vmax - vmin;
  const rgb = cats.map((c) => parseColor(c.color));
  const lut = new Uint8Array(256 * 3);
  for (let i = 0; i < 256; i++) {
    const v = vmin + ((i + 0.5) / 256) * span;
    let best = -1;
    let bestD = Infinity;
    for (let k = 0; k < cats.length; k++) {
      const d = Math.abs(v - cats[k].value);
      if (d < bestD) {
        bestD = d;
        best = k;
      }
    }
    const c: [number, number, number] = bestD <= 0.5 + 1e-9 ? rgb[best] : [60, 60, 60];
    lut[i * 3] = c[0];
    lut[i * 3 + 1] = c[1];
    lut[i * 3 + 2] = c[2];
  }
  COLORMAP_DATA[lutName] = lut; // new array each time => the renderer re-uploads it
  return { vmin, vmax };
}

/** Colormap guess from units when the producer named none (kept free of ppiRenderer: no import cycle). */
function cmapFromUnits(units: string): string {
  const u = (units ?? '').trim().toLowerCase();
  if (u === 'dbz') return 'NWSRef';
  if (u === 'm/s' || u === 'm s-1' || u === 'ms-1' || u === 'm s^-1') return 'NWSVel';
  if (u === 'db') return 'Spectral_r';
  if (u === 'deg/km' || u === '°/km' || u === 'degrees/km') return 'YlGnBu';
  return 'turbo';
}

// ── Registry ─────────────────────────────────────────────────────────────────

/** Register (or replace) the display style of a variable name. Returns the resolved style. */
export function registerVariableStyle(name: string, input: VariableStyleInput): VariableStyle {
  const units = typeof input.units === 'string' ? input.units : '';
  const cats = cleanCategories(input.categories);
  let style: VariableStyle;
  if (cats) {
    const lut = `__derived_cat__${name}`;
    const r = registerCategoricalCmap(lut, cats);
    style = { name, label: input.label || name, units, vmin: r.vmin, vmax: r.vmax, cmap: lut, categories: cats };
  } else {
    let vmin = typeof input.vmin === 'number' && Number.isFinite(input.vmin) ? input.vmin : 0;
    let vmax = typeof input.vmax === 'number' && Number.isFinite(input.vmax) ? input.vmax : vmin + 1;
    if (vmax <= vmin) vmax = vmin + 1;
    const cmap = input.cmap && COLORMAP_DATA[input.cmap] ? input.cmap : cmapFromUnits(units);
    style = {
      name,
      label: input.label || name,
      units,
      vmin,
      vmax,
      cmap: COLORMAP_DATA[cmap] ? cmap : 'turbo',
      categories: null,
    };
  }
  styles.set(name, style);
  variableStylesRevision.update((n) => n + 1);
  return style;
}

/** Drop a registered style (the colour table of a categorical one is released too). */
export function unregisterVariableStyle(name: string): void {
  const s = styles.get(name);
  if (!s) return;
  styles.delete(name);
  if (s.categories) delete COLORMAP_DATA[s.cmap];
  variableStylesRevision.update((n) => n + 1);
}

/** The registered style of `name`, or undefined for ordinary file variables. */
export function styleFor(name: string | null | undefined): VariableStyle | undefined {
  return name ? styles.get(name) : undefined;
}

/** Human label of a variable: the registered label, or the name itself. */
export function labelFor(name: string | null | undefined): string {
  return (name && styles.get(name)?.label) || name || '';
}
