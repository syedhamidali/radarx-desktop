/**
 * The consent flow of environment data as a pure state machine (no Svelte, no sockets), so its
 * rules can be unit tested with node. `stores/environment.ts` drives it from engine messages.
 *
 *   idle -> estimating -> confirm -> downloading -> done
 *                 |           |           |
 *                 +-----------+-----------+--> error | cancelled
 *
 * The rule the whole feature rests on: a download starts only from `confirm`, only through the
 * user's explicit approval, and only for the estimate that was shown.
 */
import { formatBytes } from './updateLogic.ts';

export interface EnvEstimateFile {
  name: string;
  url?: string | null;
  size?: number | null;
  cached: boolean;
}

export interface EnvEstimate {
  provider: string;
  provider_name: string;
  digest: string;
  files: EnvEstimateFile[];
  n_files: number;
  total_bytes: number;
  cached_bytes: number;
  network_bytes: number;
  size_exact: boolean;
  max_network_bytes: number;
  destination: string;
  attribution: string;
  licence: string;
  source: string;
  offline: boolean;
  needs_network: boolean;
  requires_approval: boolean;
  can_fetch: boolean;
  reason: string;
  notes: string[];
}

export type EnvPhase = 'idle' | 'estimating' | 'confirm' | 'downloading' | 'done' | 'error' | 'cancelled';

export interface EnvProgress {
  percent: number;
  message: string;
  file: string;
  bytes_done: number;
  bytes_total: number | null;
}

export interface EnvFlow {
  phase: EnvPhase;
  requestId: string | null;
  request: Record<string, unknown> | null;
  estimate: EnvEstimate | null;
  progress: EnvProgress | null;
  result: any | null;
  error: string | null;
  code: string | null;
}

export const IDLE: EnvFlow = {
  phase: 'idle',
  requestId: null,
  request: null,
  estimate: null,
  progress: null,
  result: null,
  error: null,
  code: null,
};

export type EnvEvent =
  | { type: 'ask'; requestId: string; request: Record<string, unknown> }
  | { type: 'estimate'; requestId: string; estimate: EnvEstimate }
  | { type: 'approve'; requestId: string }
  | { type: 'progress'; requestId: string; progress: EnvProgress }
  | { type: 'result'; requestId: string; result: any }
  | { type: 'error'; requestId: string | null; message: string; code?: string }
  | { type: 'cancelled'; requestId: string | null }
  | { type: 'dismiss' };

/** Does a message about `requestId` belong to the flow in progress? */
const mine = (s: EnvFlow, id: string | null) => !!s.requestId && (id === null || id === s.requestId);

export function reduceEnv(s: EnvFlow, e: EnvEvent): EnvFlow {
  switch (e.type) {
    case 'ask':
      return { ...IDLE, phase: 'estimating', requestId: e.requestId, request: e.request };
    case 'estimate': {
      if (s.phase !== 'estimating' || !mine(s, e.requestId)) return s;
      const est = e.estimate;
      if (!est.can_fetch) return { ...s, phase: 'error', estimate: est, error: est.reason || 'This request cannot be fetched.', code: est.offline ? 'offline' : 'cannot_fetch' };
      // Nothing to download (all cached, or a local file): no consent needed, the fetch is immediate.
      return { ...s, phase: est.requires_approval ? 'confirm' : 'downloading', estimate: est };
    }
    case 'approve':
      // The only way into `downloading` from a shown estimate.
      if (s.phase !== 'confirm' || !mine(s, e.requestId)) return s;
      return { ...s, phase: 'downloading', progress: null };
    case 'progress':
      if (s.phase !== 'downloading' || !mine(s, e.requestId)) return s;
      return { ...s, progress: e.progress };
    case 'result':
      if (!mine(s, e.requestId) || (s.phase !== 'downloading' && s.phase !== 'confirm')) return s;
      return { ...s, phase: 'done', result: e.result };
    case 'error':
      if (!mine(s, e.requestId)) return s;
      return { ...s, phase: 'error', error: e.message, code: e.code ?? null };
    case 'cancelled':
      if (!mine(s, e.requestId)) return s;
      return { ...s, phase: 'cancelled' };
    case 'dismiss':
      return IDLE;
  }
}

/** Does the flow currently need the user (a dialog must be visible)? */
export const needsDialog = (s: EnvFlow): boolean =>
  s.phase === 'confirm' || s.phase === 'error' || (s.phase === 'downloading' && !!s.estimate?.requires_approval);

/** "3 files, 12.4 MB" / "about 2 files, at least 3.1 MB (some sizes are not known in advance)". */
export function sizeText(est: EnvEstimate): string {
  const files = est.files.filter((f) => !f.cached);
  const n = files.length;
  const noun = n === 1 ? 'file' : 'files';
  const bytes = formatBytes(est.network_bytes) || '0 B';
  if (n === 0) return 'nothing to download (all in the cache)';
  return est.size_exact ? `${n} ${noun}, ${bytes}` : `${n} ${noun}, at least ${bytes}; some sizes are not known in advance (a limit of ${formatBytes(est.max_network_bytes)} applies)`;
}

export function cachedText(est: EnvEstimate): string {
  const n = est.files.filter((f) => f.cached).length;
  return n ? `${n} of ${est.n_files} file${est.n_files === 1 ? '' : 's'} already in the cache (${formatBytes(est.cached_bytes) || '0 B'}), not downloaded again` : '';
}

export function progressText(p: EnvProgress | null): string {
  if (!p) return 'Starting...';
  const done = formatBytes(p.bytes_done) || '0 B';
  const total = p.bytes_total ? ` of ${formatBytes(p.bytes_total)}` : '';
  return `${p.message || 'Downloading'}  ${p.percent}%  (${done}${total})`;
}

/** A short, honest label for the provider's class of data. */
export const hostOf = (url: string | null | undefined): string => {
  if (!url) return '';
  try {
    return new URL(url).host;
  } catch {
    return '';
  }
};

/** Hosts the files come from (what the user's computer will contact). */
export function hostsOf(est: EnvEstimate): string[] {
  return [...new Set(est.files.filter((f) => !f.cached).map((f) => hostOf(f.url)).filter(Boolean))];
}
