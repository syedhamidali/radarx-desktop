/**
 * Custom colormap store — persists user-created colormaps in localStorage.
 */
import { writable, get } from 'svelte/store';
import { registerColormap, unregisterColormap, COLORMAP_DATA } from '../utils/colormaps';

/** Names of the built-in colormaps (snapshot taken before any custom map is registered). */
const BUILTIN_NAMES = new Set(Object.keys(COLORMAP_DATA));

/** True if `name` belongs to a built-in colormap that a custom map must not replace. */
export function isBuiltinColormapName(name: string): boolean {
  return BUILTIN_NAMES.has(name);
}

// ─── Types ─────────────────────────────────────────────────────────────

export interface ColorStop {
  position: number; // 0–1
  color: [number, number, number]; // [r, g, b] 0–255
}

export interface CustomColormap {
  name: string;
  stops: ColorStop[];
  isReversed: boolean;
  isDiscrete: boolean;
  discreteSteps: number; // only used when isDiscrete=true
}

// ─── Interpolation ─────────────────────────────────────────────────────

/** Interpolate color stops into a 256x3 Uint8Array. */
export function interpolateColormap(
  stops: ColorStop[],
  numSteps: number = 256,
  isReversed: boolean = false,
  isDiscrete: boolean = false,
  discreteSteps: number = 16,
): Uint8Array {
  const sorted = [...stops].sort((a, b) => a.position - b.position);
  if (sorted.length === 0) return new Uint8Array(numSteps * 3);
  if (sorted.length === 1) {
    const arr = new Uint8Array(numSteps * 3);
    for (let i = 0; i < numSteps; i++) {
      arr[i * 3] = sorted[0].color[0];
      arr[i * 3 + 1] = sorted[0].color[1];
      arr[i * 3 + 2] = sorted[0].color[2];
    }
    return arr;
  }

  const arr = new Uint8Array(numSteps * 3);
  for (let i = 0; i < numSteps; i++) {
    let t = i / (numSteps - 1);
    if (isReversed) t = 1 - t;
    if (isDiscrete && discreteSteps > 1) {
      t = Math.floor(t * discreteSteps) / (discreteSteps - 1);
      t = Math.min(1, t);
    }

    // Find surrounding stops
    let lo = sorted[0];
    let hi = sorted[sorted.length - 1];
    for (let j = 0; j < sorted.length - 1; j++) {
      if (t >= sorted[j].position && t <= sorted[j + 1].position) {
        lo = sorted[j];
        hi = sorted[j + 1];
        break;
      }
    }

    const range = hi.position - lo.position;
    const segT = range > 0 ? (t - lo.position) / range : 0;
    const clamped = Math.max(0, Math.min(1, segT));

    arr[i * 3] = Math.round(lo.color[0] + (hi.color[0] - lo.color[0]) * clamped);
    arr[i * 3 + 1] = Math.round(lo.color[1] + (hi.color[1] - lo.color[1]) * clamped);
    arr[i * 3 + 2] = Math.round(lo.color[2] + (hi.color[2] - lo.color[2]) * clamped);
  }
  return arr;
}

// ─── Persistence ───────────────────────────────────────────────────────

const STORAGE_KEY = 'xradar-custom-colormaps';

/** Validate/normalise untrusted colormap JSON (import or localStorage); null if unusable. */
function sanitizeColormap(parsed: any): CustomColormap | null {
  if (!parsed || typeof parsed.name !== 'string' || !parsed.name.trim()) return null;
  if (!Array.isArray(parsed.stops) || parsed.stops.length < 2) return null;
  const stops: ColorStop[] = [];
  for (const s of parsed.stops) {
    const pos = Number(s?.position);
    const col = Array.isArray(s?.color) ? s.color.slice(0, 3).map(Number) : [];
    if (!Number.isFinite(pos) || col.length !== 3 || col.some((v: number) => !Number.isFinite(v))) return null;
    stops.push({
      position: Math.max(0, Math.min(1, pos)),
      color: col.map((v: number) => Math.max(0, Math.min(255, Math.round(v)))) as [number, number, number],
    });
  }
  const steps = Math.round(Number(parsed.discreteSteps));
  return {
    name: parsed.name.trim().slice(0, 48),
    stops,
    isReversed: !!parsed.isReversed,
    isDiscrete: !!parsed.isDiscrete,
    discreteSteps: Number.isFinite(steps) ? Math.max(2, Math.min(256, steps)) : 16,
  };
}

function loadFromStorage(): CustomColormap[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        // Drop corrupt entries and any that shadow a built-in colormap.
        return parsed
          .map(sanitizeColormap)
          .filter((c): c is CustomColormap => !!c && !BUILTIN_NAMES.has(c.name));
      }
    }
  } catch { /* ignore */ }
  return [];
}

function saveToStorage(cmaps: CustomColormap[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(cmaps));
  } catch { /* ignore */ }
}

// ─── Store ─────────────────────────────────────────────────────────────

const initial = loadFromStorage();
export const customColormaps = writable<CustomColormap[]>(initial);

// Register all loaded custom colormaps into COLORMAP_DATA on init
initial.forEach((cm) => {
  const rgb = interpolateColormap(cm.stops, 256, cm.isReversed, cm.isDiscrete, cm.discreteSteps);
  registerColormap(cm.name, rgb);
});

/**
 * Add or update a custom colormap. Returns false (and changes nothing) if the name is empty or
 * belongs to a built-in colormap, which would otherwise be overwritten (and lost on delete).
 */
export function saveCustomColormap(cm: CustomColormap): boolean {
  if (!cm.name.trim() || BUILTIN_NAMES.has(cm.name)) return false;
  const rgb = interpolateColormap(cm.stops, 256, cm.isReversed, cm.isDiscrete, cm.discreteSteps);
  registerColormap(cm.name, rgb);

  customColormaps.update((list) => {
    const idx = list.findIndex((c) => c.name === cm.name);
    const next = [...list];
    if (idx >= 0) {
      next[idx] = cm;
    } else {
      next.push(cm);
    }
    saveToStorage(next);
    return next;
  });
  return true;
}

/** Delete a custom colormap by name. */
export function deleteCustomColormap(name: string): void {
  if (BUILTIN_NAMES.has(name)) return;
  unregisterColormap(name);
  customColormaps.update((list) => {
    const next = list.filter((c) => c.name !== name);
    saveToStorage(next);
    return next;
  });
}

/** Get a custom colormap by name. */
export function getCustomColormap(name: string): CustomColormap | undefined {
  return get(customColormaps).find((c) => c.name === name);
}

/** Export a custom colormap as a JSON string. */
export function exportColormapJSON(cm: CustomColormap): string {
  return JSON.stringify(cm, null, 2);
}

/** Import a colormap from a JSON string, returns null on error. */
export function importColormapJSON(json: string): CustomColormap | null {
  try {
    return sanitizeColormap(JSON.parse(json));
  } catch {
    return null;
  }
}
