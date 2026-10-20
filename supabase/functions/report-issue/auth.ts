/**
 * Verifying the signed-in user of an "app" report with the Supabase auth API.
 *
 * The token is sent to `GET {SUPABASE_URL}/auth/v1/user` together with the project's anon (publishable) key; the
 * user id comes from Supabase's answer, never from anything in the request body.
 */
import type { FetchLike } from './types.ts';

export type UserCheck = { status: 'ok'; userId: string } | { status: 'invalid' } | { status: 'unavailable' };

const JWT = /^[A-Za-z0-9_-]{4,2048}\.[A-Za-z0-9_-]{4,4096}\.[A-Za-z0-9_-]{4,2048}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The token of an `Authorization: Bearer <token>` header, or null. */
export function bearerToken(header: string | null): string | null {
  if (!header) return null;
  const m = /^Bearer\s+(\S+)$/i.exec(header.trim());
  return m && JWT.test(m[1]) ? m[1] : null;
}

export async function verifyUser(opts: {
  supabaseUrl: string;
  anonKey: string;
  token: string;
  fetchFn: FetchLike;
  timeoutMs?: number;
}): Promise<UserCheck> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), opts.timeoutMs ?? 5000);
  try {
    const res = await opts.fetchFn(`${opts.supabaseUrl.replace(/\/+$/, '')}/auth/v1/user`, {
      method: 'GET',
      headers: { apikey: opts.anonKey, Authorization: `Bearer ${opts.token}` },
      signal: ctl.signal,
    });
    if (res.status === 401 || res.status === 403) return { status: 'invalid' };
    if (!res.ok) return { status: 'unavailable' };
    const user = (await res.json()) as { id?: unknown; is_anonymous?: unknown } | null;
    if (!user || typeof user.id !== 'string' || !UUID.test(user.id)) return { status: 'unavailable' };
    if (user.is_anonymous === true) return { status: 'invalid' };
    return { status: 'ok', userId: user.id.toLowerCase() };
  } catch {
    return { status: 'unavailable' };
  } finally {
    clearTimeout(timer);
  }
}
