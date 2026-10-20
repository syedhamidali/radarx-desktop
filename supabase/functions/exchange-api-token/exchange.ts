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

export const LIMITS = {
  /** exchanges per client address per 10 minutes (a cluster behind one address starts many jobs) */
  ipCount: 300,
  ipWindowSeconds: 600,
  /** exchanges per token (by its hash) per hour */
  tokenCount: 600,
  tokenWindowSeconds: 3600,
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
  /** public.api_token_rate_hit: false once the bucket is over its limit */
  rateHit(bucket: string, limit: number, windowSeconds: number): Promise<boolean>;
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

  // 1. the per-address limit comes first: it counts malformed requests too
  const ipBucket = 'ip:' + (await sha256Hex('ip:' + (input.clientAddress ?? 'unknown'))).slice(0, 40);
  if (!(await deps.rateHit(ipBucket, LIMITS.ipCount, LIMITS.ipWindowSeconds))) {
    return limited(LIMITS.ipWindowSeconds);
  }

  const presented = (input.body as { token?: unknown } | null)?.token;
  if (typeof presented !== 'string' || !TOKEN_FORMAT.test(presented)) return INVALID;

  // 2. the per-token limit, on the hash of what was presented (real or not)
  const hash = await sha256Hex(presented);
  if (!(await deps.rateHit('tok:' + hash.slice(0, 40), LIMITS.tokenCount, LIMITS.tokenWindowSeconds))) {
    return limited(LIMITS.tokenWindowSeconds);
  }

  // 3. the lookup: unknown, revoked, expired, banned and deleted all come back as null
  const row = await deps.lookup(hash);
  if (row === null) return INVALID;

  const patExpiry = Math.floor(Date.parse(row.expires_at) / 1000);
  const issuedAt = deps.nowSeconds();
  if (!Number.isFinite(patExpiry) || patExpiry <= issuedAt) return INVALID;
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
