/**
 * Multi-Doppler wind store: retrieve u / v / w from two or more open radar files and show the
 * result on the 2-D map and in the Wind panel.
 *
 *   -> {type:'run_multidoppler', request_id, file_ids, grid, options}
 *   <- {type:'progress', ...}* then {type:'multidoppler_result', request_id, ...} + float32 frames
 *      (the websocket manager reassembles the frames and emits `multidoppler_result_ready`
 *      with `{header, data}`), or {type:'multidoppler_error', request_id, message}
 *      or {type:'multidoppler_cancelled', request_id}
 *   -> {type:'multidoppler_cancel', request_id}
 *   -> {type:'export_multidoppler', wind_id, path, overwrite}  <- {type:'file_saved', path}
 *
 * Only the most recent request_id is honoured: results of older runs are dropped.
 */

import { writable, derived, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { beginOp, cancelOp, type OpHandle } from '../utils/requestScheduler';
import { openFiles } from './fileManager';
import { connectionStatus } from './radarData';
import { addToast } from './toasts';
import { RADAR_COLORS, fileSite, fileTimeMs, formatTimeMs, fitMapToBounds, inTauri } from './mosaic';
import {
  FIELD_STYLES,
  decodeResult,
  defaultRange,
  nearestLevel,
  type WindFieldKey,
  type WindHeader,
  type WindLayer,
  type WindResult,
} from '../utils/wind';

// ── Settings ───────────────────────────────────────────────────────────────

export type WindMethod = 'analytic' | 'variational';
export type WindBoundary = 'upward' | 'downward' | 'obrien';
export type WindBackgroundMode = 'none' | 'vvp' | 'uniform' | 'manual';

export interface WindSettings {
  // grid
  dxM: number;
  dzM: number;
  zMaxM: number;
  zMinAuto: boolean;
  zMinM: number;
  maxRangeKm: number;
  extentAuto: boolean;
  xMinKm: number;
  xMaxKm: number;
  yMinKm: number;
  yMaxKm: number;
  // retrieval
  method: WindMethod;
  minCrossing: number;
  boundary: WindBoundary;
  fallSpeed: boolean;
  freezingManual: boolean;
  freezingLevelM: number;
  meltingDepthM: number;
  // background wind (variational method)
  bgMode: WindBackgroundMode;
  bgU: number;
  bgV: number;
  bgProfile: string;
  // storm motion (advect the volumes to a common time)
  motionOn: boolean;
  motionU: number;
  motionV: number;
  uncertainty: boolean;
}

export const DEFAULT_WIND_SETTINGS: WindSettings = {
  dxM: 1000,
  dzM: 500,
  zMaxM: 10000,
  zMinAuto: true,
  zMinM: 1000,
  maxRangeKm: 100,
  extentAuto: true,
  xMinKm: -40,
  xMaxKm: 40,
  yMinKm: -40,
  yMaxKm: 40,
  method: 'analytic',
  minCrossing: 30,
  boundary: 'upward',
  fallSpeed: true,
  freezingManual: false,
  freezingLevelM: 4000,
  meltingDepthM: 500,
  bgMode: 'none',
  bgU: 10,
  bgV: 0,
  bgProfile: '0, 5, 0\n3000, 12, 3\n8000, 25, 5',
  motionOn: false,
  motionU: 10,
  motionV: 5,
  uncertainty: false,
};

const STORAGE_KEY = 'xradar.wind.settings.v1';

function loadSettings(): WindSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_WIND_SETTINGS };
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return { ...DEFAULT_WIND_SETTINGS };
    const out: any = { ...DEFAULT_WIND_SETTINGS };
    for (const k of Object.keys(DEFAULT_WIND_SETTINGS) as (keyof WindSettings)[]) {
      if (k in parsed && typeof parsed[k] === typeof DEFAULT_WIND_SETTINGS[k]) out[k] = parsed[k];
    }
    if (!['analytic', 'variational'].includes(out.method)) out.method = 'analytic';
    if (!['upward', 'downward', 'obrien'].includes(out.boundary)) out.boundary = 'upward';
    if (!['none', 'vvp', 'uniform', 'manual'].includes(out.bgMode)) out.bgMode = 'none';
    return out as WindSettings;
  } catch {
    return { ...DEFAULT_WIND_SETTINGS };
  }
}

function saveSettings(s: WindSettings): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch {
    /* storage unavailable: ignore */
  }
}

export const windSettings = writable<WindSettings>(loadSettings());
windSettings.subscribe(saveSettings);

export function updateWindSettings(patch: Partial<WindSettings>): void {
  windSettings.update((s) => ({ ...s, ...patch }));
}

export function resetWindSettings(): void {
  windSettings.set({ ...DEFAULT_WIND_SETTINGS });
}

// ── Run state, result, display ─────────────────────────────────────────────

export interface WindRunState {
  running: boolean;
  requestId: string | null;
  percent: number;
  message: string;
  error: string | null;
}

export interface WindDisplay {
  field: WindFieldKey;
  level: number;
  visible: boolean;
  showRaster: boolean;
  showVectors: boolean;
  /** Hide the PPI canvas so the map with the wind analysis is what you see. */
  hidePpi: boolean;
  opacity: number;
  /** Only show cells whose beam-crossing angle is at least this (degrees). */
  minCrossing: number;
  /** Only show cells whose wind error is at most this (m/s); null = no limit. */
  maxUncertainty: number | null;
  /** Manual colour range (null = automatic from the data). */
  vmin: number | null;
  vmax: number | null;
  sectionField: WindFieldKey;
  sectionAxis: 'x' | 'y';
  /** Row (axis 'x') or column (axis 'y') of the vertical cross-section. */
  sectionIndex: number;
}

export const windRun = writable<WindRunState>({
  running: false,
  requestId: null,
  percent: 0,
  message: '',
  error: null,
});

export const windResult = writable<WindResult | null>(null);

export const windDisplay = writable<WindDisplay>({
  field: 'speed',
  level: 0,
  visible: true,
  showRaster: true,
  showVectors: true,
  hidePpi: true,
  opacity: 0.85,
  minCrossing: 0,
  maxUncertainty: null,
  vmin: null,
  vmax: null,
  sectionField: 'w',
  sectionAxis: 'x',
  sectionIndex: 0,
});

export const windExporting = writable<boolean>(false);

export function updateWindDisplay(patch: Partial<WindDisplay>): void {
  windDisplay.update((d) => ({ ...d, ...patch }));
}

/** Everything the map component needs to draw the wind analysis; null when there is nothing. */
export const windLayer = derived([windResult, windDisplay], ([$res, $d]): WindLayer | null => {
  if (!$res) return null;
  const level = Math.max(0, Math.min($res.grid.nz - 1, $d.level));
  const [lo, hi] = defaultRange($res, $d.field);
  const vmin = $d.vmin ?? lo;
  const vmax = $d.vmax !== null && $d.vmax > vmin ? $d.vmax : hi;
  return {
    res: $res,
    level,
    field: $d.field,
    vmin,
    vmax,
    kind: FIELD_STYLES[$d.field].kind,
    showRaster: $d.showRaster,
    showVectors: $d.showVectors,
    hidePpi: $d.hidePpi,
    opacity: $d.opacity,
    visible: $d.visible,
    minCrossing: $d.minCrossing,
    maxUncertainty: $d.maxUncertainty,
    radars: $res.radars.map((r, i) => ({ ...r, color: RADAR_COLORS[i % RADAR_COLORS.length] })),
  };
});

// ── File rows ──────────────────────────────────────────────────────────────

const VELOCITY_NAMES = ['VRADH', 'VEL', 'velocity', 'V', 'radial_velocity', 'VRAD', 'VRADV'];
const TIME_WARN_MS = 5 * 60 * 1000;
const MIN_BASELINE_KM = 5;

export interface WindFileRow {
  id: string;
  name: string;
  filename: string;
  lat: number | null;
  lon: number | null;
  timeMs: number | null;
  timeLabel: string;
  hasVelocity: boolean;
  selected: boolean;
  warnings: string[];
}

/** File ids the user switched OFF (default: every open file with velocity takes part). */
export const windDeselected = writable<string[]>([]);

function distanceKm(a: { lat: number; lon: number }, b: { lat: number; lon: number }): number {
  const R = 6371.0088;
  const rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad;
  const dLon = (b.lon - a.lon) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}

export const windFileRows = derived([openFiles, windDeselected], ([$files, $off]): WindFileRow[] => {
  const rows: WindFileRow[] = $files.map((f) => {
    const site = fileSite(f);
    const timeMs = fileTimeMs(f);
    const a = f.attributes ?? {};
    const name = a.instrument_name ?? a.station_name ?? a.station ?? a.site_name;
    const hasVelocity = f.variables.some((v) => VELOCITY_NAMES.includes(v));
    return {
      id: f.id,
      name: name ? String(name) : f.filename,
      filename: f.filename,
      lat: site?.lat ?? null,
      lon: site?.lon ?? null,
      timeMs,
      timeLabel: formatTimeMs(timeMs),
      hasVelocity,
      selected: hasVelocity && !$off.includes(f.id),
      warnings: hasVelocity ? [] : ['no radial velocity'],
    };
  });
  const sel = rows.filter((r) => r.selected);
  const times = sel.map((r) => r.timeMs).filter((t): t is number => t !== null);
  if (times.length >= 2 && Math.max(...times) - Math.min(...times) > TIME_WARN_MS) {
    const spread = Math.round((Math.max(...times) - Math.min(...times)) / 60000);
    for (const r of sel) if (r.timeMs !== null) r.warnings.push(`volumes ${spread} min apart`);
  }
  for (let i = 0; i < sel.length; i++) {
    for (let j = i + 1; j < sel.length; j++) {
      const a = sel[i];
      const b = sel[j];
      if (a.lat === null || b.lat === null || a.lon === null || b.lon === null) continue;
      if (distanceKm({ lat: a.lat, lon: a.lon }, { lat: b.lat, lon: b.lon }) < MIN_BASELINE_KM) {
        for (const r of [a, b]) if (!r.warnings.includes('same site')) r.warnings.push('same site');
      }
    }
  }
  for (const r of sel) if (r.lat === null) r.warnings.push('no site location');
  return rows;
});

export const windSelectedIds = derived(windFileRows, ($rows) => $rows.filter((r) => r.selected).map((r) => r.id));

openFiles.subscribe((files) => {
  const ids = new Set(files.map((f) => f.id));
  const off = get(windDeselected);
  if (off.some((id) => !ids.has(id))) windDeselected.set(off.filter((id) => ids.has(id)));
});

export function toggleWindFile(id: string): void {
  windDeselected.update((off) => (off.includes(id) ? off.filter((x) => x !== id) : [...off, id]));
}

// ── Running ────────────────────────────────────────────────────────────────

let requestCounter = 0;
let runSafetyTimer: ReturnType<typeof setTimeout> | null = null;
const RUN_TIMEOUT_MS = 15 * 60 * 1000;
let windOp: OpHandle | null = null;

function newRequestId(): string {
  requestCounter += 1;
  return `wind-${Date.now().toString(36)}-${requestCounter}`;
}

function finishRun(patch: Partial<WindRunState>): void {
  if (runSafetyTimer) {
    clearTimeout(runSafetyTimer);
    runSafetyTimer = null;
  }
  const op = windOp;
  windOp = null;
  windRun.update((r) => ({ ...r, running: false, ...patch }));
  if (op) {
    if (patch.error) op.fail(patch.error);
    else if (patch.percent === 100) op.done();
    else cancelOp(op.id);
  }
}

/** Parse "height_m, u, v" lines of the manual background profile. */
export function parseProfile(text: string): { height_m: number; u: number; v: number }[] | null {
  const rows: { height_m: number; u: number; v: number }[] = [];
  for (const line of text.split(/\r?\n/)) {
    const t = line.trim();
    if (!t) continue;
    const p = t.split(/[\s,;]+/).map(Number);
    if (p.length !== 3 || p.some((n) => !Number.isFinite(n))) return null;
    rows.push({ height_m: p[0], u: p[1], v: p[2] });
  }
  return rows.length > 0 ? rows : null;
}

export function buildRequest(s: WindSettings, ids: string[], requestId: string): Record<string, any> | string {
  const grid: Record<string, any> = {
    dx_m: s.dxM,
    dz_m: s.dzM,
    z_max_m: s.zMaxM,
    max_range_km: s.maxRangeKm,
  };
  if (!s.zMinAuto) grid.z_min_m = s.zMinM;
  if (!s.extentAuto) {
    if (!(s.xMaxKm > s.xMinKm) || !(s.yMaxKm > s.yMinKm)) return 'The manual extent needs max > min on both axes';
    // The extent is relative to the origin: use the mean site position so it is predictable.
    const sites = get(windFileRows)
      .filter((r) => ids.includes(r.id) && r.lat !== null && r.lon !== null)
      .map((r) => ({ lat: r.lat as number, lon: r.lon as number }));
    if (sites.length === 0) return 'A manual extent needs the radar site locations';
    grid.origin = {
      lat: sites.reduce((a, b) => a + b.lat, 0) / sites.length,
      lon: sites.reduce((a, b) => a + b.lon, 0) / sites.length,
    };
    grid.extent = { x_min_km: s.xMinKm, x_max_km: s.xMaxKm, y_min_km: s.yMinKm, y_max_km: s.yMaxKm };
  }
  const options: Record<string, any> = {
    method: s.method,
    min_crossing_angle: s.minCrossing,
    boundary: s.boundary,
    fall_speed_correction: s.fallSpeed,
    freezing_level: s.freezingManual ? s.freezingLevelM : 'auto',
    melting_depth_m: s.meltingDepthM,
    uncertainty: s.method === 'variational' && s.uncertainty,
  };
  if (s.motionOn) options.storm_motion = [s.motionU, s.motionV];
  if (s.method === 'variational') {
    if (s.bgMode === 'uniform') options.background = { mode: 'uniform', u: s.bgU, v: s.bgV };
    else if (s.bgMode === 'vvp') options.background = { mode: 'vvp' };
    else if (s.bgMode === 'manual') {
      const profile = parseProfile(s.bgProfile);
      if (!profile) return 'The background profile needs lines of "height_m, u, v"';
      options.background = { mode: 'manual', profile };
    }
  }
  return { type: 'run_multidoppler', request_id: requestId, file_ids: ids, grid, options };
}

/** Build and send a run_multidoppler request. Returns the request_id, or null if it did not start. */
export function runWind(): string | null {
  const s = get(windSettings);
  const ids = get(windSelectedIds);
  const fail = (error: string) => {
    windRun.update((r) => ({ ...r, error }));
    return null;
  };
  if (get(connectionStatus) !== 'connected') return fail('Sidecar not connected');
  if (ids.length < 2) return fail('Select at least two open radar files with radial velocity');
  const requestId = newRequestId();
  const msg = buildRequest(s, ids, requestId);
  if (typeof msg === 'string') return fail(msg);

  if (windOp) {
    const prev = windOp;
    windOp = null;
    cancelOp(prev.id);
    const old = get(windRun).requestId;
    if (old) void wsManager.send({ type: 'multidoppler_cancel', request_id: old });
  }
  windOp = beginOp({
    kind: 'wind',
    label: `Wind retrieval (${ids.length} radars)`,
    slowMs: 30_000,
    timeoutMs: 0,
    onCancel: () => {
      windOp = null;
      cancelWindRun();
    },
    onRetry: () => {
      runWind();
    },
  });
  windRun.set({ running: true, requestId, percent: 0, message: 'Starting…', error: null });
  if (runSafetyTimer) clearTimeout(runSafetyTimer);
  runSafetyTimer = setTimeout(() => {
    runSafetyTimer = null;
    if (get(windRun).requestId === requestId && get(windRun).running) {
      finishRun({ error: 'No response from the sidecar (timed out)' });
    }
  }, RUN_TIMEOUT_MS);
  void wsManager.send(msg);
  return requestId;
}

/** Ask the sidecar to stop the run at its next stage and stop waiting for it. */
export function cancelWindRun(): void {
  const id = get(windRun).requestId;
  if (id) void wsManager.send({ type: 'multidoppler_cancel', request_id: id });
  finishRun({ requestId: null, percent: 0, message: '', error: null });
}

function handleResultReady(payload: { header: WindHeader; data: Float32Array }): void {
  const { header, data } = payload;
  const run = get(windRun);
  if (header.request_id !== run.requestId) return; // stale or cancelled
  const res = decodeResult(header, data);
  if (!res) {
    finishRun({ error: 'The wind result could not be decoded (unexpected size)' });
    return;
  }
  windResult.set(res);
  windDisplay.update((d) => {
    const level = nearestLevel(res.grid.z, d.level > 0 && d.level < res.grid.nz ? res.grid.z[d.level] : 3000);
    const mid = d.sectionAxis === 'x' ? Math.floor(res.grid.ny / 2) : Math.floor(res.grid.nx / 2);
    return {
      ...d,
      visible: true,
      level,
      sectionIndex: Math.min(d.sectionIndex > 0 ? d.sectionIndex : mid, (d.sectionAxis === 'x' ? res.grid.ny : res.grid.nx) - 1),
      vmin: null,
      vmax: null,
    };
  });
  finishRun({ percent: 100, message: 'Done', error: null });
  fitWindToMap();
}

function handleProgress(msg: any): void {
  const run = get(windRun);
  if (!run.running) return;
  windRun.update((r) => ({
    ...r,
    percent: typeof msg.percent === 'number' ? Math.max(0, Math.min(100, msg.percent)) : r.percent,
    message: typeof msg.message === 'string' && msg.message ? msg.message : r.message,
  }));
  const after = get(windRun);
  windOp?.update(after.message ? `${after.message} (${Math.round(after.percent)}%)` : `${Math.round(after.percent)}%`);
}

let exportPending: { reject: (e: Error) => void } | null = null;

function handleError(msg: any): void {
  const text = String(msg.message ?? 'Wind retrieval failed');
  if (msg.request_id) {
    if (msg.request_id !== get(windRun).requestId) return;
    finishRun({ error: text });
    return;
  }
  if (exportPending) {
    const p = exportPending;
    exportPending = null;
    p.reject(new Error(text));
    return;
  }
  if (get(windRun).running) finishRun({ error: text });
}

function handleCancelled(msg: any): void {
  if (msg.request_id === get(windRun).requestId) finishRun({ requestId: null, percent: 0, message: '', error: null });
}

function handleGenericError(msg: any): void {
  const text = String(msg.message ?? 'Server error');
  if (exportPending) {
    const p = exportPending;
    exportPending = null;
    p.reject(new Error(text));
    return;
  }
  if (get(windRun).running) finishRun({ error: text });
}

export function clearWind(): void {
  windResult.set(null);
  finishRun({ requestId: null, percent: 0, message: '', error: null });
}

export function fitWindToMap(): void {
  const r = get(windResult);
  if (!r) return;
  fitMapToBounds({ west: r.grid.west, south: r.grid.south, east: r.grid.east, north: r.grid.north });
}

// ── Export ─────────────────────────────────────────────────────────────────

/** Choose a path with the Tauri dialog and let the sidecar write a NetCDF file. */
export async function exportWind(): Promise<boolean> {
  const res = get(windResult);
  if (!res) return false;
  if (!inTauri()) {
    addToast('info', 'Exporting a wind analysis needs the desktop app (file dialog)');
    return false;
  }
  if (exportPending) return false;
  windExporting.set(true);
  try {
    const { save } = await import('@tauri-apps/plugin-dialog');
    const path = await save({
      title: 'Export wind analysis as NetCDF',
      defaultPath: 'multidoppler_wind.nc',
      filters: [{ name: 'NetCDF', extensions: ['nc'] }],
    });
    if (!path) return false;
    const saved = await new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => {
        if (exportPending) {
          exportPending = null;
          offSaved();
          reject(new Error('No response from the sidecar'));
        }
      }, 60_000);
      const offSaved = wsManager.onMessage('file_saved', (m) => {
        if (!exportPending) return;
        clearTimeout(timer);
        exportPending = null;
        offSaved();
        resolve(String(m.path ?? path));
      });
      exportPending = {
        reject: (e) => {
          clearTimeout(timer);
          offSaved();
          reject(e);
        },
      };
      void wsManager.send({ type: 'export_multidoppler', wind_id: res.wind_id, path, overwrite: true });
    });
    addToast('success', `Saved ${saved}`, 5000);
    return true;
  } catch (err) {
    addToast('error', `Export failed: ${err instanceof Error ? err.message : String(err)}`, 8000);
    return false;
  } finally {
    exportPending = null;
    windExporting.set(false);
  }
}

// ── Initialisation ─────────────────────────────────────────────────────────

let initialised = false;

/** Register WebSocket handlers. Idempotent; they live for the lifetime of the app. */
export function initWind(): void {
  if (initialised || typeof window === 'undefined') return;
  initialised = true;
  wsManager.onMessage('multidoppler_result_ready', handleResultReady);
  wsManager.onMessage('multidoppler_error', handleError);
  wsManager.onMessage('multidoppler_cancelled', handleCancelled);
  wsManager.onMessage('progress', handleProgress);
  wsManager.onMessage('error', handleGenericError);
  connectionStatus.subscribe((status) => {
    if (status !== 'connected' && get(windRun).running) {
      finishRun({ error: 'Connection to the sidecar was lost' });
    }
  });
}

initWind();
