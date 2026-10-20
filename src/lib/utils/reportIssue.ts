/**
 * "Report an issue": the pure parts (docs/REPORTING.md). The draft, its checks, the exact payload that is sent, the
 * request to the `report-issue` Edge Function and the plain sentences for everything that can go wrong.
 * No DOM and no Svelte here, so Node tests it (reportIssue.test.mjs); the dialog only wires it up.
 */
import { buildDiagnosticsText } from './reportScrub.ts';
import type { ScrubContext } from './reportScrub.ts';

export type ReportKind = 'bug' | 'idea' | 'question';

export const KINDS: { id: ReportKind; label: string; hint: string }[] = [
  { id: 'bug', label: 'Something is wrong', hint: 'A bug, a crash, a wrong result' },
  { id: 'idea', label: 'An idea', hint: 'A feature or an improvement' },
  { id: 'question', label: 'A question', hint: 'How do I ...?' },
];

/** The same limits as the function and the database (supabase/functions/report-issue/validate.ts). */
export const LIMITS = {
  title: { min: 3, max: 140 },
  description: { min: 10, max: 5000 },
  appVersion: 40,
  os: 60,
  engineVersion: 40,
};

export interface Draft {
  kind: ReportKind;
  title: string;
  description: string;
  includeDiagnostics: boolean;
}

export const emptyDraft = (): Draft => ({ kind: 'bug', title: '', description: '', includeDiagnostics: false });

/** Characters as Postgres counts them (code points), not UTF-16 units. */
export const cpLength = (s: string): number => {
  let n = 0;
  for (const _ of s) n++;
  return n;
};

export interface DraftProblems {
  title?: string;
  description?: string;
}

/** What is wrong with the draft, in words for the person; an empty object means it can be sent. */
export function checkDraft(d: Draft): DraftProblems {
  const out: DraftProblems = {};
  const t = cpLength(d.title.trim());
  if (t < LIMITS.title.min) out.title = `Give it a short title (at least ${LIMITS.title.min} characters).`;
  else if (t > LIMITS.title.max) out.title = `The title is ${t - LIMITS.title.max} characters too long (at most ${LIMITS.title.max}).`;
  const n = cpLength(d.description.trim());
  if (n < LIMITS.description.min) out.description = `Tell us a little more (at least ${LIMITS.description.min} characters).`;
  else if (n > LIMITS.description.max) out.description = `The description is ${n - LIMITS.description.max} characters too long (at most ${LIMITS.description.max}).`;
  return out;
}

/** The facts the dialog gathers about this install. */
export interface Environment {
  appVersion: string;
  os: string;
  engineVersion?: string | null;
  /** What the engine told the app this session (newest last). */
  engineLines: string[];
}

/**
 * A short, honest name for the system: the family from the user agent, the processor family when the browser
 * says it, and "desktop" or "web". No version numbers (the webview's user agent does not know them anyway).
 */
export function systemName(opts: { userAgent?: string; arch?: string | null; desktop: boolean }): string {
  const ua = opts.userAgent ?? '';
  const family = /Android/i.test(ua) ? 'Android' : /iPhone|iPad|iPod/i.test(ua) ? 'iOS' : /Windows/i.test(ua) ? 'Windows' : /Mac OS X|Macintosh/i.test(ua) ? 'macOS' : /Linux|X11/i.test(ua) ? 'Linux' : 'Unknown system';
  const arch = opts.arch && /^[A-Za-z0-9_-]{1,12}$/.test(opts.arch) ? ` ${opts.arch}` : '';
  return `${family}${arch} (${opts.desktop ? 'desktop' : 'web'})`.slice(0, LIMITS.os);
}

export interface ReportPayload {
  source: 'app';
  kind: ReportKind;
  title: string;
  description: string;
  app_version: string;
  os: string;
  engine_version?: string;
  diagnostics?: string;
}

const clip = (s: string, n: number) => Array.from(s).slice(0, n).join('');

/**
 * Exactly what is sent. The title and description go as typed (trimmed). The app version and the system name always
 * go (a report is hard to use without them). The engine version and the diagnostics go only when the box is ticked,
 * and the diagnostics are scrubbed (reportScrub.ts). The preview shows this object.
 */
export function buildPayload(d: Draft, env: Environment, ctx: ScrubContext = {}): ReportPayload {
  const payload: ReportPayload = {
    source: 'app',
    kind: d.kind,
    title: d.title.trim(),
    description: d.description.trim(),
    app_version: clip(env.appVersion, LIMITS.appVersion),
    os: clip(env.os, LIMITS.os),
  };
  if (d.includeDiagnostics) {
    if (env.engineVersion) payload.engine_version = clip(env.engineVersion, LIMITS.engineVersion);
    payload.diagnostics = buildDiagnosticsText({ appVersion: env.appVersion, os: env.os, engineVersion: env.engineVersion, engineLines: env.engineLines }, ctx);
  }
  return payload;
}

// ---------------------------------------------------------------------------------------------- sending

export type FailureKind = 'offline' | 'timeout' | 'network' | 'rate' | 'signin' | 'input' | 'server' | 'unknown';

export type SubmitResult =
  | { ok: true; id: string }
  | { ok: false; kind: FailureKind; message: string; field?: string; retryAfterS?: number };

const SENTENCES: Record<FailureKind, string> = {
  offline: 'You seem to be offline. Your report is kept here: send it when the connection is back.',
  timeout: 'The report service did not answer in time. Your report is kept here: try again in a moment.',
  network: 'The report service could not be reached. Your report is kept here: try again in a moment.',
  rate: 'You have sent the most reports allowed for now. Please try again later (your text is kept here).',
  signin: 'You need to be signed in to send a report. Sign in again, then send it (your text is kept here).',
  input: 'The report was not accepted. Please check the highlighted part.',
  server: 'The report service had a problem. Your report is kept here: try again in a moment.',
  unknown: 'The report could not be sent. Your report is kept here: try again in a moment.',
};

/** Turns an answer of the function (status and parsed JSON) or a transport problem into a sentence. */
export function describeFailure(status: number | null, body: unknown, transport?: 'offline' | 'timeout' | 'network'): Extract<SubmitResult, { ok: false }> {
  if (transport) return { ok: false, kind: transport, message: SENTENCES[transport] };
  const err = (body as { error?: { code?: unknown; message?: unknown; field?: unknown } } | null)?.error;
  const code = typeof err?.code === 'string' ? err.code : '';
  const field = typeof err?.field === 'string' && /^[a-z_]{1,30}$/.test(err.field) ? err.field : undefined;
  if (status === 429 || code === 'rate_limited') return { ok: false, kind: 'rate', message: SENTENCES.rate };
  if (status === 401 || code === 'unauthorized') return { ok: false, kind: 'signin', message: SENTENCES.signin };
  if (status === 400 || code === 'invalid_input') {
    // the function's own sentence is short and never repeats what was sent: safe to show
    const msg = typeof err?.message === 'string' && err.message.length <= 160 ? err.message : SENTENCES.input;
    return { ok: false, kind: 'input', message: msg, field };
  }
  if (status !== null && status >= 500) return { ok: false, kind: 'server', message: SENTENCES.server };
  return { ok: false, kind: 'unknown', message: SENTENCES.unknown };
}

type FetchLike = (url: string, init: { method: string; headers: Record<string, string>; body: string; signal: AbortSignal }) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** POSTs the payload. Never throws; the draft stays with the caller whatever happens. */
export async function submitReport(opts: {
  url: string;
  apiKey: string;
  token: string | null;
  payload: ReportPayload;
  fetchFn: FetchLike;
  /** Asked before sending and again if the request fails; false = the device is offline. */
  isOnline?: () => boolean;
  timeoutMs?: number;
}): Promise<SubmitResult> {
  if (!opts.token) return describeFailure(401, null);
  const online = () => (opts.isOnline ? opts.isOnline() : true);
  if (!online()) return describeFailure(null, null, 'offline');
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), opts.timeoutMs ?? 20000);
  try {
    const res = await opts.fetchFn(opts.url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${opts.token}`, apikey: opts.apiKey },
      body: JSON.stringify(opts.payload),
      signal: ctl.signal,
    });
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      /* not JSON: described by the status alone */
    }
    const id = (body as { id?: unknown } | null)?.id;
    if (res.ok && typeof id === 'string' && UUID.test(id)) return { ok: true, id };
    if (res.ok) return describeFailure(500, null);
    return describeFailure(res.status, body);
  } catch {
    return describeFailure(null, null, ctl.signal.aborted ? 'timeout' : !online() ? 'offline' : 'network');
  } finally {
    clearTimeout(timer);
  }
}

/** The address of the function for a Supabase project URL. */
export function reportUrl(supabaseUrl: string): string {
  return `${supabaseUrl.replace(/\/+$/, '')}/functions/v1/report-issue`;
}
