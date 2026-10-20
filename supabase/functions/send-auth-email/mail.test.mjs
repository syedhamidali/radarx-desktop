import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildMessage, parseFrom, providersFromEnv, sendWithFailover, resendProvider, mailjetProvider, MailError,
} from './mail.ts';

const payload = (type = 'signup', token = '123456', email = 'ada@example.test') => ({
  user: { email }, email_data: { token, email_action_type: type },
});
const FROM = 'Radarx <no-reply@labshope.com>';
const KEY_R = 're_SECRETKEY_1234567890';
const KEY_M = 'mjkey_ABCDEF';
const SEC_M = 'mjsecret_UVWXYZ';

/** A fake fetch: `plan` maps a host to a handler; records every call. */
function fakeFetch(plan, calls = []) {
  const f = async (url, init) => {
    calls.push({ url, init });
    const host = new URL(url).host;
    return plan[host](init);
  };
  f.calls = calls;
  return f;
}
const ok = (json = {}) => ({ ok: true, status: 200, json: async () => json });
const fail = (status) => ({ ok: false, status, json: async () => ({}) });

test('the message carries the code, for the kinds the app uses', () => {
  for (const kind of ['signup', 'recovery', 'magiclink', 'reauthentication']) {
    const m = buildMessage(payload(kind));
    assert.equal(m.to, 'ada@example.test');
    assert.match(m.text, /123456/);
    assert.match(m.html, /123456/);
    assert.ok(m.subject.length > 3);
  }
  assert.match(buildMessage(payload('recovery')).subject, /Reset/);
});

test('unsupported kinds, bad codes and bad recipients are refused, not guessed', () => {
  assert.throws(() => buildMessage(payload('email_change')), /unsupported/);
  assert.throws(() => buildMessage(payload('invite')), MailError);
  assert.throws(() => buildMessage(payload('signup', '12ab56')), /invalid code/);
  assert.throws(() => buildMessage(payload('signup', '<script>')), /invalid code/);
  assert.throws(() => buildMessage(payload('signup', '123456', 'not-an-address')), /invalid recipient/);
  assert.throws(() => buildMessage(payload('signup', '123456', 'a@b.co"><img>')), /invalid recipient/);
  assert.throws(() => buildMessage({}), MailError);
});

test('the sender is parsed from "Name <addr>" and from a bare address', () => {
  assert.deepEqual(parseFrom('Radarx <no-reply@labshope.com>'), { name: 'Radarx', email: 'no-reply@labshope.com' });
  assert.deepEqual(parseFrom('no-reply@labshope.com'), { name: '', email: 'no-reply@labshope.com' });
  assert.throws(() => parseFrom('nonsense'), /MAIL_FROM/);
});

test('the environment decides the providers and their order', () => {
  const f = fakeFetch({});
  const env = { MAIL_FROM: FROM, RESEND_API_KEY: KEY_R, MAILJET_API_KEY: KEY_M, MAILJET_SECRET_KEY: SEC_M };
  assert.deepEqual(providersFromEnv(env, f).map((p) => p.name), ['resend', 'mailjet']);
  assert.deepEqual(providersFromEnv({ ...env, MAIL_ORDER: 'mailjet,resend' }, f).map((p) => p.name), ['mailjet', 'resend']);
  assert.deepEqual(providersFromEnv({ ...env, MAILJET_API_KEY: undefined }, f).map((p) => p.name), ['resend']);
  assert.throws(() => providersFromEnv({ ...env, RESEND_API_KEY: undefined, MAILJET_API_KEY: undefined }, f), /no mail provider/);
  assert.throws(() => providersFromEnv({ ...env, MAIL_FROM: undefined }, f), /MAIL_FROM is not set/);
  assert.throws(() => providersFromEnv({ ...env, MAIL_FROM: 'garbage' }, f), /MAIL_FROM/);
});

test('Resend is used first and Mailjet is not called when it works', async () => {
  const f = fakeFetch({ 'api.resend.com': () => ok(), 'api.mailjet.com': () => ok({ Messages: [{ Status: 'success' }] }) });
  const env = { MAIL_FROM: FROM, RESEND_API_KEY: KEY_R, MAILJET_API_KEY: KEY_M, MAILJET_SECRET_KEY: SEC_M };
  const done = await sendWithFailover(providersFromEnv(env, f), buildMessage(payload()));
  assert.equal(done.provider, 'resend');
  assert.equal(f.calls.length, 1);
  const sent = JSON.parse(f.calls[0].init.body);
  assert.deepEqual(sent.to, ['ada@example.test']);
  assert.equal(sent.from, FROM);
  assert.equal(f.calls[0].init.headers.Authorization, `Bearer ${KEY_R}`);
});

test('a refusing Resend falls back to Mailjet, with the right request', async () => {
  const f = fakeFetch({ 'api.resend.com': () => fail(503), 'api.mailjet.com': () => ok({ Messages: [{ Status: 'success' }] }) });
  const env = { MAIL_FROM: FROM, RESEND_API_KEY: KEY_R, MAILJET_API_KEY: KEY_M, MAILJET_SECRET_KEY: SEC_M };
  const done = await sendWithFailover(providersFromEnv(env, f), buildMessage(payload()));
  assert.equal(done.provider, 'mailjet');
  assert.deepEqual(done.attempts.map((a) => [a.provider, a.ok, a.reason]), [['resend', false, 'refused'], ['mailjet', true, undefined]]);
  const call = f.calls[1];
  assert.equal(call.init.headers.Authorization, `Basic ${btoa(`${KEY_M}:${SEC_M}`)}`);
  const msg = JSON.parse(call.init.body).Messages[0];
  assert.deepEqual(msg.From, { Email: 'no-reply@labshope.com', Name: 'Radarx' });
  assert.deepEqual(msg.To, [{ Email: 'ada@example.test' }]);
  assert.match(msg.TextPart, /123456/);
});

test('a network error and a timeout also fall back', async () => {
  const boom = { 'api.resend.com': () => { throw new Error('ECONNRESET'); }, 'api.mailjet.com': () => ok({ Messages: [{ Status: 'success' }] }) };
  const env = { MAIL_FROM: FROM, RESEND_API_KEY: KEY_R, MAILJET_API_KEY: KEY_M, MAILJET_SECRET_KEY: SEC_M };
  let f = fakeFetch(boom);
  let done = await sendWithFailover(providersFromEnv(env, f), buildMessage(payload()));
  assert.equal(done.provider, 'mailjet');
  assert.equal(done.attempts[0].reason, 'network');

  // a provider that never answers is cut off by the time limit and the next one is tried
  const hang = (init) => new Promise((_, reject) => init.signal.addEventListener('abort', () => reject(new Error('aborted'))));
  f = fakeFetch({ 'api.resend.com': hang, 'api.mailjet.com': () => ok({ Messages: [{ Status: 'success' }] }) });
  const t0 = Date.now();
  done = await sendWithFailover(providersFromEnv(env, f), buildMessage(payload()), { timeoutMs: 80 });
  assert.equal(done.provider, 'mailjet');
  assert.equal(done.attempts[0].reason, 'timeout');
  assert.ok(Date.now() - t0 < 2000);
});

test('Mailjet answering 200 with a rejected message is a failure, not a success', async () => {
  const f = fakeFetch({ 'api.mailjet.com': () => ok({ Messages: [{ Status: 'error' }] }) });
  const p = mailjetProvider(KEY_M, SEC_M, FROM, f);
  await assert.rejects(sendWithFailover([p], buildMessage(payload())), (e) => e instanceof MailError && e.attempts[0].reason === 'refused');
});

test('when every provider fails the error lists attempts and leaks nothing', async () => {
  const f = fakeFetch({ 'api.resend.com': () => fail(401), 'api.mailjet.com': () => fail(500) });
  const env = { MAIL_FROM: FROM, RESEND_API_KEY: KEY_R, MAILJET_API_KEY: KEY_M, MAILJET_SECRET_KEY: SEC_M };
  await assert.rejects(
    sendWithFailover(providersFromEnv(env, f), buildMessage(payload('signup', '987654', 'secret.person@example.test'))),
    (e) => {
      assert.ok(e instanceof MailError);
      assert.equal(e.attempts.length, 2);
      const all = JSON.stringify({ m: e.message, a: e.attempts, s: String(e) });
      for (const leak of [KEY_R, KEY_M, SEC_M, btoa(`${KEY_M}:${SEC_M}`), '987654', 'secret.person@example.test']) {
        assert.ok(!all.includes(leak), `leaked: ${leak.slice(0, 6)}...`);
      }
      return true;
    },
  );
});

test('a Resend-only deployment (no Mailjet keys) still works and fails cleanly', async () => {
  const f = fakeFetch({ 'api.resend.com': () => fail(500) });
  const providers = providersFromEnv({ MAIL_FROM: FROM, RESEND_API_KEY: KEY_R }, f);
  assert.equal(providers.length, 1);
  await assert.rejects(sendWithFailover(providers, buildMessage(payload())), /every mail provider failed/);
});
