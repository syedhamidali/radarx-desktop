/**
 * The sounding shown by the Sounding panel: a radiosonde profile read from a local file (or the
 * synthetic example, or an archive download the user approved) with the engine's labelled
 * diagnostics.
 *
 *   -> {type:'sounding_open', request_id, path, format?, wind_units?}  or  {..., example: true}
 *   <- {type:'sounding_result', request_id, sounding}  |  {type:'sounding_error', request_id, message}
 *
 * An archive sounding goes through the environment framework first (`requestEnvData`): the user
 * sees the source, size and destination and approves before anything is downloaded.
 */
import { writable, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { connectionStatus } from './radarData';
import { requestEnvData } from './environment';
import type { Provenance } from '../utils/provenance';
import type { ParcelKey } from '../utils/soundingView';

export interface SoundingLevels {
  pressure_hpa: (number | null)[];
  height_msl_m: (number | null)[];
  temperature_c: (number | null)[];
  dewpoint_c: (number | null)[];
  u_ms: (number | null)[];
  v_ms: (number | null)[];
  n_levels: number;
  thinned: boolean;
}

export interface SoundingData {
  station: string;
  station_name: string;
  latitude: number | null;
  longitude: number | null;
  launch_time: string;
  source: string;
  format: string;
  file: string;
  surface_height_m: number;
  notes: string[];
  drift_note: string;
  drift: { max_km: number; towards_deg: number; duration_min: number } | null;
  levels: SoundingLevels;
  provenance: Provenance;
  diagnostics: any;
}

export interface SoundingState {
  data: SoundingData | null;
  loading: boolean;
  error: string | null;
  requestId: string | null;
}

export const sounding = writable<SoundingState>({ data: null, loading: false, error: null, requestId: null });
export const selectedParcel = writable<ParcelKey>('surface-based');

let counter = 0;
const nextId = () => `snd-${Date.now().toString(36)}-${++counter}`;

function begin(): string | null {
  if (get(connectionStatus) !== 'connected') {
    sounding.update((s) => ({ ...s, error: 'The engine is not connected yet.' }));
    return null;
  }
  const requestId = nextId();
  sounding.update((s) => ({ ...s, loading: true, error: null, requestId }));
  return requestId;
}

/** Read a local sounding file (any format the engine reads). `windUnits` for a speed column without a unit. */
export function openSoundingFile(path: string, opts: { format?: string; windUnits?: string } = {}): void {
  const requestId = begin();
  if (!requestId) return;
  void wsManager.send({
    type: 'sounding_open',
    request_id: requestId,
    path,
    ...(opts.format ? { format: opts.format } : {}),
    ...(opts.windUnits ? { wind_units: opts.windUnits } : {}),
  });
}

/** The synthetic Weisman-Klemp (1982) sounding: labelled "simulated", for trying the panel. */
export function openExampleSounding(): void {
  const requestId = begin();
  if (!requestId) return;
  void wsManager.send({ type: 'sounding_open', request_id: requestId, example: true });
}

/** Fetch one observed sounding from the public archive (asks for approval first), then show it. */
export async function fetchArchiveSounding(station: string, launchUtc: string): Promise<void> {
  if (get(connectionStatus) !== 'connected') {
    sounding.update((s) => ({ ...s, error: 'The engine is not connected yet.' }));
    return;
  }
  sounding.update((s) => ({ ...s, error: null }));
  const result = await requestEnvData({ provider: 'iem_raob', time_start: launchUtc, params: { station: station.trim().toUpperCase() } });
  const path = result?.paths?.[0];
  if (typeof path === 'string') openSoundingFile(path, { format: 'iem_json' });
}

let started = false;
export function initSounding(): void {
  if (started) return;
  started = true;
  wsManager.onMessage('sounding_result', (msg) => {
    if (msg.request_id && msg.request_id !== get(sounding).requestId) return; // a stale answer
    sounding.set({ data: msg.sounding as SoundingData, loading: false, error: null, requestId: null });
    selectedParcel.set('surface-based');
  });
  wsManager.onMessage('sounding_error', (msg) => {
    if (msg.request_id && msg.request_id !== get(sounding).requestId) return;
    sounding.update((s) => ({ ...s, loading: false, error: String(msg.message ?? 'The sounding could not be read.'), requestId: null }));
  });
}
initSounding();

export function clearSounding(): void {
  sounding.set({ data: null, loading: false, error: null, requestId: null });
}
