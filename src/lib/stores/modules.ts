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
 */

import { writable, derived, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { connectionStatus, radarData, selectedSweep } from './radarData';
import { activeFileId, getActiveFile } from './fileManager';
import { addToast } from './toasts';

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

export type ModuleBlock =
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

/** Overlay visibility per module id (missing = visible). */
export const overlayVisibility = writable<Record<string, boolean>>({});

/** Currently selected overlay feature (shared between panel and overlay). */
export const selectedFeature = writable<SelectedFeature | null>(null);

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

const ALIAS_GROUPS: string[][] = [
  // Keep in sync with VAR_ALIASES in python/engine/plugins.py
  ['DBZH', 'DBZ', 'REFLECTIVITY', 'Z', 'CORRECTED_REFLECTIVITY_HORIZONTAL', 'DBZHCC', 'DBZHC'],
  ['ZDR', 'DIFFERENTIAL_REFLECTIVITY', 'ZDRC'],
  ['PHIDP', 'DIFFERENTIAL_PHASE', 'UPHIDP'],
  ['KDP', 'SPECIFIC_DIFFERENTIAL_PHASE'],
  ['RHOHV', 'CROSS_CORRELATION_RATIO', 'CCORH'],
  ['VRADH', 'VRAD', 'VELOCITY', 'VEL', 'V', 'RADIAL_VELOCITY'],
  ['WRADH', 'WIDTH', 'SPECTRUM_WIDTH'],
];

const ALIAS_MAP: Map<string, string> = new Map(
  ALIAS_GROUPS.flatMap((g) => g.map((n) => [n, g[0]] as [string, string])),
);

export function canonicalVariable(name: string): string {
  const up = name.trim().toUpperCase();
  return ALIAS_MAP.get(up) ?? up;
}

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
}

// ── Running state ──────────────────────────────────────────────────────────

const runTimers = new Map<string, ReturnType<typeof setTimeout>>();

function setRunning(moduleId: string, running: boolean): void {
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
        addToast('warning', `${title}: no response after ${RUN_TIMEOUT_MS / 1000} s`);
      }, RUN_TIMEOUT_MS),
    );
  }
}

function clearAllRunning(): void {
  for (const id of [...runTimers.keys()]) {
    clearTimeout(runTimers.get(id));
  }
  runTimers.clear();
  runningModules.set({});
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

/** Run a module on the currently displayed sweep with its current parameters. */
export function runModule(moduleId: string): void {
  const spec = get(modulesState).modules.find((m) => m.id === moduleId);
  if (!spec || get(runningModules)[moduleId]) return;

  const values = get(moduleParams)[moduleId] ?? {};
  const params: Record<string, any> = {};
  for (const p of spec.params ?? []) params[p.name] = coerceParam(p, values[p.name]);

  // Keep the form in sync with what was actually sent (clamped / defaulted).
  moduleParams.update((all) => ({ ...all, [moduleId]: { ...values, ...params } }));
  saveParams();

  const msg: Record<string, any> = {
    type: 'run_module',
    module: moduleId,
    sweep: get(selectedSweep),
    params,
  };
  const file = getActiveFile();
  if (file) msg.file_id = file.id;

  setRunning(moduleId, true);
  void wsManager.send(msg);
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
  setRunning(id, false);
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

  // Previous selection may refer to a feature that no longer exists.
  const sel = get(selectedFeature);
  if (sel && sel.module === id && !result.features.some((f) => f.id === sel.id)) {
    selectedFeature.set(null);
  }

  addToast('success', `${moduleTitle(id)}: done in ${formatElapsed(result.elapsed_ms)}`, 3000);
}

function handleModuleError(msg: any): void {
  const id = msg.module ? String(msg.module) : null;
  if (id) setRunning(id, false);
  else clearAllRunning();
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
  wsManager.onMessage('module_error', handleModuleError);
  // A generic server error while a module is running would otherwise leave the
  // spinner up until the safety timeout (the toast is shown by wsManager).
  wsManager.onMessage('error', () => clearAllRunning());

  connectionStatus.subscribe((status) => {
    if (status === 'connected') {
      requestModules(false);
    } else {
      modulesState.update((s) => (s.loading ? { ...s, loading: false } : s));
      clearAllRunning();
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
    selectedFeature.set(null);
    clearAllRunning();
  });

  // File closed / reset without a file manager id: also drop results when no variables remain.
  let hadVariables = false;
  radarData.subscribe((d) => {
    const has = d.variables.length > 0;
    if (hadVariables && !has) {
      moduleResults.set({});
      selectedFeature.set(null);
    }
    hadVariables = has;
  });
}
