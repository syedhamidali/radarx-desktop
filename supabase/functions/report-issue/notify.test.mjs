import test from 'node:test';
import assert from 'node:assert/strict';
import { buildNotification, notifyOwner, SUBJECT_PREFIX } from './notify.ts';
import { buildIssue, fenceFor, createGithubReporter } from './github.ts';

const report = (over = {}) => ({
  source: 'web', kind: 'bug', title: 'Crash on open', description: 'The app closes when I open a file.',
  contact_email: 'ada@example.org', app_version: '0.3.0', os: 'macOS', engine_version: null, diagnostics: null, turnstile_token: null, ...over,
});
const stored = { id: '11111111-2222-3333-4444-555555555555', createdAt: '2026-10-24T10:00:00.000Z', userId: null };
const ok = (json = {}) => ({ ok: true, status: 200, json: async () => json });
const fail = (status) => ({ ok: false, status, json: async () => ({}) });

test('the notification carries the facts as plain text and as HTML', () => {
  const m = buildNotification('owner@example.org', report(), stored);
  assert.equal(m.to, 'owner@example.org');
  assert.equal(m.replyTo, 'ada@example.org');
  assert.ok(m.text.includes('Crash on open') && m.text.includes('The app closes when I open a file.'));
  assert.ok(m.text.includes(stored.id));
  assert.ok(m.html.includes('<pre') && m.html.includes(stored.id));
  assert.equal(m.subject, `${SUBJECT_PREFIX}bug: Crash on open`);
});

test('no contact address: no Reply-To', () => {
  assert.equal('replyTo' in buildNotification('o@example.org', report({ contact_email: null }), stored), false);
});

test('everything the reporter wrote is escaped in the HTML part', () => {
  const evil = '<script>alert(1)</script><img src=x onerror=alert(2)>"\'&';
  const m = buildNotification('o@example.org', report({ title: evil, description: evil + ' more text', os: evil, app_version: evil, diagnostics: evil }), stored);
  assert.equal(m.html.includes('<script'), false);
  assert.equal(m.html.includes('<img'), false);
  assert.equal(m.html.includes('onerror=alert(2)>'), false);
  assert.ok(m.html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
  // the only tags in the HTML are ours
  const tags = new Set([...m.html.matchAll(/<\/?([a-z0-9]+)/gi)].map((x) => x[1].toLowerCase()));
  assert.deepEqual([...tags].sort(), ['h2', 'h3', 'p', 'pre', 'strong', 'table', 'td', 'tr']);
});

test('the subject is a fixed prefix plus the title on one line, shortened, with no control characters', () => {
  const m = buildNotification('o@example.org', report({ title: 'Hi\r\nBcc: attacker@example.org\r\nSubject: spoof\u0000\u0007' + 'x'.repeat(300) }), stored);
  assert.ok(m.subject.startsWith(SUBJECT_PREFIX));
  assert.equal(/[\r\n\u0000-\u001f\u007f]/.test(m.subject), false);
  assert.ok([...m.subject].length <= SUBJECT_PREFIX.length + 'bug: '.length + 100);
});

test('long diagnostics are cut in the e-mail and say so', () => {
  const m = buildNotification('o@example.org', report({ diagnostics: 'd'.repeat(10000) }), stored);
  assert.ok(m.text.length < 9000);
  assert.ok(m.text.includes('6000 more characters are in the database'));
});

test('notifyOwner: Resend first', async () => {
  const calls = [];
  const fetchFn = async (url, init) => (calls.push({ url, init }), ok());
  const env = { REPORT_NOTIFY_TO: 'owner@example.org', MAIL_FROM: 'Radarx <no-reply@radarx.labshope.com>', RESEND_API_KEY: 're_x', MAILJET_API_KEY: 'mk', MAILJET_SECRET_KEY: 'ms' };
  const r = await notifyOwner({ env: (n) => env[n], fetchFn, report: report(), stored });
  assert.equal(r.sent, true);
  assert.equal(r.provider, 'resend');
  assert.equal(calls.length, 1);
  const body = JSON.parse(calls[0].init.body);
  assert.deepEqual(body.to, ['owner@example.org']);
  assert.equal(body.reply_to, 'ada@example.org');
  assert.ok(body.subject.startsWith(SUBJECT_PREFIX));
});

test('notifyOwner: Mailjet takes over when Resend fails, with the reply-to', async () => {
  const calls = [];
  const fetchFn = async (url, init) => {
    calls.push(url);
    if (url.includes('resend')) return fail(500);
    return ok({ Messages: [{ Status: 'success' }] });
  };
  const env = { REPORT_NOTIFY_TO: 'owner@example.org', MAIL_FROM: 'no-reply@radarx.labshope.com', RESEND_API_KEY: 're_x', MAILJET_API_KEY: 'mk', MAILJET_SECRET_KEY: 'ms' };
  let mj;
  const f2 = async (url, init) => ((mj = url.includes('mailjet') ? JSON.parse(init.body) : mj), fetchFn(url, init));
  const r = await notifyOwner({ env: (n) => env[n], fetchFn: f2, report: report(), stored });
  assert.equal(r.sent, true);
  assert.equal(r.provider, 'mailjet');
  assert.deepEqual(r.attempts.map((a) => [a.provider, a.ok]), [['resend', false], ['mailjet', true]]);
  assert.equal(mj.Messages[0].ReplyTo.Email, 'ada@example.org');
  assert.equal(mj.Messages[0].To[0].Email, 'owner@example.org');
});

test('notifyOwner never throws: no recipient, no provider, all providers failing', async () => {
  const f = async () => fail(500);
  const base = { MAIL_FROM: 'no-reply@radarx.labshope.com', RESEND_API_KEY: 're_x' };
  assert.equal((await notifyOwner({ env: (n) => base[n], fetchFn: f, report: report(), stored })).sent, false); // no REPORT_NOTIFY_TO
  assert.equal((await notifyOwner({ env: (n) => ({ ...base, REPORT_NOTIFY_TO: 'not an address' })[n], fetchFn: f, report: report(), stored })).sent, false);
  assert.equal((await notifyOwner({ env: (n) => ({ REPORT_NOTIFY_TO: 'o@example.org' })[n], fetchFn: f, report: report(), stored })).sent, false); // no MAIL_FROM
  const r = await notifyOwner({ env: (n) => ({ ...base, REPORT_NOTIFY_TO: 'o@example.org' })[n], fetchFn: f, report: report(), stored });
  assert.equal(r.sent, false);
  assert.equal(r.attempts[0].status, 500);
});

// ---- GitHub

test('the GitHub issue holds the reporter text in a code block, never the e-mail, never the diagnostics', () => {
  const d = buildIssue(report({ title: 'Bug @everyone [x](http://evil) <b>', description: 'See ![img](http://evil/x.png) and @octocat\n```\nclose fence?\n```', diagnostics: 'SECRET-DIAG-LINE' }), stored.id);
  assert.equal(d.body.includes('ada@example.org'), false);
  assert.equal(d.body.includes('SECRET-DIAG-LINE'), false);
  assert.ok(d.body.includes('Diagnostics were attached'));
  assert.ok(d.title.startsWith('[bug] '));
  assert.ok(d.title.length <= 120);
  // the fence is longer than the longest backtick run inside the text
  const open = /^(`{3,})text\n/m.exec(d.body);
  assert.ok(open);
  assert.ok(open[1].length >= 4);
  assert.equal(d.body.split('\n').filter((l) => l === open[1]).length, 1); // exactly one closing line
  // everything the user wrote sits between the fences
  const inside = d.body.slice(d.body.indexOf(open[0]) + open[0].length, d.body.lastIndexOf('\n' + open[1]));
  assert.ok(inside.includes('@octocat') && inside.includes('![img]'));
  const outside = d.body.replace(inside, '');
  assert.equal(/@octocat|!\[img\]|evil/.test(outside), false);
});

test('labels come from the fixed list', () => {
  assert.deepEqual(buildIssue(report({ kind: 'idea', source: 'app' }), 'id').labels, ['idea', 'from-app']);
  assert.deepEqual(buildIssue(report({ kind: 'bug', source: 'web' }), 'id').labels, ['bug', 'from-web']);
  assert.deepEqual(buildIssue(report({ kind: 'question' }), 'id').labels, ['question', 'from-web']);
});

test('fence length grows with backtick runs', () => {
  assert.equal(fenceFor('no ticks'), '```');
  assert.equal(fenceFor('``'), '```');
  assert.equal(fenceFor('```'), '````');
  assert.equal(fenceFor('a ````` b'), '``````');
});

test('GitHub: checks that the repo is private, then posts with a bearer token', async () => {
  const calls = [];
  const fetchFn = async (url, init) => {
    calls.push({ url, init });
    if (init.method === 'GET') return ok({ private: true });
    return ok({ number: 42 });
  };
  const gh = createGithubReporter({ token: 'ghp_FAKE', repo: 'owner/reports', fetchFn });
  const r = await gh.report(report(), stored.id);
  assert.deepEqual(r, { ok: true, number: 42 });
  assert.equal(calls[0].url, 'https://api.github.com/repos/owner/reports');
  assert.equal(calls[1].url, 'https://api.github.com/repos/owner/reports/issues');
  assert.equal(calls[1].init.headers.Authorization, 'Bearer ghp_FAKE');
  const body = JSON.parse(calls[1].init.body);
  assert.ok(body.title && body.body && Array.isArray(body.labels));
  // a second report within the cache window does not ask again
  await gh.report(report(), stored.id);
  assert.equal(calls.filter((c) => c.init.method === 'GET').length, 1);
});

test('GitHub: a public repository gets NOTHING', async () => {
  const calls = [];
  const fetchFn = async (url, init) => (calls.push(init.method), ok({ private: false }));
  const r = await createGithubReporter({ token: 't', repo: 'owner/public', fetchFn }).report(report(), stored.id);
  assert.equal(r.ok, false);
  assert.match(r.reason, /not private/);
  assert.deepEqual(calls, ['GET']);
});

test('GitHub: failures are reported, never thrown', async () => {
  assert.equal((await createGithubReporter({ token: 't', repo: 'bad repo', fetchFn: async () => ok() }).report(report(), 'i')).ok, false);
  assert.equal((await createGithubReporter({ token: 't', repo: 'o/r', fetchFn: async () => fail(404) }).report(report(), 'i')).ok, false);
  assert.equal((await createGithubReporter({ token: 't', repo: 'o/r', fetchFn: async () => { throw new Error('down'); } }).report(report(), 'i')).ok, false);
  let n = 0;
  const f = async (u, init) => (init.method === 'GET' ? ok({ private: true }) : (n++, fail(422)));
  assert.equal((await createGithubReporter({ token: 't', repo: 'o/r', fetchFn: f }).report(report(), 'i')).ok, false);
  assert.equal(n, 1);
});
