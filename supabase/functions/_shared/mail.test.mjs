import test from 'node:test';
import assert from 'node:assert/strict';
import { resendProvider, mailjetProvider, sendWithFailover, providersFromEnv, EMAIL } from './mail.ts';
import * as viaAuth from '../send-auth-email/mail.ts';

const ok = (json = {}) => ({ ok: true, status: 200, json: async () => json });
const msg = { to: 'owner@example.org', subject: 'S', html: '<p>h</p>', text: 't' };

test('the sign-up function still gets every export from its own mail.ts', () => {
  for (const name of ['buildMessage', 'parseFrom', 'providersFromEnv', 'sendWithFailover', 'resendProvider', 'mailjetProvider', 'MailError']) {
    assert.equal(typeof viaAuth[name], 'function', name);
  }
});

test('Resend gets reply_to only when the message has one', async () => {
  const bodies = [];
  const f = async (url, init) => (bodies.push(JSON.parse(init.body)), ok());
  const p = resendProvider('k', 'Radarx <no-reply@radarx.labshope.com>', f);
  await p.send(msg, new AbortController().signal);
  await p.send({ ...msg, replyTo: 'ada@example.org' }, new AbortController().signal);
  assert.equal('reply_to' in bodies[0], false);
  assert.equal(bodies[1].reply_to, 'ada@example.org');
});

test('Mailjet gets ReplyTo only when the message has one', async () => {
  const bodies = [];
  const f = async (url, init) => (bodies.push(JSON.parse(init.body)), ok({ Messages: [{ Status: 'success' }] }));
  const p = mailjetProvider('k', 's', 'no-reply@radarx.labshope.com', f);
  await p.send(msg, new AbortController().signal);
  await p.send({ ...msg, replyTo: 'ada@example.org' }, new AbortController().signal);
  assert.equal('ReplyTo' in bodies[0].Messages[0], false);
  assert.deepEqual(bodies[1].Messages[0].ReplyTo, { Email: 'ada@example.org' });
});

test('failover is unchanged: Resend refuses, Mailjet sends', async () => {
  const f = async (url) => (url.includes('resend') ? { ok: false, status: 429, json: async () => ({}) } : ok({ Messages: [{ Status: 'success' }] }));
  const providers = providersFromEnv({ MAIL_FROM: 'no-reply@radarx.labshope.com', RESEND_API_KEY: 'r', MAILJET_API_KEY: 'a', MAILJET_SECRET_KEY: 'b' }, f);
  const r = await sendWithFailover(providers, msg);
  assert.equal(r.provider, 'mailjet');
  assert.equal(EMAIL.test('a@b.co'), true);
});
