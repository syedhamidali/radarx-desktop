/**
 * State of the (remote) engine in web mode.
 *
 * `webInfo` holds the `web_info` message the engine sends on connect; `webUi` is true in a browser when the
 * engine said it runs in web mode (or, before it said anything, when a remote engine is configured) -
 * that is the single switch the UI adapts on. `engineState` describes the connection to a remote engine so the UI can explain a
 * cold-starting free host ("waking up") or a rejected connection (server full, origin refused).
 */
import { writable, derived, get } from 'svelte/store';
import { isWebMode, engineUrl } from '../utils/env';

export interface WebLimits {
  max_upload_mb: number;
  max_sessions: number;
  idle_timeout_s: number;
  max_open_files: number;
}

export interface WebFeatures {
  uploads: boolean;
  cloud: boolean;
  local_paths: boolean;
  downloads: boolean;
}

export interface WebInfo {
  web_mode: boolean;
  limits: WebLimits;
  features: WebFeatures;
}

const DEFAULT_LIMITS: WebLimits = { max_upload_mb: 200, max_sessions: 0, idle_timeout_s: 0, max_open_files: 0 };
const DEFAULT_FEATURES: WebFeatures = { uploads: true, cloud: true, local_paths: false, downloads: true };

/** Coerce an untrusted `web_info` message into a WebInfo (null when it is not one). */
export function parseWebInfo(msg: any): WebInfo | null {
  if (!msg || typeof msg !== 'object' || msg.type !== 'web_info') return null;
  const l = msg.limits ?? {};
  const f = msg.features ?? {};
  const num = (v: unknown, d: number) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : d);
  return {
    web_mode: msg.web_mode === true,
    limits: {
      max_upload_mb: num(l.max_upload_mb, DEFAULT_LIMITS.max_upload_mb),
      max_sessions: num(l.max_sessions, DEFAULT_LIMITS.max_sessions),
      idle_timeout_s: num(l.idle_timeout_s, DEFAULT_LIMITS.idle_timeout_s),
      max_open_files: num(l.max_open_files, DEFAULT_LIMITS.max_open_files),
    },
    features: {
      uploads: f.uploads !== false,
      cloud: f.cloud !== false,
      local_paths: f.local_paths === true,
      downloads: f.downloads !== false,
    },
  };
}

export const webInfo = writable<WebInfo | null>(null);

/** True when the UI should behave as the hosted web app (browser tab + engine in web mode). */
export const webUi = derived(webInfo, ($i) => {
  if (!isWebMode()) return false;
  // Until the engine says otherwise, a configured remote engine (VITE_ENGINE_URL / ?engine=) means
  // the hosted web app: the page must not flash desktop-only UI while the engine is waking up.
  if ($i === null) return engineUrl() !== null;
  return $i.web_mode === true;
});

export type EnginePhase =
  | 'idle' // desktop, or nothing attempted yet
  | 'connecting' // first attempt
  | 'waking' // retrying: a free host that went to sleep takes 30-60 s to start
  | 'connected'
  | 'rejected' // the engine closed the socket and said why (server full, origin refused, ...)
  | 'unreachable' // gave up retrying
  | 'unconfigured'; // web build without VITE_ENGINE_URL / ?engine=

export interface EngineState {
  phase: EnginePhase;
  /** Connection attempts since the last success. */
  attempt: number;
  /** Close reason sent by the engine, if any. */
  reason: string | null;
  /** Seconds since the first failed attempt (only while waking). */
  since: number | null;
}

export const engineState = writable<EngineState>({ phase: 'idle', attempt: 0, reason: null, since: null });

export function setEngineState(patch: Partial<EngineState>): void {
  engineState.update((s) => ({ ...s, ...patch }));
}

/** Limits to enforce client-side (defaults until `web_info` arrived). */
export function currentLimits(): WebLimits {
  return get(webInfo)?.limits ?? DEFAULT_LIMITS;
}

/** Message shown while the engine is not reachable (null when connected / not applicable). */
export function engineStatusMessage(s: EngineState): string | null {
  switch (s.phase) {
    case 'waking':
      return 'Engine waking up (free hosts sleep)... this can take up to a minute.';
    case 'connecting':
      return 'Connecting to the engine...';
    case 'rejected':
      return `The engine refused the connection${s.reason ? `: ${s.reason}` : '.'}`;
    case 'unreachable':
      return 'The engine could not be reached. Reload the page to try again.';
    case 'unconfigured':
      return 'No engine configured. Set VITE_ENGINE_URL at build time, or open the page with ?engine=wss://your-engine.';
    default:
      return null;
  }
}
