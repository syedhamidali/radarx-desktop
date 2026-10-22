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
 *   -> {type:'live_restyle', request_id, mosaic_id, floor_dbz}  <- the same composite redrawn
 *                                                          (a `live_frame` with restyled:true)
 *   -> {type:'live_open_volumes', request_id, stations}  <- `file_opened` per volume, then
 *                                                          {type:'live_volumes_opened', files, pinned}
 *                                                          (no download: the held files are opened)
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
import { openFiles, setActiveFile } from './fileManager';
import { windDeselected, runWind, windRun } from './wind';
import { exportMosaicById } from './mosaic';
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
import {
  engineFloor,
  openableStations,
  openedText,
  multiDopplerBlocked,
  type OpenedVolume,
} from '../utils/liveDisplay';
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
  /** The display floor (dBZ) this picture was drawn with; null = every echo. Set by the engine. */
  floor: number | null;
  /** True when the picture is smoothed (drawn pixels finer than the grid). */
  smoothed: boolean;
  /** Credit of the volumes (source, attribution, licence) as the engine states it. */
  credit: { source?: string; attribution?: string; licence?: string } | null;
}

/** A volume the live engine holds right now (from the last frame's cache report). */
export interface HeldInfo {
  station: string;
  name: string;
  size: number;
  scan_time: string | null;
  on_disk: boolean;
}

/** What the last action (open, save, ...) did, shown under the status line. */
export interface LiveNotice {
  kind: 'info' | 'error';
  text: string;
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
/** The volumes the engine holds (newest per radar), from the last update's cache report. */
export const liveHeld = writable<HeldInfo[]>([]);
/** Volumes opened as normal files from this map (still open ones are counted by `liveOpenedNow`). */
const _opened = writable<OpenedVolume[]>([]);
export const liveOpenedNow = derived([_opened, openFiles], ([$o, $files]) => $o.filter((o) => $files.some((f) => f.id === o.file_id)));
/** The action in progress ('' = none) and the outcome of the last one. */
export const liveAction = writable<string>('');
export const liveNotice = writable<LiveNotice | null>(null);
/** The frame on the map: the chosen one of the loop, else the newest. */
export const liveShownFrame = derived([_frames, liveShownIndex], ([$f, $i]) => {
  if ($f.length === 0) return null;
  if ($i === null || $i < 0 || $i >= $f.length) return $f[$f.length - 1];
  return $f[$i];
});
registerJobSource('live', derived(liveState, (s) => (s.phase === 'listing' || s.phase === 'downloading' || s.phase === 'building' ? 1 : 0)));

/**
 * Session-only consent (never stored), one per step: allowing the listings "while the app is
 * open" never covers a download, so the exact sizes are always shown at least once.
 */
export const liveListConsent = writable<ConsentState>(NO_CONSENT);
export const liveDownloadConsent = writable<ConsentState>(NO_CONSENT);
export const liveConsentPrompt = writable<ConsentPrompt | null>(null);

function patch(p: Partial<LiveState>): void {
  liveState.update((s) => ({ ...s, ...p }));
}

function setRadar(station: string, entry: Partial<RadarStatusEntry>): void {
  liveState.update((s) => ({ ...s, radars: { ...s.radars, [station]: { ...(s.radars[station] ?? { station, status: 'pending' }), ...entry } } }));
}

// ── Consent ─────────────────────────────────────────────────────────────────────────────────

function consentOf(step: 'list' | 'download') {
  return step === 'list' ? liveListConsent : liveDownloadConsent;
}

function ask(step: 'list' | 'download', text: ConsentText): Promise<ConsentChoice> {
  return new Promise<ConsentChoice>((resolve) => {
    get(liveConsentPrompt)?.answer('cancel');
    const store = consentOf(step);
    const prompt: ConsentPrompt = {
      step,
      station: '',
      text,
      answer: (choice) => {
        if (get(liveConsentPrompt) === prompt) liveConsentPrompt.set(null);
        const r = resolveChoice(get(store), choice);
        store.set(r.state);
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
        floor: typeof header.display?.floor_dbz === 'number' ? header.display.floor_dbz : null,
        smoothed: Number(header.display?.smoothing ?? 1) > 1,
        credit: header.credit && typeof header.credit === 'object' ? header.credit : null,
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
    if (needsPrompt(get(liveListConsent), 'list')) {
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
    if (plan.toDownload.length > 0 && needsPrompt(get(liveDownloadConsent), 'download')) {
      patch({ phase: 'waiting' });
      const choice = await ask('download', describeLiveDownload(plan, get(settings).live.intervalMin));
      if (choice === 'cancel' || !alive()) { cancelOp(op.id); if (alive()) stop(); return; }
      // The timer runs only when both steps were allowed for the session.
      patch({ onceOnly: choice === 'once' || !get(liveListConsent).session });
    }
    // 5. download + composite
    patch({ phase: plan.toDownload.length ? 'downloading' : 'building' });
    op.update(plan.toDownload.length ? `downloading ${plan.toDownload.length} volume${plan.toDownload.length === 1 ? '' : 's'}` : 'building the composite');
    const updateId = newRequestId();
    const files: LiveFileRequest[] = plan.files;
    const payload = await rpc<{ header: any; png: Uint8Array }>(
      () => void wsManager.send({ type: 'live_update', request_id: updateId, files, variable: 'DBZH', product: 'composite', floor_dbz: engineFloor(get(settings).live.floorDbz) }),
      'live_frame_ready', updateId, UPDATE_TIMEOUT_MS, op.signal,
    );
    if (!alive()) return;
    const frame = await makeFrame(payload.header, payload.png);
    if (!alive()) { URL.revokeObjectURL(frame.url); return; }
    liveHeld.set(Array.isArray(payload.header.cache?.stations) ? payload.header.cache.stations : []);
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
  // Frames of other radars are not a recent loop of these ones.
  const lastFrame = get(_frames)[get(_frames).length - 1];
  if (lastFrame && !lastFrame.radars.every((r) => stations.includes(r.station))) clearLiveFrames();
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
  liveHeld.set([]);
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
  restyleGone.clear();
  liveShownIndex.set(null);
  liveLoopPlaying.set(false);
  removeLayer(LIVE_LAYER_ID);
}

// ── Redraw with another display floor ───────────────────────────────────────────────────────

const restyleBusy = new Set<string>();
/** Frames the engine could no longer redraw (their composite left its cache): they keep their floor. */
const restyleGone = new Set<string>();

/**
 * Make the shown picture follow the "Hide echo below N dBZ" setting: ask the engine to draw the
 * same composite again with that floor (no download, no new merge). A frame whose composite has
 * left the engine's small cache keeps the floor it was drawn with, and its label says so.
 */
export async function ensureFrameFloor(frame: LiveFrame | null): Promise<void> {
  if (!frame || get(connectionStatus) !== 'connected') return;
  const want = engineFloor(get(settings).live.floorDbz);
  if (frame.floor === want || restyleBusy.has(frame.id) || restyleGone.has(`${frame.id}:${want}`)) return;
  restyleBusy.add(frame.id);
  const ctl = new AbortController();
  try {
    const id = newRequestId();
    const payload = await rpc<{ header: any; png: Uint8Array }>(
      () => void wsManager.send({ type: 'live_restyle', request_id: id, mosaic_id: frame.mosaicId, floor_dbz: want }),
      'live_frame_ready', id, 30_000, ctl.signal,
    );
    const fresh = await makeFrame(payload.header, payload.png);
    // keep everything the frame knew (radars, time, id); take the new picture and its floor
    const updated: LiveFrame = { ...frame, url: fresh.url, image: fresh.image, floor: fresh.floor, smoothed: fresh.smoothed };
    const cur = get(_frames);
    if (!cur.some((f) => f.id === frame.id)) { URL.revokeObjectURL(fresh.url); return; }
    _frames.set(cur.map((f) => (f.id === frame.id ? updated : f)));
    dropFrames([frame]);
  } catch (err) {
    restyleGone.add(`${frame.id}:${want}`);
    if (!(err instanceof Error) || err.message !== 'cancelled') console.info('[live] could not redraw the frame:', err);
  } finally {
    restyleBusy.delete(frame.id);
  }
}

// ── Open the held volumes: viewers, 3D, Multi-Doppler ───────────────────────────────────────

function setNotice(kind: LiveNotice['kind'], text: string): void {
  liveNotice.set({ kind, text });
}

export function clearLiveNotice(): void {
  liveNotice.set(null);
}

/** What an opening step is going to do, in words (the buttons' tooltips): the held volumes and their sizes. */
export function heldSummary(held: readonly HeldInfo[], stations?: readonly string[]): string {
  const rows = held.filter((h) => !stations || stations.includes(h.station));
  const total = rows.reduce((n, h) => n + (h.size || 0), 0);
  return `${rows.length} volume${rows.length === 1 ? '' : 's'}, ${(total / 1024 ** 2).toFixed(1)} MB, already downloaded (no new download)`;
}

type AfterOpen = 'open' | '3d' | 'wind';

/**
 * Open the volumes the engine holds for `stations` as normal files (2D / 3D viewers, Mosaic,
 * Multi-Doppler, cells, modules and export then work on them). Nothing is downloaded: the engine
 * hands the files it already holds to the normal open, and pins them so the next live update does
 * not replace them; closing the file releases the disk again. `then` also switches the view to 3D,
 * or opens the Multi-Doppler panel with these radars ticked and runs it (the engine's own
 * explanation is shown when the radars do not overlap).
 */
export async function openLiveVolumes(stations: readonly string[], then: AfterOpen = 'open'): Promise<boolean> {
  if (get(liveAction)) return false;
  const want = [...stations];
  if (want.length === 0) {
    setNotice('error', 'No radar has a volume to open yet. Update the live map first.');
    return false;
  }
  if (get(connectionStatus) !== 'connected') {
    setNotice('error', 'The engine is not connected.');
    return false;
  }
  const label = then === 'wind' ? 'Opening the volumes for multi-Doppler winds' : then === '3d' ? 'Opening the volumes for 3D' : 'Opening live volumes';
  liveAction.set(label);
  setNotice('info', `${label}: ${heldSummary(get(liveHeld), want)}.`);
  const op = beginOp({ kind: 'open', label, slowMs: 8_000, timeoutMs: 0 });
  try {
    const id = newRequestId();
    const reply = await rpc<{ files: OpenedVolume[]; failed: string[]; pinned: { bytes: number } }>(
      () => void wsManager.send({ type: 'live_open_volumes', request_id: id, stations: want, known_file_ids: get(openFiles).map((f) => f.id) }),
      'live_volumes_opened', id, 5 * 60_000, op.signal,
    );
    op.done();
    _opened.update((o) => [...o.filter((x) => !reply.files.some((f) => f.file_id === x.file_id)), ...reply.files]);
    const ids = reply.files.map((f) => f.file_id);
    // The first radar of the list is the one in front (the engine opened them in this order).
    if (ids.length > 1 && get(openFiles).some((f) => f.id === ids[0])) setActiveFile(ids[0], { force: true });
    let text = openedText(reply.files, reply.pinned?.bytes ?? 0, reply.failed ?? []);
    if (then === '3d') {
      window.dispatchEvent(new CustomEvent('xradar:set-view-mode', { detail: { mode: '3d' } }));
    } else if (then === 'wind') {
      const why = multiDopplerBlocked(reply.files.map((f) => f.station));
      // Tick exactly these radars, show the panel and run: an engine problem (no overlap, ...) is
      // shown by the panel and repeated here in plain words.
      windDeselected.set(get(openFiles).filter((f) => !ids.includes(f.id)).map((f) => f.id));
      window.dispatchEvent(new CustomEvent('xradar:set-view-mode', { detail: { mode: '2d' } }));
      window.dispatchEvent(new CustomEvent('xradar:show-panel', { detail: { id: 'wind' } }));
      if (why) text += ` ${why}`;
      runWind();
      watchWindOutcome();
    } else {
      window.dispatchEvent(new CustomEvent('xradar:set-view-mode', { detail: { mode: '2d' } }));
    }
    setNotice('info', text);
    return true;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (!op.signal.aborted) op.fail(msg);
    setNotice('error', msg.replace(/^live_open_volumes: /, ''));
    return false;
  } finally {
    liveAction.set('');
  }
}

/** After a Multi-Doppler run started from the live map: say plainly how it ended (the engine's words). */
function watchWindOutcome(): void {
  let seenRunning = get(windRun).running;
  let done = false;
  let off: (() => void) | null = null;
  const finish = () => {
    done = true;
    off?.();
  };
  // a store calls its subscriber at once, before `off` exists: `done` covers that case
  off = windRun.subscribe((r) => {
    if (done) return;
    if (r.running) seenRunning = true;
    if (!r.running && (seenRunning || r.error)) {
      if (r.error) setNotice('error', `Multi-Doppler: ${r.error}`);
      finish();
    }
  });
  if (done) off();
  setTimeout(finish, 20 * 60_000);
}

/** The newest volumes the live map holds, as the stations of the shown frame in order. */
export function openableNow(): string[] {
  const frame = get(liveShownFrame);
  const held = get(liveHeld).filter((h) => h.on_disk).map((h) => h.station);
  return openableStations(frame?.radars.map((r) => ({ station: r.station })) ?? [], held);
}

// ── Save the composite ──────────────────────────────────────────────────────────────────────

/**
 * Save the shown composite as NetCDF (the merged grid and its provenance) or a georeferenced PNG
 * (with world file and projection for GIS). The files carry the data credit of the volumes, the
 * analysis time and the note that this is not an official product. The NetCDF holds the grid
 * values themselves, not the display floor.
 */
export async function saveLiveComposite(format: 'netcdf' | 'png'): Promise<boolean> {
  const frame = get(liveShownFrame);
  if (!frame) { setNotice('error', 'There is no composite to save yet.'); return false; }
  const stamp = (frame.time ?? '').replace(/[-:]/g, '').replace(/\.\d+/, '').slice(0, 13);
  const base = `live_composite_${frame.radars.map((r) => r.station).join('-')}_${stamp || 'time_unknown'}`;
  return exportMosaicById(frame.mosaicId, format, base);
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
