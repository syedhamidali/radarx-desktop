/**
 * Access tokens ("personal access tokens") for batch jobs, cron, CI and cluster nodes that cannot
 * type a password (docs/ACCOUNTS.md). The pure parts live here, free of any browser or Supabase
 * API, so they are unit-tested: names, expiry choices, status, the list rows and the plain-words
 * version of the database's errors. The calls themselves are in stores/auth.ts.
 *
 * The same limits are enforced in the database (supabase/migrations/20261023000000_api_tokens.sql);
 * checking them here only gives a friendlier answer before the round trip.
 */

export const MAX_ACTIVE_TOKENS = 10;
export const DEFAULT_DAYS = 90;
export const MAX_DAYS = 365;
const NAME = /^[A-Za-z0-9_ .+-]{1,64}$/;
const TOKEN = /^rxd_pat_[A-Za-z0-9_-]{43}$/;
const DAY_MS = 86_400_000;

export const EXPIRY_CHOICES = [
  { days: 7, label: '7 days' },
  { days: 30, label: '30 days' },
  { days: 90, label: '90 days (default)' },
  { days: 180, label: '180 days' },
  { days: 365, label: '1 year (longest)' },
] as const;

export interface ApiTokenRow {
  id: string;
  name: string;
  hint: string;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

export type TokenStatus = 'active' | 'expired' | 'revoked';

/** The name as it will be stored: trimmed, or null when it is not acceptable. */
export function cleanTokenName(name: string): string | null {
  const trimmed = (name ?? '').trim();
  return NAME.test(trimmed) ? trimmed : null;
}

export function nameProblem(name: string): string | null {
  const trimmed = (name ?? '').trim();
  if (!trimmed) return 'Give the token a name, for example "nightly job" or "cluster".';
  if (trimmed.length > 64) return 'Use at most 64 characters.';
  if (!NAME.test(trimmed)) return 'Use letters, digits, spaces and . _ + - only.';
  return null;
}

export function tokenStatus(row: Pick<ApiTokenRow, 'expires_at' | 'revoked_at'>, now: number): TokenStatus {
  if (row.revoked_at) return 'revoked';
  return Date.parse(row.expires_at) <= now ? 'expired' : 'active';
}

export function activeCount(rows: readonly ApiTokenRow[], now: number): number {
  return rows.filter((r) => tokenStatus(r, now) === 'active').length;
}

/** Why a new token cannot be made right now, or null. */
export function createProblem(rows: readonly ApiTokenRow[], now: number): string | null {
  return activeCount(rows, now) >= MAX_ACTIVE_TOKENS
    ? `You already have ${MAX_ACTIVE_TOKENS} active tokens. Revoke one you no longer use first.`
    : null;
}

/** Whole days until expiry (0 when over). */
export function daysLeft(expiresAt: string, now: number): number {
  const t = Date.parse(expiresAt);
  return Number.isFinite(t) ? Math.max(0, Math.ceil((t - now) / DAY_MS)) : 0;
}

/** True when an active token ends within a week: worth a nudge to make a new one. */
export function expiresSoon(row: ApiTokenRow, now: number): boolean {
  return tokenStatus(row, now) === 'active' && daysLeft(row.expires_at, now) <= 7;
}

/** "rxd_pat_AbCd..." - enough to tell tokens apart, never enough to use one. */
export function hintText(row: Pick<ApiTokenRow, 'hint'>): string {
  return `rxd_pat_${row.hint}…`;
}

/** The answer of create_api_token, or null when it is not the shape a token answer has. */
export function parseCreated(value: unknown): { id: string; token: string; name: string; expires_at: string } | null {
  if (typeof value !== 'object' || value === null) return null;
  const v = value as Record<string, unknown>;
  if (typeof v.id !== 'string' || typeof v.token !== 'string' || !TOKEN.test(v.token)) return null;
  if (typeof v.name !== 'string' || typeof v.expires_at !== 'string') return null;
  return { id: v.id, token: v.token, name: v.name, expires_at: v.expires_at };
}

/** The list the database returned, keeping only well-formed rows, newest first. */
export function parseRows(value: unknown): ApiTokenRow[] {
  if (!Array.isArray(value)) return [];
  const rows: ApiTokenRow[] = [];
  for (const r of value) {
    if (typeof r !== 'object' || r === null) continue;
    const x = r as Record<string, unknown>;
    if (typeof x.id !== 'string' || typeof x.name !== 'string' || typeof x.expires_at !== 'string' || typeof x.created_at !== 'string') continue;
    rows.push({
      id: x.id,
      name: x.name,
      hint: typeof x.hint === 'string' ? x.hint : '',
      created_at: x.created_at,
      expires_at: x.expires_at,
      last_used_at: typeof x.last_used_at === 'string' ? x.last_used_at : null,
      revoked_at: typeof x.revoked_at === 'string' ? x.revoked_at : null,
    });
  }
  return rows.sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
}

/** A sentence for a failed create/revoke/list (the database's own wording is not shown). */
export function friendlyTokenError(error: unknown): string {
  const e = (error ?? {}) as { code?: string; message?: string };
  const text = String(e.message ?? '').toLowerCase();
  if (text.includes('at most 10 active')) return `You already have ${MAX_ACTIVE_TOKENS} active tokens. Revoke one you no longer use first.`;
  if (text.includes('per day')) return 'Too many tokens made today. Try again tomorrow.';
  if (text.includes('too many access tokens made or ended')) return 'Too many tokens were made or ended in the last 30 days (limit 200). Try again when older ones have aged out.';
  if (e.code === '28000' || text.includes('sign in')) return 'Sign in again to manage access tokens.';
  if (e.code === '22023') return 'That name or expiry is not allowed.';
  if (e.code === '42883' || e.code === 'PGRST202' || e.code === '42P01' || e.code === 'PGRST205') {
    return 'Access tokens are not available on the account server yet.';
  }
  return 'Could not reach the account server. Check your connection and try again.';
}

/**
 * What revoking does and does not do, in plain words (the toast, the confirmation and the docs say the
 * same): a job that can reach the account server stops within the hour; one that cannot keeps working
 * on its last sign-in until that lapses (the 30-day offline allowance), but never past the token's own
 * expiry date.
 */
export const REVOKE_NOTE =
  'Jobs that are online stop within the hour. A job that cannot reach the account server keeps working on its last sign-in until that lapses (up to 30 days, never past the token\'s own expiry).';

/** Changing your password revokes every token; "sign out everywhere" does not. */
export const ACCOUNT_NOTE = 'Changing your password revokes all your tokens. "Sign out everywhere" does not: revoke tokens here.';

/** The two ways to use a new token, with the token left out (the dialog shows them next to it). */
export const USAGE = [
  { cmd: 'rxd login --token < token.txt', note: 'keeps it on this computer, in a private file' },
  { cmd: 'export RADARX_TOKEN=rxd_pat_...', note: 'for one job (CI): nothing is written to disk' },
] as const;
