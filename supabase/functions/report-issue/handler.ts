/**
 * The request handler of report-issue, with every dependency injected (fetch, database, clock, secrets, log) so Node
 * can run it under test. index.ts wires the real ones for Deno.
 *
 * Order of work, and why:
 *   1. CORS preflight / method / content type / size / JSON       (cheap refusals first)
 *   2. honeypot                                                    (a bot gets the normal success answer, nothing stored)
 *   3. validation and sanitising
 *   4. who is it: a verified Supabase user (app) or a passed Turnstile check (web)
 *   5. rate limits (per address hour + global day for the web; per account in the database for the app)
 *   6. STORE the report
 *   7. only then notify the owner (and optionally GitHub): a failure there never loses or fails a stored report
 *
 * Logging: only event names, outcomes, error CODES and timings. Never the text, an e-mail address, a token, an
 * address of the client or a secret (tests/handler.test.mjs asserts it).
 */
import { bearerToken, verifyUser } from './auth.ts';
import { corsHeaders } from './cors.ts';
import { createGithubReporter } from './github.ts';
import { GLOBAL_KEY, LIMITS, clientIp, dayBucket, hashIp, hourBucket, secondsToNextDay, secondsToNextHour } from './limits.ts';
import { notifyOwner } from './notify.ts';
import type { FetchLike, Logger } from './types.ts';
import { verifyTurnstile } from './turnstile.ts';
import { honeypotFilled, validateReport } from './validate.ts';
import type { Report } from './validate.ts';

export type ErrorCode = 'invalid_input' | 'turnstile_failed' | 'unauthorized' | 'rate_limited' | 'server_error';

export type InsertRow = Omit<Report, 'turnstile_token'> & { user_id: string | null };

/** What the function needs from the database. index.ts implements it with the service-role client. */
export type Db = {
  /** Counts one use of `key` in `bucket` unless it is at `max`; false when the limit is reached. */
  takeLimit(key: string, bucket: string, max: number): Promise<boolean>;
  /** Stores the report. Applies the per-account cap and (when given) the global bucket in the same transaction. */
  insertReport(
    row: InsertRow,
    caps: { userCap: number; globalKey?: string; globalMax?: number },
  ): Promise<{ id: string; createdAt: string } | { limited: 'user' | 'global' }>;
};

export type Deps = {
  env: (name: string) => string | undefined;
  fetchFn: FetchLike;
  now: () => Date;
  db: Db;
  log: Logger;
  randomUUID: () => string;
  /** Keeps the notification running after the response was sent (Supabase: EdgeRuntime.waitUntil). Absent: awaited. */
  defer?: (p: Promise<unknown>) => void;
  /** Time limit for each outgoing call (sign-in check, Turnstile, each mail provider); tests shorten it. */
  timeoutMs?: number;
};

const MAX_BODY_CHARS = 200_000;

const STATUS: Record<ErrorCode, number> = {
  invalid_input: 400,
  turnstile_failed: 400,
  unauthorized: 401,
  rate_limited: 429,
  server_error: 500,
};

export async function handleRequest(req: Request, deps: Deps): Promise<Response> {
  const started = deps.now().getTime();
  const origin = req.headers.get('origin');
  let source: 'app' | 'web' | undefined;

  const send = (status: number, body: unknown, extra: Record<string, string> = {}): Response =>
    new Response(status === 204 ? null : JSON.stringify(body), {
      status,
      headers: {
        ...(status === 204 ? {} : { 'Content-Type': 'application/json; charset=utf-8' }),
        'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff',
        ...corsHeaders(origin, source, { REPORT_ALLOWED_ORIGINS: deps.env('REPORT_ALLOWED_ORIGINS'), REPORT_ALLOW_LOCALHOST: deps.env('REPORT_ALLOW_LOCALHOST') }),
        ...extra,
      },
    });

  const fail = (code: ErrorCode, message: string, field?: string, extra: Record<string, string> = {}, why?: string): Response => {
    deps.log({ event: 'report', outcome: 'error', code, source: source ?? null, why: why ?? field ?? null, ms: deps.now().getTime() - started });
    return send(STATUS[code], { error: { code, message, ...(field ? { field } : {}) } }, extra);
  };

  const corsEnv = { REPORT_ALLOWED_ORIGINS: deps.env('REPORT_ALLOWED_ORIGINS'), REPORT_ALLOW_LOCALHOST: deps.env('REPORT_ALLOW_LOCALHOST') };

  // 1. preflight and basic request shape
  if (req.method === 'OPTIONS') {
    const h = corsHeaders(origin, undefined, corsEnv);
    if (origin && !('Access-Control-Allow-Origin' in h)) return new Response(null, { status: 403, headers: h });
    return new Response(null, { status: 204, headers: h });
  }
  if (req.method !== 'POST') {
    deps.log({ event: 'report', outcome: 'error', code: 'invalid_input', source: null, why: 'method', ms: 0 });
    return send(405, { error: { code: 'invalid_input', message: 'Use POST.' } }, { Allow: 'POST, OPTIONS' });
  }
  if (!(req.headers.get('content-type') ?? '').toLowerCase().startsWith('application/json')) {
    return fail('invalid_input', 'Send the report as JSON.', undefined, {}, 'content_type');
  }
  const declared = Number(req.headers.get('content-length') ?? '0');
  if (Number.isFinite(declared) && declared > MAX_BODY_CHARS * 2) return fail('invalid_input', 'The report is too large.', undefined, {}, 'too_large');
  let raw: string;
  try {
    raw = await req.text();
  } catch {
    return fail('invalid_input', 'The request could not be read.', undefined, {}, 'unreadable');
  }
  if (raw.length > MAX_BODY_CHARS) return fail('invalid_input', 'The report is too large.', undefined, {}, 'too_large');
  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    return fail('invalid_input', 'The request must be valid JSON.', undefined, {}, 'json');
  }
  if (typeof body === 'object' && body !== null && !Array.isArray(body)) {
    const s = (body as Record<string, unknown>).source;
    if (s === 'app' || s === 'web') source = s;
  }

  // 2. honeypot: the same success answer, nothing stored, no hint
  if (honeypotFilled(body)) {
    deps.log({ event: 'report', outcome: 'honeypot', source: source ?? null, ms: deps.now().getTime() - started });
    return send(200, { id: deps.randomUUID() });
  }

  // 3. validation
  const v = validateReport(body);
  if (!v.ok) return fail('invalid_input', v.message, v.field);
  const report = v.value;
  source = report.source;

  // 4 + 5. who is it, and how often
  let userId: string | null = null;
  if (report.source === 'app') {
    const token = bearerToken(req.headers.get('authorization'));
    if (!token) return fail('unauthorized', 'Sign in to radarx to send a report.', undefined, {}, 'no_token');
    const url = deps.env('SUPABASE_URL');
    const anon = deps.env('SUPABASE_ANON_KEY');
    if (!url || !anon) return fail('server_error', 'The report service is not configured. Please try again later.', undefined, {}, 'config:SUPABASE_URL/ANON_KEY');
    const who = await verifyUser({ supabaseUrl: url, anonKey: anon, token, fetchFn: deps.fetchFn, timeoutMs: deps.timeoutMs });
    if (who.status === 'invalid') return fail('unauthorized', 'Your sign-in has expired. Sign in again and retry.', undefined, {}, 'bad_token');
    if (who.status === 'unavailable') return fail('server_error', 'Could not check your sign-in. Please try again in a moment.', undefined, {}, 'auth_unavailable');
    userId = who.userId;
  } else {
    const secret = deps.env('TURNSTILE_SECRET_KEY');
    const salt = deps.env('REPORT_IP_SALT');
    if (!secret || !salt) return fail('server_error', 'The report service is not configured. Please try again later.', undefined, {}, 'config:TURNSTILE/SALT');
    if (!report.turnstile_token) return fail('turnstile_failed', 'Please complete the human check and send again.', 'turnstile_token', {}, 'no_token');
    const ip = clientIp(req.headers);
    const t = await verifyTurnstile({ secret, token: report.turnstile_token, ip, fetchFn: deps.fetchFn, timeoutMs: deps.timeoutMs });
    if (t.outcome === 'unavailable') {
      deps.log({ event: 'turnstile_unavailable', codes: t.codes });
      return fail('server_error', 'The human check could not be completed. Please try again in a moment.', undefined, {}, 'turnstile_unavailable');
    }
    if (t.outcome === 'fail') return fail('turnstile_failed', 'The human check failed. Please try again.', 'turnstile_token', {}, 'turnstile_fail');
    try {
      const now = deps.now();
      const key = `ip:${await hashIp(ip ?? 'unknown', salt)}`;
      if (!(await deps.db.takeLimit(key, hourBucket(now), LIMITS.webPerIpHour))) {
        return fail('rate_limited', 'Too many reports from this connection. Please try again later.', undefined, { 'Retry-After': String(secondsToNextHour(now)) }, 'ip_hour');
      }
      if (!(await deps.db.takeLimit(GLOBAL_KEY.web, dayBucket(now), LIMITS.webGlobalDay))) {
        return fail('rate_limited', 'We are receiving a lot of reports right now. Please try again tomorrow.', undefined, { 'Retry-After': String(secondsToNextDay(now)) }, 'global_day');
      }
    } catch (e) {
      return fail('server_error', 'The report could not be saved. Please try again later.', undefined, {}, `limits:${errorClass(e)}`);
    }
  }

  // 6. store
  let stored: { id: string; createdAt: string };
  try {
    const { turnstile_token: _t, ...fields } = report;
    const result = await deps.db.insertReport(
      { ...fields, user_id: userId },
      report.source === 'app'
        ? { userCap: LIMITS.appPerUserDay, globalKey: GLOBAL_KEY.app, globalMax: LIMITS.appGlobalDay }
        : { userCap: LIMITS.appPerUserDay },
    );
    if ('limited' in result) {
      return result.limited === 'user'
        ? fail('rate_limited', 'You have sent the most reports allowed in a day. Please try again tomorrow.', undefined, { 'Retry-After': '3600' }, 'user_day')
        : fail('rate_limited', 'We are receiving a lot of reports right now. Please try again tomorrow.', undefined, { 'Retry-After': String(secondsToNextDay(deps.now())) }, 'global_day');
    }
    stored = result;
  } catch (e) {
    return fail('server_error', 'The report could not be saved. Please try again later.', undefined, {}, `store:${errorClass(e)}`);
  }

  // 7. notify after storing; the answer does not depend on it
  const after = notifyAll(report, { id: stored.id, createdAt: stored.createdAt, userId }, deps).catch(() => undefined);
  if (deps.defer) deps.defer(after);
  else await after;

  deps.log({ event: 'report', outcome: 'stored', source: report.source, kind: report.kind, ms: deps.now().getTime() - started });
  return send(200, { id: stored.id });
}

/** A name for an error that is safe to log (never its message, which could hold a row). */
function errorClass(e: unknown): string {
  const name = (e as { name?: unknown })?.name;
  return typeof name === 'string' && /^[A-Za-z]{1,40}$/.test(name) ? name : 'Error';
}

async function notifyAll(report: Report, stored: { id: string; createdAt: string; userId: string | null }, deps: Deps): Promise<void> {
  try {
    const n = await notifyOwner({ env: deps.env, fetchFn: deps.fetchFn, report, stored, timeoutMs: deps.timeoutMs });
    deps.log({ event: 'notify', sent: n.sent, provider: n.provider ?? null, attempts: n.attempts, reason: n.reason ?? null });
  } catch {
    deps.log({ event: 'notify', sent: false, reason: 'unexpected' });
  }
  if (deps.env('REPORT_GITHUB_ISSUES') === '1') {
    const token = deps.env('GITHUB_REPORT_TOKEN');
    const repo = deps.env('GITHUB_REPORT_REPO');
    if (!token || !repo) {
      deps.log({ event: 'github', ok: false, reason: 'GITHUB_REPORT_TOKEN or GITHUB_REPORT_REPO is not set' });
      return;
    }
    try {
      const gh = createGithubReporter({ token, repo, fetchFn: deps.fetchFn, timeoutMs: deps.timeoutMs });
      const r = await gh.report(report, stored.id);
      deps.log({ event: 'github', ok: r.ok, reason: r.reason ?? null, number: r.number ?? null });
    } catch {
      deps.log({ event: 'github', ok: false, reason: 'unexpected' });
    }
  }
}
