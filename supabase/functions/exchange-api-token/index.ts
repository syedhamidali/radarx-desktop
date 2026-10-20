// exchange-api-token: a personal access token in, a one-hour entitlement token out.
// Logic and tests: exchange.ts / exchange.test.mjs. This file only does the Deno parts: environment,
// the database calls with the service-role key, and the HTTP answers.
// Deploy: see README.md. It needs `--no-verify-jwt`: callers have no Supabase token, the access
// token itself is the credential.
// deno-lint-ignore-file no-explicit-any
import { handleExchange } from './exchange.ts';
import { loadSigningKey, signEntitlement } from '../_shared/entitlement.ts';
import type { SigningKey } from '../_shared/entitlement.ts';

function reply(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...headers },
  });
}

let keyPromise: Promise<SigningKey> | null = null;
function signingKey(): Promise<SigningKey> {
  keyPromise ??= loadSigningKey(Deno.env.get('ENTITLEMENT_PRIVATE_KEY') ?? '').catch((e) => {
    keyPromise = null; // try again on the next request (the secret may have been fixed)
    throw e;
  });
  return keyPromise;
}

async function rpc(url: string, serviceKey: string, name: string, args: Record<string, unknown>): Promise<any> {
  const response = await fetch(`${url}/rest/v1/rpc/${name}`, {
    method: 'POST',
    headers: { apikey: serviceKey, Authorization: `Bearer ${serviceKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(args),
  });
  // the status is all that is ever reported: never the body, never a key
  if (!response.ok) throw new Error(`rpc ${name} failed: ${response.status}`);
  return response.json();
}

function clientAddress(req: Request): string | null {
  // cf-connecting-ip is set by the platform's edge and cannot be forged by the caller; without it
  // the LAST x-forwarded-for entry (the one the platform added) is used.
  const cf = req.headers.get('cf-connecting-ip');
  if (cf) return cf.trim();
  const forwarded = req.headers.get('x-forwarded-for');
  return forwarded ? forwarded.split(',').pop()!.trim() : null;
}

Deno.serve(async (req: Request) => {
  const url = Deno.env.get('SUPABASE_URL');
  const serviceKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
  if (!url || !serviceKey || !Deno.env.get('ENTITLEMENT_PRIVATE_KEY')) {
    console.error(JSON.stringify({ event: 'exchange_not_configured' }));
    return reply(503, { error: 'unavailable' });
  }
  let body: unknown = null;
  if (req.method === 'POST') {
    const length = Number(req.headers.get('content-length') ?? '0');
    if (length > 2048) return reply(413, { error: 'too_large' });
    try {
      const text = await req.text();
      body = text.length <= 2048 ? JSON.parse(text) : null;
    } catch {
      body = null; // the logic answers invalid_token for anything without a proper token
    }
  }
  try {
    const key = await signingKey();
    const answer = await handleExchange(
      { method: req.method, body, clientAddress: clientAddress(req) },
      {
        lookup: async (hashHex) => (await rpc(url, serviceKey, 'use_api_token', { p_hash: '\\x' + hashHex })) ?? null,
        rateHit: async (bucket, limit, windowSeconds) =>
          (await rpc(url, serviceKey, 'api_token_rate_hit', { p_bucket: bucket, p_limit: limit, p_window_s: windowSeconds })) === true,
        sign: (claims) => signEntitlement(claims, key),
        nowSeconds: () => Math.floor(Date.now() / 1000),
        newId: () => crypto.randomUUID(),
      },
    );
    return reply(answer.status, answer.body, answer.headers);
  } catch (e) {
    // a transient problem (database, secret): 503, so a client retries and never concludes the token is bad
    console.error(JSON.stringify({ event: 'exchange_failed', error: e instanceof Error ? e.message.slice(0, 120) : 'unknown' }));
    return reply(503, { error: 'unavailable' });
  }
});
