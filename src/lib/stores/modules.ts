/**
 * Plugin-module store.
 *
 * The Python sidecar auto-discovers analysis modules and describes them
 * declaratively (params, requirements, output blocks). This store holds the
 * module specs, per-module parameter values (persisted in localStorage),
 * last results, running state and overlay/selection state, and wires up the
 * WebSocket protocol:
 *
 *   -> {type:'list_modules', reload?}   <- {type:'modules_list', modules, errors, dirs}
 *   -> {type:'run_module', ...}         <- {type:'module_result', ...} | {type:'module_error', ...}
 *                                      <- {type:'module_field', ...} + binary frames (per field block)
 *   -> {type:'save_text_file', ...}     <- {type:'file_saved', path} | {type:'error', ...}
 */

import { writable, derived, get } from 'svelte/store';
import { wsManager, type ModuleFieldEntry } from '../utils/websocket';
import { beginOp, cancelOp, type OpHandle } from '../utils/requestScheduler';
import { COLORMAP_DATA } from '../utils/colormaps';
import { getDefaultCmap, type SweepDataEntry } from '../utils/ppiRenderer';
import { connectionStatus, radarData, selectedSweep, selectedVariable, processingProgress } from './radarData';
import { activeFileId, getActiveFile } from './fileManager';
import { addToast } from './toasts';
import {
  cleanCategories,
  parseColor,
  registerCategoricalCmap,
  type FieldCategory,
} from '../utils/variableStyles';
import {
  derivedByFile,
  activeDerived,
  clearDerived,
  dropDerivedCaches,
  normaliseDerived,
  refreshIfSelected,
  setDerivedForFile,
  type DerivedVariable,
} from './derived';

// Shared with the derived-variable registry (it builds the same discrete colour tables).
export { cleanCategories, parseColor, registerCategoricalCmap };

// ── Types ──────────────────────────────────────────────────────────────────

export type ParamType = 'number' | 'int' | 'bool' | 'select' | 'string';

export interface ModuleParam {
  name: string;
  type: ParamType;
  default: any;
  label?: string;
  min?: number;
  max?: number;
  step?: number;
  options?: any[];
  help?: string;
}

export interface ModuleSpec {
  id: string;
  title: string;
  description: string;
  category: string;
  requires: string[];
  optional: string[];
  params: ModuleParam[];
  source?: string;
  version?: string;
  /** Optional pip packages the module imports (import names). */
  needs: string[];
  /** False when a needed package is missing: listed, but cannot run. */
  available: boolean;
  unavailable_reason: string | null;
  /** 'volume' modules analyse the whole volume in one run; 'sweep' modules work on one sweep. */
  scope: 'sweep' | 'volume';
  /** True when the module can be run on every sweep and its fields registered as derived variables. */
  per_sweep: boolean;
  /** Roles it can take from the paired same-elevation sweep (NEXRAD split cuts), e.g. ['VRADH']. */
  paired: string[];
}

export interface ModuleLoadError {
  file: string;
  error: string;
}

export interface TableColumn {
  key: string;
  label: string;
  unit?: string;
  digits?: number;
}

export type { FieldCategory };

/** A gridded product on the sweep's polar grid; the values arrive as a separate binary payload. */
export interface FieldBlock {
  type: 'field';
  name: string;
  title?: string;
  units: string;
  vmin: number | null;
  vmax: number | null;
  cmap: string | null;
  categories: FieldCategory[] | null;
  shape: [number, number];
  valid_fraction: number;
}

export type ModuleBlock =
  | FieldBlock
  | { type: 'scalars'; title?: string; items: { label: string; value: any; unit?: string }[] }
  | { type: 'table'; title?: string; columns: TableColumn[]; rows: Record<string, any>[]; id_key?: string }
  | { type: 'overlay'; title?: string; features: RawOverlayFeature[] }
  | { type: 'text'; title?: string; body: string }
  | { type: string; title?: string; [k: string]: any };

export interface RawOverlayFeature {
  id: string | number;
  kind: 'polygon' | 'point' | 'line';
  frame: 'polar' | 'xy';
  coords: [number, number][];
  label?: string | null;
  color?: string | null;
  fill?: number;
}

/** Overlay feature after validation/normalisation. */
export interface OverlayFeature {
  id: string;
  kind: 'polygon' | 'point' | 'line';
  frame: 'polar' | 'xy';
  coords: [number, number][];
  label: string | null;
  color: string | null;
  fill: number | null;
}

export interface ModuleResult {
  module: string;
  sweep: number;
  params: Record<string, any>;
  elapsed_ms: number;
  blocks: ModuleBlock[];
  /** All overlay features across the result's overlay blocks. */
  features: OverlayFeature[];
}

/** Outcome of a run on all sweeps (`module_volume_result`). */
export interface VolumeRunResult {
  module: string;
  fileId: string | null;
  params: Record<string, any>;
  elapsed_ms: number;
  /** The derived variables this run created. */
  derived: (DerivedVariable & { skipped: { sweep: number; reason: string }[] })[];
  /** Per-sweep scalar summaries the module reported. */
  summaries: { sweep: number; scalars: Record<string, any> }[];
  /** Older derived variables the server dropped to make room. */
  evicted: string[];
  at: number;
}

export interface ModulesState {
  modules: ModuleSpec[];
  errors: ModuleLoadError[];
  dirs: string[];
  loading: boolean;
  loaded: boolean;
}

export interface SelectedFeature {
  module: string;
  id: string;
}

export interface OverlayLayer {
  module: string;
  color: string;
  features: OverlayFeature[];
}

// ── Constants ──────────────────────────────────────────────────────────────

const PARAMS_STORAGE_KEY = 'xradar.modules.params.v1';
const RUN_TIMEOUT_MS = 120_000;
/** A run on every sweep takes many times a single run. */
const VOLUME_RUN_TIMEOUT_MS = 600_000;

/** Small categorical palette used for per-module default overlay colours. */
export const MODULE_PALETTE = [
  '#22d3ee',
  '#f472b6',
  '#a3e635',
  '#fbbf24',
  '#c084fc',
  '#fb923c',
  '#34d399',
  '#60a5fa',
];

// ── Stores ─────────────────────────────────────────────────────────────────

export const modulesState = writable<ModulesState>({
  modules: [],
  errors: [],
  dirs: [],
  loading: false,
  loaded: false,
});

/** Parameter values: moduleId -> paramName -> value. */
export const moduleParams = writable<Record<string, Record<string, any>>>({});

/** Last result per module id. */
export const moduleResults = writable<Record<string, ModuleResult>>({});

/** Module ids currently running. */
export const runningModules = writable<Record<string, boolean>>({});

/** Module ids whose current run covers all sweeps (a subset of runningModules). */
export const runningVolume = writable<Record<string, boolean>>({});

/** Last all-sweeps run per module id. */
export const moduleVolumeResults = writable<Record<string, VolumeRunResult>>({});

/** Overlay visibility per module id (missing = visible). */
export const overlayVisibility = writable<Record<string, boolean>>({});

/** Currently selected overlay feature (shared between panel and overlay). */
export const selectedFeature = writable<SelectedFeature | null>(null);

/** Gridded module products received so far, keyed by `module/name` (see {@link fieldKey}). */
export const moduleFields = writable<Record<string, ModuleFieldEntry>>({});

/** How a displayed module field is drawn (resolved colormap + range + legend). */
export interface FieldRenderSpec {
  cmap: string;
  vmin: number;
  vmax: number;
  units: string;
  categories: FieldCategory[] | null;
}

/** A module field currently replacing the PPI's variable (null = the normal variable is shown). */
export interface DisplayedField {
  module: string;
  moduleTitle: string;
  name: string;
  title: string;
  sweep: number;
  /** Stable renderer entry (identity matters: the renderer short-circuits on the same object). */
  entry: SweepDataEntry;
  spec: FieldRenderSpec;
}

export const displayedField = writable<DisplayedField | null>(null);

// ── Auto-show: a finished run puts its (first) gridded product on the map ─────────
const AUTO_SHOW_KEY = 'xradar.modules.autoShow.v1';
function loadAutoShow(): boolean {
  try {
    return localStorage.getItem(AUTO_SHOW_KEY) !== '0';
  } catch {
    return true;
  }
}
/** When on (default), a module's first field is drawn on the PPI as soon as it arrives. */
export const autoShowResults = writable<boolean>(loadAutoShow());
autoShowResults.subscribe((v) => {
  try {
    localStorage.setItem(AUTO_SHOW_KEY, v ? '1' : '0');
  } catch {
    /* storage unavailable — ignore */
  }
});
/** module id -> name of the field to display when its data arrives. */
const pendingAutoShow = new Map<string, string>();
/** Set when a run finishes so the panel can scroll that module's card into view. */
export const lastFinishedModule = writable<{ id: string; at: number } | null>(null);

/** Colour assigned to a module by its position in the module list. */
export const moduleColors = derived(modulesState, ($s) => {
  const out: Record<string, string> = {};
  $s.modules.forEach((m, i) => {
    out[m.id] = MODULE_PALETTE[i % MODULE_PALETTE.length];
  });
  return out;
});

/**
 * Overlay layers to draw: visible modules whose last result was computed for
 * the sweep currently displayed. Only recomputed when results, visibility,
 * colours or the displayed sweep change.
 */
export const overlayLayers = derived(
  [moduleResults, overlayVisibility, moduleColors, selectedSweep],
  ([$results, $vis, $colors, $sweep]) => {
    const layers: OverlayLayer[] = [];
    for (const [id, res] of Object.entries($results)) {
      if ($vis[id] === false) continue;
      if (res.sweep !== $sweep) continue;
      if (res.features.length === 0) continue;
      layers.push({ module: id, color: $colors[id] ?? MODULE_PALETTE[0], features: res.features });
    }
    return layers;
  },
);

// ── Parameter persistence ──────────────────────────────────────────────────

function loadSavedParams(): Record<string, Record<string, any>> {
  try {
    const raw = localStorage.getItem(PARAMS_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function saveParams(): void {
  try {
    localStorage.setItem(PARAMS_STORAGE_KEY, JSON.stringify(get(moduleParams)));
  } catch {
    /* storage unavailable — ignore */
  }
}

function optionValue(opt: any): any {
  return opt !== null && typeof opt === 'object' && 'value' in opt ? opt.value : opt;
}

/** Coerce a candidate value into something valid for the param, or fall back to its default. */
export function coerceParam(p: ModuleParam, v: any): any {
  switch (p.type) {
    case 'bool':
      return typeof v === 'boolean' ? v : Boolean(p.default);
    case 'string':
      return typeof v === 'string' ? v : String(p.default ?? '');
    case 'select': {
      const opts = (p.options ?? []).map(optionValue);
      if (opts.some((o) => o === v)) return v;
      return opts.some((o) => o === p.default) ? p.default : (opts[0] ?? p.default);
    }
    case 'int':
    case 'number': {
      let n = typeof v === 'number' ? v : Number.NaN;
      if (!Number.isFinite(n)) {
        n = Number(p.default);
        if (!Number.isFinite(n)) return p.default;
      }
      if (p.type === 'int') n = Math.round(n);
      if (typeof p.min === 'number' && n < p.min) n = p.min;
      if (typeof p.max === 'number' && n > p.max) n = p.max;
      return n;
    }
    default:
      return v ?? p.default;
  }
}

function defaultParams(spec: ModuleSpec, saved: Record<string, any> | undefined): Record<string, any> {
  const out: Record<string, any> = {};
  for (const p of spec.params ?? []) {
    out[p.name] = coerceParam(p, saved && p.name in saved ? saved[p.name] : p.default);
  }
  return out;
}

/** Set one parameter value (not clamped while typing; clamped on run). */
export function setParam(moduleId: string, name: string, value: any): void {
  moduleParams.update((all) => ({ ...all, [moduleId]: { ...(all[moduleId] ?? {}), [name]: value } }));
  saveParams();
}

/** Reset a module's parameters to the declared defaults. */
export function resetParams(moduleId: string): void {
  const spec = get(modulesState).modules.find((m) => m.id === moduleId);
  if (!spec) return;
  moduleParams.update((all) => ({ ...all, [moduleId]: defaultParams(spec, undefined) }));
  saveParams();
}

// ── Variable availability ──────────────────────────────────────────────────

export { canonicalVariable } from '../utils/variableAliases';
import { canonicalVariable } from '../utils/variableAliases';

/**
 * Variables available for the given sweep of the active file. Uses the sweep's
 * own variable list when the server provided one, else the file-level list.
 */
export function availableVariables(
  variables: string[],
  sweeps: { index: number; variables?: string[] }[],
  sweep: number,
): Set<string> {
  const sw = sweeps.find((s) => s.index === sweep);
  const list = sw?.variables && sw.variables.length > 0 ? sw.variables : variables;
  return new Set(list.map(canonicalVariable));
}

/** Required variables of a module that are not present. Empty if everything is available. */
export function missingRequirements(spec: ModuleSpec, available: Set<string>): string[] {
  return (spec.requires ?? []).filter((r) => !available.has(canonicalVariable(r)));
}

// ── Selection / overlay actions ────────────────────────────────────────────

export function selectFeature(moduleId: string, id: string | null): void {
  selectedFeature.update((cur) => {
    if (id === null) return null;
    return cur && cur.module === moduleId && cur.id === id ? null : { module: moduleId, id };
  });
}

export function setOverlayVisible(moduleId: string, visible: boolean): void {
  overlayVisibility.update((v) => ({ ...v, [moduleId]: visible }));
}

export function clearResult(moduleId: string): void {
  moduleResults.update((r) => {
    if (!(moduleId in r)) return r;
    const { [moduleId]: _drop, ...rest } = r;
    return rest;
  });
  if (get(selectedFeature)?.module === moduleId) selectedFeature.set(null);
  moduleFields.update((f) => {
    const prefix = `${moduleId}/`;
    const next: Record<string, ModuleFieldEntry> = {};
    for (const [k, v] of Object.entries(f)) if (!k.startsWith(prefix)) next[k] = v;
    return next;
  });
  if (get(displayedField)?.module === moduleId) displayedField.set(null);
}

// ── Gridded fields (Show on PPI) ───────────────────────────────────────────

export function fieldKey(moduleId: string, name: string): string {
  return `${moduleId}/${name}`;
}

function fieldBlocksOf(res: ModuleResult | undefined): FieldBlock[] {
  return ((res?.blocks ?? []) as ModuleBlock[]).filter(
    (b): b is FieldBlock => b?.type === 'field' && typeof (b as FieldBlock).name === 'string',
  );
}

export function findFieldBlock(moduleId: string, name: string): FieldBlock | null {
  return fieldBlocksOf(get(moduleResults)[moduleId]).find((b) => b.name === name) ?? null;
}

/** Sensible default colormap from the field's units / name when the module gave none. */
export function cmapFromUnits(units: string, name: string): string {
  const u = (units ?? '').trim().toLowerCase();
  let guess: string | null = null;
  if (u === 'dbz') guess = 'NWSRef';
  else if (u === 'm/s' || u === 'm s-1' || u === 'ms-1' || u === 'm s^-1') guess = 'NWSVel';
  else if (u === 'db') guess = 'Spectral_r';
  else if (u === 'deg/km' || u === '°/km' || u === 'degrees/km') guess = 'YlGnBu';
  else if (u === 'deg' || u === '°' || u === 'degrees') guess = 'plasma';
  if (!guess) guess = getDefaultCmap(name);
  return COLORMAP_DATA[guess] ? guess : 'turbo';
}

/** Name of the colormap used for a continuous field block. */
export function resolveFieldCmap(block: Pick<FieldBlock, 'cmap' | 'units' | 'name'>): string {
  if (block.cmap && COLORMAP_DATA[block.cmap]) return block.cmap;
  return cmapFromUnits(block.units, block.name);
}

/** CSS linear-gradient for a named colormap (legend bar). */
export function cmapGradientCSS(name: string): string {
  const rgb = COLORMAP_DATA[name];
  if (!rgb) return 'linear-gradient(to right, #000, #fff)';
  const stops: string[] = [];
  const n = 16;
  for (let i = 0; i < n; i++) {
    const idx = Math.round((i / (n - 1)) * 255);
    stops.push(`rgb(${rgb[idx * 3]},${rgb[idx * 3 + 1]},${rgb[idx * 3 + 2]}) ${((i / (n - 1)) * 100).toFixed(0)}%`);
  }
  return `linear-gradient(to right, ${stops.join(', ')})`;
}

function buildDisplayedField(
  moduleId: string,
  name: string,
  fieldEntry: ModuleFieldEntry,
): DisplayedField | null {
  const block = findFieldBlock(moduleId, name);
  if (!block) return null;
  const cats = cleanCategories(block.categories);
  let spec: FieldRenderSpec;
  if (cats) {
    const lut = `__module_cat__${moduleId}/${name}`;
    const r = registerCategoricalCmap(lut, cats);
    spec = { cmap: lut, vmin: r.vmin, vmax: r.vmax, units: block.units ?? '', categories: cats };
  } else {
    let vmin = typeof block.vmin === 'number' ? block.vmin : fieldEntry.vmin;
    let vmax = typeof block.vmax === 'number' ? block.vmax : fieldEntry.vmax;
    if (!Number.isFinite(vmin)) vmin = 0;
    if (!Number.isFinite(vmax) || vmax <= vmin) vmax = vmin + 1;
    spec = {
      cmap: resolveFieldCmap(block),
      vmin,
      vmax,
      units: block.units || fieldEntry.units || '',
      categories: null,
    };
  }
  const entry: SweepDataEntry = {
    variable: `module:${moduleId}/${name}`,
    sweep: fieldEntry.sweep,
    azimuth: fieldEntry.azimuth,
    range: fieldEntry.range,
    values: fieldEntry.values,
    nAz: fieldEntry.nAz,
    nRange: fieldEntry.nRange,
    maxRange: fieldEntry.maxRange,
    vmin: spec.vmin,
    vmax: spec.vmax,
    units: spec.units,
  };
  return {
    module: moduleId,
    moduleTitle: moduleTitle(moduleId),
    name,
    title: block.title || name,
    sweep: fieldEntry.sweep,
    entry,
    spec,
  };
}

/** Replace the PPI's variable with a module field (cleared again on variable/sweep/file change). */
export function showFieldOnPpi(moduleId: string, name: string): boolean {
  const fieldEntry = get(moduleFields)[fieldKey(moduleId, name)];
  if (!fieldEntry) {
    addToast('warning', 'The field data has not arrived yet');
    return false;
  }
  if (fieldEntry.sweep !== get(selectedSweep)) {
    addToast('warning', `Field was computed for sweep ${fieldEntry.sweep}; select that sweep to show it`);
    return false;
  }
  const next = buildDisplayedField(moduleId, name, fieldEntry);
  if (!next) {
    addToast('error', 'Field description missing from the module result');
    return false;
  }
  displayedField.set(next);
  return true;
}

/** Go back to the normal variable (RadarViewer re-renders it). */
export function hideField(): void {
  displayedField.set(null);
}

// ── Export (CSV / GeoJSON) ─────────────────────────────────────────────────

/** Radar site (degrees) from the open file's attributes, or null when unknown. */
export const radarSite = derived(radarData, ($d) => siteFromAttributes($d.attributes));

export function siteFromAttributes(attrs: Record<string, any> | undefined): { lat: number; lon: number } | null {
  if (!attrs) return null;
  const num = (v: any): number => (v === null || v === undefined || v === '' ? NaN : Number(v));
  const lat = [attrs.latitude, attrs.origin_latitude, attrs.siteLat].map(num).find(Number.isFinite);
  const lon = [attrs.longitude, attrs.origin_longitude, attrs.siteLon].map(num).find(Number.isFinite);
  if (lat === undefined || lon === undefined) return null;
  if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
  if (lat === 0 && lon === 0) return null; // the viewer treats 0/0 as "unknown" too
  return { lat, lon };
}

const EARTH_RADIUS_M = 6371008.8;

/** Spherical destination point: start (lat, lon) deg, bearing deg clockwise from north, distance m. */
export function destinationPoint(
  lat: number,
  lon: number,
  bearingDeg: number,
  distanceM: number,
): [number, number] {
  const rad = Math.PI / 180;
  const phi1 = lat * rad;
  const lam1 = lon * rad;
  const theta = bearingDeg * rad;
  const delta = distanceM / EARTH_RADIUS_M;
  const sinPhi2 = Math.sin(phi1) * Math.cos(delta) + Math.cos(phi1) * Math.sin(delta) * Math.cos(theta);
  const phi2 = Math.asin(Math.max(-1, Math.min(1, sinPhi2)));
  const lam2 =
    lam1 +
    Math.atan2(
      Math.sin(theta) * Math.sin(delta) * Math.cos(phi1),
      Math.cos(delta) - Math.sin(phi1) * sinPhi2,
    );
  const lonOut = ((((lam2 / rad + 180) % 360) + 360) % 360) - 180;
  return [lonOut, phi2 / rad];
}

/** Overlay feature coordinate -> GeoJSON [lon, lat]. */
export function featureCoordToLonLat(
  frame: 'polar' | 'xy',
  a: number,
  b: number,
  site: { lat: number; lon: number },
): [number, number] {
  if (frame === 'xy') {
    const dist = Math.hypot(a, b);
    const bearing = (Math.atan2(a, b) * 180) / Math.PI; // x east, y north
    return destinationPoint(site.lat, site.lon, bearing, dist);
  }
  return destinationPoint(site.lat, site.lon, a, b); // [azimuth_deg, range_m]
}

function csvCell(v: any): string {
  if (v === null || v === undefined) return '';
  let t: string;
  if (typeof v === 'number') t = Number.isFinite(v) ? String(v) : '';
  else if (typeof v === 'object') t = JSON.stringify(v);
  else t = String(v);
  return /[",\r\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
}

/** RFC 4180 CSV (CRLF line endings, quoted when needed) of a table block. */
export function tableToCsv(columns: TableColumn[], rows: Record<string, any>[]): string {
  const lines = [columns.map((c) => csvCell(c.key)).join(',')];
  for (const row of rows) lines.push(columns.map((c) => csvCell(row[c.key])).join(','));
  return lines.join('\r\n') + '\r\n';
}

type GeoJsonGeometry =
  | { type: 'Point'; coordinates: [number, number] }
  | { type: 'LineString'; coordinates: [number, number][] }
  | { type: 'Polygon'; coordinates: [number, number][][] };

/** GeoJSON FeatureCollection (WGS84 lon/lat) from a result's overlay features. */
export function overlayToGeoJson(
  res: ModuleResult,
  site: { lat: number; lon: number },
): { type: 'FeatureCollection'; features: any[] } {
  // id -> merged row fields from every table block whose id_key matches
  const rowsById = new Map<string, Record<string, any>>();
  for (const b of res.blocks) {
    if (b?.type !== 'table') continue;
    const idKey = (b as any).id_key as string | undefined;
    if (!idKey) continue;
    for (const row of ((b as any).rows ?? []) as Record<string, any>[]) {
      const v = row?.[idKey];
      if (v === null || v === undefined) continue;
      const id = String(v);
      rowsById.set(id, { ...(rowsById.get(id) ?? {}), ...row });
    }
  }

  const features: any[] = [];
  for (const f of res.features) {
    const pts = f.coords.map(([a, b]) => featureCoordToLonLat(f.frame, a, b, site));
    let geometry: GeoJsonGeometry;
    if (f.kind === 'point' || pts.length === 1) {
      geometry = { type: 'Point', coordinates: pts[0] };
    } else if (f.kind === 'polygon' && pts.length >= 3) {
      const ring = [...pts];
      const [fx, fy] = ring[0];
      const [lx, ly] = ring[ring.length - 1];
      if (fx !== lx || fy !== ly) ring.push([fx, fy]);
      geometry = { type: 'Polygon', coordinates: [ring] };
    } else {
      geometry = { type: 'LineString', coordinates: pts };
    }
    const properties: Record<string, any> = { ...(rowsById.get(f.id) ?? {}) };
    properties.id = f.id;
    if (f.label !== null || properties.label === undefined) properties.label = f.label;
    properties.module = res.module;
    properties.sweep = res.sweep;
    features.push({ type: 'Feature', id: f.id, properties, geometry });
  }
  return { type: 'FeatureCollection', features };
}

function sanitizeFilename(name: string): string {
  return name.replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^_+|_+$/g, '') || 'export';
}

function inTauri(): boolean {
  return typeof window !== 'undefined' && !!(window as any).__TAURI_INTERNALS__;
}

/** Ask the sidecar to write a text file; resolves with the saved path. */
function saveViaSidecar(path: string, content: string, overwrite: boolean): Promise<string> {
  return new Promise((resolve, reject) => {
    if (!wsManager.isConnected) {
      reject(new Error('Sidecar not connected'));
      return;
    }
    const cleanup = () => {
      clearTimeout(timer);
      offSaved();
      offErr();
    };
    const offSaved = wsManager.onMessage('file_saved', (m) => {
      cleanup();
      resolve(String(m.path ?? path));
    });
    const offErr = wsManager.onMessage('error', (m) => {
      cleanup();
      reject(new Error(String(m.message ?? 'save failed')));
    });
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error('No response from the sidecar'));
    }, 20_000);
    void wsManager.send({ type: 'save_text_file', path, content, overwrite });
  });
}

function downloadBlob(filename: string, content: string, mime: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: `${mime};charset=utf-8` }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/**
 * Save text via the Tauri save dialog + sidecar (atomic write), or a Blob download in a
 * plain browser. Toasts the outcome; returns true on success, false on cancel/failure.
 */
export async function saveTextExport(
  defaultName: string,
  content: string,
  kind: { label: string; ext: string; mime: string },
): Promise<boolean> {
  const filename = `${sanitizeFilename(defaultName)}.${kind.ext}`;
  try {
    if (inTauri()) {
      const { save } = await import('@tauri-apps/plugin-dialog');
      const path = await save({
        title: `Export ${kind.label}`,
        defaultPath: filename,
        filters: [{ name: kind.label, extensions: [kind.ext] }],
      });
      if (!path) return false; // cancelled
      // The OS dialog already asked the user to confirm replacing an existing file.
      const saved = await saveViaSidecar(path, content, true);
      addToast('success', `Saved ${saved}`, 5000);
      return true;
    }
    downloadBlob(filename, content, kind.mime);
    addToast('success', `Downloaded ${filename}`, 4000);
    return true;
  } catch (err) {
    addToast('error', `Export failed: ${err instanceof Error ? err.message : String(err)}`, 8000);
    return false;
  }
}

/** File name only (never the folder), so exports do not reveal the user's directory layout. */
function baseName(path: string | null | undefined): string {
  return path ? (String(path).split(/[\\/]/).pop() ?? '') : '';
}

function appVersion(): string {
  return typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : 'unknown';
}

/** Compact one-line text of a parameter object: `a=1, b=[1, 2]`. */
function paramsText(params: Record<string, any> | undefined): string {
  if (!params) return '';
  return Object.entries(params)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join(', ');
}

/**
 * `# key: value` lines for the head of a CSV (read with `comment="#"` in pandas / R): what the
 * table is, the module and parameters that made it, the source file name, the units of each
 * column, how missing values are written and the time standard. The column row follows.
 */
export function csvMetadataBlock(meta: {
  title: string;
  module: string;
  sweep: number;
  params?: Record<string, any>;
  source?: string;
  columns: TableColumn[];
  created?: Date;
}): string {
  const created = (meta.created ?? new Date()).toISOString().replace(/\.\d{3}Z$/, 'Z');
  const lines = [
    `# title: ${meta.title}`,
    `# software: xradar-desktop ${appVersion()}`,
    `# created: ${created}`,
    `# module: ${meta.module}`,
    `# sweep: ${meta.sweep}`,
  ];
  if (meta.source) lines.push(`# source_files: ${meta.source}`);
  const p = paramsText(meta.params);
  if (p) lines.push(`# parameters: ${p}`);
  lines.push(
    '# columns: ' +
      meta.columns.map((c) => `${c.key} [${c.unit && c.unit.trim() ? c.unit : '1'}]`).join('; '),
  );
  lines.push('# missing_values: empty field');
  lines.push('# time_standard: UTC (ISO 8601)');
  return lines.map((l) => l.replace(/[\r\n]+/g, ' ')).join('\r\n') + '\r\n';
}

export function exportTableCsv(
  moduleId: string,
  res: ModuleResult,
  block: { title?: string; columns: TableColumn[]; rows: Record<string, any>[] },
): Promise<boolean> {
  const header = csvMetadataBlock({
    title: `${moduleId}: ${block.title ?? 'table'}`,
    module: moduleId,
    sweep: res.sweep,
    params: res.params,
    source: baseName(get(radarData).filePath),
    columns: block.columns ?? [],
  });
  const csv = header + tableToCsv(block.columns ?? [], block.rows ?? []);
  const name = `${moduleId}_sweep${res.sweep}_${block.title ?? 'table'}`;
  return saveTextExport(name, csv, { label: 'CSV', ext: 'csv', mime: 'text/csv' });
}

export function exportOverlayGeoJson(moduleId: string, res: ModuleResult): Promise<boolean> {
  const site = siteFromAttributes(get(radarData).attributes);
  if (!site) {
    addToast('error', 'Radar site location unknown: cannot georeference the overlay');
    return Promise.resolve(false);
  }
  const fc = {
    ...overlayToGeoJson(res, site),
    // RFC 7946 allows foreign members: who made this, from what, with which parameters
    metadata: {
      software: `xradar-desktop ${appVersion()}`,
      created: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
      source_files: baseName(get(radarData).filePath),
      module: moduleId,
      sweep: res.sweep,
      parameters: res.params ?? {},
      crs: 'EPSG:4326 (WGS 84, longitude/latitude in degrees)',
    },
  };
  const name = `${moduleId}_sweep${res.sweep}_overlay`;
  return saveTextExport(name, JSON.stringify(fc), {
    label: 'GeoJSON',
    ext: 'geojson',
    mime: 'application/geo+json',
  });
}

// ── Running state ──────────────────────────────────────────────────────────

const runTimers = new Map<string, ReturnType<typeof setTimeout>>();
/** Operations-tray handle of each in-flight run (module id -> op). */
const runOps = new Map<string, OpHandle>();
/** Results to drop because the user cancelled (or the file changed) while the run was in flight. */
const ignoreResults = new Map<string, number>();

function setRunning(
  moduleId: string,
  running: boolean,
  opts: { volume?: boolean } = {},
): void {
  const timeoutMs = opts.volume ? VOLUME_RUN_TIMEOUT_MS : RUN_TIMEOUT_MS;
  runningVolume.update((r) => {
    const want = running && !!opts.volume;
    if (!!r[moduleId] === want) return r;
    const next = { ...r };
    if (want) next[moduleId] = true;
    else delete next[moduleId];
    return next;
  });
  const timer = runTimers.get(moduleId);
  if (timer) {
    clearTimeout(timer);
    runTimers.delete(moduleId);
  }
  runningModules.update((r) => {
    if (!!r[moduleId] === running) return r;
    const next = { ...r };
    if (running) next[moduleId] = true;
    else delete next[moduleId];
    return next;
  });
  if (running) {
    runTimers.set(
      moduleId,
      setTimeout(() => {
        runTimers.delete(moduleId);
        const title = moduleTitle(moduleId);
        setRunning(moduleId, false);
        endRunOp(moduleId, 'fail', `No response after ${timeoutMs / 1000} s`);
        addToast('warning', `${title}: no response after ${timeoutMs / 1000} s`);
      }, timeoutMs),
    );
  }
}

/** Finish the tray entry of a run (no-op when it was already cancelled / finished). */
function endRunOp(moduleId: string, how: 'done' | 'fail', message?: string): void {
  const op = runOps.get(moduleId);
  if (!op) return;
  runOps.delete(moduleId);
  if (how === 'done') op.done();
  else op.fail(message ?? 'Failed');
}

/** True when this result/error belongs to a run the user already cancelled. */
function consumeIgnored(moduleId: string): boolean {
  const n = ignoreResults.get(moduleId) ?? 0;
  if (n <= 0) return false;
  if (n === 1) ignoreResults.delete(moduleId);
  else ignoreResults.set(moduleId, n - 1);
  return true;
}

/**
 * Stop tracking every run. With `failMessage` the tray entries turn into retryable failures
 * (connection lost, server error); without it they are cancelled and any late result is ignored
 * (the active file changed).
 */
function clearAllRunning(failMessage?: string): void {
  for (const id of [...runTimers.keys()]) {
    clearTimeout(runTimers.get(id));
  }
  runTimers.clear();
  runningModules.set({});
  runningVolume.set({});
  for (const [id, op] of [...runOps]) {
    runOps.delete(id);
    if (failMessage) op.fail(failMessage);
    else cancelOp(op.id); // onCancel records the late result to ignore
  }
  if (failMessage) ignoreResults.clear();
}

function moduleTitle(moduleId: string): string {
  return get(modulesState).modules.find((m) => m.id === moduleId)?.title ?? moduleId;
}

// ── Protocol ───────────────────────────────────────────────────────────────

let reloadToast = false;

/** Ask the server for the module list (reload:true re-scans the plugin directories). */
export function requestModules(reload = false): void {
  reloadToast = reload;
  modulesState.update((s) => ({ ...s, loading: true }));
  void wsManager.send(reload ? { type: 'list_modules', reload: true } : { type: 'list_modules' });
}

/** Parameter values to send for a module, clamped / defaulted; the form is synced to them. */
function collectParams(spec: ModuleSpec): Record<string, any> {
  const values = get(moduleParams)[spec.id] ?? {};
  const params: Record<string, any> = {};
  for (const p of spec.params ?? []) params[p.name] = coerceParam(p, values[p.name]);
  // Keep the form in sync with what was actually sent (clamped / defaulted).
  moduleParams.update((all) => ({ ...all, [spec.id]: { ...values, ...params } }));
  saveParams();
  return params;
}

/** Run a module on the currently displayed sweep with its current parameters. */
export function runModule(moduleId: string): void {
  const spec = get(modulesState).modules.find((m) => m.id === moduleId);
  if (!spec || get(runningModules)[moduleId]) return;
  if (!spec.available) {
    addToast('warning', `${spec.title}: ${spec.unavailable_reason ?? 'module unavailable'}`, 6000);
    return;
  }

  const params = collectParams(spec);

  const msg: Record<string, any> = {
    type: 'run_module',
    module: moduleId,
    sweep: get(selectedSweep),
    params,
  };
  const file = getActiveFile();
  if (file) msg.file_id = file.id;

  setRunning(moduleId, true);
  // Tray entry: the module's own safety timer (RUN_TIMEOUT_MS) ends it, so no second timeout here.
  const title = spec.title;
  runOps.set(
    moduleId,
    beginOp({
      kind: 'module',
      label: `Running ${title}`,
      slowMs: 10_000,
      timeoutMs: 0,
      onCancel: () => {
        // Stop waiting; the server cannot abort the run, so its late result is dropped.
        runOps.delete(moduleId);
        ignoreResults.set(moduleId, (ignoreResults.get(moduleId) ?? 0) + 1);
        setRunning(moduleId, false);
      },
      onRetry: () => runModule(moduleId),
    }),
  );
  void wsManager.send(msg);
}

/**
 * Run a per-sweep module on EVERY sweep of the active file and register each output field as a
 * derived variable (viewable in 2D and 3D). The result arrives as `module_volume_result`.
 */
export function runModuleAllSweeps(moduleId: string): void {
  const spec = get(modulesState).modules.find((m) => m.id === moduleId);
  if (!spec || get(runningModules)[moduleId]) return;
  if (!spec.available) {
    addToast('warning', `${spec.title}: ${spec.unavailable_reason ?? 'module unavailable'}`, 6000);
    return;
  }
  if (!spec.per_sweep) {
    addToast('warning', `${spec.title} analyses the whole volume in one run; it cannot be run per sweep`, 6000);
    return;
  }

  const params = collectParams(spec);

  const msg: Record<string, any> = {
    type: 'run_module',
    module: moduleId,
    sweep: get(selectedSweep),
    params,
    all_sweeps: true,
  };
  const file = getActiveFile();
  if (file) msg.file_id = file.id;

  setRunning(moduleId, true, { volume: true });
  const title = spec.title;
  runOps.set(
    moduleId,
    beginOp({
      kind: 'module',
      label: `Running ${title} on all sweeps`,
      slowMs: 30_000,
      timeoutMs: 0, // VOLUME_RUN_TIMEOUT_MS (setRunning) ends it
      onCancel: () => {
        runOps.delete(moduleId);
        ignoreResults.set(moduleId, (ignoreResults.get(moduleId) ?? 0) + 1);
        setRunning(moduleId, false);
      },
      onRetry: () => runModuleAllSweeps(moduleId),
    }),
  );
  void wsManager.send(msg);
}

/** Stop waiting for a running module (its late result is ignored). */
export function cancelModuleRun(moduleId: string): void {
  const op = runOps.get(moduleId);
  if (op) cancelOp(op.id);
}

function normaliseFeatures(blocks: ModuleBlock[]): OverlayFeature[] {
  const out: OverlayFeature[] = [];
  for (const b of blocks) {
    if (b?.type !== 'overlay' || !Array.isArray((b as any).features)) continue;
    for (const f of (b as any).features as RawOverlayFeature[]) {
      if (!f || !Array.isArray(f.coords)) continue;
      const kind = f.kind === 'polygon' || f.kind === 'line' || f.kind === 'point' ? f.kind : null;
      if (!kind) continue;
      const coords = f.coords.filter(
        (c): c is [number, number] =>
          Array.isArray(c) && Number.isFinite(c[0]) && Number.isFinite(c[1]),
      );
      if (coords.length === 0) continue;
      out.push({
        id: String(f.id),
        kind,
        frame: f.frame === 'xy' ? 'xy' : 'polar',
        coords,
        label: f.label ? String(f.label) : null,
        color: f.color ? String(f.color) : null,
        fill: typeof f.fill === 'number' && Number.isFinite(f.fill) ? f.fill : null,
      });
    }
  }
  return out;
}

function handleModulesList(msg: any): void {
  const modules: ModuleSpec[] = (Array.isArray(msg.modules) ? msg.modules : []).map((m: any) => ({
    id: String(m.id),
    title: m.title ?? String(m.id),
    description: m.description ?? '',
    category: m.category || 'Other',
    requires: Array.isArray(m.requires) ? m.requires : [],
    optional: Array.isArray(m.optional) ? m.optional : [],
    params: Array.isArray(m.params) ? m.params : [],
    source: m.source,
    version: m.version,
    needs: Array.isArray(m.needs) ? m.needs.map(String) : [],
    available: m.available !== false,
    unavailable_reason: typeof m.unavailable_reason === 'string' ? m.unavailable_reason : null,
    scope: m.scope === 'volume' ? 'volume' : 'sweep',
    per_sweep: m.per_sweep === true,
    paired: Array.isArray(m.paired) ? m.paired.map(String) : [],
  }));

  // Merge parameter values: declared defaults < saved < current session.
  const saved = loadSavedParams();
  const current = get(moduleParams);
  const nextParams: Record<string, Record<string, any>> = {};
  for (const m of modules) {
    nextParams[m.id] = defaultParams(m, { ...(saved[m.id] ?? {}), ...(current[m.id] ?? {}) });
  }
  moduleParams.set(nextParams);

  // Drop results / state for modules that no longer exist.
  const ids = new Set(modules.map((m) => m.id));
  moduleResults.update((r) => {
    const next: Record<string, ModuleResult> = {};
    for (const [k, v] of Object.entries(r)) if (ids.has(k)) next[k] = v;
    return next;
  });
  const sel = get(selectedFeature);
  if (sel && !ids.has(sel.module)) selectedFeature.set(null);

  const errors: ModuleLoadError[] = (Array.isArray(msg.errors) ? msg.errors : []).map((e: any) => ({
    file: String(e?.file ?? '?'),
    error: String(e?.error ?? ''),
  }));

  modulesState.set({
    modules,
    errors,
    dirs: Array.isArray(msg.dirs) ? msg.dirs.map(String) : [],
    loading: false,
    loaded: true,
  });

  if (reloadToast) {
    reloadToast = false;
    addToast(
      errors.length ? 'warning' : 'success',
      `Reloaded ${modules.length} module${modules.length === 1 ? '' : 's'}` +
        (errors.length ? ` (${errors.length} failed to load)` : ''),
    );
  }
}

function handleModuleResult(msg: any): void {
  const id = String(msg.module);
  if (consumeIgnored(id)) return; // run was cancelled in the tray
  setRunning(id, false);
  endRunOp(id, 'done');
  const blocks: ModuleBlock[] = Array.isArray(msg.blocks) ? msg.blocks : [];
  const result: ModuleResult = {
    module: id,
    sweep: typeof msg.sweep === 'number' ? msg.sweep : get(selectedSweep),
    params: msg.params ?? {},
    elapsed_ms: Number(msg.elapsed_ms) || 0,
    blocks,
    features: normaliseFeatures(blocks),
  };
  moduleResults.update((r) => ({ ...r, [id]: result }));
  // Fields of the previous run are superseded; the new ones follow as binary payloads.
  // (A field already on screen keeps its own entry until its replacement arrives.)
  moduleFields.update((f) => {
    const prefix = `${id}/`;
    const next: Record<string, ModuleFieldEntry> = {};
    for (const [k, v] of Object.entries(f)) if (!k.startsWith(prefix)) next[k] = v;
    return next;
  });

  // Previous selection may refer to a feature that no longer exists.
  const sel = get(selectedFeature);
  if (sel && sel.module === id && !result.features.some((f) => f.id === sel.id)) {
    selectedFeature.set(null);
  }

  const firstField = blocks.find((b) => b.type === 'field') as { name?: string; title?: string } | undefined;
  const willShow = !!firstField?.name && get(autoShowResults);
  if (willShow && firstField?.name) pendingAutoShow.set(id, String(firstField.name));
  else pendingAutoShow.delete(id);
  lastFinishedModule.set({ id, at: Date.now() });

  const nShapes = result.features.length;
  const what = blocks.some((b) => b.type === 'overlay')
    ? nShapes > 0
      ? ` — ${nShapes} outline${nShapes === 1 ? '' : 's'} drawn on the map`
      : ' — nothing found with these settings'
    : '';
  addToast(
    'success',
    willShow
      ? `${moduleTitle(id)}: done in ${formatElapsed(result.elapsed_ms)} — showing it on the map (use "Back to" to return)`
      : `${moduleTitle(id)}: done in ${formatElapsed(result.elapsed_ms)}${what}`,
    5000,
  );
}

function handleModuleVolumeResult(msg: any): void {
  const id = String(msg.module);
  if (consumeIgnored(id)) return; // run was cancelled in the tray
  setRunning(id, false);
  endRunOp(id, 'done');

  const fileId: string | null =
    typeof msg.file_id === 'string' && msg.file_id ? msg.file_id : (getActiveFile()?.id ?? null);
  const made: VolumeRunResult['derived'] = (Array.isArray(msg.derived) ? msg.derived : [])
    .map((raw: any) => {
      const d = normaliseDerived(raw, id);
      if (!d) return null;
      const skipped = (Array.isArray(raw.skipped) ? raw.skipped : [])
        .filter((x: any) => x && Number.isInteger(Number(x.sweep)))
        .map((x: any) => ({ sweep: Number(x.sweep), reason: String(x.reason ?? '') }));
      return { ...d, skipped };
    })
    .filter((d: any) => d !== null);
  const evicted: string[] = (Array.isArray(msg.evicted) ? msg.evicted : []).map(String);

  const result: VolumeRunResult = {
    module: id,
    fileId,
    params: msg.params ?? {},
    elapsed_ms: Number(msg.elapsed_ms) || 0,
    derived: made,
    // The sidecar sends {sweep, blocks_scalars:{label:value}}; accept {scalars} as well.
    summaries: (Array.isArray(msg.summaries) ? msg.summaries : [])
      .map((x: any) => ({ sweep: Number(x?.sweep), scalars: x?.blocks_scalars ?? x?.scalars }))
      .filter((x: any) => Number.isInteger(x.sweep) && x.scalars && typeof x.scalars === 'object'),
    evicted,
    at: Date.now(),
  };
  moduleVolumeResults.update((r) => ({ ...r, [id]: result }));

  // Register right away (the server's own `derived_variables` follows and confirms the list).
  const names = made.map((d) => d.name);
  if (fileId) {
    const prev = get(derivedByFile)[fileId] ?? [];
    const drop = new Set([...names, ...evicted]);
    dropDerivedCaches(names); // recomputed under the same name: cached sweeps are stale
    setDerivedForFile(fileId, [...prev.filter((p) => !drop.has(p.name)), ...made]);
    if (fileId === getActiveFile()?.id) refreshIfSelected(names);
  }

  const total = get(radarData).sweeps.length;
  const toastMs = 9000;
  if (made.length === 0) {
    addToast('warning', `${moduleTitle(id)}: finished, but no sweep produced a field`, 8000);
    return;
  }
  const first = made[0];
  const skippedN = first.skipped.length;
  const sweepsTxt = `${first.sweeps.length} of ${total || first.sweeps.length + skippedN} sweeps`;
  const what =
    made.length === 1
      ? `"${first.label}" for ${sweepsTxt}`
      : `${made.length} variables (${made.map((d) => `"${d.label}"`).join(', ')}) for up to ${sweepsTxt}`;
  addToast(
    skippedN > 0 ? 'warning' : 'success',
    `Added ${what} — switch to 3D to see it${skippedN > 0 ? ` (${skippedN} sweep${skippedN === 1 ? '' : 's'} skipped)` : ''}` +
      (evicted.length > 0 ? `; ${evicted.length} older derived variable${evicted.length === 1 ? '' : 's'} removed` : ''),
    toastMs,
  );
}

function handleModuleField(entry: ModuleFieldEntry): void {
  moduleFields.update((f) => ({ ...f, [fieldKey(entry.module, entry.name)]: entry }));
  if (pendingAutoShow.get(entry.module) === entry.name) {
    pendingAutoShow.delete(entry.module);
    if (entry.sweep === get(selectedSweep)) {
      const auto = buildDisplayedField(entry.module, entry.name, entry);
      if (auto) {
        displayedField.set(auto);
        return;
      }
    }
  }
  // A field already on screen is refreshed in place when its module is re-run.
  const shown = get(displayedField);
  if (shown && shown.module === entry.module && shown.name === entry.name) {
    const next = buildDisplayedField(entry.module, entry.name, entry);
    if (next && entry.sweep === get(selectedSweep)) displayedField.set(next);
    else displayedField.set(null);
  }
}

function handleModuleError(msg: any): void {
  const id = msg.module ? String(msg.module) : null;
  if (id && consumeIgnored(id)) return;
  const reason = String(msg.message ?? 'failed');
  if (id) {
    pendingAutoShow.delete(id);
    setRunning(id, false);
    endRunOp(id, 'fail', reason);
  } else clearAllRunning(reason);
  addToast('error', `${id ? moduleTitle(id) : 'Module'}: ${msg.message ?? 'failed'}`, 8000);
}

export function formatElapsed(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`;
}

// ── Initialisation ─────────────────────────────────────────────────────────

let initialised = false;

/**
 * Register WebSocket handlers and request the module list on connect.
 * Idempotent; the handlers live for the lifetime of the app.
 */
export function initModules(): void {
  if (initialised) return;
  initialised = true;

  wsManager.onMessage('modules_list', handleModulesList);
  wsManager.onMessage('module_result', handleModuleResult);
  wsManager.onMessage('module_volume_result', handleModuleVolumeResult);
  wsManager.onMessage('module_error', handleModuleError);
  wsManager.onMessage('module_field_ready', handleModuleField);
  // A generic server error while a module is running would otherwise leave the
  // spinner up until the safety timeout (the toast is shown by wsManager).
  wsManager.onMessage('error', (m) => clearAllRunning(String(m?.message ?? 'Server error')));

  // Surface the server's progress text on the running module entries in the tray.
  processingProgress.subscribe((p) => {
    if (!p || runOps.size === 0) return;
    const text = p.message ? `${p.message} (${Math.round(p.percent)}%)` : `${Math.round(p.percent)}%`;
    for (const op of runOps.values()) op.update(text);
  });

  connectionStatus.subscribe((status) => {
    if (status === 'connected') {
      requestModules(false);
    } else {
      modulesState.update((s) => (s.loading ? { ...s, loading: false } : s));
      clearAllRunning('Connection to the sidecar was lost');
    }
  });

  // Results belong to a specific file; drop them when the active file changes.
  let first = true;
  activeFileId.subscribe(() => {
    if (first) {
      first = false;
      return;
    }
    moduleResults.set({});
    moduleVolumeResults.set({});
    moduleFields.set({});
    displayedField.set(null);
    selectedFeature.set(null);
    clearAllRunning();
  });

  // File closed / reset without a file manager id: also drop results when no variables remain.
  let hadVariables = false;
  radarData.subscribe((d) => {
    const has = d.variables.length > 0;
    if (hadVariables && !has) {
      moduleResults.set({});
      moduleVolumeResults.set({});
      moduleFields.set({});
      displayedField.set(null);
      selectedFeature.set(null);
    }
    hadVariables = has;
  });
}

// ── Derived variables: viewing and removing ─────────────────────────────────

/** Ask the app shell to switch the main view ('2d' | '3d'). */
export function requestViewMode(mode: '2d' | '3d'): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent('xradar:set-view-mode', { detail: { mode } }));
}

/** Select a derived variable (and a sweep that carries it), optionally switching the main view. */
export function showDerivedVariable(d: DerivedVariable, mode: '2d' | '3d'): void {
  requestViewMode(mode);
  // A sweep first: selecting the variable on a sweep that lacks it would bounce straight back.
  const cur = get(selectedSweep);
  if (d.sweeps.length > 0 && !d.sweeps.includes(cur)) selectedSweep.set(d.sweeps[0]);
  hideField();
  selectedVariable.set(d.name);
}

/** Remove one derived variable from the sidecar. */
export function removeDerived(d: DerivedVariable): void {
  clearDerived(d.name);
}

export { activeDerived };
