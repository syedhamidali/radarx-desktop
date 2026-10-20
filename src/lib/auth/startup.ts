/**
 * What to do at start-up, decided without any browser or Supabase API so it is unit-tested
 * (`startup.test.mjs`). The rules are the same as the engine's (docs/ACCOUNTS.md "The offline rule"):
 *
 * - The stored session is read straight from storage. supabase-js `getSession()` returns null when the
 *   access token has expired and the refresh cannot reach the server, which would show the sign-in
 *   screen to a laptop opened offline the next day.
 * - Offline, a stored session is good for `graceDays` after the `iat` of its access token: the moment
 *   the server last vouched for the account. That is a signed claim; a timestamp the app itself wrote
 *   would prove nothing. (The engine verifies the signature; this only decides which screen to show.)
 * - Only a DEFINITE refusal by the server signs the user out. A 5xx, a 429, a captive-portal page or a
 *   dropped connection keeps the session and uses the offline rule.
 */

export interface StoredSession {
  access_token: string;
  refresh_token?: string;
  expires_at?: number;
  user?: { email?: string | null };
}

const DAY_S = 86_400;

/** The session supabase-js keeps under its storage key (JSON), or null when absent or unusable. */
export function parseStoredSession(raw: string | null | undefined): StoredSession | null {
  if (!raw) return null;
  try {
    const v = JSON.parse(raw);
    if (v && typeof v === 'object' && typeof v.access_token === 'string' && v.access_token.split('.').length === 3) {
      return v as StoredSession;
    }
  } catch {
    /* not JSON */
  }
  return null;
}

/** The `iat` claim (seconds) of a JWT, read without verifying (the engine verifies). Null if unreadable. */
export function tokenIssuedAt(jwt: string): number | null {
  try {
    const part = jwt.split('.')[1];
    const b64 = part.replace(/-/g, '+').replace(/_/g, '/');
    // atob exists in every browser and in Node 16+; no Node-only fallback (the web build has no Node types)
    const json = atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4));
    const iat = JSON.parse(json).iat;
    return typeof iat === 'number' && Number.isFinite(iat) ? iat : null;
  } catch {
    return null;
  }
}

export type OfflineDecision =
  | { gate: 'open-offline'; daysLeft: number; stale: boolean }
  | { gate: 'reconnect' };

/** Offline: may the app open with this stored session? `nowMs` is the clock in ms. */
export function decideOffline(stored: StoredSession, nowMs: number, graceDays: number): OfflineDecision {
  const iat = tokenIssuedAt(stored.access_token);
  if (iat == null) return { gate: 'reconnect' };
  const now = nowMs / 1000;
  const elapsed = now - Math.min(iat, now); // a clock behind the token cannot make it look newer
  if (elapsed > graceDays * DAY_S) return { gate: 'reconnect' };
  const exp = typeof stored.expires_at === 'number' ? stored.expires_at : iat + 3600;
  return { gate: 'open-offline', daysLeft: Math.max(0, Math.ceil(graceDays - elapsed / DAY_S)), stale: now > exp };
}

export type StartupPlan =
  | { kind: 'needs-account' }
  | { kind: 'verify-online' }
  | ({ kind: 'offline' } & OfflineDecision);

export function planStartup(i: { stored: StoredSession | null; online: boolean; nowMs: number; graceDays: number }): StartupPlan {
  if (!i.stored) return { kind: 'needs-account' };
  if (i.online) return { kind: 'verify-online' };
  return { kind: 'offline', ...decideOffline(i.stored, i.nowMs, i.graceDays) };
}

export interface AuthErrorLike {
  status?: number | null;
  code?: string | null;
  message?: string | null;
}

const DEFINITE_CODES = new Set([
  'user_not_found',
  'session_not_found',
  'session_expired',
  'refresh_token_not_found',
  'refresh_token_already_used',
  'invalid_grant',
  'user_banned',
]);

/**
 * Did the SERVER say the account / session is finished? Only then is the user signed out. A 5xx, 429, a
 * portal's HTML, a timeout or a dropped connection is not an answer about the account.
 */
export function isDefiniteAuthFailure(e: AuthErrorLike | null | undefined): boolean {
  if (!e) return false;
  const code = (e.code ?? '').toLowerCase();
  if (DEFINITE_CODES.has(code)) return true;
  const status = e.status ?? 0;
  // 401 / 403 from the auth server for a user that is gone; a proxy's 403 has no such code, and a bare
  // 401/403 without any code is what GoTrue sends for a rejected token.
  if ((status === 401 || status === 403) && !/proxy|portal|<html/i.test(e.message ?? '') && code !== 'unexpected_failure') {
    return true;
  }
  const m = (e.message ?? '').toLowerCase();
  return /invalid refresh token|refresh token not found|user from sub claim in jwt does not exist|user not found/.test(m);
}

/** What to present to the engine: the live Supabase session, or else the stored (possibly expired) one. */
export function sessionForEngine(
  live: { access_token: string; expires_at?: number | null } | null | undefined,
  stored: StoredSession | null,
): { access_token: string; expires_at?: number | null } | null {
  if (live) return live;
  if (stored) return { access_token: stored.access_token, expires_at: stored.expires_at ?? null };
  return null;
}

/** What the server accepts as an install id: ASCII letters and digits, 8 to 80 of them (no accents). */
export function isValidDeviceId(id: string | null | undefined): id is string {
  return typeof id === 'string' && /^[A-Za-z0-9]{8,80}$/.test(id);
}

/** Is the stored access token good for at least another minute? Then it can be used without asking Supabase. */
export function storedTokenIsCurrent(stored: StoredSession | null, nowMs: number): boolean {
  if (!stored) return false;
  const exp = typeof stored.expires_at === 'number' ? stored.expires_at : null;
  return exp != null && exp * 1000 - nowMs > 60_000;
}
