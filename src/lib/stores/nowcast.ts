/**
 * Nowcast store: extrapolate the last radar frames 5 to 120 minutes ahead (an advection nowcast with
 * an optional ensemble) and show the result on the map as a time slider of leads.
 *
 *   -> {type:'nowcast_run', request_id, file_ids | mosaic_ids | frames_path, options}
 *                                      <- progress*, {type:'nowcast_result', ...} | {type:'nowcast_error'}
 *                                         | {type:'nowcast_cancelled'}
 *   -> {type:'nowcast_cancel', request_id}
 *   -> {type:'nowcast_layers', request_id, nowcast_id, kind, threshold?, rate?}
 *                                      <- {type:'nowcast_layers_result', layers:[...every lead...]}
 *   -> {type:'nowcast_export', request_id, nowcast_id, path, overwrite}  <- {type:'file_saved'}
 *
 * Science rule (docs/SCIENCE.md, class FORECAST): a nowcast is a forecast, never an observation. The
 * map layer carries a `forecast` label (chip, origin, valid time, lead, skill statement, guidance
 * sentence) that the map draws on every frame, and a saved picture has the same words burnt in.
 * The map layer is the mosaic layer (opacity, legend); it has no engine grid to probe.
 *
 * Only the newest request_id is honoured. The probability layers of a run are fetched on demand
 * (one request per product, all leads) and kept in memory until the next run.
 */

import { writable, get, derived } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { beginOp, cancelOp, type OpHandle } from '../utils/requestScheduler';
import { connectionStatus } from './radarData';
import { mosaicFileRows, mosaicResult, showExternalLayer, clearMosaic, inTauri, type ForecastTag } from './mosaic';
import { liveFrames } from './live';
import { openFiles } from './fileManager';
import { addToast } from './toasts';
import { savePng } from '../utils/crossSectionExport';
import { imageFromBase64, renderFramePng } from '../utils/nowcastPicture';
import {
  DEFAULT_NOWCAST_SETTINGS,
  DETERMINISTIC,
  buildRunMessage,
  exportBaseName,
  frameLabel,
  inputFromFiles,
  inputFromGrid,
  inputFromLive,
  layerKey,
  parseLayer,
  parseProduct,
  parseResult,
  sanitizeSettings,
  stepIndex,
  withStep,
  type NowcastInput,
  type NowcastLayerImage,
  type NowcastResult,
  type NowcastSettings,
  type NowcastSourceKind,
  type ProductKey,
} from '../utils/nowcastLogic';

// ── Persistence ────────────────────────────────────────────────────────────

const STORAGE_KEY = 'xradar.nowcast.settings.v1';

function loadSettings(): NowcastSettings {
  try {
    return sanitizeSettings(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null'));
  } catch {
    return { ...DEFAULT_NOWCAST_SETTINGS }; // storage unavailable or corrupt: defaults
  }
}

// ── Stores ─────────────────────────────────────────────────────────────────

export const nowcastSettings = writable<NowcastSettings>(loadSettings());
nowcastSettings.subscribe((s) => {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch {
    /* ignore */
  }
});

export interface NowcastRun {
  running: boolean;
  requestId: string | null;
  percent: number;
  message: string;
  error: string | null;
}

export interface NowcastView {
  product: ProductKey;
  leadIndex: number;
  playing: boolean;
}

export const nowcastRun = writable<NowcastRun>({ running: false, requestId: null, percent: 0, message: '', error: null });
export const nowcastResult = writable<NowcastResult | null>(null);
export const nowcastView = writable<NowcastView>({ product: DETERMINISTIC, leadIndex: 0, playing: false });
/** What is being fetched or written right now ('layers' | 'export' | 'picture'). */
export const nowcastBusy = writable<string | null>(null);
/** The products whose layers are in memory (the selector enables them at once). */
export const nowcastLoaded = writable<ProductKey[]>([]);

/** True while the map shows a frame of the current result (another mosaic or QPE layer replaces it). */
export const nowcastOnMap = derived([nowcastResult, mosaicResult], ([$r, $m]) => !!$r && $m?.forecast?.nowcastId === $r.nowcastId);

/** What the selected source offers to run right now (null input problem = ready). */
export const nowcastInput = derived([nowcastSettings, mosaicFileRows, liveFrames], ([$s, $rows, $live]): NowcastInput => {
  if ($s.source === 'live') return inputFromLive($live.map((f) => ({ id: f.id, time: f.time, mosaicId: f.mosaicId })));
  if ($s.source === 'grid') return inputFromGrid($s.gridPath);
  return inputFromFiles($rows.map((r) => ({ id: r.id, name: r.name, timeMs: r.timeMs, selected: r.selected })));
});

export function updateNowcastSettings(patch: Partial<NowcastSettings>): void {
  nowcastSettings.update((s) => sanitizeSettings({ ...s, ...patch }));
}

export function setNowcastStep(stepMin: number): void {
  nowcastSettings.update((s) => withStep(s, stepMin));
}

export function setNowcastSource(source: NowcastSourceKind): void {
  updateNowcastSettings({ source });
}

/** The command palette asks for a source before the panel exists; the panel is lazy-loaded. */
export function requestNowcastSource(source: NowcastSourceKind): void {
  setNowcastSource(source);
}

// ── Requests ───────────────────────────────────────────────────────────────

let counter = 0;
const newId = (kind: string) => `${kind}-${Date.now().toString(36)}-${++counter}`;
let op: OpHandle | null = null;
let safetyTimer: ReturnType<typeof setTimeout> | null = null;
const RUN_TIMEOUT_MS = 20 * 60 * 1000;
/** product -> lead -> layer. Cleared with every new run. */
const cache = new Map<ProductKey, Map<number, NowcastLayerImage>>();
let pendingLayers: { requestId: string; product: ProductKey; nowcastId: string; fit: boolean } | null = null;
let playTimer: ReturnType<typeof setInterval> | null = null;
const PLAY_MS = 650;

function finishRun(patch: Partial<NowcastRun>): void {
  if (safetyTimer) {
    clearTimeout(safetyTimer);
    safetyTimer = null;
  }
  const o = op;
  op = null;
  nowcastRun.update((r) => ({ ...r, running: false, ...patch }));
  if (o) {
    if (patch.error) o.fail(patch.error);
    else if (patch.percent === 100) o.done();
    else cancelOp(o.id);
  }
}

export function runNowcast(): string | null {
  if (get(connectionStatus) !== 'connected') {
    nowcastRun.update((r) => ({ ...r, error: 'The engine is not connected' }));
    return null;
  }
  const input = get(nowcastInput);
  if (input.problem) {
    nowcastRun.update((r) => ({ ...r, error: input.problem }));
    return null;
  }
  if (get(nowcastRun).running) return null;
  const id = newId('nowcast');
  op = beginOp({
    kind: 'nowcast',
    label: `Nowcast (${input.summary || 'radar frames'})`,
    slowMs: 30_000,
    timeoutMs: 0,
    onCancel: () => {
      op = null;
      cancelNowcast();
    },
    onRetry: () => {
      runNowcast();
    },
  });
  nowcastRun.set({ running: true, requestId: id, percent: 0, message: 'Starting…', error: null });
  safetyTimer = setTimeout(() => {
    safetyTimer = null;
    if (get(nowcastRun).requestId === id && get(nowcastRun).running) finishRun({ error: 'No response from the engine (timed out)' });
  }, RUN_TIMEOUT_MS);
  void wsManager.send(buildRunMessage(get(nowcastSettings), input, id));
  return id;
}

/** Stop waiting and ask the engine to stop at its next check. */
export function cancelNowcast(): void {
  const id = get(nowcastRun).requestId;
  if (id && get(nowcastRun).running) void wsManager.send({ type: 'nowcast_cancel', request_id: id });
  finishRun({ requestId: null, percent: 0, message: 'Cancelled', error: null });
}

// ── Showing a frame on the map ─────────────────────────────────────────────

function currentLayer(r: NowcastResult, product: ProductKey, leadIndex: number): NowcastLayerImage | null {
  return cache.get(product)?.get(r.leads[leadIndex]) ?? null;
}

function bboxOf(r: NowcastResult): { west: number; south: number; east: number; north: number } {
  const c = r.corners; // NW, NE, SE, SW as [lon, lat]
  return { west: c[0][0], north: c[0][1], east: c[2][0], south: c[2][1] };
}

/** Draw the frame (product, lead) of the current result on the map. */
async function showFrame(fit: boolean): Promise<boolean> {
  const r = get(nowcastResult);
  const v = get(nowcastView);
  if (!r || r.corners.length !== 4) return false;
  const layer = currentLayer(r, v.product, v.leadIndex);
  if (!layer) return false;
  const label = frameLabel(r, v.leadIndex, v.product);
  const tag: ForecastTag = { ...label, nowcastId: r.nowcastId, leadIndex: v.leadIndex, leadCount: r.leads.length };
  const p = parseProduct(v.product);
  const title = !p || p.kind === 'deterministic' ? `FCST ${r.units}` : `FCST P(≥${p.threshold}${p.rate ? ' mm/h' : ''})`;
  const b = bboxOf(r);
  try {
    await showExternalLayer(
      {
        mosaic_id: `nowcast:${r.nowcastId}:${v.product}:${layer.lead}`,
        type: 'mosaic_result',
        product: 'composite',
        variable: title,
        units: layer.kind === 'probability' ? '' : layer.units || r.units,
        height_m: null,
        grid: { nx: 0, ny: 0, resolution_m: 0, ...b },
        corners: r.corners,
        vmin: layer.vmin,
        vmax: layer.vmax,
        cmap: '',
        legend: layer.legend,
        stats: { valid_fraction: 0, min: 0, max: 0, mean: 0 },
        radars: [],
        forecast: tag,
      },
      layer.png,
      { fit },
    );
    return true;
  } catch (e) {
    nowcastRun.update((x) => ({ ...x, error: e instanceof Error ? e.message : String(e) }));
    return false;
  }
}

/** Show the frame again (after another layer replaced it on the map). */
export function showNowcastOnMap(): void {
  void showFrame(true);
}

/** Take the forecast off the map (the observation underneath is what is left). */
export function hideNowcastFromMap(): void {
  stopPlaying();
  if (get(mosaicResult)?.forecast) clearMosaic();
}

export function setLeadIndex(i: number): void {
  const r = get(nowcastResult);
  if (!r) return;
  const k = stepIndex(Math.round(i), r.leads.length, 0);
  nowcastView.update((v) => ({ ...v, leadIndex: k }));
  void showFrame(false);
}

export function stepLead(dir: number | 'first' | 'last'): void {
  const r = get(nowcastResult);
  if (!r) return;
  setLeadIndex(stepIndex(get(nowcastView).leadIndex, r.leads.length, dir));
}

function stopPlaying(): void {
  if (playTimer) {
    clearInterval(playTimer);
    playTimer = null;
  }
  nowcastView.update((v) => (v.playing ? { ...v, playing: false } : v));
}

export function togglePlaying(): void {
  if (get(nowcastView).playing) {
    stopPlaying();
    return;
  }
  const r = get(nowcastResult);
  if (!r || r.leads.length < 2) return;
  nowcastView.update((v) => ({ ...v, playing: true }));
  playTimer = setInterval(() => {
    const res = get(nowcastResult);
    if (!res || !get(nowcastOnMap)) {
      stopPlaying();
      return;
    }
    const n = res.leads.length;
    setLeadIndex((get(nowcastView).leadIndex + 1) % n);
  }, PLAY_MS);
}

/** Choose what the map shows: the nowcast or the probability of exceeding a threshold. */
export function setProduct(product: ProductKey): void {
  const r = get(nowcastResult);
  const req = parseProduct(product);
  if (!r || !req) return;
  nowcastView.update((v) => ({ ...v, product }));
  if (cache.has(product)) {
    void showFrame(false);
    return;
  }
  const id = newId('nowcast-layers');
  pendingLayers = { requestId: id, product, nowcastId: r.nowcastId, fit: false };
  nowcastBusy.set('layers');
  void wsManager.send({
    type: 'nowcast_layers',
    request_id: id,
    nowcast_id: r.nowcastId,
    kind: req.kind,
    ...(req.threshold !== null ? { threshold: req.threshold, rate: req.rate } : {}),
  });
}

// ── Pictures and files ─────────────────────────────────────────────────────

/** Save the frame on the map as a PNG with the whole FORECAST label in the picture. */
export async function saveFramePicture(): Promise<boolean> {
  const r = get(nowcastResult);
  const v = get(nowcastView);
  if (!r) return false;
  const layer = currentLayer(r, v.product, v.leadIndex);
  if (!layer) return false;
  nowcastBusy.set('picture');
  try {
    const image = await imageFromBase64(layer.png);
    const blob = await renderFramePng({
      image,
      label: frameLabel(r, v.leadIndex, v.product),
      legend: layer.legend,
      vmin: layer.vmin,
      vmax: layer.vmax,
      units: layer.kind === 'probability' ? 'probability' : layer.units || r.units,
    });
    return await savePng(exportBaseName(r, v.leadIndex, v.product), blob, 'Save the forecast picture');
  } catch (err) {
    addToast('error', `Could not make the picture: ${err instanceof Error ? err.message : String(err)}`, 8000);
    return false;
  } finally {
    nowcastBusy.set(null);
  }
}

/** Save the whole nowcast: NetCDF (CF, with the forecast attributes), the run record, or the overview figure. */
export async function exportNowcast(format: 'nc' | 'json' | 'png'): Promise<boolean> {
  const r = get(nowcastResult);
  if (!r) return false;
  if (!inTauri()) {
    addToast('info', 'Saving the nowcast files needs the desktop app (file dialog); the picture of one frame can be saved here');
    return false;
  }
  nowcastBusy.set('export');
  try {
    const { save } = await import('@tauri-apps/plugin-dialog');
    const stamp = r.originTime.replace(/[^0-9T]/g, '').slice(0, 13);
    const path = await save({
      title: `Save the nowcast as ${format.toUpperCase()}`,
      defaultPath: `fcst_nowcast_${stamp}.${format}`,
      filters: [{ name: format.toUpperCase(), extensions: [format] }],
    });
    if (!path) return false;
    const saved = await new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => {
        off();
        offErr();
        reject(new Error('No response from the engine'));
      }, 120_000);
      const off = wsManager.onMessage('file_saved', (m) => {
        clearTimeout(timer);
        off();
        offErr();
        resolve(String(m.path ?? path));
      });
      const offErr = wsManager.onMessage('nowcast_error', (m) => {
        if (m.kind !== 'nowcast_export') return;
        clearTimeout(timer);
        off();
        offErr();
        reject(new Error(String(m.message ?? 'export failed')));
      });
      void wsManager.send({ type: 'nowcast_export', request_id: newId('nowcast-export'), nowcast_id: r.nowcastId, path, overwrite: true });
    });
    addToast('success', `Saved ${saved}`, 5000);
    return true;
  } catch (err) {
    addToast('error', `Export failed: ${err instanceof Error ? err.message : String(err)}`, 8000);
    return false;
  } finally {
    nowcastBusy.set(null);
  }
}

export async function pickGridFile(): Promise<string | null> {
  if (!inTauri()) return null;
  try {
    const { open } = await import('@tauri-apps/plugin-dialog');
    const p = await open({ title: 'Gridded radar frames', multiple: false, directory: false, filters: [{ name: 'NetCDF', extensions: ['nc', 'nc4', 'cdf'] }] });
    return typeof p === 'string' ? p : null;
  } catch {
    return null;
  }
}

export function clearNowcast(): void {
  stopPlaying();
  if (get(mosaicResult)?.forecast) clearMosaic();
  cache.clear();
  pendingLayers = null;
  nowcastLoaded.set([]);
  nowcastResult.set(null);
  nowcastView.set({ product: DETERMINISTIC, leadIndex: 0, playing: false });
  finishRun({ requestId: null, percent: 0, message: '', error: null });
}

// ── Replies ────────────────────────────────────────────────────────────────

function remember(layers: unknown): ProductKey | null {
  if (!Array.isArray(layers)) return null;
  let key: ProductKey | null = null;
  for (const raw of layers) {
    const l = parseLayer(raw);
    if (!l) continue;
    const k = layerKey(l);
    if (!cache.has(k)) cache.set(k, new Map());
    cache.get(k)!.set(l.lead, l);
    key = k;
  }
  if (key) nowcastLoaded.set([...cache.keys()]);
  return key;
}

async function onResult(msg: any): Promise<void> {
  if (msg.request_id !== get(nowcastRun).requestId) return; // stale or cancelled
  const result = parseResult(msg);
  if (!result || result.corners.length !== 4) {
    finishRun({ error: 'The engine sent a nowcast that cannot be drawn on the map' });
    return;
  }
  stopPlaying();
  cache.clear();
  pendingLayers = null;
  if (!remember(msg.layers)) {
    finishRun({ error: 'The engine sent no map images for the nowcast' });
    return;
  }
  nowcastResult.set(result);
  nowcastView.set({ product: DETERMINISTIC, leadIndex: 0, playing: false });
  const ok = await showFrame(true);
  finishRun({ percent: 100, message: ok ? 'Done' : '', error: null });
  // the viewer that can show it: the 2D map when radar files are open, else the map of the live view
  // (a live run, or a gridded file with nothing open: that view only needs the map)
  const source = get(nowcastSettings).source;
  const mode = source === 'live' || get(openFiles).length === 0 ? 'live' : '2d';
  window.dispatchEvent(new CustomEvent('xradar:set-view-mode', { detail: { mode } }));
}

function onError(msg: any): void {
  const text = String(msg.message ?? 'The nowcast failed');
  if (msg.kind === 'nowcast_export') return; // the export promise reports it
  if (msg.kind === 'nowcast_layers' || (pendingLayers && msg.request_id === pendingLayers.requestId)) {
    pendingLayers = null;
    nowcastBusy.set(null);
    nowcastRun.update((r) => ({ ...r, error: text }));
    // fall back to the product that is in memory
    const v = get(nowcastView);
    if (!cache.has(v.product)) nowcastView.update((x) => ({ ...x, product: DETERMINISTIC }));
    return;
  }
  if (msg.request_id && msg.request_id !== get(nowcastRun).requestId) return;
  finishRun({ error: text });
}

function onProgress(msg: any): void {
  const run = get(nowcastRun);
  if (!run.running) return;
  if (msg.request_id && msg.request_id !== run.requestId) return;
  nowcastRun.update((r) => ({
    ...r,
    percent: typeof msg.percent === 'number' ? Math.max(0, Math.min(100, msg.percent)) : r.percent,
    message: typeof msg.message === 'string' && msg.message ? msg.message : r.message,
  }));
  const a = get(nowcastRun);
  op?.update(a.message ? `${a.message} (${Math.round(a.percent)}%)` : `${Math.round(a.percent)}%`);
}

async function onLayers(msg: any): Promise<void> {
  const pend = pendingLayers;
  if (!pend || msg.request_id !== pend.requestId) return;
  pendingLayers = null;
  const r = get(nowcastResult);
  if (!r || r.nowcastId !== pend.nowcastId) {
    nowcastBusy.set(null);
    return;
  }
  const key = remember(msg.layers);
  nowcastBusy.set(null);
  if (!key) {
    nowcastRun.update((x) => ({ ...x, error: 'The engine sent no map images for this product' }));
    nowcastView.update((x) => (cache.has(x.product) ? x : { ...x, product: DETERMINISTIC }));
    return;
  }
  if (get(nowcastView).product === pend.product) await showFrame(pend.fit);
}

let initialised = false;

/** Register the websocket handlers (once). */
export function initNowcast(): void {
  if (initialised) return;
  initialised = true;
  wsManager.onMessage('nowcast_result', (m) => void onResult(m));
  wsManager.onMessage('nowcast_error', onError);
  wsManager.onMessage('nowcast_cancelled', (m) => {
    if (m.request_id && m.request_id !== get(nowcastRun).requestId) return;
    finishRun({ requestId: null, percent: 0, message: 'Cancelled', error: null });
  });
  wsManager.onMessage('nowcast_layers_result', (m) => void onLayers(m));
  wsManager.onMessage('progress', onProgress);
}
