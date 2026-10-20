import test from 'node:test';
import assert from 'node:assert/strict';
import { checkDraft, emptyDraft, buildPayload, systemName, describeFailure, submitReport, reportUrl, cpLength, LIMITS, KINDS } from './reportIssue.ts';
import { recordEngineMessage, engineLogLines, clearEngineLog } from './engineLog.ts';

const draft = (over = {}) => ({ ...emptyDraft(), title: 'Crash on open', description: 'The app closes when I open a file.', ...over });
const env = (over = {}) => ({ appVersion: '0.3.0', os: 'macOS arm64 (desktop)', engineVersion: null, engineLines: [], ...over });
const UID = '11111111-2222-3333-4444-555555555555';

test('the three kinds match the function', () => {
  assert.deepEqual(KINDS.map((k) => k.id), ['bug', 'idea', 'question']);
  assert.equal(emptyDraft().kind, 'bug');
  assert.equal(emptyDraft().includeDiagnostics, false, 'diagnostics are OFF by default');
});

test('the draft is checked against the same limits as the server', () => {
  assert.deepEqual(checkDraft(draft()), {});
  assert.ok(checkDraft(draft({ title: 'ab' })).title);
  assert.ok(checkDraft(draft({ title: '   a   ' })).title);
  assert.ok(checkDraft(draft({ title: 'x'.repeat(141) })).title);
  assert.deepEqual(checkDraft(draft({ title: 'x'.repeat(140) })), {});
  assert.ok(checkDraft(draft({ description: 'too short' })).description);
  assert.ok(checkDraft(draft({ description: 'd'.repeat(5001) })).description);
  assert.deepEqual(checkDraft(draft({ description: 'd'.repeat(5000) })), {});
  assert.deepEqual(checkDraft(draft({ title: '\u{1F600}'.repeat(140) })), {}); // code points, like the database
  assert.equal(cpLength('a\u{1F600}'), 2);
  assert.equal(LIMITS.title.max, 140);
});

test('the payload is exactly what the function expects, with no diagnostics unless asked', () => {
  const p = buildPayload(draft({ title: '  Crash on open  ', description: '  text text text  ' }), env({ engineLines: ['10:00:00 error: secret@example.org'] }));
  assert.deepEqual(p, { source: 'app', kind: 'bug', title: 'Crash on open', description: 'text text text', app_version: '0.3.0', os: 'macOS arm64 (desktop)' });
  assert.equal('diagnostics' in p, false);
  assert.equal('engine_version' in p, false);
  assert.equal(JSON.stringify(p).includes('secret@example.org'), false);
});

test('with diagnostics ticked the payload carries the scrubbed diagnostics and the engine version', () => {
  const p = buildPayload(
    draft({ includeDiagnostics: true }),
    env({ engineVersion: '1.4.2', engineLines: ['10:00:00 error: cannot open /Users/ada/data/KTLX20130520_201643_V06.gz for ada@example.org'] }),
    { home: '/Users/ada', user: 'ada' },
  );
  assert.equal(p.engine_version, '1.4.2');
  assert.match(p.diagnostics, /^app: 0\.3\.0\nsystem: macOS arm64 \(desktop\)\nengine: 1\.4\.2/);
  assert.ok(p.diagnostics.includes('~/data/[file].gz'));
  assert.equal(/ada|KTLX|example\.org/.test(p.diagnostics), false);
  assert.ok(p.diagnostics.length <= 20000);
});

test('version and system fields are clipped to the server limits', () => {
  const p = buildPayload(draft(), env({ appVersion: 'v'.repeat(80), os: 'o'.repeat(200) }));
  assert.equal(p.app_version.length, 40);
  assert.equal(p.os.length, 60);
});

test('the system name: family, processor when known, desktop or web, no versions', () => {
  assert.equal(systemName({ userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15', arch: 'arm', desktop: true }), 'macOS arm (desktop)');
  assert.equal(systemName({ userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', arch: 'x86', desktop: true }), 'Windows x86 (desktop)');
  assert.equal(systemName({ userAgent: 'Mozilla/5.0 (X11; Linux x86_64)', desktop: false }), 'Linux (web)');
  assert.equal(systemName({ userAgent: 'Mozilla/5.0 (Linux; Android 14)', desktop: false }), 'Android (web)');
  assert.equal(systemName({ userAgent: '', desktop: false }), 'Unknown system (web)');
  assert.equal(systemName({ userAgent: 'Macintosh', arch: 'arm; DROP', desktop: true }), 'macOS (desktop)');
});

test('failures become plain sentences, each telling the draft is kept where that matters', () => {
  assert.equal(describeFailure(null, null, 'offline').kind, 'offline');
  assert.match(describeFailure(null, null, 'offline').message, /offline/i);
  assert.match(describeFailure(null, null, 'timeout').message, /did not answer in time/);
  assert.match(describeFailure(null, null, 'network').message, /could not be reached/);
  const rate = describeFailure(429, { error: { code: 'rate_limited', message: 'Too many' } });
  assert.equal(rate.kind, 'rate');
  assert.match(rate.message, /most reports allowed/);
  assert.equal(describeFailure(401, { error: { code: 'unauthorized' } }).kind, 'signin');
  assert.equal(describeFailure(500, { error: { code: 'server_error' } }).kind, 'server');
  assert.equal(describeFailure(503, null).kind, 'server');
  assert.equal(describeFailure(418, null).kind, 'unknown');
  const bad = describeFailure(400, { error: { code: 'invalid_input', message: 'The title must be at least 3 characters.', field: 'title' } });
  assert.deepEqual([bad.kind, bad.field, bad.message], ['input', 'title', 'The title must be at least 3 characters.']);
  // a field name or message that is not what we expect is not shown
  const odd = describeFailure(400, { error: { code: 'invalid_input', message: 'x'.repeat(500), field: '<script>' } });
  assert.equal(odd.field, undefined);
  assert.ok(odd.message.length < 160);
  for (const k of ['offline', 'timeout', 'network', 'rate', 'signin', 'server', 'unknown']) {
    const m = describeFailure(null, null, k === 'offline' || k === 'timeout' || k === 'network' ? k : undefined);
    assert.ok(m.message.length > 20);
  }
});

const okFetch = (status, json) => async () => ({ ok: status < 300, status, json: async () => json });

test('submit: sends the payload with the access token and returns the id', async () => {
  const calls = [];
  const fetchFn = async (url, init) => (calls.push({ url, init }), { ok: true, status: 200, json: async () => ({ id: UID }) });
  const payload = buildPayload(draft(), env());
  const r = await submitReport({ url: reportUrl('https://p.supabase.co/'), apiKey: 'PUBLISHABLE', token: 'a.b.c', payload, fetchFn });
  assert.deepEqual(r, { ok: true, id: UID });
  assert.equal(calls[0].url, 'https://p.supabase.co/functions/v1/report-issue');
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers.Authorization, 'Bearer a.b.c');
  assert.equal(calls[0].init.headers.apikey, 'PUBLISHABLE');
  assert.equal(calls[0].init.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(calls[0].init.body), payload);
});

test('submit: no token means sign in, with no request', async () => {
  let called = false;
  const r = await submitReport({ url: 'u', apiKey: 'k', token: null, payload: buildPayload(draft(), env()), fetchFn: async () => ((called = true), null) });
  assert.equal(r.ok, false);
  assert.equal(r.kind, 'signin');
  assert.equal(called, false);
});

test('submit: offline means no request and the offline sentence', async () => {
  let called = false;
  const r = await submitReport({ url: 'u', apiKey: 'k', token: 't', payload: buildPayload(draft(), env()), online: false, fetchFn: async () => ((called = true), null) });
  assert.equal(r.kind, 'offline');
  assert.equal(called, false);
});

test('submit: every kind of failure comes back as a result, never a throw', async () => {
  const payload = buildPayload(draft(), env());
  const base = { url: 'u', apiKey: 'k', token: 't', payload };
  assert.equal((await submitReport({ ...base, fetchFn: okFetch(429, { error: { code: 'rate_limited' } }) })).kind, 'rate');
  assert.equal((await submitReport({ ...base, fetchFn: okFetch(401, { error: { code: 'unauthorized' } }) })).kind, 'signin');
  assert.equal((await submitReport({ ...base, fetchFn: okFetch(500, { error: { code: 'server_error' } }) })).kind, 'server');
  assert.equal((await submitReport({ ...base, fetchFn: okFetch(400, { error: { code: 'invalid_input', field: 'title', message: 'The title is required.' } }) })).field, 'title');
  assert.equal((await submitReport({ ...base, fetchFn: async () => { throw new TypeError('Failed to fetch'); } })).kind, 'network');
  assert.equal((await submitReport({ ...base, fetchFn: async () => ({ ok: false, status: 502, json: async () => { throw new SyntaxError('html'); } }) })).kind, 'server');
  // a 200 without a proper id is not a success
  assert.equal((await submitReport({ ...base, fetchFn: okFetch(200, { id: 'nope' }) })).ok, false);
  assert.equal((await submitReport({ ...base, fetchFn: okFetch(200, {}) })).ok, false);
});

test('submit: a slow service times out', async () => {
  const fetchFn = (url, init) => new Promise((_, rej) => init.signal.addEventListener('abort', () => rej(new Error('aborted'))));
  const r = await submitReport({ url: 'u', apiKey: 'k', token: 't', payload: buildPayload(draft(), env()), fetchFn, timeoutMs: 30 });
  assert.equal(r.kind, 'timeout');
});

test('the engine log keeps the last 200 lines, in memory, with a time stamp', () => {
  clearEngineLog();
  for (let i = 0; i < 250; i++) recordEngineMessage(i % 2 ? 'warning' : 'error', `m${i}`, new Date(2026, 9, 24, 10, 5, 7));
  const lines = engineLogLines();
  assert.equal(lines.length, 200);
  assert.equal(lines[0], '10:05:07 error: m50');
  assert.equal(lines[199], '10:05:07 warning: m249');
  recordEngineMessage('error', 'x'.repeat(2000));
  assert.ok(engineLogLines().at(-1).length < 520);
  clearEngineLog();
  assert.deepEqual(engineLogLines(), []);
});
