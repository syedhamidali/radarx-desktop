// Smoke test of index.ts, the Deno wiring: a stand-in Deno global, a stand-in for npm:@supabase/supabase-js and a
// fake network. It cannot prove the code runs on Supabase (no Deno on the build machine) but it does run the real
// file: environment reading, the database adapter (rpc names and arguments, error mapping) and the handler.
import test from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';

register(
  'data:text/javascript,' +
    encodeURIComponent(`export async function resolve(spec, ctx, next) {
      if (spec.startsWith('npm:@supabase/supabase-js')) {
        return { url: 'data:text/javascript,' + encodeURIComponent('export const createClient = (...a) => globalThis.__createClient(...a);'), shortCircuit: true };
      }
      return next(spec, ctx);
    }`),
  import.meta.url,
);

const env = {
  SUPABASE_URL: 'https://proj.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'SERVICE_ROLE_SENTINEL', SUPABASE_ANON_KEY: 'ANON',
  TURNSTILE_SECRET_KEY: '1x0000000000000000000000000000000AA', REPORT_IP_SALT: 'salt',
};
const rpcCalls = [];
let rpcPlan = {};
const created = [];
globalThis.__createClient = (url, key, opts) => {
  created.push({ url, key, opts });
  return { rpc: async (name, args) => (rpcCalls.push({ name, args }), rpcPlan[name] ? rpcPlan[name](args) : { data: null, error: null }) };
};
let serve;
globalThis.Deno = { env: { get: (n) => env[n] }, serve: (fn) => (serve = fn) };
const waited = [];
globalThis.EdgeRuntime = { waitUntil: (p) => waited.push(p) };
const logs = [];
console.log = (s) => logs.push(String(s));
globalThis.fetch = async (url, init) => {
  const u = new URL(url);
  if (u.host === 'challenges.cloudflare.com') return { ok: true, status: 200, json: async () => ({ success: true }) };
  if (u.host === 'proj.supabase.co') return { ok: true, status: 200, json: async () => ({ id: '3f2b6c1e-8a4d-4b7e-9f10-0123456789ab' }) };
  throw new Error('unexpected ' + url);
};

await import('./index.ts');

const post = (body, headers = {}) => serve(new Request('https://proj.supabase.co/functions/v1/report-issue', { method: 'POST', headers: { 'content-type': 'application/json', ...headers }, body: JSON.stringify(body) }));
const web = { source: 'web', kind: 'bug', title: 'A title', description: 'A description long enough.', turnstile_token: 'tok' };

test('index.ts registers a handler with Deno.serve', () => {
  assert.equal(typeof serve, 'function');
});

test('a web report goes through the real handler to the right database functions', async () => {
  rpcCalls.length = 0;
  rpcPlan = { take_issue_limit: () => ({ data: true, error: null }), insert_issue_report: () => ({ data: '00000000-0000-4000-8000-000000000001', error: null }) };
  const res = await post(web, { 'cf-connecting-ip': '198.51.100.9' });
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { id: '00000000-0000-4000-8000-000000000001' });
  assert.deepEqual(rpcCalls.map((c) => c.name), ['take_issue_limit', 'take_issue_limit', 'insert_issue_report']);
  const ins = rpcCalls[2].args;
  assert.equal(ins.p_source, 'web');
  assert.equal(ins.p_user_id, null);
  assert.equal(ins.p_title, 'A title');
  assert.equal(ins.p_global_key, null);
  assert.match(rpcCalls[0].args.p_key, /^ip:[0-9a-f]{64}$/);
  assert.equal(rpcCalls[1].args.p_key, 'global:web');
  assert.equal(rpcCalls[0].args.p_max, 5);
  assert.equal(JSON.stringify(rpcCalls).includes('198.51.100.9'), false);
  // the service-role client is built from the function's own environment, without a stored session
  assert.equal(created[0].url, 'https://proj.supabase.co');
  assert.equal(created[0].key, 'SERVICE_ROLE_SENTINEL');
  assert.equal(created[0].opts.auth.persistSession, false);
  // the notification was handed to the platform to finish after the answer
  assert.equal(waited.length >= 1, true);
  await Promise.all(waited);
});

test('an app report passes the verified user, the cap and the global bucket to insert_issue_report', async () => {
  rpcCalls.length = 0;
  rpcPlan = { insert_issue_report: () => ({ data: '00000000-0000-4000-8000-000000000002', error: null }) };
  const res = await post({ ...web, source: 'app', turnstile_token: undefined }, { authorization: 'Bearer aaaaaaaa.bbbbbbbb.cccccccc' });
  assert.equal(res.status, 200);
  const a = rpcCalls.find((c) => c.name === 'insert_issue_report').args;
  assert.equal(a.p_user_id, '3f2b6c1e-8a4d-4b7e-9f10-0123456789ab');
  assert.equal(a.p_user_cap, 10);
  assert.equal(a.p_global_key, 'global:app');
  assert.equal(a.p_global_max, 1000);
  await Promise.all(waited);
});

test('the database saying "report limit" or "global limit" becomes 429', async () => {
  for (const message of ['report limit', 'global limit']) {
    rpcPlan = { insert_issue_report: () => ({ data: null, error: { code: 'P0001', message } }) };
    const res = await post({ ...web, source: 'app', turnstile_token: undefined }, { authorization: 'Bearer aaaaaaaa.bbbbbbbb.cccccccc' });
    assert.equal(res.status, 429, message);
    assert.equal((await res.json()).error.code, 'rate_limited');
  }
});

test('any other database error is a server_error that leaks nothing, in the answer or the log', async () => {
  logs.length = 0;
  rpcPlan = { take_issue_limit: () => ({ data: true, error: null }), insert_issue_report: () => ({ data: null, error: { code: '23514', message: 'new row for relation "issue_reports" violates check constraint, DETAIL: Failing row contains (A description long enough.)' } }) };
  const res = await post(web);
  assert.equal(res.status, 500);
  const text = await res.text();
  assert.equal((JSON.parse(text)).error.code, 'server_error');
  const all = text + logs.join('\n');
  assert.equal(/issue_reports|DETAIL|Failing row|A description long enough|SERVICE_ROLE/.test(all), false);
  assert.ok(logs.some((l) => l.includes('"outcome":"error"')));
});

test('a limit RPC failure is a server_error too', async () => {
  rpcPlan = { take_issue_limit: () => ({ data: null, error: { code: '42501', message: 'permission denied' } }) };
  const res = await post(web);
  assert.equal(res.status, 500);
});
