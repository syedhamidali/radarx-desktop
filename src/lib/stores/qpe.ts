/**
 * Rainfall (QPE) store: estimate rain rate / accumulation from the open radar files, adjust it to
 * rain gauges, and summarise basins. The map shows the result through the mosaic map layer.
 *
 *   -> {type:'qpe_info'}                              <- {type:'qpe_info_result', relations, methods, fields}
 *   -> {type:'qpe_run', request_id, file_ids, config, options, basins?, gauges?}
 *                                                     <- progress*, {type:'qpe_result', ...} | {type:'qpe_error'}
 *   -> {type:'qpe_layer', qpe_id, field}              <- {type:'qpe_layer_result', layer}
 *   -> {type:'qpe_adjust', qpe_id, gauges}            <- {type:'qpe_adjust_result', validation, layer}
 *   -> {type:'qpe_series', qpe_id, basin, bin_minutes} <- {type:'qpe_series_result', rows, hyetograph, ...}
 *   -> {type:'qpe_export', qpe_id, format, path}      <- {type:'file_saved'}
 *
 * Everything shown is an estimate: results carry `provenance.type` RETRIEVED or ADJUSTED, and the
 * UI always says so. Only the newest request_id is honoured.
 */

import { writable, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { beginOp, cancelOp, type OpHandle } from '../utils/requestScheduler';
import { connectionStatus } from './radarData';
import { mosaicSelectedIds, showExternalLayer, inTauri } from './mosaic';
import { addToast } from './toasts';
import { binMinutesFor } from '../utils/qpeView';

// ── Types ──────────────────────────────────────────────────────────────────

export interface QpeRelation {
  key: string;
  label: string;
  kind: 'rain' | 'snow';
  equation: string;
  reference: string;
  note: string;
}

export interface QpeInfo {
  relations: QpeRelation[];
  default_relation: { rain: string; snow: string };
  methods: string[];
  fields: { name: string; label: string; units: string }[];
}

export interface QpeSettings {
  method: string;
  relation: string;
  precip: 'rain' | 'snow';
  band: 'S' | 'C' | 'X';
  freezingLevelM: number | null;
  zCap: number | null;
  attenuation: 'auto' | 'on' | 'off';
  composite: 'weighted' | 'best';
  advect: boolean;
  resolutionM: number;
  maxRangeKm: number;
  start: string;
  end: string;
  holdEdgesS: number;
  gaugePath: string;
  adjust: 'mfb' | 'brandes' | 'ked';
  gaugeUnits: string;
  gaugeTz: string;
  gaugeIntervalS: number | null;
  gaugeStations: string;
  basinPath: string;
  basinIdField: string;
  thresholds: string;
  frequencyPath: string;
  /** ensemble of the accumulation under an assumed error model: 0 = off */
  ensembleMembers: number;
  ensembleSeed: number;
  ensembleThresholds: string;
}

export const DEFAULT_QPE_SETTINGS: QpeSettings = {
  method: 'auto',
  relation: 'wsr88d',
  precip: 'rain',
  band: 'S',
  freezingLevelM: null,
  zCap: 53,
  attenuation: 'auto',
  composite: 'weighted',
  advect: true,
  resolutionM: 1000,
  maxRangeKm: 230,
  start: '',
  end: '',
  holdEdgesS: 0,
  gaugePath: '',
  adjust: 'mfb',
  gaugeUnits: 'mm',
  gaugeTz: 'UTC',
  gaugeIntervalS: null,
  gaugeStations: '',
  basinPath: '',
  basinIdField: '',
  thresholds: '10,25,50,100',
  frequencyPath: '',
  ensembleMembers: 0,
  ensembleSeed: 0,
  ensembleThresholds: '5,10,25,50,100',
};

export interface QpeRadar {
  name: string;
  latitude: number;
  longitude: number;
  scans: number;
  first: string | null;
  last: string | null;
  motion_mean_ms: [number, number] | null;
}

export interface QpeSummary {
  qpe_id: string;
  start: string;
  end: string;
  duration_s: number;
  stats: {
    cells_with_data: number;
    area_km2: number;
    max_mm: number | null;
    mean_mm: number | null;
    wet_area_km2: number;
    max_rate_mm_h: number | null;
    mean_valid_fraction: number | null;
    uncertainty_median_rel: number | null;
  };
  grid: { ny: number; nx: number; resolution_m: number };
  radars: QpeRadar[];
  nodes: number;
  notes: string[];
  timing: { total_s?: number };
  provenance: {
    type: 'RETRIEVED' | 'ADJUSTED';
    method: string;
    uncertainty: { kind: string; model: string };
    parameters: Record<string, any>;
  };
  adjustment?: Record<string, any>;
  basins?: Record<string, any>[];
  exceedance?: Record<string, any>[];
  /** ensemble under an ASSUMED error model: error model with where each number came from, run record, reliability */
  ensemble?: Record<string, any>;
  gauge_validation?: Record<string, any>;
  fields: { name: string; label: string; units: string }[];
  default_field: string;
}

export interface QpeLayer {
  mosaic_id: string;
  field: string;
  label: string;
  units: string;
  vmin: number;
  vmax: number;
  legend: [number, string][];
  provenance_type: 'RETRIEVED' | 'ADJUSTED';
  stats: { valid_fraction: number; min: number | null; max: number | null; mean: number | null };
}

export interface QpeState {
  qpeId: string;
  summary: QpeSummary;
  layer: QpeLayer;
}

export interface QpeRun {
  running: boolean;
  requestId: string | null;
  percent: number;
  message: string;
  error: string | null;
}

// ── Persistence ────────────────────────────────────────────────────────────

const STORAGE_KEY = 'xradar.qpe.settings.v1';

function loadSettings(): QpeSettings {
  const out: any = { ...DEFAULT_QPE_SETTINGS };
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null');
    if (parsed && typeof parsed === 'object') {
      for (const k of Object.keys(DEFAULT_QPE_SETTINGS) as (keyof QpeSettings)[]) {
        if (!(k in parsed)) continue;
        const d = DEFAULT_QPE_SETTINGS[k];
        if (typeof parsed[k] === typeof d || (parsed[k] === null && d === null)) out[k] = parsed[k];
        else if (d === null && typeof parsed[k] === 'number') out[k] = parsed[k];
      }
    }
  } catch {
    /* storage unavailable or corrupt: defaults */
  }
  return out as QpeSettings;
}

// ── Stores ─────────────────────────────────────────────────────────────────

export const qpeSettings = writable<QpeSettings>(loadSettings());
qpeSettings.subscribe((s) => {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch {
    /* ignore */
  }
});

export const qpeInfo = writable<QpeInfo | null>(null);
export const qpeRun = writable<QpeRun>({ running: false, requestId: null, percent: 0, message: '', error: null });
export const qpeState = writable<QpeState | null>(null);
export const qpeSeries = writable<Record<string, any> | null>(null);
export const qpeBusy = writable<string | null>(null); // 'layer' | 'adjust' | 'series' | 'export'

export function updateQpeSettings(patch: Partial<QpeSettings>): void {
  qpeSettings.update((s) => ({ ...s, ...patch }));
}

export function resetQpeSettings(): void {
  qpeSettings.set({ ...DEFAULT_QPE_SETTINGS });
}

// ── Requests ───────────────────────────────────────────────────────────────

let counter = 0;
const newId = (kind: string) => `${kind}-${Date.now().toString(36)}-${++counter}`;
let op: OpHandle | null = null;
let safetyTimer: ReturnType<typeof setTimeout> | null = null;
const RUN_TIMEOUT_MS = 20 * 60 * 1000;

function finishRun(patch: Partial<QpeRun>): void {
  if (safetyTimer) {
    clearTimeout(safetyTimer);
    safetyTimer = null;
  }
  const o = op;
  op = null;
  qpeRun.update((r) => ({ ...r, running: false, ...patch }));
  if (o) {
    if (patch.error) o.fail(patch.error);
    else if (patch.percent === 100) o.done();
    else cancelOp(o.id);
  }
}

/** The `qpe_run` message for the current settings (exported for tests of the wire format). */
export function buildRunMessage(s: QpeSettings, fileIds: string[], requestId: string): Record<string, any> {
  const config: Record<string, any> = {
    method: s.method,
    relation: s.relation,
    precip: s.precip,
    band: s.band,
    attenuation: s.attenuation,
    z_cap: s.precip === 'snow' ? null : s.zCap,
  };
  if (s.freezingLevelM !== null && Number.isFinite(s.freezingLevelM) && s.freezingLevelM > 0) {
    config.freezing_level_m = s.freezingLevelM;
  }
  const options: Record<string, any> = {
    resolution_m: s.resolutionM,
    max_range_km: s.maxRangeKm,
    composite: s.composite,
    advect: s.advect,
  };
  if (s.start.trim()) options.start = s.start.trim();
  if (s.end.trim()) options.end = s.end.trim();
  if (s.holdEdgesS > 0) options.hold_edges_s = s.holdEdgesS;
  const msg: Record<string, any> = { type: 'qpe_run', request_id: requestId, file_ids: fileIds, config, options };
  if (s.basinPath.trim()) {
    const thresholds = s.thresholds
      .split(',')
      .map((t) => Number(t.trim()))
      .filter((t) => Number.isFinite(t) && t > 0);
    msg.basins = {
      path: s.basinPath.trim(),
      ...(s.basinIdField.trim() ? { id_field: s.basinIdField.trim() } : {}),
      ...(thresholds.length ? { thresholds } : {}),
      ...(s.frequencyPath.trim() ? { frequency: s.frequencyPath.trim() } : {}),
    };
  }
  if (s.gaugePath.trim()) msg.gauges = gaugeSpec(s);
  if (s.ensembleMembers >= 2) {
    const thresholds = s.ensembleThresholds
      .split(',')
      .map((t) => Number(t.trim()))
      .filter((t) => Number.isFinite(t) && t > 0);
    msg.ensemble = {
      n_members: Math.min(500, Math.round(s.ensembleMembers)),
      seed: Math.max(0, Math.round(s.ensembleSeed)),
      ...(thresholds.length ? { thresholds } : {}),
    };
  }
  return msg;
}

export function gaugeSpec(s: QpeSettings): Record<string, any> {
  return {
    path: s.gaugePath.trim(),
    adjust: s.adjust,
    units: s.gaugeUnits,
    tz: s.gaugeTz,
    ...(s.gaugeIntervalS ? { interval_s: s.gaugeIntervalS } : {}),
    ...(s.gaugeStations.trim() ? { stations: s.gaugeStations.trim() } : {}),
  };
}

export function runQpe(): string | null {
  if (get(connectionStatus) !== 'connected') {
    qpeRun.update((r) => ({ ...r, error: 'Sidecar not connected' }));
    return null;
  }
  const ids = get(mosaicSelectedIds);
  if (ids.length === 0) {
    qpeRun.update((r) => ({ ...r, error: 'Select at least one open radar file' }));
    return null;
  }
  const id = newId('qpe');
  if (op) {
    const prev = op;
    op = null;
    cancelOp(prev.id);
  }
  op = beginOp({
    kind: 'qpe',
    label: `Rainfall estimate (${ids.length} volume${ids.length === 1 ? '' : 's'})`,
    slowMs: 30_000,
    timeoutMs: 0,
    onCancel: () => {
      op = null;
      cancelQpe();
    },
    onRetry: () => {
      runQpe();
    },
  });
  qpeRun.set({ running: true, requestId: id, percent: 0, message: 'Starting…', error: null });
  safetyTimer = setTimeout(() => {
    safetyTimer = null;
    if (get(qpeRun).requestId === id && get(qpeRun).running) finishRun({ error: 'No response from the sidecar (timed out)' });
  }, RUN_TIMEOUT_MS);
  void wsManager.send(buildRunMessage(get(qpeSettings), ids, id));
  return id;
}

export function cancelQpe(): void {
  finishRun({ requestId: null, percent: 0, message: '', error: null });
}

function send(type: string, body: Record<string, any>, busy: string): string | null {
  const st = get(qpeState);
  if (!st) return null;
  const id = newId(type);
  qpeBusy.set(busy);
  void wsManager.send({ type, request_id: id, qpe_id: st.qpeId, ...body });
  pending.set(id, busy);
  return id;
}
const pending = new Map<string, string>();

/** Show another field of the current result on the map (no recomputation). */
export function setQpeField(field: string): void {
  send('qpe_layer', { field }, 'layer');
}

/** Adjust the current accumulation to the gauge file of the settings (no recomputation). */
export function adjustQpe(): void {
  const s = get(qpeSettings);
  if (!s.gaugePath.trim()) {
    qpeRun.update((r) => ({ ...r, error: 'Give the gauge file first' }));
    return;
  }
  send('qpe_adjust', { gauges: gaugeSpec(s) }, 'adjust');
}

export function loadSeries(basin: string | number, binMinutes = 60): void {
  send('qpe_series', { basin, bin_minutes: binMinutes }, 'series');
}

export async function pickFile(title: string, extensions: string[]): Promise<string | null> {
  if (!inTauri()) return null;
  try {
    const { open } = await import('@tauri-apps/plugin-dialog');
    const p = await open({ title, multiple: false, directory: false, filters: [{ name: title, extensions }] });
    return typeof p === 'string' ? p : null;
  } catch {
    return null;
  }
}

export async function exportQpe(format: 'nc' | 'png' | 'json' | 'csv'): Promise<boolean> {
  const st = get(qpeState);
  if (!st) return false;
  if (!inTauri()) {
    addToast('info', 'Exporting needs the desktop app (file dialog)');
    return false;
  }
  qpeBusy.set('export');
  try {
    const { save } = await import('@tauri-apps/plugin-dialog');
    const start = st.summary.start.replace(/[^0-9T]/g, '').slice(0, 13);
    const path = await save({
      title: `Save rainfall as ${format.toUpperCase()}`,
      defaultPath: `qpe_${start}.${format}`,
      filters: [{ name: format.toUpperCase(), extensions: [format] }],
    });
    if (!path) return false;
    const saved = await new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => {
        off();
        reject(new Error('No response from the sidecar'));
      }, 60_000);
      const off = wsManager.onMessage('file_saved', (m) => {
        clearTimeout(timer);
        off();
        resolve(String(m.path ?? path));
      });
      void wsManager.send({
        type: 'qpe_export',
        request_id: newId('qpe-export'),
        qpe_id: st.qpeId,
        format,
        path,
        field: st.layer.field,
        overwrite: true,
      });
    });
    addToast('success', `Saved ${saved}`, 5000);
    return true;
  } catch (err) {
    addToast('error', `Export failed: ${err instanceof Error ? err.message : String(err)}`, 8000);
    return false;
  } finally {
    qpeBusy.set(null);
  }
}

export function clearQpe(): void {
  qpeState.set(null);
  qpeSeries.set(null);
  finishRun({ requestId: null, percent: 0, message: '', error: null });
}

// ── Replies ────────────────────────────────────────────────────────────────

async function showLayer(layer: QpeLayer & Record<string, any>): Promise<void> {
  await showExternalLayer(layer as any, layer.png_b64);
}

async function onResult(msg: any): Promise<void> {
  if (msg.request_id !== get(qpeRun).requestId) return; // stale or cancelled
  try {
    await showLayer(msg.layer);
  } catch (e) {
    finishRun({ error: e instanceof Error ? e.message : String(e) });
    return;
  }
  const { png_b64, ...layer } = msg.layer;
  void png_b64;
  qpeState.set({ qpeId: msg.qpe_id, summary: msg.summary, layer });
  qpeSeries.set(null);
  finishRun({ percent: 100, message: 'Done', error: null });
  const first = msg.summary?.basins?.[0]?.id;
  if (first !== undefined) loadSeries(first, binMinutesFor(msg.summary.duration_s));
}

function onError(msg: any): void {
  const text = String(msg.message ?? 'The rainfall estimate failed');
  if (msg.kind === 'qpe_run' || (msg.request_id && msg.request_id === get(qpeRun).requestId)) {
    if (msg.request_id && msg.request_id !== get(qpeRun).requestId) return;
    finishRun({ error: text });
    return;
  }
  if (msg.request_id) pending.delete(msg.request_id);
  qpeBusy.set(null);
  qpeRun.update((r) => ({ ...r, error: text }));
}

function onProgress(msg: any): void {
  const run = get(qpeRun);
  if (!run.running) return;
  if (msg.request_id && msg.request_id !== run.requestId) return;
  qpeRun.update((r) => ({
    ...r,
    percent: typeof msg.percent === 'number' ? Math.max(0, Math.min(100, msg.percent)) : r.percent,
    message: typeof msg.message === 'string' && msg.message ? msg.message : r.message,
  }));
  const a = get(qpeRun);
  op?.update(a.message ? `${a.message} (${Math.round(a.percent)}%)` : `${Math.round(a.percent)}%`);
}

async function onLayerLike(msg: any): Promise<void> {
  if (msg.request_id) pending.delete(msg.request_id);
  const st = get(qpeState);
  if (!st || msg.qpe_id !== st.qpeId) return;
  try {
    await showLayer(msg.layer);
  } catch (e) {
    qpeRun.update((r) => ({ ...r, error: e instanceof Error ? e.message : String(e) }));
    qpeBusy.set(null);
    return;
  }
  const { png_b64, ...layer } = msg.layer;
  void png_b64;
  qpeState.set({ ...st, summary: msg.summary ?? st.summary, layer });
  qpeBusy.set(null);
  if (msg.type === 'qpe_adjust_result') {
    // the basin series was re-derived with the adjustment
    const cur = get(qpeSeries);
    if (cur?.basin !== undefined) loadSeries(cur.basin as string, binMinutesFor(st.summary.duration_s));
  }
}

let initialised = false;

/** Register the websocket handlers (once) and ask the engine what it offers. */
export function initQpe(): void {
  if (initialised) return;
  initialised = true;
  wsManager.onMessage('qpe_result', (m) => void onResult(m));
  wsManager.onMessage('qpe_error', onError);
  wsManager.onMessage('qpe_layer_result', (m) => void onLayerLike(m));
  wsManager.onMessage('qpe_adjust_result', (m) => void onLayerLike(m));
  wsManager.onMessage('qpe_series_result', (m) => {
    if (m.request_id) pending.delete(m.request_id);
    const st = get(qpeState);
    if (st && m.qpe_id === st.qpeId) qpeSeries.set(m);
    qpeBusy.set(null);
  });
  wsManager.onMessage('qpe_info_result', (m) => qpeInfo.set(m as QpeInfo));
  wsManager.onMessage('progress', onProgress);
  const ask = () => {
    if (get(connectionStatus) === 'connected' && !get(qpeInfo)) void wsManager.send({ type: 'qpe_info', request_id: newId('info') });
  };
  connectionStatus.subscribe(ask);
}
