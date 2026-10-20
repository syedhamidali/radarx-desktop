/**
 * State of the Cartesian 3D scenes (merged mosaic volume, multi-Doppler wind): which source the 3D
 * tab shows, the decoded volumes and how they are fetched.
 *
 *   -> {type:'get_mosaic_volume', request_id, mosaic_id}
 *   <- {type:'mosaic_volume', ...} + uint8 frames   (websocket manager: `mosaic_volume_ready`)
 *      or {type:'mosaic_error', request_id, for:'volume', message}
 *   -> {type:'get_wind_volume', request_id, wind_id}
 *   <- {type:'wind_volume', ...} + float32 frames   (`wind_volume_ready`)
 *      or {type:'multidoppler_error', request_id, message}
 *
 * The 3D tab (Viewer3D.svelte) lets the user pick the source; the "View in 3D" buttons of the
 * Mosaic and Wind panels fetch the data, select the source and ask the app to open the 3D tab.
 */

import { writable, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { connectionStatus } from './radarData';
import { addToast } from './toasts';
import { mosaicResult } from './mosaic';
import { windResult } from './wind';
import {
  decodeMosaicVolume,
  decodeWindVolume,
  type CartVolume,
  type WindVolume,
} from '../utils/cartesianVolume';

import { source3d } from './source3d';
export { source3d };
export type { Source3d } from './source3d';
export const mosaicVolume = writable<CartVolume | null>(null);
export const windVolume = writable<WindVolume | null>(null);
export const volume3dLoading = writable<{ mosaic: boolean; wind: boolean }>({ mosaic: false, wind: false });

let counter = 0;
const nextId = (p: string) => `${p}-${Date.now().toString(36)}-${++counter}`;
const REPLY_TIMEOUT_MS = 60_000;

function setLoading(kind: 'mosaic' | 'wind', on: boolean): void {
  volume3dLoading.update((l) => ({ ...l, [kind]: on }));
}

/** Ask the app shell to switch the main view to the 3D tab. */
export function showThreeD(): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent('xradar:set-view-mode', { detail: { mode: '3d' } }));
}

interface Pending {
  requestId: string;
  resolve: (ok: boolean) => void;
  timer: ReturnType<typeof setTimeout>;
}
const pending: Record<'mosaic' | 'wind', Pending | null> = { mosaic: null, wind: null };

function finish(kind: 'mosaic' | 'wind', ok: boolean, error?: string): void {
  const p = pending[kind];
  if (!p) return;
  pending[kind] = null;
  clearTimeout(p.timer);
  setLoading(kind, false);
  if (!ok && error) addToast('error', error, 8000);
  p.resolve(ok);
}

function request(kind: 'mosaic' | 'wind', msg: Record<string, unknown>): Promise<boolean> {
  if (get(connectionStatus) !== 'connected') {
    addToast('error', 'Sidecar not connected');
    return Promise.resolve(false);
  }
  finish(kind, false); // a newer request supersedes
  return new Promise<boolean>((resolve) => {
    const requestId = nextId(kind);
    const timer = setTimeout(() => finish(kind, false, 'No response from the sidecar (3D data timed out)'), REPLY_TIMEOUT_MS);
    pending[kind] = { requestId, resolve, timer };
    setLoading(kind, true);
    void wsManager.send({ ...msg, request_id: requestId });
  });
}

/** Fetch the merged volume of a mosaic. Resolves true when it is loaded. */
export function fetchMosaicVolume(mosaicId: string): Promise<boolean> {
  return request('mosaic', { type: 'get_mosaic_volume', mosaic_id: mosaicId });
}

/** Fetch the wind grids of a multi-Doppler analysis. Resolves true when they are loaded. */
export function fetchWindVolume(windId: string): Promise<boolean> {
  return request('wind', { type: 'get_wind_volume', wind_id: windId });
}

/** "View in 3D" of a mosaic: fetch (when not already here), select the source, open the tab. */
export async function viewMosaicIn3D(mosaicId: string): Promise<boolean> {
  const cur = get(mosaicVolume);
  const ok = cur && cur.id === mosaicId ? true : await fetchMosaicVolume(mosaicId);
  if (!ok) return false;
  source3d.set('mosaic');
  showThreeD();
  return true;
}

/** "View in 3D" of a wind analysis. */
export async function viewWindIn3D(windId: string): Promise<boolean> {
  const cur = get(windVolume);
  const ok = cur && cur.id === windId ? true : await fetchWindVolume(windId);
  if (!ok) return false;
  await ensureMosaicBehindWind();
  source3d.set('wind');
  showThreeD();
  return true;
}

/** The merged reflectivity of a mosaic, if there is one, is the better volume under the arrows. */
export async function ensureMosaicBehindWind(): Promise<void> {
  const r = get(mosaicResult);
  if (r?.has_volume && !get(mosaicVolume)) await fetchMosaicVolume(r.mosaic_id);
}

export function clearMosaicVolume(): void {
  mosaicVolume.set(null);
  if (get(source3d) === 'mosaic') source3d.set(get(windVolume) ? 'wind' : 'radar');
}

export function clearWindVolume(): void {
  windVolume.set(null);
  if (get(source3d) === 'wind') source3d.set(get(mosaicVolume) ? 'mosaic' : 'radar');
}

function onMosaicVolume(payload: { header: any; data: Uint8Array }): void {
  const p = pending.mosaic;
  if (!p || payload.header.request_id !== p.requestId) return;
  const vol = decodeMosaicVolume(payload.header, payload.data);
  if (!vol) {
    finish('mosaic', false, 'The 3D mosaic data arrived in an unexpected format');
    return;
  }
  mosaicVolume.set(vol);
  finish('mosaic', true);
}

function onWindVolume(payload: { header: any; data: Float32Array }): void {
  const p = pending.wind;
  if (!p || payload.header.request_id !== p.requestId) return;
  const vol = decodeWindVolume(payload.header, payload.data);
  if (!vol) {
    finish('wind', false, 'The 3D wind data arrived in an unexpected format');
    return;
  }
  windVolume.set(vol);
  finish('wind', true);
}

function onMosaicError(msg: any): void {
  const p = pending.mosaic;
  if (p && msg.request_id === p.requestId) finish('mosaic', false, String(msg.message ?? 'The 3D mosaic could not be loaded'));
}

function onWindError(msg: any): void {
  const p = pending.wind;
  if (p && msg.request_id === p.requestId) finish('wind', false, String(msg.message ?? 'The 3D wind could not be loaded'));
}

let initialised = false;
export function initVolume3d(): void {
  if (initialised || typeof window === 'undefined') return;
  initialised = true;
  wsManager.onMessage('mosaic_volume_ready', onMosaicVolume);
  wsManager.onMessage('wind_volume_ready', onWindVolume);
  wsManager.onMessage('mosaic_error', onMosaicError);
  wsManager.onMessage('multidoppler_error', onWindError);
}

initVolume3d();

// A volume belongs to one mosaic / wind analysis: drop it when that result is replaced or cleared.
mosaicResult.subscribe((r) => {
  const cur = get(mosaicVolume);
  if (cur && (!r || r.mosaic_id !== cur.id)) clearMosaicVolume();
});
windResult.subscribe((r) => {
  const cur = get(windVolume);
  if (cur && (!r || r.wind_id !== cur.id)) clearWindVolume();
});
