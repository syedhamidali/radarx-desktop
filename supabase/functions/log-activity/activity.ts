// The logic of log-activity: the activity log of a token session (rxd / import rxd with a personal
// access token). A token session has no Supabase database token, so row-level security cannot
// admit its inserts; this function takes a batch with the ENTITLEMENT token, applies the SAME
// allow-list as the database and as python/engine/account/activity.py (allowlist.json is the one
// copy of the lists; python/tests/test_account_activity_allowlist.py fails when the three drift) and
// inserts for that token's user with the service role. The database trigger still runs on the
// insert, so its caps (2000 events per account per day) hold for this path too.
//
// Feature level only: no file names, paths, places or free text can pass, because a value can only
// be a short plain word and every key has a fixed name.

import allowlist from './allowlist.json' with { type: 'json' };

export const LIMITS = {
  /** log-activity calls per account per hour */
  callsPerHour: 240,
  windowSeconds: 3600,
};

const EVENTS: Record<string, string[]> = allowlist.events;
const VALUE = new RegExp(allowlist.value_pattern);
const DEVICE = new RegExp(allowlist.device_pattern);
const VERSION = new RegExp(allowlist.version_pattern);
const ROW_KEYS = new Set(['event', 'surface', 'occurred_at', 'device_id', 'app_version', 'os', 'detail']);

export interface Row {
  user_id: string;
  event: string;
  surface: string;
  occurred_at: string;
  device_id: string;
  app_version: string | null;
  os: string | null;
  detail: Record<string, string>;
}

export type Checked = { ok: true; row: Row } | { ok: false; reason: string };

/** One event, checked as strictly as the table's trigger. */
export function checkEvent(raw: unknown, userId: string): Checked {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return { ok: false, reason: 'not_an_object' };
  const ev = raw as Record<string, unknown>;
  for (const key of Object.keys(ev)) if (!ROW_KEYS.has(key)) return { ok: false, reason: 'unknown_field' };

  const name = ev.event;
  if (typeof name !== 'string' || name.length > allowlist.max_event_name || !Object.hasOwn(EVENTS, name)) {
    return { ok: false, reason: 'event_not_allowed' };
  }
  const detail = ev.detail === undefined ? {} : ev.detail; // (null is refused, like the table)
  if (typeof detail !== 'object' || detail === null || Array.isArray(detail)) return { ok: false, reason: 'detail' };
  if (new TextEncoder().encode(JSON.stringify(detail)).length > allowlist.max_detail_bytes) {
    return { ok: false, reason: 'detail_too_large' };
  }
  const clean: Record<string, string> = {};
  for (const [key, value] of Object.entries(detail as Record<string, unknown>)) {
    if (!EVENTS[name].includes(key)) return { ok: false, reason: 'detail_key' };
    if (typeof value !== 'string' || !VALUE.test(value)) return { ok: false, reason: 'detail_value' };
    clean[key] = value;
  }
  if (typeof ev.surface !== 'string' || !allowlist.surfaces.includes(ev.surface)) return { ok: false, reason: 'surface' };
  if (typeof ev.device_id !== 'string' || !DEVICE.test(ev.device_id)) return { ok: false, reason: 'device_id' };

  let version: string | null = null;
  if (ev.app_version !== undefined && ev.app_version !== null) {
    if (typeof ev.app_version !== 'string' || ev.app_version.length > 40) return { ok: false, reason: 'app_version' };
    if (!VERSION.test(ev.app_version) && ev.app_version !== allowlist.version_unknown) {
      return { ok: false, reason: 'app_version' };
    }
    version = ev.app_version;
  }
  let os: string | null = null;
  if (ev.os !== undefined && ev.os !== null) {
    if (typeof ev.os !== 'string' || !allowlist.os.includes(ev.os)) return { ok: false, reason: 'os' };
    os = ev.os;
  }
  // always present, so a bulk insert never has rows with different columns (the column is NOT NULL)
  let when: string = new Date().toISOString();
  if (ev.occurred_at !== undefined && ev.occurred_at !== null) {
    if (typeof ev.occurred_at !== 'string' || ev.occurred_at.length > 40 || !Number.isFinite(Date.parse(ev.occurred_at))) {
      return { ok: false, reason: 'occurred_at' };
    }
    when = ev.occurred_at; // the database clamps it to a sane range
  }
  return {
    ok: true,
    row: {
      user_id: userId, // always the token's own account, whatever the body says
      event: name,
      surface: ev.surface,
      occurred_at: when,
      device_id: ev.device_id,
      app_version: version,
      os,
      detail: clean,
    },
  };
}

export interface Dependencies {
  /** verifies the entitlement token; returns its claims or throws */
  verify(token: string): Promise<Record<string, unknown>>;
  rateHit(bucket: string, limit: number, windowSeconds: number): Promise<boolean>;
  /** inserts as the service role; the status is PostgREST's, `code` the database error code */
  insert(rows: Row[]): Promise<{ status: number; code: string }>;
}

export interface Answer {
  status: number;
  body: Record<string, unknown>;
  headers?: Record<string, string>;
}

export async function handleLog(
  input: { method: string; authorization: string | null; body: unknown },
  deps: Dependencies,
): Promise<Answer> {
  if (input.method !== 'POST') return { status: 405, body: { error: 'method_not_allowed' }, headers: { Allow: 'POST' } };
  const match = /^Bearer ([A-Za-z0-9._-]{20,4096})$/.exec(input.authorization ?? '');
  if (!match) return { status: 401, body: { error: 'invalid_token' } };
  let claims: Record<string, unknown>;
  try {
    claims = await deps.verify(match[1]);
  } catch {
    return { status: 401, body: { error: 'invalid_token' } };
  }
  const userId = claims.sub as string;

  if (!(await deps.rateHit('act:' + userId, LIMITS.callsPerHour, LIMITS.windowSeconds))) {
    return { status: 429, body: { error: 'rate_limited' }, headers: { 'Retry-After': '600' } };
  }

  const events = (input.body as { events?: unknown } | null)?.events;
  if (!Array.isArray(events) || events.length === 0 || events.length > allowlist.max_batch) {
    return { status: 400, body: { error: 'invalid_event', reason: 'batch' } };
  }
  const rows: Row[] = [];
  for (const raw of events) {
    const checked = checkEvent(raw, userId);
    // like the table, one bad row refuses the whole batch (the client then splits it)
    if (!checked.ok) return { status: 400, body: { error: 'invalid_event', reason: checked.reason } };
    rows.push(checked.row);
  }
  const done = await deps.insert(rows);
  if (done.status >= 200 && done.status < 300) return { status: 200, body: { inserted: rows.length } };
  if (done.code === 'P0001') return { status: 429, body: { error: 'rate_limited' }, headers: { 'Retry-After': '3600' } };
  if (done.status === 400 || done.status === 409 || done.status === 422) {
    return { status: 400, body: { error: 'invalid_event', reason: 'database' } };
  }
  return { status: 503, body: { error: 'unavailable' } };
}
