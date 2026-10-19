// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { IDLE, reduceEnv, needsDialog, sizeText, cachedText, progressText, hostsOf } from './envFlow.ts';

const est = (over = {}) => ({
  provider: 'fake',
  provider_name: 'Fake model',
  digest: 'abc',
  files: [
    { name: 'a.json', url: 'https://data.example.org/a.json', size: 4096, cached: false },
    { name: 'b.json', url: 'https://data.example.org/b.json', size: 4096, cached: false },
    { name: 'c.json', url: 'https://other.example.org/c.json', size: 1024, cached: true },
  ],
  n_files: 3,
  total_bytes: 9216,
  cached_bytes: 1024,
  network_bytes: 8192,
  size_exact: true,
  max_network_bytes: 8192,
  destination: '/home/u/.xradar-desktop/environment-cache',
  attribution: 'Test data',
  licence: 'CC0',
  source: 'https://data.example.org/',
  offline: false,
  needs_network: true,
  requires_approval: true,
  can_fetch: true,
  reason: '',
  notes: [],
  ...over,
});

const run = (events, start = IDLE) => events.reduce(reduceEnv, start);
const ask = { type: 'ask', requestId: 'r1', request: { provider: 'fake' } };

test('the happy path: ask, estimate, approve, progress, result', () => {
  let s = run([ask]);
  assert.equal(s.phase, 'estimating');
  s = run([{ type: 'estimate', requestId: 'r1', estimate: est() }], s);
  assert.equal(s.phase, 'confirm');
  assert.ok(needsDialog(s), 'the dialog is shown before anything downloads');
  s = run([{ type: 'approve', requestId: 'r1' }], s);
  assert.equal(s.phase, 'downloading');
  s = run([{ type: 'progress', requestId: 'r1', progress: { percent: 40, message: 'Downloading a.json', file: 'a.json', bytes_done: 3000, bytes_total: 8192 } }], s);
  assert.equal(s.progress.percent, 40);
  s = run([{ type: 'result', requestId: 'r1', result: { paths: ['/x'] } }], s);
  assert.equal(s.phase, 'done');
  assert.deepEqual(s.result, { paths: ['/x'] });
});

test('nothing downloads without approval: only "confirm" accepts approve', () => {
  for (const phase of ['idle', 'estimating', 'downloading', 'done', 'error']) {
    const s = { ...IDLE, phase, requestId: 'r1' };
    assert.equal(reduceEnv(s, { type: 'approve', requestId: 'r1' }), s, phase);
  }
  // an approval for another request is ignored
  const confirm = run([ask, { type: 'estimate', requestId: 'r1', estimate: est() }]);
  assert.equal(reduceEnv(confirm, { type: 'approve', requestId: 'r2' }), confirm);
});

test('data that needs no download (cached, local files) goes straight through without a dialog', () => {
  const s = run([ask, { type: 'estimate', requestId: 'r1', estimate: est({ requires_approval: false, network_bytes: 0 }) }]);
  assert.equal(s.phase, 'downloading');
  assert.equal(needsDialog(s), false);
  const done = run([{ type: 'result', requestId: 'r1', result: { paths: [] } }], s);
  assert.equal(done.phase, 'done');
});

test('a request that cannot be fetched (offline, too large) is an error the user can read', () => {
  const s = run([ask, { type: 'estimate', requestId: 'r1', estimate: est({ can_fetch: false, offline: true, reason: 'Offline mode is on and 2 of 3 file(s) are not in the cache.' }) }]);
  assert.equal(s.phase, 'error');
  assert.equal(s.code, 'offline');
  assert.match(s.error, /Offline mode is on/);
  assert.ok(needsDialog(s));
});

test('replies of another request never touch the flow', () => {
  let s = run([ask, { type: 'estimate', requestId: 'r1', estimate: est() }]);
  const before = s;
  for (const e of [
    { type: 'estimate', requestId: 'r9', estimate: est() },
    { type: 'progress', requestId: 'r9', progress: { percent: 90, message: '', file: '', bytes_done: 1, bytes_total: null } },
    { type: 'result', requestId: 'r9', result: {} },
    { type: 'error', requestId: 'r9', message: 'x' },
    { type: 'cancelled', requestId: 'r9' },
  ]) {
    assert.equal(reduceEnv(s, e), before, e.type);
  }
  s = reduceEnv(IDLE, { type: 'result', requestId: 'r1', result: {} });
  assert.equal(s, IDLE, 'no flow, no result');
});

test('errors, cancel and dismiss', () => {
  let s = run([ask, { type: 'estimate', requestId: 'r1', estimate: est() }, { type: 'approve', requestId: 'r1' }]);
  const failed = reduceEnv(s, { type: 'error', requestId: 'r1', message: 'Could not download a.json', code: 'failed' });
  assert.equal(failed.phase, 'error');
  assert.equal(failed.code, 'failed');
  const cancelled = reduceEnv(s, { type: 'cancelled', requestId: 'r1' });
  assert.equal(cancelled.phase, 'cancelled');
  assert.equal(needsDialog(cancelled), false);
  assert.equal(reduceEnv(failed, { type: 'dismiss' }), IDLE);
  // an error that names no request (null) belongs to the flow in progress
  assert.equal(reduceEnv(s, { type: 'error', requestId: null, message: 'boom' }).phase, 'error');
  // a new question starts clean
  assert.equal(reduceEnv(failed, ask).error, null);
});

test('a result that arrives while the dialog still shows "confirm" is accepted (fast cached fetch)', () => {
  const s = run([ask, { type: 'estimate', requestId: 'r1', estimate: est() }, { type: 'result', requestId: 'r1', result: { paths: [] } }]);
  assert.equal(s.phase, 'done');
});

test('the dialog texts say how much, from where, and what is already cached', () => {
  const e = est();
  assert.equal(sizeText(e), '2 files, 8 KB');
  assert.equal(cachedText(e), '1 of 3 files already in the cache (1 KB), not downloaded again');
  assert.deepEqual(hostsOf(e), ['data.example.org']); // the cached file's host is not contacted
  assert.match(sizeText(est({ size_exact: false, max_network_bytes: 100 * 1024 * 1024 })), /at least 8 KB.*not known in advance.*100\.0 MB/);
  assert.equal(sizeText(est({ files: e.files.map((f) => ({ ...f, cached: true })), network_bytes: 0 })), 'nothing to download (all in the cache)');
  assert.equal(cachedText(est({ files: e.files.map((f) => ({ ...f, cached: false })) })), '');
  assert.equal(sizeText(est({ files: [e.files[0]], network_bytes: 4096 })), '1 file, 4 KB');
  assert.equal(progressText(null), 'Starting...');
  assert.match(progressText({ percent: 50, message: 'Downloading a.json', file: 'a.json', bytes_done: 2048, bytes_total: 4096 }), /50%.*2 KB of 4 KB/);
});
