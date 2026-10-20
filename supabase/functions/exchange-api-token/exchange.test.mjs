// Tests of exchange.ts under plain Node, with fake dependencies (no Deno, no database, no network).
import test from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync } from 'node:crypto';
import { handleExchange, LIMITS, TOKEN_FORMAT } from './exchange.ts';
import { AUDIENCE, ISSUER, loadSigningKey, sha256Hex, signEntitlement, verifyEntitlement } from '../_shared/entitlement.ts';

const NOW = 1_800_000_000;
const USER = '11111111-2222-3333-4444-555555555555';
const GOOD = 'rxd_pat_' + 'A'.repeat(43);

async function setup(over = {}) {
  const pem = generateKeyPairSync('ec', { namedCurve: 'P-256' }).privateKey.export({ type: 'pkcs8', format: 'pem' });
  const key = await loadSigningKey(pem);
  const calls = { lookup: [], rate: [], sign: 0 };
  const counters = new Map();
  const goodHash = await sha256Hex(GOOD);
  const rows = new Map([[goodHash, {
    id: 'pat-1', user_id: USER, email: 'ada@example.test', name: 'nightly',
    expires_at: new Date((NOW + 30 * 86400) * 1000).toISOString(),
  }]]);
  let n = 0;
  const deps = {
    lookup: async (hash) => { calls.lookup.push(hash); return rows.get(hash) ?? null; },
    rateHit: async (bucket, limit, windowSeconds) => {
      calls.rate.push({ bucket, limit, windowSeconds });
      const c = (counters.get(bucket) ?? 0) + 1;
      counters.set(bucket, c);
      return c <= limit;
    },
    sign: async (claims) => { calls.sign++; return signEntitlement(claims, key); },
    nowSeconds: () => NOW,
    newId: () => `jti-${++n}`,
    ...over,
  };
  return { key, deps, calls, rows, counters, goodHash };
}
const post = (token, ip = '203.0.113.7') => ({ method: 'POST', body: { token }, clientAddress: ip });

test('a good token gives a verifiable one-hour entitlement token', async () => {
  const { key, deps, calls } = await setup();
  const out = await handleExchange(post(GOOD), deps);
  assert.equal(out.status, 200);
  assert.equal(out.headers['Cache-Control'], 'no-store');
  const claims = await verifyEntitlement(out.body.entitlement_token, key, NOW + 5);
  assert.deepEqual(claims, {
    iss: ISSUER, aud: AUDIENCE, sub: USER, src: 'pat', jti: 'jti-1', iat: NOW, exp: NOW + 3600,
    email: 'ada@example.test', pat_id: 'pat-1', pat_name: 'nightly', pat_exp: NOW + 30 * 86400,
  });
  assert.equal(out.body.expires_at, NOW + 3600);
  assert.equal(out.body.token_name, 'nightly');
  assert.equal(out.body.token_expires_at, NOW + 30 * 86400);
  assert.equal(calls.lookup.length, 1);
  assert.equal(calls.lookup[0], await sha256Hex(GOOD)); // looked up by hash, never by the token
  assert.ok(!JSON.stringify(out.body).includes(GOOD));
});

test('every exchange gets its own jti', async () => {
  const { deps, key } = await setup();
  const a = await handleExchange(post(GOOD), deps);
  const b = await handleExchange(post(GOOD), deps);
  const ja = (await verifyEntitlement(a.body.entitlement_token, key, NOW)).jti;
  const jb = (await verifyEntitlement(b.body.entitlement_token, key, NOW)).jti;
  assert.notEqual(ja, jb);
});

test('the token never outlives the access token it came from', async () => {
  const { deps, rows, goodHash, key } = await setup();
  rows.get(goodHash).expires_at = new Date((NOW + 600) * 1000).toISOString();
  const out = await handleExchange(post(GOOD), deps);
  assert.equal(out.status, 200);
  assert.equal(out.body.expires_at, NOW + 600);
  assert.equal((await verifyEntitlement(out.body.entitlement_token, key, NOW)).exp, NOW + 600);
});

test('unknown, revoked/expired (null from the database), malformed and missing tokens are indistinguishable', async () => {
  const { deps } = await setup();
  const answers = [];
  for (const token of ['rxd_pat_' + 'B'.repeat(43), GOOD.slice(0, -1), GOOD + 'x', 'rxd_pat_' + '!'.repeat(43), '', null, 5, undefined, {}, [GOOD]]) {
    answers.push(JSON.stringify(await handleExchange(post(token), deps)));
  }
  answers.push(JSON.stringify(await handleExchange({ method: 'POST', body: null, clientAddress: '1.2.3.4' }, deps)));
  answers.push(JSON.stringify(await handleExchange({ method: 'POST', body: 'text', clientAddress: '1.2.3.4' }, deps)));
  assert.equal(new Set(answers).size, 1, 'all failures look the same');
  assert.deepEqual(JSON.parse(answers[0]), { status: 401, body: { error: 'invalid_token' } });
  // a database row that says the token is expired (clock skew) is the same answer too
  const { deps: d2, rows, goodHash } = await setup();
  rows.get(goodHash).expires_at = new Date((NOW - 1) * 1000).toISOString();
  assert.equal(JSON.stringify(await handleExchange(post(GOOD), d2)), answers[0]);
  rows.get(goodHash).expires_at = 'garbage';
  assert.equal(JSON.stringify(await handleExchange(post(GOOD), d2)), answers[0]);
});

test('the format check is the one the app and the database use', () => {
  assert.ok(TOKEN_FORMAT.test(GOOD));
  assert.ok(!TOKEN_FORMAT.test(GOOD + '\n'));
  assert.ok(!TOKEN_FORMAT.test(' ' + GOOD));
  assert.ok(!TOKEN_FORMAT.test('rxd_pat_' + 'A'.repeat(44)));
});

test('only POST is accepted', async () => {
  const { deps, calls } = await setup();
  for (const method of ['GET', 'PUT', 'DELETE', 'PATCH']) {
    const out = await handleExchange({ method, body: { token: GOOD }, clientAddress: '1.1.1.1' }, deps);
    assert.equal(out.status, 405);
  }
  assert.equal(calls.lookup.length, 0);
  assert.equal(calls.rate.length, 0);
});

test('the per-address limit counts every request, even malformed ones, and comes before the lookup', async () => {
  const { deps, calls } = await setup();
  let last;
  for (let i = 0; i < LIMITS.ipCount + 1; i++) last = await handleExchange(post('junk', '198.51.100.9'), deps);
  assert.equal(last.status, 429);
  assert.deepEqual(last.body, { error: 'rate_limited' });
  assert.ok(Number(last.headers['Retry-After']) > 0);
  assert.equal(calls.lookup.length, 0, 'a limited or malformed request never reaches the database lookup');
  // another address is unaffected
  assert.equal((await handleExchange(post(GOOD, '198.51.100.10'), deps)).status, 200);
});

test('the per-token limit counts what was presented, real or not, before the lookup', async () => {
  const { deps, calls } = await setup();
  const fake = 'rxd_pat_' + 'C'.repeat(43);
  let status = 0;
  for (let i = 0; i < LIMITS.tokenCount + 1; i++) status = (await handleExchange(post(fake, `10.0.${i % 250}.${Math.floor(i / 250)}`), deps)).status;
  assert.equal(status, 429);
  assert.equal(calls.lookup.length, LIMITS.tokenCount, 'the limited request was not looked up');
  // the answer for an unknown token is 401 until the limit, then 429: the limit is the only difference,
  // and it is the same for a real token presented as often
  const real = await setup();
  let s = 0;
  for (let i = 0; i < LIMITS.tokenCount + 1; i++) s = (await handleExchange(post(GOOD, `10.1.${i % 250}.${Math.floor(i / 250)}`), real.deps)).status;
  assert.equal(s, 429);
});

test('rate-limit keys are hashes: no address and no token appears in them', async () => {
  const { deps, calls } = await setup();
  await handleExchange(post(GOOD, '203.0.113.77'), deps);
  for (const { bucket } of calls.rate) {
    assert.ok(!bucket.includes('203.0.113.77') && !bucket.includes(GOOD) && !bucket.includes('AAAA'));
    assert.match(bucket, /^(ip|tok):[0-9a-f]{40}$/);
    assert.ok(bucket.length <= 100);
  }
  assert.deepEqual(calls.rate.map((r) => r.bucket.split(':')[0]), ['ip', 'tok']);
  assert.equal(calls.rate[0].limit, LIMITS.ipCount);
  assert.equal(calls.rate[1].limit, LIMITS.tokenCount);
});

test('a missing address is counted as one bucket, not skipped', async () => {
  const { deps, calls } = await setup();
  await handleExchange({ method: 'POST', body: { token: GOOD }, clientAddress: null }, deps);
  assert.equal(calls.rate.length, 2);
});

test('trouble in the database or the signer is an exception for the caller to turn into 503, never a 401', async () => {
  const boom = await setup({ lookup: async () => { throw new Error('db down'); } });
  await assert.rejects(handleExchange(post(GOOD), boom.deps), /db down/);
  const noRate = await setup({ rateHit: async () => { throw new Error('rate down'); } });
  await assert.rejects(handleExchange(post(GOOD), noRate.deps), /rate down/);
  const noSign = await setup({ sign: async () => { throw new Error('no key'); } });
  await assert.rejects(handleExchange(post(GOOD), noSign.deps), /no key/);
});

test('nothing secret is in the answers: no token, no hash, no key material', async () => {
  const { deps } = await setup();
  const bad = JSON.stringify(await handleExchange(post('rxd_pat_' + 'Z'.repeat(43)), deps));
  assert.ok(!bad.includes('Z'.repeat(10)) && !bad.includes(await sha256Hex('rxd_pat_' + 'Z'.repeat(43))));
  const good = JSON.stringify(await handleExchange(post(GOOD), deps));
  assert.ok(!good.includes(await sha256Hex(GOOD)) && !good.includes(GOOD));
});
