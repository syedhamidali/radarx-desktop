// The logic of exchange-api-token: a personal access token in, a one-hour entitlement token out.
// Everything that touches the outside world (the database, the clock, the signing key) is passed
// in, so exchange.test.mjs runs it under plain Node with fakes. index.ts only wires it to Deno.
//
// What the answers reveal (nothing about which tokens exist):
//   * an unknown, revoked, expired or malformed token, or a token of a banned or deleted account,
//     all get the SAME 401 {"error":"invalid_token"};
//   * the rate limits are counted BEFORE the lookup and on values that do not depend on whether
//     the token is real (the client address, the SHA-256 of what was presented);
//   * nothing is logged here: no token, no hash, no address, no key.

import { AUDIENCE, ISSUER, LIFETIME_SECONDS, sha256Hex } from '../_shared/entitlement.ts';

/** `rxd_pat_` + 43 URL-safe base64 characters = 256 random bits. */
export const TOKEN_FORMAT = /^rxd_pat_[A-Za-z0-9_-]{43}$/;

/**
 * Limits. They are chosen so that the main use (a loop of `rxd` commands, dask workers, a Slurm array of
 * hundreds of tasks, all with one RADARX_TOKEN) is never limited, while guessing is:
 *
 *  - FAILED attempts are limited per client address (when the platform gives a trustworthy one) and per
 *    presented string: a guesser needs billions of tries for a 256-bit token, and is stopped after a few
 *    dozen. A failure is counted AFTER the work and checked BEFORE it, so a valid token is never touched
 *    by anybody else's failures and one address cannot lock out another.
 *  - SUCCESSFUL exchanges are limited per token (cost control and a leaked token's blast radius) and in
 *    all (a ceiling for the database); both numbers allow thousands of jobs an hour. Only successes are
 *    counted here, so an attacker without a valid token cannot use them to deny service.
 *  - The clients also share one exchange per machine per hour (a cache file), so real load is far
 *    below these numbers.
 */
export const LIMITS = {
  ipFailures: 30,
  ipFailureWindowSeconds: 600,
  tokenFailures: 20,
  tokenFailureWindowSeconds: 3600,
  tokenSuccesses: 5000,
  tokenSuccessWindowSeconds: 3600,
  allSuccesses: 100000,
  allSuccessWindowSeconds: 3600,
};

export interface TokenRow {
  id: string;
  user_id: string;
  email: string | null;
  name: string;
  /** ISO timestamp */
  expires_at: string;
}

export interface Dependencies {
  /** the database lookup by SHA-256 (public.use_api_token); null for any token that is not usable */
  lookup(hashHex: string): Promise<TokenRow | null>;
  /** public.api_token_rate_hit: counts one, false once the bucket is over its limit */
  rateHit(bucket: string, limit: number, windowSeconds: number): Promise<boolean>;
  /** public.api_token_rate_peek: true while the bucket is under its limit (counts nothing) */
  rateCheck(bucket: string, limit: number, windowSeconds: number): Promise<boolean>;
  sign(claims: Record<string, unknown>): Promise<string>;
  nowSeconds(): number;
  newId(): string;
}

export interface Answer {
  status: number;
  body: Record<string, unknown>;
  headers?: Record<string, string>;
}

const INVALID: Answer = { status: 401, body: { error: 'invalid_token' } };

function limited(windowSeconds: number): Answer {
  return {
    status: 429,
    body: { error: 'rate_limited' },
    headers: { 'Retry-After': String(Math.min(windowSeconds, 600)) },
  };
}

export async function handleExchange(
  input: { method: string; body: unknown; clientAddress: string | null },
  deps: Dependencies,
): Promise<Answer> {
  if (input.method !== 'POST') return { status: 405, body: { error: 'method_not_allowed' }, headers: { Allow: 'POST' } };

  // The per-address limit exists only when there is a trustworthy address. Without one nobody is put into
  // a shared bucket (that would let one caller lock everybody out): the per-token limits still apply.
  const ipBucket = input.clientAddress
    ? 'ipf:' + (await sha256Hex('ip:' + input.clientAddress)).slice(0, 40)
    : null;
  const failed = async (tokenBucket: string | null) => {
    if (ipBucket) await deps.rateHit(ipBucket, LIMITS.ipFailures, LIMITS.ipFailureWindowSeconds);
    if (tokenBucket) await deps.rateHit(tokenBucket, LIMITS.tokenFailures, LIMITS.tokenFailureWindowSeconds);
    return INVALID;
  };
  if (ipBucket && !(await deps.rateCheck(ipBucket, LIMITS.ipFailures, LIMITS.ipFailureWindowSeconds))) {
    return limited(LIMITS.ipFailureWindowSeconds);
  }

  const presented = (input.body as { token?: unknown } | null)?.token;
  if (typeof presented !== 'string' || !TOKEN_FORMAT.test(presented)) return failed(null);

  // the same string failing again and again, before anything else is looked at
  const hash = await sha256Hex(presented);
  const failBucket = 'tkf:' + hash.slice(0, 40);
  if (!(await deps.rateCheck(failBucket, LIMITS.tokenFailures, LIMITS.tokenFailureWindowSeconds))) {
    return limited(LIMITS.tokenFailureWindowSeconds);
  }

  // the lookup: unknown, revoked, expired, banned and deleted all come back as null
  const row = await deps.lookup(hash);
  if (row === null) return failed(failBucket);

  const patExpiry = Math.floor(Date.parse(row.expires_at) / 1000);
  const issuedAt = deps.nowSeconds();
  if (!Number.isFinite(patExpiry) || patExpiry <= issuedAt) return failed(failBucket);

  // a real token: only now do the success ceilings count
  if (!(await deps.rateHit('tok:' + hash.slice(0, 40), LIMITS.tokenSuccesses, LIMITS.tokenSuccessWindowSeconds))) {
    return limited(LIMITS.tokenSuccessWindowSeconds);
  }
  if (!(await deps.rateHit('all:ok', LIMITS.allSuccesses, LIMITS.allSuccessWindowSeconds))) {
    return limited(LIMITS.allSuccessWindowSeconds);
  }
  // never longer than the token it came from
  const expiresAt = Math.min(issuedAt + LIFETIME_SECONDS, patExpiry);

  const claims: Record<string, unknown> = {
    iss: ISSUER,
    aud: AUDIENCE,
    sub: row.user_id,
    src: 'pat',
    jti: deps.newId(),
    iat: issuedAt,
    exp: expiresAt,
    email: row.email ?? '',
    pat_id: row.id,
    pat_name: row.name,
    pat_exp: patExpiry,
  };
  return {
    status: 200,
    body: {
      entitlement_token: await deps.sign(claims),
      expires_at: expiresAt,
      token_name: row.name,
      token_expires_at: patExpiry,
    },
    headers: { 'Cache-Control': 'no-store' },
  };
}
