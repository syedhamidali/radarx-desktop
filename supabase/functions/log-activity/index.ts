// log-activity: the activity log of a token session. Logic and tests: activity.ts /
// activity.test.mjs. This file only does the Deno parts.
// Deploy: see README.md (`--no-verify-jwt`: the caller holds an entitlement token, not a Supabase one).
// deno-lint-ignore-file no-explicit-any
import { handleLog } from './activity.ts';
import type { Row } from './activity.ts';
import { loadVerifyKey, verifyEntitlement } from '../_shared/entitlement.ts';
import { BodyTooLarge, parseJson, readLimitedText } from '../_shared/http.ts';

function reply(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...headers },
  });
}

type VerifyKey = Awaited<ReturnType<typeof loadVerifyKey>>;
let keyPromise: Promise<VerifyKey> | null = null;
function verifyKey(): Promise<VerifyKey> {
  // Only the PUBLIC half (ENTITLEMENT_PUBLIC_JWK) is read here, never the private key.
  keyPromise ??= loadVerifyKey(Deno.env.get('ENTITLEMENT_PUBLIC_JWK') ?? '').catch((e) => {
    keyPromise = null;
    throw e;
  });
  return keyPromise;
}

Deno.serve(async (req: Request) => {
  const url = Deno.env.get('SUPABASE_URL');
  const serviceKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
  if (!url || !serviceKey || !Deno.env.get('ENTITLEMENT_PUBLIC_JWK')) {
    console.error(JSON.stringify({ event: 'log_not_configured' }));
    return reply(503, { error: 'unavailable' });
  }
  let body: unknown = null;
  if (req.method === 'POST') {
    try {
      body = parseJson(await readLimitedText(req, 65536)); // the bytes actually read, never the header
    } catch (e) {
      if (e instanceof BodyTooLarge) return reply(413, { error: 'too_large' });
      body = null;
    }
  }
  const headers = { apikey: serviceKey, Authorization: `Bearer ${serviceKey}`, 'Content-Type': 'application/json' };
  try {
    const key = await verifyKey();
    const answer = await handleLog(
      { method: req.method, authorization: req.headers.get('authorization'), body },
      {
        verify: (token) => verifyEntitlement(token, key, Math.floor(Date.now() / 1000)),
        rateHit: async (bucket, limit, windowSeconds) => {
          const r = await fetch(`${url}/rest/v1/rpc/api_token_rate_hit`, {
            method: 'POST',
            headers,
            body: JSON.stringify({ p_bucket: bucket, p_limit: limit, p_window_s: windowSeconds }),
          });
          if (!r.ok) throw new Error(`rate counter failed: ${r.status}`);
          return (await r.json()) === true;
        },
        insert: async (rows: Row[]) => {
          const r = await fetch(`${url}/rest/v1/activity_events`, {
            method: 'POST',
            headers: { ...headers, Prefer: 'return=minimal' },
            body: JSON.stringify(rows),
          });
          let code = '';
          if (!r.ok) {
            try {
              code = String(((await r.json()) as any)?.code ?? '');
            } catch {
              /* the status is enough */
            }
          }
          return { status: r.status, code };
        },
      },
    );
    return reply(answer.status, answer.body, answer.headers);
  } catch (e) {
    console.error(JSON.stringify({ event: 'log_failed', error: e instanceof Error ? e.message.slice(0, 120) : 'unknown' }));
    return reply(503, { error: 'unavailable' });
  }
});
