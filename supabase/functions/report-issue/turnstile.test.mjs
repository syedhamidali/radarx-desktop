import test from 'node:test';
import assert from 'node:assert/strict';
import { verifyTurnstile, SITEVERIFY_URL } from './turnstile.ts';

// Cloudflare's public test secrets: 1x...AA always passes, 2x...AA always fails. (Not real secrets.)
const SECRET_PASS = '1x0000000000000000000000000000000AA';
const SECRET_FAIL = '2x0000000000000000000000000000000AA';

const reply = (json, status = 200) => async () => ({ ok: status >= 200 && status < 300, status, json: async () => json });

test('success: true is a pass, and the request is what Cloudflare documents', async () => {
  const calls = [];
  const fetchFn = async (url, init) => {
    calls.push({ url, init });
    return { ok: true, status: 200, json: async () => ({ success: true }) };
  };
  const r = await verifyTurnstile({ secret: SECRET_PASS, token: 'XXXX.DUMMY.TOKEN.XXXX', ip: '203.0.113.7', fetchFn });
  assert.deepEqual(r, { outcome: 'pass', codes: [] });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, SITEVERIFY_URL);
  assert.equal(calls[0].url, 'https://challenges.cloudflare.com/turnstile/v0/siteverify');
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers['Content-Type'], 'application/x-www-form-urlencoded');
  const form = new URLSearchParams(calls[0].init.body);
  assert.equal(form.get('secret'), SECRET_PASS);
  assert.equal(form.get('response'), 'XXXX.DUMMY.TOKEN.XXXX');
  assert.equal(form.get('remoteip'), '203.0.113.7');
});

test('remoteip is left out when the address is not known', async () => {
  let body = '';
  const fetchFn = async (url, init) => ((body = init.body), { ok: true, status: 200, json: async () => ({ success: true }) });
  await verifyTurnstile({ secret: SECRET_PASS, token: 't', ip: null, fetchFn });
  assert.equal(new URLSearchParams(body).has('remoteip'), false);
});

test('a token Cloudflare refuses is a fail (not a server error)', async () => {
  for (const codes of [['invalid-input-response'], ['timeout-or-duplicate'], ['missing-input-response'], []]) {
    const r = await verifyTurnstile({ secret: SECRET_FAIL, token: 't', ip: null, fetchFn: reply({ success: false, 'error-codes': codes }) });
    assert.equal(r.outcome, 'fail', codes.join());
  }
});

test('a wrong secret or Cloudflare trouble is "unavailable", never a pass and never blamed on the visitor', async () => {
  for (const code of ['invalid-input-secret', 'missing-input-secret', 'internal-error', 'bad-request']) {
    const r = await verifyTurnstile({ secret: 'x', token: 't', ip: null, fetchFn: reply({ success: false, 'error-codes': [code] }) });
    assert.equal(r.outcome, 'unavailable', code);
  }
});

test('fail closed: HTTP errors, garbage and odd answers are unavailable', async () => {
  for (const [name, f] of [
    ['http 500', reply({ success: true }, 500)],
    ['http 403', reply({ success: true }, 403)],
    ['not an object', reply('yes')],
    ['null', reply(null)],
    ['success missing', reply({})],
    ['success is a string', reply({ success: 'true' })],
    ['success is 1', reply({ success: 1 })],
    ['json throws', async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('bad json'); } })],
  ]) {
    const r = await verifyTurnstile({ secret: 'x', token: 't', ip: null, fetchFn: f });
    assert.equal(r.outcome, 'unavailable', name);
    assert.notEqual(r.outcome, 'pass');
  }
});

test('fail closed: an unreachable siteverify is unavailable', async () => {
  const r = await verifyTurnstile({ secret: 'x', token: 't', ip: null, fetchFn: async () => { throw new TypeError('fetch failed'); } });
  assert.deepEqual(r, { outcome: 'unavailable', codes: ['network'] });
});

test('fail closed: a siteverify that never answers times out', async () => {
  const fetchFn = (url, init) => new Promise((_, reject) => init.signal.addEventListener('abort', () => reject(new Error('aborted'))));
  const t0 = Date.now();
  const r = await verifyTurnstile({ secret: 'x', token: 't', ip: null, fetchFn, timeoutMs: 30 });
  assert.deepEqual(r, { outcome: 'unavailable', codes: ['timeout'] });
  assert.ok(Date.now() - t0 < 1000);
});

test('error codes are passed on only when they look like Cloudflare codes (nothing else reaches a log)', async () => {
  const r = await verifyTurnstile({ secret: 'x', token: 't', ip: null, fetchFn: reply({ success: false, 'error-codes': ['invalid-input-response', 'Secret Leak 123', 5, 'x'.repeat(200)] }) });
  assert.deepEqual(r.codes, ['invalid-input-response']);
});
