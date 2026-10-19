/**
 * Environment data (model fields, reanalysis, soundings, satellite, gauges): the consent flow and
 * the settings, driven from the engine's `env_*` messages.
 *
 *   -> {type:'env_status'}                      <- {type:'env_status', providers, settings, cache}
 *   -> {type:'env_estimate', request_id, request}   <- {type:'env_estimate_result', estimate}
 *   -> {type:'env_fetch', request_id, approve}      <- env_progress ... env_fetch_result | env_error | env_cancelled
 *   -> {type:'env_cancel', request_id}
 *   -> {type:'env_set_settings', settings} / {type:'env_clear_cache'}   <- env_status
 *
 * Other features use one call:  `const result = await requestEnvData({provider: 'iem_raob', ...})`.
 * It estimates first, shows the approval dialog (source, size, destination, licence) and downloads
 * only if the user approves; it resolves with the fetch result (files, provenance) or null when the
 * request was declined, cancelled or failed (the dialog already said why). Cached data and local
 * files need no approval and no dialog.
 */
import { writable, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { connectionStatus } from './radarData';
import { IDLE, reduceEnv, type EnvEvent, type EnvFlow } from '../utils/envFlow';

export interface EnvProvider {
  id: string;
  name: string;
  kind: string;
  attribution: string;
  licence: string;
  homepage: string;
  description: string;
  requires_network: boolean;
  inventory: {
    product: string;
    title: string;
    description: string;
    variables: string[];
    resolution: string;
    coverage: string;
    cadence: string;
  }[];
}

export interface EnvSettings {
  cache_dir: string;
  cache_max_mb: number;
  offline: boolean;
  max_concurrent: number;
  effective_cache_dir: string;
  effective_cache_max_mb: number;
  effective_offline: boolean;
  locked: string[];
  default_cache_dir: string;
}

export interface EnvStatus {
  providers: EnvProvider[];
  settings: EnvSettings;
  cache: { root: string; bytes: number; files: number; max_bytes: number };
  loaded: boolean;
}

export const envStatus = writable<EnvStatus | null>(null);
export const envFlow = writable<EnvFlow>(IDLE);
export const envSettingsError = writable<string | null>(null);

let counter = 0;
let resolveCurrent: ((r: any | null) => void) | null = null;

function dispatch(e: EnvEvent): EnvFlow {
  const before = get(envFlow);
  const after = reduceEnv(before, e);
  if (after !== before) envFlow.set(after);
  if (after.phase === 'done' || after.phase === 'error' || after.phase === 'cancelled' || (after.phase === 'idle' && before.phase !== 'idle')) {
    // 'error' keeps the dialog open so the user can read it; the caller is released at once.
    const done = resolveCurrent;
    resolveCurrent = null;
    done?.(after.phase === 'done' ? after.result : null);
  }
  return after;
}

function send(msg: Record<string, unknown>): void {
  void wsManager.send(msg);
}

/** Ask for environment data. See the file header. */
export function requestEnvData(request: Record<string, unknown>): Promise<any | null> {
  if (get(connectionStatus) !== 'connected') return Promise.resolve(null);
  // One consent flow at a time: a new request replaces an unanswered one.
  const stale = resolveCurrent;
  resolveCurrent = null;
  stale?.(null);
  const requestId = `env-${Date.now().toString(36)}-${++counter}`;
  dispatch({ type: 'ask', requestId, request });
  return new Promise((resolve) => {
    resolveCurrent = resolve;
    send({ type: 'env_estimate', request_id: requestId, request });
  });
}

/** The user pressed "Download" in the approval dialog. */
export function approveEnvFetch(): void {
  const s = get(envFlow);
  if (s.phase !== 'confirm' || !s.requestId) return;
  dispatch({ type: 'approve', requestId: s.requestId });
  send({ type: 'env_fetch', request_id: s.requestId, approve: true });
}

/** "Cancel" in the dialog: before approval it just closes; while downloading it stops the transfer. */
export function cancelEnvFlow(): void {
  const s = get(envFlow);
  if (s.phase === 'downloading' && s.requestId) {
    send({ type: 'env_cancel', request_id: s.requestId });
    return; // the engine answers env_cancelled
  }
  dismissEnvFlow();
}

export function dismissEnvFlow(): void {
  dispatch({ type: 'dismiss' });
}

// ── status and settings ─────────────────────────────────────────────────────────────────────────

export function loadEnvStatus(): void {
  if (get(connectionStatus) === 'connected') send({ type: 'env_status' });
}

export function setEnvSettings(changes: Partial<Pick<EnvSettings, 'cache_dir' | 'cache_max_mb' | 'offline' | 'max_concurrent'>>): void {
  envSettingsError.set(null);
  send({ type: 'env_set_settings', settings: changes });
}

export function clearEnvCache(): void {
  send({ type: 'env_clear_cache' });
}

function onStatus(msg: any): void {
  if (!msg?.settings) return;
  envStatus.update((old) => ({
    providers: Array.isArray(msg.providers) ? msg.providers : (old?.providers ?? []),
    settings: msg.settings,
    cache: msg.cache,
    loaded: true,
  }));
  envSettingsError.set(null);
}

let started = false;
export function initEnvironment(): void {
  if (started) return;
  started = true;
  wsManager.onMessage('env_status', onStatus);
  wsManager.onMessage('env_estimate_result', (msg) => {
    const after = dispatch({ type: 'estimate', requestId: msg.request_id, estimate: msg.estimate });
    // nothing to download (cached / local): no consent is needed, fetch straight away
    if (after.phase === 'downloading' && after.requestId === msg.request_id && !after.estimate?.requires_approval) {
      send({ type: 'env_fetch', request_id: msg.request_id });
    }
  });
  wsManager.onMessage('env_progress', (msg) => {
    dispatch({
      type: 'progress',
      requestId: msg.request_id,
      progress: {
        percent: Number(msg.percent ?? 0),
        message: String(msg.message ?? ''),
        file: String(msg.file ?? ''),
        bytes_done: Number(msg.bytes_done ?? 0),
        bytes_total: msg.bytes_total ?? null,
      },
    });
  });
  wsManager.onMessage('env_fetch_result', (msg) => dispatch({ type: 'result', requestId: msg.request_id, result: msg.result }));
  wsManager.onMessage('env_cancelled', (msg) => dispatch({ type: 'cancelled', requestId: msg.request_id ?? null }));
  wsManager.onMessage('env_error', (msg) => {
    const flow = get(envFlow);
    if (flow.requestId && msg.request_id === flow.requestId) {
      dispatch({ type: 'error', requestId: msg.request_id, message: String(msg.message ?? 'Environment data request failed'), code: msg.code });
    } else if (msg.code === 'invalid' || msg.code === 'unavailable') {
      envSettingsError.set(String(msg.message ?? ''));
    }
  });
}
initEnvironment();

/** Loaded when the connection comes up, so the panels have providers and settings. */
connectionStatus.subscribe((s) => {
  if (s === 'connected') loadEnvStatus();
});
