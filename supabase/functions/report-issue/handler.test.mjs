import test from 'node:test';
import assert from 'node:assert/strict';
import { setup, fakeNet, fakeDb, appReport, webReport, asApp, fromIp, JWT, USER_ID, OTHER_USER_ID, SECRETS } from './harness.mjs';

const json = async (res) => JSON.parse(await res.text());
const errOf = async (res) => (await json(res)).error;

// ------------------------------------------------------------------ app reports

test('app report: stored for the VERIFIED user, answer is {id}, no Turnstile involved', async () => {
  const t = setup();
  const res = await t.call(appReport(), { headers: asApp });
  assert.equal(res.status, 200);
  const body = await json(res);
  assert.deepEqual(Object.keys(body), ['id']);
  assert.match(body.id, /^[0-9a-f-]{36}$/);
  assert.equal(t.db.rows.length, 1);
  assert.equal(t.db.rows[0].user_id, USER_ID);
  assert.equal(t.db.rows[0].source, 'app');
  assert.equal(t.db.rows[0].id, body.id);
  assert.equal(t.net.to('challenges.cloudflare.com').length, 0);
  const authCall = t.net.to('proj.supabase.co')[0];
  assert.equal(authCall.path, '/auth/v1/user');
  assert.equal(authCall.headers.apikey, SECRETS.SUPABASE_ANON_KEY);
  assert.equal(authCall.headers.Authorization, `Bearer ${JWT}`);
});

test('app report: a user id or status in the body is ignored', async () => {
  const t = setup();
  await t.call({ ...appReport(), user_id: OTHER_USER_ID, status: 'closed', id: 'mine' }, { headers: asApp });
  assert.equal(t.db.rows[0].user_id, USER_ID);
  assert.equal('status' in t.db.rows[0], false);
  assert.notEqual(t.db.rows[0].id, 'mine');
});

test('app report: no / malformed / rejected / unverifiable token', async () => {
  const t = setup();
  for (const headers of [{}, { authorization: 'Bearer' }, { authorization: 'Bearer not-a-jwt' }, { authorization: `Basic ${JWT}` }, { authorization: 'Bearer aaaaaaaa.bbbbbbbb.dddddddd' }]) {
    const res = await t.call(appReport(), { headers });
    assert.equal(res.status, 401, JSON.stringify(headers));
    assert.equal((await errOf(res)).code, 'unauthorized');
  }
  assert.equal(t.db.rows.length, 0);
  // the auth API itself is down: not a pass, not "unauthorized"
  const down = setup({ net: fakeNet({ auth: () => 'throw' }) });
  const res = await down.call(appReport(), { headers: asApp });
  assert.equal(res.status, 500);
  assert.equal((await errOf(res)).code, 'server_error');
  assert.equal(down.db.rows.length, 0);
});

test('app report: the account cap (10 a day) answers 429 rate_limited', async () => {
  const t = setup();
  for (let i = 0; i < 10; i++) assert.equal((await t.call(appReport({ title: `Report ${i} here` }), { headers: asApp })).status, 200);
  const res = await t.call(appReport(), { headers: asApp });
  assert.equal(res.status, 429);
  assert.equal((await errOf(res)).code, 'rate_limited');
  assert.ok(res.headers.get('retry-after'));
  assert.equal(t.db.rows.length, 10);
});

test('app report: the global safety net answers 429 too', async () => {
  const t = setup();
  t.db.limits.set('global:app|day', 1000);
  const res = await t.call(appReport(), { headers: asApp });
  assert.equal(res.status, 429);
  assert.equal((await errOf(res)).code, 'rate_limited');
});

// ------------------------------------------------------------------ web reports

test('web report: Turnstile is verified server-side with the secret, the token and the client address', async () => {
  const t = setup();
  const res = await t.call(webReport(), { headers: fromIp('198.51.100.77') });
  assert.equal(res.status, 200);
  const c = t.net.to('challenges.cloudflare.com');
  assert.equal(c.length, 1);
  const form = new URLSearchParams(c[0].body);
  assert.equal(form.get('secret'), SECRETS.TURNSTILE_SECRET_KEY);
  assert.equal(form.get('response'), 'good-token');
  assert.equal(form.get('remoteip'), '198.51.100.77');
  assert.equal(t.db.rows.length, 1);
  assert.equal(t.db.rows[0].user_id, null);
  assert.equal(t.db.rows[0].source, 'web');
  assert.equal('turnstile_token' in t.db.rows[0], false);
});

test('web report: a user id in the body is ignored (a web report is never linked to an account)', async () => {
  const t = setup();
  await t.call({ ...webReport(), user_id: USER_ID, source_user: USER_ID }, { headers: { ...fromIp('198.51.100.3'), ...asApp } });
  assert.equal(t.db.rows[0].user_id, null);
  assert.equal(t.net.to('proj.supabase.co').length, 0, 'a web report never asks the auth API');
});

test('web report: the per-address key is a salted hash, not the address', async () => {
  const t = setup();
  await t.call(webReport(), { headers: fromIp('198.51.100.77') });
  const limit = t.db.calls.find((c) => c[0] === 'takeLimit' && c[1].startsWith('ip:'));
  assert.match(limit[1], /^ip:[0-9a-f]{64}$/);
  assert.equal(JSON.stringify(t.db.calls).includes('198.51.100.77'), false);
  assert.equal(limit[2], '2026-10-24T10:00:00.000Z'); // hourly bucket
  assert.equal(limit[3], 5);
  const g = t.db.calls.find((c) => c[0] === 'takeLimit' && c[1] === 'global:web');
  assert.equal(g[2], '2026-10-24T00:00:00.000Z');
  // another salt, another key
  const t2 = setup({ env: { REPORT_IP_SALT: 'other-salt' } });
  await t2.call(webReport(), { headers: fromIp('198.51.100.77') });
  assert.notEqual(t2.db.calls.find((c) => c[1]?.startsWith?.('ip:'))[1], limit[1]);
});

test('web report: no token, a refused token', async () => {
  const t = setup();
  let res = await t.call(webReport({ turnstile_token: undefined }));
  assert.equal(res.status, 400);
  assert.equal((await errOf(res)).code, 'turnstile_failed');
  res = await t.call(webReport({ turnstile_token: 'bad-token' }));
  assert.equal(res.status, 400);
  const e = await errOf(res);
  assert.equal(e.code, 'turnstile_failed');
  assert.equal(e.field, 'turnstile_token');
  assert.equal(t.db.rows.length, 0);
  assert.equal(t.db.calls.length, 0, 'no limit is consumed and nothing is stored on a failed check');
});

test('web report: fail closed when Turnstile is unreachable, slow or answers nonsense', async () => {
  for (const [name, token, net] of [
    ['network error', 'down', fakeNet()],
    ['timeout', 'hang', fakeNet()],
    ['http 500', 'good-token', fakeNet({ turnstile: () => ({ ok: false, status: 500, json: async () => ({}) }) })],
    ['garbage', 'good-token', fakeNet({ turnstile: () => ({ ok: true, status: 200, json: async () => 'success' }) })],
    ['bad secret', 'good-token', fakeNet({ turnstile: () => ({ ok: true, status: 200, json: async () => ({ success: false, 'error-codes': ['invalid-input-secret'] }) }) })],
  ]) {
    const t = setup({ net, timeoutMs: 40 });
    const res = await t.call(webReport({ turnstile_token: token }));
    assert.equal(res.status, 500, name);
    assert.equal((await errOf(res)).code, 'server_error', name);
    assert.equal(t.db.rows.length, 0, name);
  }
});

test('web report: honeypot gets the normal success answer and NOTHING happens', async () => {
  const t = setup();
  const res = await t.call({ ...webReport(), website: 'http://spam.example' }, { headers: fromIp('198.51.100.1') });
  assert.equal(res.status, 200);
  assert.deepEqual(await json(res), { id: 'ffffffff-ffff-4fff-8fff-ffffffffffff' });
  assert.equal(t.db.rows.length, 0);
  assert.equal(t.db.calls.length, 0);
  assert.equal(t.net.calls.length, 0, 'no Turnstile, no e-mail');
  // empty / absent honeypot is a normal report
  assert.equal((await t.call({ ...webReport(), website: '' })).status, 200);
  assert.equal(t.db.rows.length, 1);
  // the honeypot answer looks like a real one: same status, same keys, same headers
  const real = await t.call(webReport());
  const fake = await t.call({ ...webReport(), website: 'x' });
  assert.equal(fake.status, real.status);
  assert.deepEqual(Object.keys(await json(fake)), Object.keys(await json(real)));
  assert.equal(fake.headers.get('content-type'), real.headers.get('content-type'));
});

test('web report: 5 per address per hour, then 429 with Retry-After', async () => {
  const t = setup();
  for (let i = 0; i < 5; i++) assert.equal((await t.call(webReport({ title: `Question ${i} here` }), { headers: fromIp('198.51.100.5') })).status, 200, `report ${i + 1}`);
  const res = await t.call(webReport(), { headers: fromIp('198.51.100.5') });
  assert.equal(res.status, 429);
  assert.equal((await errOf(res)).code, 'rate_limited');
  assert.equal(res.headers.get('retry-after'), '1800'); // clock is 10:30
  assert.equal(t.db.rows.length, 5);
  // another address is fine
  assert.equal((await t.call(webReport(), { headers: fromIp('198.51.100.6') })).status, 200);
});

test('web report: addresses of the same IPv6 /64 share one allowance; a forwarded-for header does not override cf-connecting-ip', async () => {
  const t = setup();
  for (let i = 0; i < 5; i++) await t.call(webReport(), { headers: fromIp(`2001:db8:1:2:${i}::${i + 1}`) });
  assert.equal((await t.call(webReport(), { headers: fromIp('2001:db8:1:2:ffff::9') })).status, 429);
  const t2 = setup();
  for (let i = 0; i < 5; i++) await t2.call(webReport(), { headers: { 'cf-connecting-ip': '198.51.100.9', 'x-forwarded-for': `10.0.0.${i}` } });
  assert.equal((await t2.call(webReport(), { headers: { 'cf-connecting-ip': '198.51.100.9', 'x-forwarded-for': '10.9.9.9' } })).status, 429);
});

test('web report: the global daily cap is a safety net over all addresses', async () => {
  const t = setup();
  t.db.limits.set('global:web|2026-10-24T00:00:00.000Z', 199);
  assert.equal((await t.call(webReport(), { headers: fromIp('198.51.100.20') })).status, 200);
  const res = await t.call(webReport(), { headers: fromIp('198.51.100.21') });
  assert.equal(res.status, 429);
  assert.equal((await errOf(res)).code, 'rate_limited');
  assert.ok(Number(res.headers.get('retry-after')) > 0);
});

test('web report: unknown client address still works, in one shared bucket', async () => {
  const t = setup();
  for (let i = 0; i < 5; i++) assert.equal((await t.call(webReport())).status, 200);
  assert.equal((await t.call(webReport())).status, 429);
});

test('missing configuration is a server_error that names no secret', async () => {
  for (const missing of ['TURNSTILE_SECRET_KEY', 'REPORT_IP_SALT']) {
    const t = setup({ env: { [missing]: undefined } });
    const res = await t.call(webReport());
    assert.equal(res.status, 500);
    const text = await res.text();
    assert.equal(text.includes(missing), false);
    assert.equal(t.db.rows.length, 0);
  }
  const t = setup({ env: { SUPABASE_ANON_KEY: undefined } });
  assert.equal((await t.call(appReport(), { headers: asApp })).status, 500);
});

// ------------------------------------------------------------------ request shape and validation

test('invalid input: 400 with code, a short sentence and the field', async () => {
  const t = setup();
  for (const [body, field] of [
    [appReport({ title: 'ab' }), 'title'],
    [appReport({ description: 'short' }), 'description'],
    [appReport({ kind: 'rant' }), 'kind'],
    [appReport({ source: 'cli' }), 'source'],
    [appReport({ contact_email: 'nope' }), 'contact_email'],
    [appReport({ diagnostics: 'x'.repeat(20001) }), 'diagnostics'],
    [appReport({ title: 'NUL\u0000' }), 'title'],
  ]) {
    const res = await t.call(body, { headers: asApp });
    assert.equal(res.status, 400, field);
    const e = await errOf(res);
    assert.equal(e.code, 'invalid_input');
    assert.equal(e.field, field);
    assert.ok(e.message.length > 5 && e.message.length < 120);
  }
  assert.equal(t.db.rows.length, 0);
  assert.equal(t.net.calls.length, 0, 'validation happens before any network call');
});

test('invalid requests: wrong method, wrong content type, not JSON, not an object, too large', async () => {
  const t = setup();
  let res = await t.call(null, { method: 'GET' });
  assert.equal(res.status, 405);
  assert.equal(res.headers.get('allow'), 'POST, OPTIONS');
  res = await t.call(null, { headers: { 'content-type': 'text/plain' }, raw: JSON.stringify(appReport()) });
  assert.equal(res.status, 400);
  res = await t.call(null, { raw: '{not json' });
  assert.equal(res.status, 400);
  assert.equal((await errOf(res)).code, 'invalid_input');
  res = await t.call(null, { raw: '[1,2]', headers: asApp });
  assert.equal(res.status, 400);
  res = await t.call(null, { raw: 'null', headers: asApp });
  assert.equal(res.status, 400);
  res = await t.call(null, { raw: JSON.stringify(appReport({ diagnostics: 'x'.repeat(300000) })), headers: asApp });
  assert.equal(res.status, 400);
  assert.equal(t.db.rows.length, 0);
});

test('stored text is the cleaned text', async () => {
  const t = setup();
  await t.call(appReport({ title: 'Crash\r\nBcc: x@y.co\u0007', description: 'Line one\r\nLine two\u0000'.replace('\u0000', '') + '‮    ' + ' '.repeat(40) + 'end here' }), { headers: asApp });
  const row = t.db.rows[0];
  assert.equal(row.title, 'Crash Bcc: x@y.co');
  assert.equal(row.description, 'Line one\nLine two    end here');
});

test('every error answer has the documented shape and never a stack trace', async () => {
  const t = setup();
  const responses = [
    await t.call(null, { raw: '{' }),
    await t.call(appReport()),
    await t.call(webReport({ turnstile_token: 'bad-token' })),
    await t.call(appReport({ title: 'x' }), { headers: asApp }),
  ];
  const expected = { 400: ['invalid_input', 'turnstile_failed'], 401: ['unauthorized'] };
  for (const r of responses) {
    const text = await r.text();
    const body = JSON.parse(text);
    assert.deepEqual(Object.keys(body), ['error']);
    assert.ok(expected[r.status].includes(body.error.code), `${r.status} ${body.error.code}`);
    assert.equal(typeof body.error.message, 'string');
    assert.equal(/\n\s+at |node:|file:\/\/|\.ts:\d/.test(text), false);
    assert.match(r.headers.get('content-type'), /^application\/json/);
    assert.equal(r.headers.get('cache-control'), 'no-store');
  }
});

// ------------------------------------------------------------------ CORS

test('CORS: preflight from the website, from the desktop app, from a stranger', async () => {
  const t = setup();
  let res = await t.call(null, { method: 'OPTIONS', headers: { origin: 'https://radarx.labshope.com', 'access-control-request-method': 'POST' } });
  assert.equal(res.status, 204);
  assert.equal(res.headers.get('access-control-allow-origin'), 'https://radarx.labshope.com');
  assert.equal(res.headers.get('access-control-allow-headers'), 'Authorization, Content-Type, apikey');
  assert.equal(res.headers.get('access-control-allow-credentials'), null);
  for (const o of ['tauri://localhost', 'http://tauri.localhost', 'https://tauri.localhost']) {
    res = await t.call(null, { method: 'OPTIONS', headers: { origin: o } });
    assert.equal(res.status, 204, o);
    assert.equal(res.headers.get('access-control-allow-origin'), o);
  }
  res = await t.call(null, { method: 'OPTIONS', headers: { origin: 'https://evil.example' } });
  assert.equal(res.status, 403);
  assert.equal(res.headers.get('access-control-allow-origin'), null);
  res = await t.call(null, { method: 'OPTIONS' });
  assert.equal(res.status, 204);
  assert.equal(res.headers.get('access-control-allow-origin'), null);
});

test('CORS: answers carry the headers for an allowed origin (errors too), none for others, never "*"', async () => {
  const t = setup();
  let res = await t.call(webReport(), { headers: { origin: 'https://radarx.labshope.com' } });
  assert.equal(res.headers.get('access-control-allow-origin'), 'https://radarx.labshope.com');
  res = await t.call(webReport({ title: 'x' }), { headers: { origin: 'https://radarx.labshope.com' } });
  assert.equal(res.status, 400);
  assert.equal(res.headers.get('access-control-allow-origin'), 'https://radarx.labshope.com');
  res = await t.call(webReport(), { headers: { origin: 'https://evil.example' } });
  assert.equal(res.headers.get('access-control-allow-origin'), null);
  assert.equal(res.headers.get('vary'), 'Origin');
  // the app from Tauri: allowed; a "web" report claiming a Tauri origin: not
  res = await t.call(appReport(), { headers: { ...asApp, origin: 'tauri://localhost' } });
  assert.equal(res.headers.get('access-control-allow-origin'), 'tauri://localhost');
  res = await t.call(webReport(), { headers: { origin: 'tauri://localhost' } });
  assert.equal(res.headers.get('access-control-allow-origin'), null);
  // never a wildcard
  const star = setup({ env: { REPORT_ALLOWED_ORIGINS: '*' } });
  res = await star.call(webReport(), { headers: { origin: 'https://evil.example' } });
  assert.equal(res.headers.get('access-control-allow-origin'), null);
});

test('CORS: REPORT_ALLOWED_ORIGINS and the localhost switch', async () => {
  const t = setup({ env: { REPORT_ALLOWED_ORIGINS: 'https://one.example,https://two.example' } });
  let res = await t.call(webReport(), { headers: { origin: 'https://two.example' } });
  assert.equal(res.headers.get('access-control-allow-origin'), 'https://two.example');
  res = await t.call(webReport(), { headers: { origin: 'https://radarx.labshope.com' } });
  assert.equal(res.headers.get('access-control-allow-origin'), null);
  res = await t.call(webReport(), { headers: { origin: 'http://localhost:5173' } });
  assert.equal(res.headers.get('access-control-allow-origin'), null);
  const dev = setup({ env: { REPORT_ALLOW_LOCALHOST: '1' } });
  res = await dev.call(webReport(), { headers: { origin: 'http://localhost:5173' } });
  assert.equal(res.headers.get('access-control-allow-origin'), 'http://localhost:5173');
  res = await dev.call(null, { method: 'OPTIONS', headers: { origin: 'http://127.0.0.1:3000' } });
  assert.equal(res.status, 204);
});

// ------------------------------------------------------------------ store first, notify after

test('the owner is notified by e-mail after the report is stored', async () => {
  const t = setup();
  const res = await t.call(webReport({ contact_email: 'visitor@example.org' }));
  assert.equal(res.status, 200);
  assert.equal(t.net.to('api.resend.com').length, 1);
  const mail = JSON.parse(t.net.to('api.resend.com')[0].body);
  assert.deepEqual(mail.to, ['owner@example.org']);
  assert.equal(mail.reply_to, 'visitor@example.org');
  assert.ok(mail.subject.startsWith('[radarx report] '));
  const n = t.logs.find((l) => l.event === 'notify');
  assert.equal(n.sent, true);
  assert.equal(n.provider, 'resend');
});

test('Resend down: Mailjet sends the notification', async () => {
  const t = setup({ net: fakeNet({ resend: () => ({ ok: false, status: 503, json: async () => ({}) }) }) });
  assert.equal((await t.call(webReport())).status, 200);
  assert.equal(t.net.to('api.mailjet.com').length, 1);
  assert.equal(t.logs.find((l) => l.event === 'notify').provider, 'mailjet');
});

test('every mail provider down, or no mail configured: the report is still stored and the answer is a success', async () => {
  const dead = setup({ net: fakeNet({ resend: () => 'throw', mailjet: () => ({ ok: false, status: 500, json: async () => ({}) }) }) });
  let res = await dead.call(webReport());
  assert.equal(res.status, 200);
  assert.equal(dead.db.rows.length, 1);
  assert.equal(dead.logs.find((l) => l.event === 'notify').sent, false);
  const none = setup({ env: { REPORT_NOTIFY_TO: undefined, RESEND_API_KEY: undefined, MAILJET_API_KEY: undefined } });
  res = await none.call(webReport());
  assert.equal(res.status, 200);
  assert.equal(none.db.rows.length, 1);
  const slow = setup({ net: fakeNet({ resend: () => 'hang', mailjet: () => 'hang' }), timeoutMs: 30 });
  res = await slow.call(webReport());
  assert.equal(res.status, 200);
  assert.equal(slow.db.rows.length, 1);
});

test('the notification runs after the response when the platform can defer it', async () => {
  const pending = [];
  let release;
  const gate = new Promise((r) => (release = r));
  const net = fakeNet({ resend: () => gate.then(() => ({ ok: true, status: 200, json: async () => ({}) })) });
  const t = setup({ net, defer: (p) => pending.push(p), timeoutMs: 5000 });
  const res = await t.call(webReport());
  assert.equal(res.status, 200, 'the answer does not wait for the mail');
  assert.equal(pending.length, 1);
  assert.equal(t.logs.some((l) => l.event === 'notify'), false);
  release();
  await Promise.all(pending);
  assert.equal(t.logs.find((l) => l.event === 'notify').sent, true);
});

test('if storing fails: server_error, and NO notification is sent', async () => {
  const t = setup();
  t.db.failNext = 'insert';
  const res = await t.call(webReport());
  assert.equal(res.status, 500);
  assert.equal((await errOf(res)).code, 'server_error');
  assert.equal(t.net.to('api.resend.com').length, 0);
  assert.equal(t.net.to('api.mailjet.com').length, 0);
  const t2 = setup();
  t2.db.failNext = 'limits';
  assert.equal((await t2.call(webReport())).status, 500);
  assert.equal(t2.db.rows.length, 0);
});

// ------------------------------------------------------------------ optional GitHub issue

test('GitHub is off unless REPORT_GITHUB_ISSUES=1 and both secrets are set', async () => {
  const gh = { GITHUB_REPORT_TOKEN: 'ghp_SENTINEL', GITHUB_REPORT_REPO: 'owner/reports' };
  let t = setup({ env: gh });
  await t.call(webReport());
  assert.equal(t.net.to('api.github.com').length, 0);
  t = setup({ env: { ...gh, REPORT_GITHUB_ISSUES: '1', GITHUB_REPORT_TOKEN: undefined } });
  assert.equal((await t.call(webReport())).status, 200);
  assert.equal(t.net.to('api.github.com').length, 0);
  assert.equal(t.logs.find((l) => l.event === 'github').ok, false);
});

test('GitHub on: a private repo gets the issue, without the contact address', async () => {
  const t = setup({ env: { GITHUB_REPORT_TOKEN: 'ghp_SENTINEL', GITHUB_REPORT_REPO: 'owner/reports', REPORT_GITHUB_ISSUES: '1' } });
  const res = await t.call(webReport({ contact_email: 'visitor@example.org', diagnostics: 'DIAG_LINE' }));
  assert.equal(res.status, 200);
  const calls = t.net.to('api.github.com');
  assert.deepEqual(calls.map((c) => c.method), ['GET', 'POST']);
  assert.equal(calls[1].body.includes('visitor@example.org'), false);
  assert.equal(calls[1].body.includes('DIAG_LINE'), false);
  assert.equal(t.logs.find((l) => l.event === 'github').number, 7);
});

test('GitHub on, repo is public or GitHub fails: nothing is sent / the report is unaffected', async () => {
  const env = { GITHUB_REPORT_TOKEN: 'ghp_SENTINEL', GITHUB_REPORT_REPO: 'owner/reports', REPORT_GITHUB_ISSUES: '1' };
  const pub = setup({ env, net: fakeNet({ github: () => ({ ok: true, status: 200, json: async () => ({ private: false }) }) }) });
  assert.equal((await pub.call(webReport())).status, 200);
  assert.deepEqual(pub.net.to('api.github.com').map((c) => c.method), ['GET']);
  assert.equal(pub.logs.find((l) => l.event === 'github').ok, false);
  const down = setup({ env, net: fakeNet({ github: () => 'throw' }) });
  assert.equal((await down.call(webReport())).status, 200);
  assert.equal(down.db.rows.length, 1);
});

// ------------------------------------------------------------------ nothing leaks into logs or answers

test('no log line, answer or error contains the text, the address, the token, an IP or a secret', async () => {
  const SENT = {
    desc: 'SENTINEL_DESC_ALPHA the description text',
    title: 'SENTINEL_TITLE_BETA',
    mail: 'sentinel.person@example.net',
    token: 'TURNSTILE_TOKEN_SENTINEL',
    ip: '198.51.100.77',
    diag: 'SENTINEL_DIAG_GAMMA',
  };
  const env = {
    GITHUB_REPORT_TOKEN: 'GH_TOKEN_SENTINEL', GITHUB_REPORT_REPO: 'owner/reports', REPORT_GITHUB_ISSUES: '1',
  };
  const secretValues = [...Object.values(SECRETS).filter((v) => /SENTINEL/.test(v)), 'GH_TOKEN_SENTINEL'];
  const userJwt = 'zzzzzzzz.yyyyyyyy.xxxxxxxx';
  const scenarios = [];
  const mk = (label, opts, build, headers = {}) => scenarios.push({ label, opts, build, headers });
  const web = (over = {}) => webReport({ title: SENT.title, description: SENT.desc, contact_email: SENT.mail, diagnostics: SENT.diag, turnstile_token: SENT.token, ...over });
  const app = (over = {}) => appReport({ title: SENT.title, description: SENT.desc, contact_email: SENT.mail, diagnostics: SENT.diag, ...over });
  const ipH = { 'cf-connecting-ip': SENT.ip, 'x-forwarded-for': SENT.ip };
  const turnstileGood = (form) => (form.get('response') === SENT.token ? { ok: true, status: 200, json: async () => ({ success: true }) } : { ok: true, status: 200, json: async () => ({ success: false, 'error-codes': ['invalid-input-response'] }) });
  const goodNet = (extra = {}) => fakeNet({ turnstile: turnstileGood, auth: (tok) => (tok === userJwt ? { ok: true, status: 200, json: async () => ({ id: USER_ID }) } : { ok: false, status: 401, json: async () => ({}) }), ...extra });

  mk('web ok', { env, net: goodNet() }, web, ipH);
  mk('web ok, mail down, github down', { env, net: goodNet({ resend: () => 'throw', mailjet: () => 'throw', github: () => 'throw' }) }, web, ipH);
  mk('web turnstile refused', { env, net: goodNet({ turnstile: () => ({ ok: true, status: 200, json: async () => ({ success: false, 'error-codes': ['invalid-input-response'] }) }) }) }, web, ipH);
  mk('web turnstile down', { env, net: goodNet({ turnstile: () => 'throw' }) }, web, ipH);
  mk('web db insert error with the description inside the error', { env, net: goodNet() }, web, ipH);
  mk('web honeypot', { env, net: goodNet() }, (o) => web({ website: SENT.token, ...o }), ipH);
  mk('web invalid', { env, net: goodNet() }, (o) => web({ title: 'x', ...o }), ipH);
  mk('web rate limited', { env, net: goodNet() }, web, ipH);
  mk('app ok', { env, net: goodNet() }, app, { authorization: `Bearer ${userJwt}` });
  mk('app bad token', { env, net: goodNet() }, app, { authorization: 'Bearer qqqqqqqq.rrrrrrrr.ssssssss' });
  mk('app auth down', { env, net: goodNet({ auth: () => 'throw' }) }, app, { authorization: `Bearer ${userJwt}` });

  for (const s of scenarios) {
    const db = fakeDb();
    if (s.label.includes('db insert error')) db.failNext = 'insert';
    if (s.label === 'web rate limited') db.limits.set(`ip:x|y`, 0);
    const t = setup({ ...s.opts, db });
    if (s.label === 'web rate limited') {
      // fill the allowance for this address first
      for (let i = 0; i < 5; i++) await t.call(s.build(), { headers: s.headers });
    }
    const res = await t.call(s.build(), { headers: s.headers });
    const everything = JSON.stringify(t.logs) + (await res.text()) + JSON.stringify([...res.headers]);
    for (const [name, v] of Object.entries(SENT)) {
      // the description may legitimately never appear; the address and token especially must not
      assert.equal(everything.includes(v), false, `${s.label}: ${name} leaked`);
    }
    for (const v of secretValues) assert.equal(everything.includes(v), false, `${s.label}: a secret leaked`);
    assert.equal(everything.includes(userJwt), false, `${s.label}: the user token leaked`);
    assert.equal(everything.includes('owner@example.org'), false, `${s.label}: the owner address leaked`);
    assert.equal(/\n\s+at |stack/i.test(everything), false, `${s.label}: stack trace`);
  }
});

test('logs hold only event names, outcomes and codes', async () => {
  const t = setup();
  await t.call(webReport(), { headers: fromIp('198.51.100.1') });
  await t.call(webReport({ turnstile_token: 'bad-token' }));
  const allowedKeys = new Set(['event', 'outcome', 'code', 'source', 'kind', 'why', 'ms', 'sent', 'provider', 'attempts', 'reason', 'ok', 'number', 'codes']);
  for (const line of t.logs) for (const k of Object.keys(line)) assert.ok(allowedKeys.has(k), `unexpected log key ${k}`);
  assert.ok(t.logs.some((l) => l.event === 'report' && l.outcome === 'stored'));
  assert.ok(t.logs.some((l) => l.event === 'report' && l.code === 'turnstile_failed'));
});
