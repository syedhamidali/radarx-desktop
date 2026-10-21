/**
 * The live map: the newest volumes of the default radars, merged by the engine into one
 * composite, refreshed on a timer, with the last few composites kept as a "recent loop".
 *
 *   -> {type:'live_check', request_id, stations}        <- {type:'live_check_result', stations:[...]}
 *   -> {type:'live_update', request_id, files:[...]}    <- progress*, then {type:'live_frame', ...} + PNG
 *                                                          frames (the websocket manager emits
 *                                                          `live_frame_ready` {header, png}), or
 *                                                          {type:'live_error', request_id, message}
 *   -> {type:'live_stop'}                               <- {type:'live_stopped'}
 *
 * Network rule (docs/SCIENCE.md 2.7): the first start asks twice, once for the listings and once
 * for the downloads with their exact sizes; "Allow once" makes one update and pauses, "Allow while
 * the app is open" lets the timer run (memory only, forgotten on reload). Offline mode of the
 * environment settings stops every request. Every update is an operation in the tray (Cancel
 * stops waiting), and errors back the timer off (docs/LIVE.md).
 *
 * ── Frame store API (the input of a future nowcast layer; no forecast is made here) ──────────
 *
 *   liveFrames: Readable<LiveFrame[]>   oldest first, at most MAX_FRAMES (12), all in memory.
 *   LiveFrame {
 *     id          unique per frame
 *     time        analysis time of the composite (UTC ISO, the newest volume start), or null
 *     receivedAt  epoch ms when the app received it
 *     radars      [{station, name, scan_time}]: the exact volumes merged into this frame
 *     grid        {nx, ny, resolution_m, west, south, east, north}: Web-Mercator-uniform grid
 *     corners     [lon, lat] of NW, NE, SE, SW
 *     variable, units, vmin, vmax, cmap, legend: the colour scale of the image
 *     mosaicId    engine id of the float grid (valid while the engine keeps it; a probe or an
 *                 export can ask the engine for the values under this id)
 *     url, image  the rendered PNG (object URL; revoked when the frame leaves the ring)
 *     provenance  the engine's provenance record (RETRIEVED: a merged, gridded product)
 *   }
 *   liveShownIndex: Writable<number | null>   which frame the map shows; null = the newest
 *   The array only ever grows at the end and drops from the front; a frame that would repeat the
 *   newest one (same volumes) is not added. Consumers must not keep `image` past a frame's removal.
 */

import { writable, derived, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { connectionStatus } from './radarData';
import { beginOp, cancelOp, type OpHandle } from '../utils/requestScheduler';
import { registerJobSource } from './jobSources';
import { addToast } from './toasts';
import { settings } from './settings';
import { envStatus } from './environment';
import { layerMap, removeLayer } from './layers';
import { withLayer } from '../utils/layerRegistry';
import { parseProvenance, type Provenance } from '../utils/provenance';
import { NO_CONSENT, needsPrompt, resolveChoice, type ConsentState, type ConsentChoice, type ConsentText } from '../utils/radarSites';
import type { ConsentPrompt } from './radarSites';
import {
  normalizeRadarIds,
  nextDelayMs,
  planDownloads,
  sameVolumes,
  describeLiveListing,
  describeLiveDownload,
  pushFrame,
  DEFAULT_MAX_FRAMES,
  type LiveCheckEntry,
  type LiveFileRequest,
  type RadarStatusEntry,
  type FrameMeta,
} from '../utils/liveLogic';
import type { MosaicRadarInfo } from './mosaic';

// ── Types ───────────────────────────────────────────────────────────────────────────────────

export type LivePhase = 'idle' | 'listing' | 'waiting' | 'downloading' | 'building' | 'live' | 'paused' | 'error';

export interface LiveState {
  /** True from start() to stop(); the timer may be paused meanwhile. */
  running: boolean;
  phase: LivePhase;
  paused: boolean;
  /** The radars this session was started with (the settings may change meanwhile). */
  stations: string[];
  radars: Record<string, RadarStatusEntry>;
  /** Epoch ms of the last successful update and of the next scheduled one. */
  lastUpdateAt: number | null;
  nextDueAt: number | null;
  failures: number;
  /** Human text of the last problem ('' when none). */
  message: string;
  /** The user chose "Allow once": one update, then the timer stays off. */
  onceOnly: boolean;
}

export interface LiveFrame extends FrameMeta {
  grid: { nx: number; ny: number; resolution_m: number; west: number; south: number; east: number; north: number };
  corners: [number, number][];
  variable: string;
  units: string;
  vmin: number;
  vmax: number;
  cmap: string;
  legend: [number, string][];
  mosaicId: string;
  radarInfo: MosaicRadarInfo[];
  url: string;
  image: HTMLImageElement;
  provenance: Provenance | null;
  warnings: string[];
}

export const MAX_FRAMES = DEFAULT_MAX_FRAMES;
export const LIVE_LAYER_ID = 'live:composite';
export const LIVE_RING_KM = 230;

const IDLE: LiveState = {
  running: false,
  phase: 'idle',
  paused: false,
  stations: [],
  radars: {},
  lastUpdateAt: null,
  nextDueAt: null,
  failures: 0,
  message: '',
  onceOnly: false,
};

// ── Stores ──────────────────────────────────────────────────────────────────────────────────

export const liveState = writable<LiveState>({ ...IDLE });
const _frames = writable<LiveFrame[]>([]);
export const liveFrames = { subscribe: _frames.subscribe };
export const liveShownIndex = writable<number | null>(null);
export const liveLoopPlaying = writable<boolean>(false);
/** The frame on the map: the chosen one of the loop, else the newest. */
export const liveShownFrame = derived([_frames, liveShownIndex], ([$f, $i]) => {
  if ($f.length === 0) return null;
  if ($i === null || $i < 0 || $i >= $f.length) return $f[$f.length - 1];
  return $f[$i];
});
registerJobSource('live', derived(liveState, (s) => (s.phase === 'listing' || s.phase === 'downloading' || s.phase === 'building' ? 1 : 0)));

/** Session-only consent (never stored). */
export const liveConsent = writable<ConsentState>(NO_CONSENT);
export const liveConsentPrompt = writable<ConsentPrompt | null>(null);

function patch(p: Partial<LiveState>): void {
  liveState.update((s) => ({ ...s, ...p }));
}

function setRadar(station: string, entry: Partial<RadarStatusEntry>): void {
  liveState.update((s) => ({ ...s, radars: { ...s.radars, [station]: { ...(s.radars[station] ?? { station, status: 'pending' }), ...entry } } }));
}

// ── Consent ─────────────────────────────────────────────────────────────────────────────────

function ask(step: 'list' | 'download', text: ConsentText): Promise<ConsentChoice> {
  return new Promise<ConsentChoice>((resolve) => {
    get(liveConsentPrompt)?.answer('cancel');
    const prompt: ConsentPrompt = {
      step,
      station: '',
      text,
      answer: (choice) => {
        if (get(liveConsentPrompt) === prompt) liveConsentPrompt.set(null);
        const r = resolveChoice(get(liveConsent), choice);
        liveConsent.set(r.state);
        resolve(choice);
      },
    };
    liveConsentPrompt.set(prompt);
  });
}

// ── Engine round trips ──────────────────────────────────────────────────────────────────────

let requestCounter = 0;
function newRequestId(): string {
  requestCounter += 1;
  return `live-${Date.now().toString(36)}-${requestCounter}`;
}

const CHECK_TIMEOUT_MS = 90_000;
const UPDATE_TIMEOUT_MS = 10 * 60_000;

/** One request / one matching reply (or a `live_error` with the same request id), bounded. */
function rpc<T>(
  send: () => void,
  okType: string,
  requestId: string,
  timeoutMs: number,
  signal: AbortSignal,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const unsubs: Array<() => void> = [];
    let done = false;
    const finish = (fn: () => void) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      unsubs.forEach((u) => u());
      fn();
    };
    const timer = setTimeout(() => finish(() => reject(new Error('the engine did not answer in time'))), timeoutMs);
    unsubs.push(wsManager.onMessage(okType, (m: any) => {
      const id = m?.request_id ?? m?.header?.request_id;
      if (id === requestId) finish(() => resolve(m as T));
    }));
    unsubs.push(wsManager.onMessage('live_error', (m: any) => {
      if (m?.request_id === requestId) finish(() => reject(new Error(String(m?.message ?? 'live map error'))));
    }));
    let first = true;
    unsubs.push(connectionStatus.subscribe((st) => {
      if (first) { first = false; return; }
      if (st !== 'connected') finish(() => reject(new Error('connection to the engine was lost')));
    }));
    if (signal.aborted) return finish(() => reject(new Error('cancelled')));
    const onAbort = () => finish(() => reject(new Error('cancelled')));
    signal.addEventListener('abort', onAbort, { once: true });
    unsubs.push(() => signal.removeEventListener('abort', onAbort));
    send();
  });
}

// ── The update cycle ────────────────────────────────────────────────────────────────────────

let timer: ReturnType<typeof setTimeout> | null = null;
let currentOp: OpHandle | null = null;
let cycleRunning = false;
let session = 0; // bumped by stop(): an update that outlives its session is dropped

function clearTimer(): void {
  if (timer) { clearTimeout(timer); timer = null; }
}

function offlineMode(): boolean {
  return get(envStatus)?.settings?.effective_offline === true;
}

function scheduleNext(failed: boolean): void {
  clearTimer();
  const s = get(liveState);
  if (!s.running || s.paused || s.onceOnly) {
    patch({ nextDueAt: null });
    return;
  }
  const delay = nextDelayMs(get(settings).live.intervalMin, failed ? s.failures : 0);
  patch({ nextDueAt: Date.now() + delay });
  timer = setTimeout(() => { timer = null; void runUpdate(); }, delay);
}

/** Build the frame object from a `live_frame_ready` payload (decodes the PNG). */
function makeFrame(header: any, png: Uint8Array): Promise<LiveFrame> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(new Blob([png as BlobPart], { type: 'image/png' }));
    const img = new Image();
    img.onload = () => {
      const stations: any[] = Array.isArray(header.stations) ? header.stations : [];
      resolve({
        id: `${header.mosaic_id ?? 'frame'}-${Date.now().toString(36)}`,
        time: typeof header.analysis_time === 'string' ? header.analysis_time : null,
        receivedAt: Date.now(),
        radars: stations.filter((s) => s?.status === 'ok').map((s) => ({ station: String(s.station), name: String(s.name), scan_time: s.scan_time ?? null })),
        grid: header.grid,
        corners: header.corners,
        variable: String(header.variable ?? 'DBZH'),
        units: String(header.units ?? ''),
        vmin: Number(header.vmin),
        vmax: Number(header.vmax),
        cmap: String(header.cmap ?? ''),
        legend: Array.isArray(header.legend) ? header.legend : [],
        mosaicId: String(header.mosaic_id ?? ''),
        radarInfo: Array.isArray(header.radars) ? header.radars : [],
        url,
        image: img,
        provenance: parseProvenance(header.provenance),
        warnings: Array.isArray(header.warnings) ? header.warnings : [],
      });
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('the composite image could not be decoded')); };
    img.src = url;
  });
}

function dropFrames(frames: LiveFrame[]): void {
  for (const f of frames) setTimeout(() => URL.revokeObjectURL(f.url), 2000);
}

/** One full update: listing (consent), plan, download (consent), composite. */
async function runUpdate(): Promise<void> {
  const mySession = session;
  const s0 = get(liveState);
  if (!s0.running || cycleRunning) return;
  if (get(connectionStatus) !== 'connected') {
    patch({ phase: 'error', message: 'The engine is not connected.', failures: s0.failures + 1 });
    scheduleNext(true);
    return;
  }
  if (offlineMode()) {
    patch({ phase: 'paused', paused: true, message: 'Offline mode is on (Environment data settings): no listing and no download.', nextDueAt: null });
    return;
  }
  cycleRunning = true;
  const stations = s0.stations;
  const op = beginOp({
    kind: 'live',
    label: `Live map: updating ${stations.length} radar${stations.length === 1 ? '' : 's'}`,
    slowMs: 20_000,
    timeoutMs: 0,
    onCancel: () => { if (currentOp === op) currentOp = null; },
  });
  currentOp = op;
  const alive = () => session === mySession && get(liveState).running && !op.signal.aborted;
  try {
    // 1. consent for the listing (first time, or every time after "Allow once")
    if (needsPrompt(get(liveConsent), 'list')) {
      patch({ phase: 'waiting', message: '' });
      const choice = await ask('list', describeLiveListing(stations, new Date()));
      if (choice === 'cancel' || !alive()) { cancelOp(op.id); if (alive()) stop(); return; }
      if (choice === 'once') patch({ onceOnly: true });
    }
    // 2. the listings
    patch({ phase: 'listing', message: '' });
    for (const st of stations) setRadar(st, { status: 'pending' });
    const checkId = newRequestId();
    op.update('listing the newest volumes');
    const reply = await rpc<{ stations: LiveCheckEntry[] }>(
      () => void wsManager.send({ type: 'live_check', request_id: checkId, stations }),
      'live_check_result', checkId, CHECK_TIMEOUT_MS, op.signal,
    );
    if (!alive()) return;
    const now = Date.now();
    for (const e of reply.stations ?? []) {
      setRadar(e.station, { status: e.status, scan_time: e.scan_time ?? null, age_s: e.age_s ?? null, receivedAt: now, message: e.message });
    }
    const plan = planDownloads(reply.stations ?? []);
    if (plan.files.length === 0) {
      const why = plan.failed.map((f) => `${f.station}: ${f.message}`).join('; ');
      throw new Error(`No radar has a usable volume (${why})`);
    }
    // 3. nothing new since the shown frame: no download, no rebuild
    const frames = get(_frames);
    const last = frames[frames.length - 1];
    if (plan.toDownload.length === 0 && last && sameVolumes(last.radars, plan.files)) {
      patch({ phase: 'live', lastUpdateAt: Date.now(), failures: 0, message: plan.failed.length ? `Unchanged. Not available: ${plan.failed.map((f) => f.station).join(', ')}.` : 'Unchanged: no radar has a newer volume yet.' });
      op.done();
      scheduleNext(false);
      return;
    }
    // 4. consent for the downloads, with the exact sizes
    if (plan.toDownload.length > 0 && needsPrompt(get(liveConsent), 'download')) {
      patch({ phase: 'waiting' });
      const choice = await ask('download', describeLiveDownload(plan, get(settings).live.intervalMin));
      if (choice === 'cancel' || !alive()) { cancelOp(op.id); if (alive()) stop(); return; }
      if (choice === 'once') patch({ onceOnly: true });
      else patch({ onceOnly: false });
    }
    // 5. download + composite
    patch({ phase: plan.toDownload.length ? 'downloading' : 'building' });
    op.update(plan.toDownload.length ? `downloading ${plan.toDownload.length} volume${plan.toDownload.length === 1 ? '' : 's'}` : 'building the composite');
    const updateId = newRequestId();
    const files: LiveFileRequest[] = plan.files;
    const payload = await rpc<{ header: any; png: Uint8Array }>(
      () => void wsManager.send({ type: 'live_update', request_id: updateId, files, variable: 'DBZH', product: 'composite' }),
      'live_frame_ready', updateId, UPDATE_TIMEOUT_MS, op.signal,
    );
    if (!alive()) return;
    const frame = await makeFrame(payload.header, payload.png);
    if (!alive()) { URL.revokeObjectURL(frame.url); return; }
    for (const r of (payload.header.stations ?? []) as any[]) {
      if (r?.status === 'error') setRadar(String(r.station), { status: 'error', message: String(r.message ?? 'download failed') });
    }
    const pushed = pushFrame(get(_frames), frame, MAX_FRAMES);
    _frames.set(pushed.frames);
    dropFrames(pushed.dropped);
    const shown = get(liveShownIndex);
    if (shown !== null && pushed.dropped.length && pushed.dropped[0] !== frame) {
      // the ring moved: keep pointing at the same frame, or at the newest when it fell out
      liveShownIndex.set(shown > 0 ? shown - 1 : null);
    }
    layerMap.update((m) => withLayer(m, { id: LIVE_LAYER_ID, group: 'Mosaic', title: `Live composite (${frame.radars.length} radar${frame.radars.length === 1 ? '' : 's'})` }, payload.header.provenance));
    const notAvail = plan.failed.length ? ` Not available: ${plan.failed.map((f) => f.station).join(', ')}.` : '';
    patch({ phase: 'live', lastUpdateAt: Date.now(), failures: 0, message: notAvail.trim() });
    op.done();
    scheduleNext(false);
  } catch (err) {
    if (!alive()) return;
    const text = err instanceof Error ? err.message : String(err);
    if (text === 'cancelled') {
      patch({ phase: get(_frames).length ? 'live' : 'paused', message: 'Update cancelled.' });
      scheduleNext(false);
      return;
    }
    const failures = get(liveState).failures + 1;
    patch({ phase: 'error', failures, message: text });
    op.fail(text);
    if (failures === 1) addToast('warning', `Live map: ${text}`, 8000);
    scheduleNext(true);
  } finally {
    if (currentOp === op) currentOp = null;
    cycleRunning = false;
    if (get(liveState).onceOnly && get(liveState).running) patch({ nextDueAt: null, phase: get(_frames).length ? 'live' : get(liveState).phase, message: get(liveState).message || 'Updated once. Choose "Update now" or allow updates while the app is open to keep it live.' });
  }
}

// ── Public actions ──────────────────────────────────────────────────────────────────────────

/** Start (or restart) the live map with the default radars of the settings. */
export function startLive(): boolean {
  const stations = normalizeRadarIds(get(settings).live.radars);
  if (stations.length === 0) return false;
  const s = get(liveState);
  if (s.running && s.stations.join() === stations.join()) {
    if (s.paused) resumeLive();
    return true;
  }
  if (s.running) stop(false);
  session += 1;
  liveState.set({ ...IDLE, running: true, phase: 'listing', stations, radars: Object.fromEntries(stations.map((st) => [st, { station: st, status: 'pending' as const }])) });
  void runUpdate();
  return true;
}

function stop(tellEngine = true): void {
  session += 1;
  clearTimer();
  if (currentOp) { const op = currentOp; currentOp = null; cancelOp(op.id); }
  cycleRunning = false;
  get(liveConsentPrompt)?.answer('cancel');
  liveState.set({ ...IDLE });
  liveLoopPlaying.set(false);
  if (tellEngine && get(connectionStatus) === 'connected') void wsManager.send({ type: 'live_stop' });
}

/** Stop the timer, drop nothing on screen (the frames stay until clearLiveFrames). */
export function stopLive(): void {
  stop(true);
}

export function pauseLive(): void {
  clearTimer();
  patch({ paused: true, nextDueAt: null, phase: get(_frames).length ? 'live' : 'paused' });
}

export function resumeLive(): void {
  const s = get(liveState);
  if (!s.running) { startLive(); return; }
  patch({ paused: false, onceOnly: false });
  if (offlineMode()) { patch({ paused: true, message: 'Offline mode is on (Environment data settings): no listing and no download.' }); return; }
  if (!cycleRunning) void runUpdate();
}

/** Update right away (keeps the session's consent; asks again after "Allow once"). */
export function refreshLive(): void {
  const s = get(liveState);
  if (!s.running) { startLive(); return; }
  if (cycleRunning) return;
  clearTimer();
  patch({ onceOnly: false, paused: false });
  void runUpdate();
}

/** Forget the recent loop (revokes the images). */
export function clearLiveFrames(): void {
  dropFrames(get(_frames));
  _frames.set([]);
  liveShownIndex.set(null);
  liveLoopPlaying.set(false);
  removeLayer(LIVE_LAYER_ID);
}

/** The interval changed: re-arm the timer from now. */
export function rescheduleLive(): void {
  if (get(liveState).running && !cycleRunning) scheduleNext(get(liveState).failures > 0);
}

// ── Lifetime ────────────────────────────────────────────────────────────────────────────────

let initialised = false;

export function initLive(): void {
  if (initialised || typeof window === 'undefined') return;
  initialised = true;
  // The window closes: stop the timer and let the engine release its volumes.
  window.addEventListener('pagehide', () => { if (get(liveState).running) stop(true); });
  connectionStatus.subscribe((st) => {
    if (st !== 'connected' && get(liveState).running && !cycleRunning) {
      patch({ phase: 'error', message: 'Connection to the engine was lost.' });
    }
  });
}

initLive();
