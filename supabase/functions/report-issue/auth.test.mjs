import test from 'node:test';
import assert from 'node:assert/strict';
import { bearerToken, verifyUser } from './auth.ts';

const JWT = 'aaaaaaaa.bbbbbbbb.cccccccc';
const UID = '3f2b6c1e-8a4d-4b7e-9f10-0123456789ab';
const ok = (json) => async () => ({ ok: true, status: 200, json: async () => json });
const status = (s) => async () => ({ ok: false, status: s, json: async () => ({}) });

test('bearer tokens: only a JWT-shaped value after "Bearer"', () => {
  assert.equal(bearerToken(`Bearer ${JWT}`), JWT);
  assert.equal(bearerToken(`bearer   ${JWT}`), JWT);
  for (const bad of [null, '', 'Bearer', 'Bearer x', `Basic ${JWT}`, JWT, `Bearer ${JWT} extra`, 'Bearer a.b', 'Bearer a.b.c.d', `Bearer ${JWT}\nX: y`, `Bearer a b.c.d`]) {
    assert.equal(bearerToken(bad), null, String(bad));
  }
});

test('the token is checked with the Supabase auth API using the anon key, and the id comes from the answer', async () => {
  const calls = [];
  const fetchFn = async (url, init) => (calls.push({ url, init }), { ok: true, status: 200, json: async () => ({ id: UID.toUpperCase(), email: 'x@y.co' }) });
  const r = await verifyUser({ supabaseUrl: 'https://p.supabase.co/', anonKey: 'ANON', token: JWT, fetchFn });
  assert.deepEqual(r, { status: 'ok', userId: UID });
  assert.equal(calls[0].url, 'https://p.supabase.co/auth/v1/user');
  assert.equal(calls[0].init.method, 'GET');
  assert.equal(calls[0].init.headers.apikey, 'ANON');
  assert.equal(calls[0].init.headers.Authorization, `Bearer ${JWT}`);
});

test('401 and 403 mean an invalid or expired sign-in', async () => {
  for (const s of [401, 403]) assert.deepEqual(await verifyUser({ supabaseUrl: 'https://p', anonKey: 'k', token: JWT, fetchFn: status(s) }), { status: 'invalid' });
});

test('anonymous users are not accounts', async () => {
  assert.deepEqual(await verifyUser({ supabaseUrl: 'https://p', anonKey: 'k', token: JWT, fetchFn: ok({ id: UID, is_anonymous: true }) }), { status: 'invalid' });
});

test('anything else fails closed as "unavailable"', async () => {
  for (const f of [status(500), status(502), status(404), ok({}), ok({ id: 'not-a-uuid' }), ok(null), ok({ id: 5 }),
    async () => { throw new TypeError('fetch failed'); }]) {
    assert.deepEqual(await verifyUser({ supabaseUrl: 'https://p', anonKey: 'k', token: JWT, fetchFn: f }), { status: 'unavailable' });
  }
});

test('a hanging auth API times out', async () => {
  const fetchFn = (url, init) => new Promise((_, rej) => init.signal.addEventListener('abort', () => rej(new Error('x'))));
  assert.deepEqual(await verifyUser({ supabaseUrl: 'https://p', anonKey: 'k', token: JWT, fetchFn, timeoutMs: 20 }), { status: 'unavailable' });
});
