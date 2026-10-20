// Tests of activity.ts under plain Node, with fake dependencies.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { checkEvent, handleLog, LIMITS } from './activity.ts';

const allowlist = JSON.parse(readFileSync(new URL('./allowlist.json', import.meta.url), 'utf8'));
const USER = '11111111-2222-3333-4444-555555555555';
const DEVICE = 'abcdef1234567890';
const event = (over = {}) => ({ event: 'command', surface: 'cli', device_id: DEVICE, app_version: '0.3.0', os: 'Linux', detail: { command: 'info' }, ...over });

function deps(over = {}) {
  const seen = { rows: [], rate: [] };
  return {
    seen,
    verify: async (token) => { if (token !== 'good-token-0123456789abcdef') throw new Error('bad'); return { sub: USER }; },
    rateHit: async (bucket, limit, window) => { seen.rate.push({ bucket, limit, window }); return true; },
    insert: async (rows) => { seen.rows.push(rows); return { status: 201, code: '' }; },
    ...over,
  };
}
const call = (body, over = {}) => ({ method: 'POST', authorization: 'Bearer good-token-0123456789abcdef', body, ...over });

test('every allowed event name with its allowed keys is accepted', () => {
  for (const [name, keys] of Object.entries(allowlist.events)) {
    const detail = Object.fromEntries(keys.map((k) => [k, 'plain word 1']));
    const checked = checkEvent(event({ event: name, detail }), USER);
    assert.equal(checked.ok, true, name);
    assert.equal(checked.row.user_id, USER);
    assert.deepEqual(checked.row.detail, detail);
  }
});

test('names, keys and values outside the lists are refused', () => {
  const bad = [
    event({ event: 'something_else' }), event({ event: '' }), event({ event: 'constructor' }), event({ event: '__proto__' }),
    event({ event: 'command', detail: { module: 'x' } }), event({ event: 'sign_in', detail: { command: 'x' } }),
    event({ detail: { command: 'a/b' } }), event({ detail: { command: 'C:\\x' } }), event({ detail: { command: 'me@example.org' } }),
    event({ detail: { command: 'a.b' } }), event({ detail: { command: '../x' } }), event({ detail: { command: 'x\n' } }),
    event({ detail: { command: 'https://example.org' } }), event({ detail: { command: 'x'.repeat(65) } }), event({ detail: { command: '' } }),
    event({ detail: { command: 5 } }), event({ detail: { command: null } }), event({ detail: { command: ['a'] } }),
    event({ detail: 'info' }), event({ detail: ['info'] }), event({ detail: null }),
    event({ surface: 'web' }), event({ surface: undefined }), event({ device_id: 'short' }), event({ device_id: 'has space 12345' }),
    event({ device_id: undefined }), event({ app_version: 'v1' }), event({ app_version: '1.2.3.4.5' }), event({ app_version: 'x'.repeat(41) }),
    event({ os: 'BeOS' }), event({ os: 5 }), event({ occurred_at: 'yesterday' }), event({ occurred_at: 5 }),
    event({ occurred_at: 'x'.repeat(50) }), event({ extra: 'field' }), event({ user_id: 'someone-else' }), event({ id: 7 }),
    null, 'text', 5, [], undefined,
  ];
  for (const e of bad) assert.equal(checkEvent(e, USER).ok, false, JSON.stringify(e));
});

test('the detail size cap and the version "unknown" match the database', () => {
  assert.equal(checkEvent(event({ app_version: 'unknown' }), USER).ok, true);
  assert.equal(checkEvent(event({ app_version: '0.3.0-rc1' }), USER).ok, true);
  assert.equal(checkEvent(event({ app_version: '12' }), USER).ok, true);
  assert.equal(checkEvent(event({ app_version: null, os: null }), USER).ok, true);
  const long = { command: 'x'.repeat(64), other: 'y'.repeat(64), more: 'z'.repeat(64), again: 'w'.repeat(64) };
  assert.equal(checkEvent(event({ detail: long }), USER).ok, false);
});

test('the row always carries the token\'s own user, whatever the body says', async () => {
  const d = deps();
  const out = await handleLog(call({ events: [event()], user_id: 'someone-else' }), d);
  assert.equal(out.status, 200);
  assert.equal(d.seen.rows[0][0].user_id, USER);
  const refused = await handleLog(call({ events: [event({ user_id: 'someone-else' })] }), d);
  assert.equal(refused.status, 400);
});

test('only the known columns are sent on, and occurred_at is always present', async () => {
  const d = deps();
  await handleLog(call({ events: [event(), event({ occurred_at: '2026-10-21T10:00:00.000Z' })] }), d);
  const [rows] = d.seen.rows;
  for (const row of rows) {
    assert.deepEqual(Object.keys(row).sort(), ['app_version', 'detail', 'device_id', 'event', 'occurred_at', 'os', 'surface', 'user_id']);
    assert.ok(Number.isFinite(Date.parse(row.occurred_at)));
  }
  assert.equal(rows[1].occurred_at, '2026-10-21T10:00:00.000Z');
});

test('authorisation: a missing, malformed, expired or foreign token is 401 before anything else happens', async () => {
  const d = deps();
  for (const authorization of [null, '', 'good-token-0123456789abcdef', 'Basic abc', 'Bearer ', 'Bearer short', 'Bearer wrong-token-0123456789abc', 'Bearer ' + 'x'.repeat(5000)]) {
    const out = await handleLog(call({ events: [event()] }, { authorization }), d);
    assert.equal(out.status, 401, String(authorization));
    assert.deepEqual(out.body, { error: 'invalid_token' });
  }
  assert.equal(d.seen.rows.length, 0);
  assert.equal(d.seen.rate.length, 0);
});

test('only POST', async () => {
  const d = deps();
  assert.equal((await handleLog(call({ events: [event()] }, { method: 'GET' }), d)).status, 405);
});

test('batch rules: 1 to 100 events, an array, and one bad row refuses the whole batch like the table', async () => {
  const d = deps();
  for (const body of [null, {}, { events: [] }, { events: 'x' }, { events: {} }, { events: Array.from({ length: allowlist.max_batch + 1 }, () => event()) }]) {
    assert.equal((await handleLog(call(body), d)).status, 400);
  }
  assert.equal((await handleLog(call({ events: Array.from({ length: allowlist.max_batch }, () => event()) }), d)).status, 200);
  const before = d.seen.rows.length;
  const mixed = await handleLog(call({ events: [event(), event({ event: 'nope' }), event()] }), d);
  assert.equal(mixed.status, 400);
  assert.equal(mixed.body.error, 'invalid_event');
  assert.equal(d.seen.rows.length, before, 'nothing of a refused batch is inserted');
});

test('a per-account call limit applies', async () => {
  const d = deps({ rateHit: async (bucket, limit, window) => { d.seen.rate.push({ bucket, limit, window }); return false; } });
  const out = await handleLog(call({ events: [event()] }), d);
  assert.equal(out.status, 429);
  assert.equal(d.seen.rows.length, 0);
  assert.deepEqual(d.seen.rate[0], { bucket: 'act:' + USER, limit: LIMITS.callsPerHour, window: LIMITS.windowSeconds });
});

test('the database\'s answers map to the ones the Python client understands', async () => {
  const cases = [
    [{ status: 400, code: '23514' }, 400], [{ status: 409, code: '23505' }, 400], [{ status: 400, code: 'P0001' }, 429],
    [{ status: 500, code: '' }, 503], [{ status: 503, code: '' }, 503],
  ];
  for (const [insertResult, expected] of cases) {
    const d = deps({ insert: async () => insertResult });
    assert.equal((await handleLog(call({ events: [event()] }), d)).status, expected, JSON.stringify(insertResult));
  }
});

test('the allow-list file is complete and well-formed', () => {
  assert.ok(Object.keys(allowlist.events).length >= 12);
  assert.deepEqual(allowlist.surfaces, ['app', 'cli', 'api', 'engine']);
  assert.ok(allowlist.max_batch === 100 && allowlist.max_detail_bytes === 200 && allowlist.max_event_name === 40);
  for (const key of ['value_pattern', 'device_pattern', 'version_pattern']) assert.doesNotThrow(() => new RegExp(allowlist[key]));
  // no event may carry a detail key that could hold free text
  const allowedKeys = new Set(['command', 'module', 'format', 'type', 'class']);
  for (const keys of Object.values(allowlist.events)) for (const k of keys) assert.ok(allowedKeys.has(k), k);
});
