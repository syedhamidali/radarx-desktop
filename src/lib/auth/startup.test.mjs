// @ts-nocheck - plain node:test file, not part of the app bundle
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  parseStoredSession,
  tokenIssuedAt,
  decideOffline,
  planStartup,
  isDefiniteAuthFailure,
  sessionForEngine,
  isValidDeviceId,
  storedTokenIsCurrent,
} from './startup.ts';
import { EngineSessionSync } from './engineSession.ts';

const DAY = 86_400;
const NOW = 1_800_000_000; // seconds
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
const jwt = (iat) => `${b64({ alg: 'ES256' })}.${b64({ iat, exp: iat + 3600, sub: 'u' })}.c2ln`;
const stored = (iatAgoDays, extra = {}) => ({
  access_token: jwt(NOW - iatAgoDays * DAY),
  refresh_token: 'r',
  expires_at: NOW - iatAgoDays * DAY + 3600,
  user: { email: 'ada@example.test' },
  ...extra,
});

test('the stored session is read from the storage text', () => {
  const s = stored(1);
  assert.equal(parseStoredSession(JSON.stringify(s)).user.email, 'ada@example.test');
  for (const bad of [null, '', '{', '[]', '{"access_token":5}', '{"access_token":"a.b"}']) {
    assert.equal(parseStoredSession(bad), null);
  }
});

test('the iat is read from the token itself', () => {
  assert.equal(tokenIssuedAt(jwt(NOW)), NOW);
  assert.equal(tokenIssuedAt('garbage'), null);
  assert.equal(tokenIssuedAt(`a.${b64({ no: 'iat' })}.c`), null);
});

test('offline, an expired access token inside the 30 days opens the app', () => {
  // review finding 1: supabase-js getSession() is null here, the sign-in screen used to show
  const d = decideOffline(stored(1), NOW * 1000, 30);
  assert.equal(d.gate, 'open-offline');
  assert.equal(d.stale, true);
  assert.equal(d.daysLeft, 29);
  assert.equal(decideOffline(stored(29.5), NOW * 1000, 30).gate, 'open-offline');
});

test('offline, a current token is not stale', () => {
  const d = decideOffline(stored(0.01, { expires_at: NOW + 3000 }), NOW * 1000, 30);
  assert.equal(d.gate, 'open-offline');
  assert.equal(d.stale, false);
});

test('offline beyond the 30 days asks to reconnect', () => {
  assert.equal(decideOffline(stored(31), NOW * 1000, 30).gate, 'reconnect');
  assert.equal(decideOffline({ access_token: 'a.b.c' }, NOW * 1000, 30).gate, 'reconnect');
});

test('the 30 days count from the token iat, not from a timestamp the app wrote', () => {
  // an old token cannot be made young by editing local data: nothing but the signed iat is used
  const s = stored(40, { expires_at: NOW + 99999 });
  assert.equal(decideOffline(s, NOW * 1000, 30).gate, 'reconnect');
});

test('a token issued in the future of this clock does not look newer', () => {
  const s = { ...stored(0), access_token: jwt(NOW + 10 * DAY) };
  assert.equal(decideOffline(s, NOW * 1000, 30).gate, 'open-offline');
});

test('the start-up plan', () => {
  assert.equal(planStartup({ stored: null, online: true, nowMs: NOW * 1000, graceDays: 30 }).kind, 'needs-account');
  assert.equal(planStartup({ stored: null, online: false, nowMs: NOW * 1000, graceDays: 30 }).kind, 'needs-account');
  assert.equal(planStartup({ stored: stored(1), online: true, nowMs: NOW * 1000, graceDays: 30 }).kind, 'verify-online');
  const off = planStartup({ stored: stored(2), online: false, nowMs: NOW * 1000, graceDays: 30 });
  assert.deepEqual([off.kind, off.gate], ['offline', 'open-offline']);
  assert.equal(planStartup({ stored: stored(45), online: false, nowMs: NOW * 1000, graceDays: 30 }).gate, 'reconnect');
});

test('only a definite refusal signs the user out', () => {
  // review finding 9
  assert.equal(isDefiniteAuthFailure({ status: 401, code: 'refresh_token_not_found' }), true);
  assert.equal(isDefiniteAuthFailure({ status: 403, code: 'user_not_found' }), true);
  assert.equal(isDefiniteAuthFailure({ status: 400, message: 'Invalid Refresh Token: Already Used' }), true);
  assert.equal(isDefiniteAuthFailure({ status: 401 }), true);
  for (const e of [
    { status: 500 },
    { status: 502, message: 'Bad Gateway' },
    { status: 503 },
    { status: 429, code: 'over_request_rate_limit' },
    { status: 0, message: 'Failed to fetch' },
    { status: 200, message: "Unexpected token '<', \"<html>\" is not valid JSON" },
    { status: 403, message: 'Forbidden by corporate proxy' },
    { status: 408 },
    { status: 404 },
    { status: undefined, message: 'network' },
    null,
  ]) {
    assert.equal(isDefiniteAuthFailure(e), false, JSON.stringify(e));
  }
});

test('the stale stored session is what is presented to the engine when Supabase has none', async () => {
  assert.equal(sessionForEngine(null, null), null);
  assert.equal(sessionForEngine({ access_token: 'live' }, stored(1)).access_token, 'live');
  const offline = sessionForEngine(null, stored(1));
  assert.equal(offline.access_token, stored(1).access_token);

  // and the engine link sends it (the "open-offline" path used to send nothing: auth_required forever)
  const sent = [];
  const sync = new EngineSessionSync({
    send: (m) => sent.push(m),
    isConnected: () => true,
    getSession: async () => offline,
    refreshSession: async () => null,
    schedule: () => () => {},
    online: () => false,
    onAuthorised: () => {},
    onRequired: () => {},
  });
  sync.connected();
  await new Promise((r) => setImmediate(r));
  assert.equal(sent.length, 1);
  assert.equal(sent[0].type, 'auth_session');
  assert.equal(sent[0].access_token, stored(1).access_token);
  await sync.handle({ type: 'auth_ok', stale: true }); // offline: no renewal attempt
  assert.equal(sent.length, 1);
});

test('an install id is ASCII letters and digits only', () => {
  assert.equal(isValidDeviceId('abcdef1234567890'), true);
  for (const bad of ['éééééééééé', 'short', 'has-dash-12345678', 'a'.repeat(81), '', null, undefined, 12345678]) {
    assert.equal(isValidDeviceId(bad), false, String(bad));
  }
});

test('a stored token that is good for another minute is used without asking Supabase', () => {
  assert.equal(storedTokenIsCurrent(stored(0, { expires_at: NOW + 3000 }), NOW * 1000), true);
  assert.equal(storedTokenIsCurrent(stored(0, { expires_at: NOW + 30 }), NOW * 1000), false);
  assert.equal(storedTokenIsCurrent(stored(1), NOW * 1000), false);
  assert.equal(storedTokenIsCurrent(null, NOW * 1000), false);
  assert.equal(storedTokenIsCurrent({ access_token: 'a.b.c' }, NOW * 1000), false);
});
