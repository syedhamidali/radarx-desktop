// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  MAX_LIVE_RADARS,
  normalizeRadarIds,
  toggleRadarId,
  moveRadarId,
  clampIntervalMin,
  nextDelayMs,
  MAX_BACKOFF_MS,
  countdownText,
  planDownloads,
  sameVolumes,
  describeLiveListing,
  describeLiveDownload,
  pushFrame,
  stepFrame,
  radarStatusText,
  hhmmss,
} from './liveLogic.ts';

// ── Radar list ──────────────────────────────────────────────────────────────────────────────

test('normalizeRadarIds: upper-case, valid, unique, ordered, bounded', () => {
  assert.deepEqual(normalizeRadarIds([' ktlx', 'KVNX', 'ktlx', 'bad id', 42, 'KINX']), ['KTLX', 'KVNX', 'KINX']);
  assert.deepEqual(normalizeRadarIds('KTLX'), []);
  const many = Array.from({ length: 12 }, (_, i) => `K${String(i).padStart(3, '0')}`);
  assert.equal(normalizeRadarIds(many).length, MAX_LIVE_RADARS);
});

test('toggleRadarId adds, removes and refuses past the bound', () => {
  assert.deepEqual(toggleRadarId([], 'ktlx'), ['KTLX']);
  assert.deepEqual(toggleRadarId(['KTLX', 'KVNX'], 'KTLX'), ['KVNX']);
  const full = Array.from({ length: MAX_LIVE_RADARS }, (_, i) => `K${String(i).padStart(3, '0')}`);
  assert.deepEqual(toggleRadarId(full, 'KZZZ'), full);
});

test('moveRadarId keeps the order a stable list', () => {
  assert.deepEqual(moveRadarId(['A', 'B', 'C'], 'C', -1), ['A', 'C', 'B']);
  assert.deepEqual(moveRadarId(['A', 'B', 'C'], 'A', -1), ['A', 'B', 'C']);
  assert.deepEqual(moveRadarId(['A', 'B', 'C'], 'C', 1), ['A', 'B', 'C']);
  assert.deepEqual(moveRadarId(['A', 'B', 'C'], 'X', 1), ['A', 'B', 'C']);
});

// ── Timer and backoff ───────────────────────────────────────────────────────────────────────

test('clampIntervalMin: 2..15, default 5', () => {
  assert.equal(clampIntervalMin(0), 2);
  assert.equal(clampIntervalMin(99), 15);
  assert.equal(clampIntervalMin(7.4), 7);
  assert.equal(clampIntervalMin('x'), 5);
  assert.equal(clampIntervalMin(NaN), 5);
});

test('nextDelayMs: the interval, then doubling per failure, capped at an hour', () => {
  assert.equal(nextDelayMs(5, 0), 5 * 60_000);
  assert.equal(nextDelayMs(5, 1), 5 * 60_000);
  assert.equal(nextDelayMs(5, 2), 10 * 60_000);
  assert.equal(nextDelayMs(5, 3), 20 * 60_000);
  assert.equal(nextDelayMs(5, 10), MAX_BACKOFF_MS);
  assert.equal(nextDelayMs(2, 2), 4 * 60_000);
});

test('countdownText', () => {
  assert.equal(countdownText(null, 0), '');
  assert.equal(countdownText(1000, 5000), 'now');
  assert.equal(countdownText(272_000, 0), '4:32');
  assert.equal(countdownText(61_000, 0), '1:01');
});

// ── Plans ───────────────────────────────────────────────────────────────────────────────────

const CHECK = [
  { station: 'KTLX', status: 'ok', name: 'KTLX20260101_120000_V06', path: 's3://b/KTLX', size: 5_000_000, scan_time: '2026-01-01T12:00:00Z', age_s: 240, held: false },
  { station: 'KVNX', status: 'ok', name: 'KVNX20260101_115500_V06', path: 's3://b/KVNX', size: 3_000_000, scan_time: '2026-01-01T11:55:00Z', age_s: 540, held: true },
  { station: 'KINX', status: 'not_found', message: 'No Level II volume for KINX today or yesterday (UTC).' },
  { station: 'KFDR', status: 'offline', message: 'Could not reach the AWS public bucket (offline or blocked).' },
];

test('planDownloads: held volumes are not downloaded again; sizes are exact; failures listed', () => {
  const p = planDownloads(CHECK);
  assert.deepEqual(p.files.map((f) => f.station), ['KTLX', 'KVNX']);
  assert.deepEqual(p.toDownload.map((f) => f.station), ['KTLX']);
  assert.equal(p.bytes, 5_000_000);
  assert.deepEqual(p.failed.map((f) => f.station), ['KINX', 'KFDR']);
  assert.match(p.failed[0].message, /No Level II volume/);
  // an ok entry without a path is unusable, never downloaded
  const q = planDownloads([{ station: 'KAAA', status: 'ok', name: 'x' }]);
  assert.equal(q.files.length, 0);
  assert.equal(q.failed.length, 1);
});

test('sameVolumes compares the exact set of station+name', () => {
  const a = [{ station: 'KTLX', name: 'a' }, { station: 'KVNX', name: 'b' }];
  assert.ok(sameVolumes(a, [a[1], a[0]]));
  assert.ok(!sameVolumes(a, [a[0]]));
  assert.ok(!sameVolumes(a, [a[0], { station: 'KVNX', name: 'c' }]));
});

// ── Consent wording ─────────────────────────────────────────────────────────────────────────

test('describeLiveListing names the stations, the folder, the host and a size bound', () => {
  const t = describeLiveListing(['KTLX', 'KVNX'], new Date(Date.UTC(2026, 9, 22, 3, 0, 0)));
  assert.match(t.title, /2 radars/);
  assert.match(t.lines[0], /2026\/10\/22\/<SITE>\//);
  assert.match(t.lines[0], /KTLX, KVNX/);
  assert.match(t.lines[0], /No radar data yet/);
  assert.match(t.host, /unidata-nexrad-level2/);
  assert.match(t.size, /0\.2 MB/);
});

test('describeLiveDownload lists every file with its size, the sum, and what each choice means', () => {
  const t = describeLiveDownload(planDownloads(CHECK), 5);
  assert.equal(t.title, 'Download 1 Level II volume for the live map');
  assert.match(t.lines[0], /KTLX: KTLX20260101_120000_V06 \(4\.8 MB\)/);
  assert.match(t.lines[1], /Not available now: KINX/);
  assert.match(t.lines[2], /every 5 min/);
  assert.match(t.lines[2], /Allow once/);
  assert.match(t.size, /4\.8 MB now/);
});

// ── Frames ──────────────────────────────────────────────────────────────────────────────────

const frame = (id, radars, time = null) => ({ id, time, receivedAt: 0, radars });

test('pushFrame: bounded ring, duplicates of the newest frame are not added', () => {
  let frames = [];
  for (let i = 0; i < 15; i++) {
    const r = pushFrame(frames, frame(`f${i}`, [{ station: 'KTLX', name: `v${i}` }]), 12);
    frames = r.frames;
  }
  assert.equal(frames.length, 12);
  assert.deepEqual(frames.map((f) => f.id).slice(0, 2), ['f3', 'f4']);
  const dup = pushFrame(frames, frame('dup', [{ station: 'KTLX', name: 'v14' }]), 12);
  assert.equal(dup.frames.length, 12);
  assert.deepEqual(dup.dropped.map((f) => f.id), ['dup']);
  const r = pushFrame(frames, frame('f15', [{ station: 'KTLX', name: 'v15' }]), 12);
  assert.deepEqual(r.dropped.map((f) => f.id), ['f3']);
  assert.equal(r.frames[r.frames.length - 1].id, 'f15');
});

test('pushFrame keeps at least one frame', () => {
  const r = pushFrame([frame('a', [{ station: 'K', name: '1' }])], frame('b', [{ station: 'K', name: '2' }]), 0);
  assert.deepEqual(r.frames.map((f) => f.id), ['b']);
});

test('stepFrame wraps and uses null for the newest frame', () => {
  assert.equal(stepFrame(null, 0, 1), null);
  assert.equal(stepFrame(null, 3, 1), 0); // after the newest comes the oldest
  assert.equal(stepFrame(0, 3, 1), 1);
  assert.equal(stepFrame(1, 3, 1), null); // index 2 is the newest
  assert.equal(stepFrame(null, 3, -1), 1);
  assert.equal(stepFrame(0, 3, -1), null);
});

// ── Status text ─────────────────────────────────────────────────────────────────────────────

test('radarStatusText states only known facts', () => {
  const now = 1_000_000;
  assert.equal(radarStatusText({ station: 'K', status: 'pending' }, now), 'checking');
  assert.equal(
    radarStatusText({ station: 'K', status: 'ok', scan_time: '2026-01-01T12:01:00Z', age_s: 240, receivedAt: now - 60_000 }, now),
    '12:01 UTC, 5 min old',
  );
  assert.equal(radarStatusText({ station: 'K', status: 'ok' }, now), 'time unknown, age unknown');
  assert.equal(radarStatusText({ station: 'K', status: 'not_found' }, now), 'no recent volume');
  assert.equal(radarStatusText({ station: 'K', status: 'offline' }, now), 'offline');
  assert.equal(radarStatusText({ station: 'K', status: 'error', message: 'boom' }, now), 'failed: boom');
});

test('hhmmss is UTC', () => {
  assert.equal(hhmmss(Date.UTC(2026, 0, 1, 7, 8, 9)), '07:08:09 UTC');
  assert.equal(hhmmss(null), '');
});
