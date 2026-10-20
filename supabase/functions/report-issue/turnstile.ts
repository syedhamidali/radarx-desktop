/**
 * Cloudflare Turnstile verification (https://developers.cloudflare.com/turnstile/get-started/server-side-validation/).
 *
 * Fails CLOSED: anything other than a clear "success: true" is not a pass, and a problem on our side or at
 * Cloudflare (timeout, network error, 5xx, unparsable answer, a bad secret) is `unavailable`, reported to the
 * visitor as a server error, never as a pass and never as "you failed the check".
 */

import type { FetchLike } from './types.ts';

export const SITEVERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify';


export type TurnstileResult = { outcome: 'pass' | 'fail' | 'unavailable'; codes: string[] };

// Error codes that mean OUR configuration is wrong or Cloudflare is failing, not that the visitor did badly.
const OUR_SIDE = new Set(['missing-input-secret', 'invalid-input-secret', 'internal-error', 'bad-request']);

export async function verifyTurnstile(opts: {
  secret: string;
  token: string;
  ip: string | null;
  fetchFn: FetchLike;
  timeoutMs?: number;
}): Promise<TurnstileResult> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), opts.timeoutMs ?? 5000);
  try {
    const form = new URLSearchParams();
    form.set('secret', opts.secret);
    form.set('response', opts.token);
    if (opts.ip) form.set('remoteip', opts.ip);
    const res = await opts.fetchFn(SITEVERIFY_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: form.toString(),
      signal: ctl.signal,
    });
    if (!res.ok) return { outcome: 'unavailable', codes: [`http-${res.status}`] };
    const data = (await res.json()) as { success?: unknown; 'error-codes'?: unknown } | null;
    if (data === null || typeof data !== 'object') return { outcome: 'unavailable', codes: ['bad-answer'] };
    const codes = Array.isArray(data['error-codes']) ? data['error-codes'].filter((c): c is string => typeof c === 'string' && /^[a-z0-9-]{1,40}$/.test(c)).slice(0, 5) : [];
    if (data.success === true) return { outcome: 'pass', codes: [] };
    if (data.success !== false) return { outcome: 'unavailable', codes: ['bad-answer'] };
    if (codes.some((c) => OUR_SIDE.has(c))) return { outcome: 'unavailable', codes };
    return { outcome: 'fail', codes };
  } catch {
    return { outcome: 'unavailable', codes: [ctl.signal.aborted ? 'timeout' : 'network'] };
  } finally {
    clearTimeout(timer);
  }
}
