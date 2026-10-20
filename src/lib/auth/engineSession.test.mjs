// @ts-nocheck - plain node:test file, not part of the app bundle
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildAuthSessionMessage, EngineSessionSync, RETRY_DELAYS_MS, SIGN_OUT_MESSAGE } from './engineSession.ts';

function harness(over = {}) {
  const sent = [];
  const timers = [];
  const calls = { authorised: [], required: [], refreshed: 0 };
  let connected = true;
  let session = { access_token: 'tok1', expires_at: 1_800_000_000 };
  let pending = over.pendingStart ?? false;
  const deps = {
    pendingSignOut: { get: () => pending, set: (v) => (pending = v) },
    send: (m) => sent.push(m),
    isConnected: () => connected,
    getSession: async () => session,
    refreshSession: async () => {
      calls.refreshed += 1;
      if (over.refreshFails) return null;
      session = { access_token: `tok${calls.refreshed + 1}`, expires_at: 1_800_003_600 };
      return session;
    },
    schedule: (fn, ms) => {
      const t = { fn, ms, cancelled: false };
      timers.push(t);
      return () => (t.cancelled = true);
    },
    online: () => over.online ?? true,
    onAuthorised: (r) => calls.authorised.push(r),
    onRequired: (reason, message) => calls.required.push({ reason, message }),
    deviceId: () => 'dev12345678',
    appVersion: () => '0.3.0',
  };
  const sync = new EngineSessionSync(deps);
  return { sync, sent, timers, calls, setConnected: (v) => (connected = v), setSession: (s) => (session = s), pending: () => pending };
}
const flush = () => new Promise((r) => setImmediate(r));

test('the message has the documented shape and never carries a refresh token', () => {
  const m = buildAuthSessionMessage({ access_token: 'a.b.c', expires_at: 123, refresh_token: 'SECRET' }, 'sign_in', {
    deviceId: 'abcdef123456',
    appVersion: '0.3.0',
  });
  assert.deepEqual(m, {
    type: 'auth_session',
    access_token: 'a.b.c',
    reason: 'sign_in',
    expires_at: 123,
    device_id: 'abcdef123456',
    app_version: '0.3.0',
  });
  assert.equal('refresh_token' in m, false);
  assert.equal(JSON.stringify(m).includes('SECRET'), false);
});

test('optional fields are left out when unknown', () => {
  assert.deepEqual(buildAuthSessionMessage({ access_token: 'x' }, 'start'), {
    type: 'auth_session',
    access_token: 'x',
    reason: 'start',
  });
});

test('connecting sends the session once, as "start"', async () => {
  const h = harness();
  h.sync.connected();
  await flush();
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].type, 'auth_session');
  assert.equal(h.sent[0].reason, 'start');
  assert.equal(h.sent[0].device_id, 'dev12345678');
});

test('a sign-in before the socket is open is remembered and sent as "sign_in" on connect', async () => {
  const h = harness();
  h.setConnected(false);
  h.sync.signedIn();
  h.setConnected(true);
  h.sync.connected();
  await flush();
  assert.equal(h.sent[0].reason, 'sign_in');
  h.sync.disconnected();
  h.sync.connected();
  await flush();
  assert.equal(h.sent[1].reason, 'reconnect');
});

test('no answer: the session is sent again with growing waits, then it gives up quietly', async () => {
  const h = harness();
  h.sync.connected();
  await flush();
  assert.equal(h.timers[0].ms, RETRY_DELAYS_MS[0]);
  for (let i = 0; i < RETRY_DELAYS_MS.length; i++) {
    h.timers.at(-1).fn();
    await flush();
  }
  assert.equal(h.sent.length, 1 + RETRY_DELAYS_MS.length);
  assert.deepEqual(h.timers.map((t) => t.ms).slice(0, 4), RETRY_DELAYS_MS);
  h.timers.at(-1).fn(); // one more time-out: gives up
  await flush();
  assert.equal(h.sent.length, 1 + RETRY_DELAYS_MS.length);
  assert.equal(h.sync.pending, false);
});

test('auth_ok stops the retry timer and releases the held requests', async () => {
  const h = harness();
  h.sync.connected();
  await flush();
  await h.sync.handle({ type: 'auth_ok', email: 'a@b.c' });
  assert.equal(h.timers.at(-1).cancelled, true);
  assert.equal(h.calls.authorised.length, 1);
  assert.equal(h.sync.pending, false);
});

test('an expired token is renewed once with Supabase and sent again', async () => {
  const h = harness();
  h.sync.connected();
  await flush();
  await h.sync.handle({ type: 'auth_required', reason: 'expired', message: 'expired' });
  assert.equal(h.calls.refreshed, 1);
  assert.equal(h.sent.at(-1).access_token, 'tok2');
  assert.equal(h.sent.at(-1).reason, 'refresh');
  assert.equal(h.calls.required.length, 0);
  // refused again: no second renewal, the sign-in screen is shown
  await h.sync.handle({ type: 'auth_required', reason: 'expired', message: 'still expired' });
  assert.equal(h.calls.refreshed, 1);
  assert.deepEqual(h.calls.required, [{ reason: 'expired', message: 'still expired' }]);
});

test('when Supabase cannot renew the token the sign-in screen is shown', async () => {
  const h = harness({ refreshFails: true });
  h.sync.connected();
  await flush();
  await h.sync.handle({ type: 'auth_required', reason: 'invalid', message: 'Please sign in again.' });
  assert.equal(h.calls.required.length, 1);
});

test('reasons a new token cannot fix go straight to the sign-in screen', async () => {
  for (const reason of ['not_entitled', 'revoked', 'offline_expired']) {
    const h = harness();
    h.sync.connected();
    await flush();
    await h.sync.handle({ type: 'auth_required', reason, message: `m-${reason}` });
    assert.equal(h.calls.refreshed, 0);
    assert.deepEqual(h.calls.required, [{ reason, message: `m-${reason}` }]);
  }
});

test('a request that overtook the session message does not trigger the sign-in screen', async () => {
  const h = harness();
  h.sync.connected();
  await flush();
  await h.sync.handle({ type: 'auth_required', reason: 'no_session', request_type: 'list_modules' });
  assert.equal(h.calls.required.length, 0);
  assert.equal(h.sent.length, 1); // ours is still on its way: not sent twice
  await h.sync.handle({ type: 'auth_ok' });
  h.sync.disconnected();
  await h.sync.handle({ type: 'auth_required', reason: 'no_session' });
  assert.equal(h.sent.length, 2); // nothing pending: send it
});

test('an expired token accepted by the offline grace is renewed when online', async () => {
  const h = harness();
  h.sync.connected();
  await flush();
  await h.sync.handle({ type: 'auth_ok', stale: true });
  assert.equal(h.calls.refreshed, 1);
  assert.equal(h.sent.at(-1).reason, 'refresh');
  const off = harness({ online: false });
  off.sync.connected();
  await flush();
  await off.sync.handle({ type: 'auth_ok', stale: true });
  assert.equal(off.calls.refreshed, 0);
});

test('a refreshed Supabase token is passed on, but only while connected', async () => {
  const h = harness();
  h.sync.tokenRefreshed();
  await flush();
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].reason, 'refresh');
  h.setConnected(false);
  h.sync.tokenRefreshed();
  await flush();
  assert.equal(h.sent.length, 1);
});

test('signing out tells the engine and stops waiting', async () => {
  const h = harness();
  h.sync.connected();
  await flush();
  h.sync.signOut();
  assert.deepEqual(h.sent.at(-1), SIGN_OUT_MESSAGE);
  assert.equal(h.timers.at(-1).cancelled, true);
  assert.equal(h.sync.pending, false);
});

test('nothing is sent when there is no session any more', async () => {
  const h = harness();
  h.setSession(null);
  h.sync.connected();
  await flush();
  assert.equal(h.sent.length, 0);
});

test('a sign-out with no connection is remembered and delivered first when the engine is reached', async () => {
  const h = harness();
  h.setConnected(false);
  h.sync.signOut();
  assert.equal(h.pending(), true);
  assert.equal(h.sent.length, 0);
  h.setConnected(true);
  h.setSession(null);
  h.sync.connected();
  await flush();
  assert.deepEqual(h.sent[0], SIGN_OUT_MESSAGE);
  assert.equal(h.pending(), false);
});

test('a sign-out that reaches the engine leaves nothing pending', () => {
  const h = harness();
  h.sync.signOut();
  assert.deepEqual(h.sent.at(-1), SIGN_OUT_MESSAGE);
  assert.equal(h.pending(), false);
});

test('signing in again cancels a sign-out that was still waiting', async () => {
  const h = harness({ pendingStart: true });
  h.sync.signedIn();
  assert.equal(h.pending(), false);
  h.sync.connected();
  await flush();
  assert.equal(h.sent.some((m) => m.type === 'auth_signout'), false);
});
