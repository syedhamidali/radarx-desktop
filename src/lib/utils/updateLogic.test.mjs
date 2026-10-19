// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  compareVersions,
  isNewer,
  normalizeVersion,
  isSkipped,
  autoCheckDue,
  CHECK_INTERVAL_MS,
  busyState,
  restartGate,
  describeUpdateError,
  progressPercent,
  formatBytes,
  safeHref,
  parseReleaseNotes,
} from './updateLogic.ts';

// --- versions ---

test('versions compare by number, not by text', () => {
  assert.equal(compareVersions('0.2.1', '0.10.0'), -1);
  assert.equal(compareVersions('1.0.0', '1.0.0'), 0);
  assert.equal(compareVersions('2.0.0', '1.99.99'), 1);
  assert.equal(compareVersions('v1.2.3', '1.2.3'), 0);
});

test('a pre-release is older than its release, and pre-releases order by identifier', () => {
  assert.equal(compareVersions('1.0.0-rc.1', '1.0.0'), -1);
  assert.equal(compareVersions('1.0.0-rc.2', '1.0.0-rc.10'), -1);
  assert.equal(compareVersions('1.0.0-alpha', '1.0.0-beta'), -1);
  assert.equal(compareVersions('1.0.0-1', '1.0.0-alpha'), -1);
  assert.equal(compareVersions('1.0.0+build5', '1.0.0'), 0);
});

test('isNewer is false for the same, older, or unparseable versions', () => {
  assert.equal(isNewer('0.2.1', '0.2.2'), true);
  assert.equal(isNewer('0.2.1', '0.2.1'), false);
  assert.equal(isNewer('0.3.0', '0.2.9'), false);
  assert.equal(isNewer('0.2.1', 'latest'), false);
  assert.equal(isNewer('0.2.1', ''), false);
  assert.equal(compareVersions('x', '1.0.0'), null);
  assert.equal(compareVersions(undefined, '1.0.0'), null);
});

test('normalizeVersion strips a leading v', () => {
  assert.equal(normalizeVersion('v0.3.0'), '0.3.0');
  assert.equal(normalizeVersion(' 0.3.0 '), '0.3.0');
});

// --- skip ---

test('skipping hides that exact version only, a newer one is offered again', () => {
  assert.equal(isSkipped('0.3.0', '0.3.0'), true);
  assert.equal(isSkipped('v0.3.0', '0.3.0'), true);
  assert.equal(isSkipped('0.3.0', '0.3.1'), false);
  assert.equal(isSkipped('', '0.3.0'), false);
});

// --- 24 h throttle ---

test('auto check: off means never', () => {
  assert.equal(autoCheckDue({ enabled: false, lastCheckedAt: 0, now: 1e12 }), false);
});

test('auto check: first launch checks, then at most once per 24 hours', () => {
  const now = 1_800_000_000_000;
  assert.equal(autoCheckDue({ enabled: true, lastCheckedAt: 0, now }), true);
  assert.equal(autoCheckDue({ enabled: true, lastCheckedAt: now - 60_000, now }), false);
  assert.equal(autoCheckDue({ enabled: true, lastCheckedAt: now - (CHECK_INTERVAL_MS - 1), now }), false);
  assert.equal(autoCheckDue({ enabled: true, lastCheckedAt: now - CHECK_INTERVAL_MS, now }), true);
  assert.equal(autoCheckDue({ enabled: true, lastCheckedAt: now - 3 * CHECK_INTERVAL_MS, now }), true);
});

test('auto check: a stamp in the future (clock set back) or garbage does not block checks', () => {
  const now = 1_800_000_000_000;
  assert.equal(autoCheckDue({ enabled: true, lastCheckedAt: now + 10 * CHECK_INTERVAL_MS, now }), true);
  assert.equal(autoCheckDue({ enabled: true, lastCheckedAt: NaN, now }), true);
  assert.equal(autoCheckDue({ enabled: true, lastCheckedAt: -5, now }), true);
});

// --- defer while busy ---

const idle = { operations: [], pipelineBusy: null, mosaicRunning: false, windRunning: false, modulesRunning: 0 };

test('idle app is not busy', () => {
  assert.deepEqual(busyState(idle), { busy: false, reason: null });
});

test('sweep loads and finished jobs do not count as busy', () => {
  const b = busyState({
    ...idle,
    operations: [
      { kind: 'sweep', state: 'running' },
      { kind: 'export', state: 'done' },
      { kind: 'module', state: 'failed' },
      { kind: 'mosaic', state: 'cancelled' },
    ],
  });
  assert.equal(b.busy, false);
});

test('export, pipeline, module, mosaic and wind runs each make the app busy and are named', () => {
  assert.deepEqual(busyState({ ...idle, operations: [{ kind: 'export', state: 'running' }] }), {
    busy: true,
    reason: 'an export',
  });
  assert.equal(busyState({ ...idle, pipelineBusy: 'run' }).reason, 'a pipeline');
  assert.equal(busyState({ ...idle, modulesRunning: 2 }).reason, 'a module run');
  assert.equal(busyState({ ...idle, mosaicRunning: true }).reason, 'a mosaic');
  assert.equal(busyState({ ...idle, windRunning: true }).reason, 'a wind retrieval');
  assert.equal(busyState({ ...idle, operations: [{ kind: 'module', state: 'slow' }] }).busy, true);
  assert.equal(busyState({ ...idle, operations: [{ kind: 'upload', state: 'queued' }] }).busy, true);
});

test('restart prompt: shown only when downloaded, wanted and idle; waits while a job runs', () => {
  assert.equal(restartGate({ ready: true, promptPending: true, busy: false }), 'show');
  assert.equal(restartGate({ ready: true, promptPending: true, busy: true }), 'wait');
  assert.equal(restartGate({ ready: true, promptPending: false, busy: false }), 'none');
  assert.equal(restartGate({ ready: false, promptPending: true, busy: false }), 'none');
});

// --- errors, numbers ---

test('errors become short human sentences; a network failure is not scary', () => {
  assert.equal(describeUpdateError('__timeout__').kind, 'timeout');
  assert.equal(describeUpdateError(new Error('error sending request for url (https://x)')).kind, 'offline');
  assert.equal(describeUpdateError('Network is unreachable').kind, 'offline');
  assert.equal(describeUpdateError('the update endpoint did not respond with a successful status code 404').kind, 'unavailable');
  assert.equal(describeUpdateError('None of the fallback platforms `["linux-x86_64"]` were found').kind, 'no-build');
  assert.equal(describeUpdateError('The signature verification failed').kind, 'signature');
  const other = describeUpdateError('boom '.repeat(100));
  assert.equal(other.kind, 'other');
  assert.ok(other.message.length < 200);
  assert.ok(describeUpdateError(undefined).message.length > 0);
});

test('download / install failures are worded for what failed, not as a "check"', () => {
  assert.match(describeUpdateError('error sending request', 'download').message, /^Could not download the update/);
  assert.match(describeUpdateError('boom', 'install').message, /^Could not install the update: boom/);
  assert.match(describeUpdateError('__timeout__', 'check').message, /^Could not check/);
  // a certificate problem is a connection problem, not a bad update signature
  assert.equal(describeUpdateError('invalid peer certificate: verify failed').kind, 'offline');
});

test('a reply that is not valid update JSON is "not available", not "offline"', () => {
  assert.equal(describeUpdateError('error decoding response body').kind, 'unavailable');
});

test('progress and sizes', () => {
  assert.equal(progressPercent(50, 200), 25);
  assert.equal(progressPercent(500, 200), 100);
  assert.equal(progressPercent(5, null), null);
  assert.equal(progressPercent(5, 0), null);
  assert.equal(formatBytes(1536), '2 KB');
  assert.equal(formatBytes(104_857_600), '100.0 MB');
  assert.equal(formatBytes(null), '');
});

// --- release notes: safety and shape ---

/** Collect every node of the tree (to assert on what can appear). */
function walk(blocks) {
  const nodes = [];
  const inl = (list) =>
    list.forEach((n) => {
      nodes.push(n);
      if (n.c) inl(n.c);
    });
  for (const b of blocks) {
    nodes.push(b);
    if (b.c) inl(b.c);
    if (b.items) b.items.forEach(inl);
  }
  return nodes;
}

test('only http(s) links survive', () => {
  assert.equal(safeHref('https://example.org/a?b=1'), 'https://example.org/a?b=1');
  assert.equal(safeHref('http://example.org'), 'http://example.org/');
  for (const bad of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'data:text/html,<b>', 'file:///etc/passwd', 'vbscript:x', '//evil.test', '/relative', '', 'not a url']) {
    assert.equal(safeHref(bad), null, bad);
  }
});

test('raw HTML in the notes stays text and is never an element', () => {
  const blocks = parseReleaseNotes('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n<b>bold</b>');
  const nodes = walk(blocks);
  assert.ok(nodes.every((n) => ['p', 'text'].includes(n.t)));
  const text = nodes.filter((n) => n.t === 'text').map((n) => n.s).join('|');
  assert.ok(text.includes('<script>alert(1)</script>'));
});

test('markdown links: javascript: is dropped to its text, https stays a link', () => {
  const nodes = walk(parseReleaseNotes('[bad](javascript:alert(1)) and [good](https://example.org/x) and ![pic](https://example.org/p.png)'));
  const links = nodes.filter((n) => n.t === 'link');
  assert.equal(links.length, 1);
  assert.equal(links[0].href, 'https://example.org/x');
  const text = nodes.filter((n) => n.t === 'text').map((n) => n.s).join('');
  assert.ok(text.includes('bad'));
  assert.ok(text.includes('pic'));
  assert.ok(!nodes.some((n) => n.t === 'img'));
});

test('headings, lists, code and emphasis', () => {
  const md = [
    '## What is new',
    '',
    '- Faster **export** of `PNG` files',
    '- Fixes *rare* crash',
    '  when opening a file',
    '',
    '1. one',
    '2. two',
    '',
    '```',
    'npm run <thing>',
    '```',
    '',
    '---',
    'Done.',
  ].join('\n');
  const b = parseReleaseNotes(md);
  assert.deepEqual(b.map((x) => x.t), ['h', 'ul', 'ol', 'code', 'hr', 'p']);
  assert.equal(b[0].level, 2);
  assert.equal(b[1].items.length, 2);
  assert.ok(b[1].items[0].some((n) => n.t === 'strong'));
  assert.ok(b[1].items[0].some((n) => n.t === 'code'));
  assert.ok(b[1].items[1].some((n) => n.t === 'em'));
  assert.ok(JSON.stringify(b[1].items[1]).includes('when opening a file'));
  assert.equal(b[3].s, 'npm run <thing>');
});

test('hostile input: huge, deeply nested, unbalanced, wrong type', () => {
  assert.deepEqual(parseReleaseNotes(undefined), []);
  assert.deepEqual(parseReleaseNotes(42), []);
  assert.deepEqual(parseReleaseNotes(''), []);
  const big = parseReleaseNotes('line\n\n'.repeat(50_000));
  assert.ok(big.length <= 400);
  const nested = parseReleaseNotes('*'.repeat(5000) + ' ' + '['.repeat(5000) + '`' + '**'.repeat(3000));
  assert.ok(Array.isArray(nested));
  const deep = parseReleaseNotes('[' .repeat(50) + 'x' + '](https://example.org)'.repeat(50));
  assert.ok(Array.isArray(deep));
  assert.ok(Array.isArray(parseReleaseNotes('**unclosed _also *')));
});
