/**
 * The observed lightning layer (GLM, LMA): the files the user opened, what the engine read from them, and
 * how the layer is shown. Pure logic is in utils/lightningLayer.ts.
 *
 *   -> {type:'lightning_open', request_id, paths, box?, parallax_to_km?, max_chi2?, min_stations?, link?}
 *   <- {type:'lightning_result', request_id, kind, points, flashes, coverage, meta, provenance, ...}
 *      | {type:'lightning_error', request_id, message}
 *
 * Files are read from the engine's disk (desktop app); nothing is downloaded. The time window of the layer
 * follows the valid time of the active radar volume (`activeScanTime`).
 */
import { writable, derived, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { connectionStatus, radarData } from './radarData';
import { activeScanTime } from './layers';
import {
  DEFAULT_OPEN,
  DEFAULT_WINDOW,
  buildOpenRequest,
  decodeLightning,
  placeAll,
  scenePoints,
  visiblePoints,
  windowSeconds,
  type LightningData,
  type OpenParams,
  type Placed,
  type ScenePoint,
  type Visible,
  type WindowSpec,
} from '../utils/lightningLayer';

export interface LightningState {
  data: LightningData | null;
  loading: boolean;
  error: string | null;
  requestId: string | null;
  paths: string[];
}

export const lightning = writable<LightningState>({ data: null, loading: false, error: null, requestId: null, paths: [] });

export interface LightningSettings {
  show2d: boolean;
  show3d: boolean;
  window: WindowSpec;
  open: OpenParams;
}

const KEY = 'xradar-lightning-settings';

function defaults(): LightningSettings {
  return { show2d: true, show3d: true, window: { ...DEFAULT_WINDOW }, open: structuredClone(DEFAULT_OPEN) };
}

function load(): LightningSettings {
  const d = defaults();
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? 'null');
    if (!raw || typeof raw !== 'object') return d;
    const num = (v: unknown, fb: number, lo: number, hi: number) => (typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : fb);
    return {
      show2d: typeof raw.show2d === 'boolean' ? raw.show2d : d.show2d,
      show3d: typeof raw.show3d === 'boolean' ? raw.show3d : d.show3d,
      window: {
        startMin: num(raw.window?.startMin, d.window.startMin, -120, 120),
        lengthMin: num(raw.window?.lengthMin, d.window.lengthMin, 0.5, 120),
        decayMin: num(raw.window?.decayMin, d.window.decayMin, 0.1, 60),
      },
      open: {
        parallaxOn: !!raw.open?.parallaxOn,
        parallaxKm: num(raw.open?.parallaxKm, d.open.parallaxKm, 0, 25),
        qc: {
          maxChi2: num(raw.open?.qc?.maxChi2, d.open.qc.maxChi2, 0.1, 50),
          minStations: Math.round(num(raw.open?.qc?.minStations, d.open.qc.minStations, 0, 30)),
        },
        link: {
          distanceM: num(raw.open?.link?.distanceM, d.open.link.distanceM, 100, 50000),
          timeS: num(raw.open?.link?.timeS, d.open.link.timeS, 0.005, 10),
          minPoints: Math.round(num(raw.open?.link?.minPoints, d.open.link.minPoints, 1, 50)),
          maxDurationS: raw.open?.link?.maxDurationS === null ? null : num(raw.open?.link?.maxDurationS, 3, 0.5, 60),
        },
      },
    };
  } catch {
    return d;
  }
}

/** How the layer is shown and read. Remembered per viewer in the browser; the data themselves are not. */
export const lightningSettings = writable<LightningSettings>(load());

lightningSettings.subscribe((s) => {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* blocked storage: the settings are simply not remembered */
  }
});

export function resetLightningSettings(): void {
  lightningSettings.set(defaults());
}

/** Valid time of the active radar volume (epoch ms), or null. */
export const volumeMs = derived(activeScanTime, ($t) => {
  const ms = $t ? Date.parse($t) : NaN;
  return Number.isFinite(ms) ? ms : null;
});

/** The seconds-after-reference window the layer shows now (null without data or a radar volume). */
export const lightningWindow = derived([lightning, lightningSettings, volumeMs], ([$l, $s, $v]) =>
  $l.data && $v !== null ? windowSeconds($l.data, $v, $s.window) : null,
);

// ── Requests ────────────────────────────────────────────────────────────────

let counter = 0;
const nextId = () => `ltg-${Date.now().toString(36)}-${++counter}`;

/** The radar of the open file (position and the range to read lightning for), or null. */
function currentRadar(): { lat: number; lon: number; rangeKm: number } | null {
  const a = get(radarData).attributes ?? {};
  const num = (v: unknown) => (v === null || v === undefined || v === '' ? NaN : Number(v));
  const lat = [a.latitude, a.origin_latitude, a.siteLat].map(num).find(Number.isFinite);
  const lon = [a.longitude, a.origin_longitude, a.siteLon].map(num).find(Number.isFinite);
  if (lat === undefined || lon === undefined || (lat === 0 && lon === 0)) return null;
  return { lat, lon, rangeKm: 450 };
}

/** Read lightning files (absolute paths on the engine's disk). With a radar open, only that radar's neighbourhood (450 km) is read. */
export function openLightningFiles(paths: string[]): void {
  if (get(connectionStatus) !== 'connected') {
    lightning.update((s) => ({ ...s, error: 'The engine is not connected yet.' }));
    return;
  }
  const requestId = nextId();
  lightning.update((s) => ({ ...s, loading: true, error: null, requestId, paths }));
  void wsManager.send(buildOpenRequest(requestId, paths, currentRadar(), get(lightningSettings).open));
}

/** Read the same files again with the current read parameters (parallax, quality filter, clustering). */
export function rereadLightning(): void {
  const { paths } = get(lightning);
  if (paths.length) openLightningFiles(paths);
}

export function clearLightning(): void {
  lightning.set({ data: null, loading: false, error: null, requestId: null, paths: [] });
}

export function initLightning(): void {
  wsManager.onMessage('lightning_result', (msg) => {
    if (msg.request_id && msg.request_id !== get(lightning).requestId) return; // a stale answer
    try {
      const data = decodeLightning(msg);
      lightning.update((s) => ({ ...s, data, loading: false, error: null, requestId: null }));
    } catch (e) {
      lightning.update((s) => ({ ...s, loading: false, error: e instanceof Error ? e.message : String(e), requestId: null }));
    }
  });
  wsManager.onMessage('lightning_error', (msg) => {
    if (msg.request_id && msg.request_id !== get(lightning).requestId) return;
    lightning.update((s) => ({ ...s, loading: false, error: String(msg.message ?? 'The lightning files could not be read.'), requestId: null }));
  });
}
initLightning();

// ── What the viewers draw ───────────────────────────────────────────────────

let placedCache: { data: LightningData; lat: number; lon: number; placed: Placed } | null = null;

/** Positions of all points relative to a radar (cached for the data and the radar). */
export function placedFor(data: LightningData, radarLat: number, radarLon: number): Placed {
  if (placedCache && placedCache.data === data && placedCache.lat === radarLat && placedCache.lon === radarLon) return placedCache.placed;
  const placed = placeAll(data, radarLat, radarLon);
  placedCache = { data, lat: radarLat, lon: radarLon, placed };
  return placed;
}

/** The visible points of the layer now, or null when there is nothing to draw (no data, no volume time). */
export function visibleNow(): Visible | null {
  const { data } = get(lightning);
  const win = get(lightningWindow);
  if (!data || !win) return null;
  return visiblePoints(data, win.start, win.end, get(lightningSettings).window.decayMin);
}

let sceneCache: { data: LightningData; key: string; pts: ScenePoint[] } | null = null;

/** Scene positions for the 3D view; cached until the data, window, radar or range change. */
export function sceneNow(radarLat: number, radarLon: number, radarAltKm: number, rangeKm: number): ScenePoint[] {
  const { data } = get(lightning);
  const win = get(lightningWindow);
  if (!data || !win) return [];
  const s = get(lightningSettings);
  const key = [win.start, win.end, s.window.decayMin, radarLat, radarLon, radarAltKm, rangeKm].join('|');
  if (sceneCache && sceneCache.data === data && sceneCache.key === key) return sceneCache.pts;
  const vis = visiblePoints(data, win.start, win.end, s.window.decayMin);
  const pts = scenePoints(data, vis, placedFor(data, radarLat, radarLon), radarAltKm, rangeKm);
  sceneCache = { data, key, pts };
  return pts;
}

declare global {
  interface Window {
    __radarxLightning?: { open: (paths: string[]) => void };
  }
}
// A hook for screenshots and scripts in a development build (no file dialog in a plain browser).
if (typeof window !== 'undefined' && import.meta.env?.DEV) window.__radarxLightning = { open: openLightningFiles };
