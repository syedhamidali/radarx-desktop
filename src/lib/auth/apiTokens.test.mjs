import test from 'node:test';
import assert from 'node:assert/strict';
import {
  EXPIRY_CHOICES, MAX_ACTIVE_TOKENS, activeCount, cleanTokenName, createProblem, daysLeft, expiresSoon, friendlyTokenError,
  hintText, nameProblem, parseCreated, parseRows, tokenStatus, DEFAULT_DAYS, MAX_DAYS,
} from './apiTokens.ts';

const now = Date.parse('2026-10-20T12:00:00Z');
const row = (over = {}) => ({
  id: 'a', name: 'nightly', hint: 'AbCd', created_at: '2026-10-01T00:00:00Z', expires_at: '2027-01-01T00:00:00Z',
  last_used_at: null, revoked_at: null, ...over,
});

test('names: plain words only, trimmed, 1 to 64 characters', () => {
  assert.equal(cleanTokenName('  nightly job '), 'nightly job');
  assert.equal(cleanTokenName('a.b_c+d-e 1'), 'a.b_c+d-e 1');
  for (const bad of ['', '   ', 'x'.repeat(65), 'a/b', '<b>', 'a\nb', 'né', 'a@b', 'C:\\x']) assert.equal(cleanTokenName(bad), null, bad);
  assert.equal(cleanTokenName('x'.repeat(64)), 'x'.repeat(64));
  assert.equal(nameProblem('ok'), null);
  assert.match(nameProblem(''), /name/);
  assert.match(nameProblem('x'.repeat(65)), /64/);
  assert.match(nameProblem('a/b'), /letters/);
});

test('expiry choices: default 90, longest 365, all within the database range', () => {
  assert.equal(DEFAULT_DAYS, 90);
  assert.equal(MAX_DAYS, 365);
  assert.ok(EXPIRY_CHOICES.some((c) => c.days === DEFAULT_DAYS));
  assert.equal(Math.max(...EXPIRY_CHOICES.map((c) => c.days)), MAX_DAYS);
  assert.ok(EXPIRY_CHOICES.every((c) => c.days >= 1 && c.days <= MAX_DAYS));
});

test('status and counts', () => {
  assert.equal(tokenStatus(row(), now), 'active');
  assert.equal(tokenStatus(row({ expires_at: '2026-10-20T11:59:59Z' }), now), 'expired');
  assert.equal(tokenStatus(row({ revoked_at: '2026-10-05T00:00:00Z' }), now), 'revoked');
  assert.equal(tokenStatus(row({ revoked_at: '2026-10-05T00:00:00Z', expires_at: '2020-01-01T00:00:00Z' }), now), 'revoked');
  const rows = [row(), row({ id: 'b', revoked_at: '2026-10-05T00:00:00Z' }), row({ id: 'c', expires_at: '2026-10-01T00:00:00Z' })];
  assert.equal(activeCount(rows, now), 1);
});

test('the limit of ten active tokens gives a friendly reason', () => {
  const ten = Array.from({ length: MAX_ACTIVE_TOKENS }, (_, i) => row({ id: String(i) }));
  assert.equal(createProblem(ten.slice(1), now), null);
  assert.match(createProblem(ten, now), /10 active tokens/);
  assert.equal(createProblem([...ten, row({ id: 'x', revoked_at: '2026-10-05T00:00:00Z' })].slice(1), now), null);
});

test('days left and the expiry nudge', () => {
  assert.equal(daysLeft('2026-10-21T12:00:00Z', now), 1);
  assert.equal(daysLeft('2026-10-20T12:00:01Z', now), 1);
  assert.equal(daysLeft('2026-10-19T12:00:00Z', now), 0);
  assert.equal(daysLeft('garbage', now), 0);
  assert.equal(expiresSoon(row({ expires_at: '2026-10-25T00:00:00Z' }), now), true);
  assert.equal(expiresSoon(row({ expires_at: '2026-12-25T00:00:00Z' }), now), false);
  assert.equal(expiresSoon(row({ expires_at: '2026-10-25T00:00:00Z', revoked_at: '2026-10-19T00:00:00Z' }), now), false);
});

test('the hint shows four characters and never anything usable', () => {
  assert.equal(hintText({ hint: 'AbCd' }), 'rxd_pat_AbCd…');
});

test('the created token is recognised by its shape, and nothing else passes as one', () => {
  const token = 'rxd_pat_' + 'A'.repeat(43);
  const ok = { id: 'i', token, name: 'n', expires_at: '2027-01-01T00:00:00Z' };
  assert.deepEqual(parseCreated(ok), ok);
  for (const bad of [null, undefined, 'x', 5, [], {}, { ...ok, token: 'rxd_pat_short' }, { ...ok, token: token + 'x' }, { ...ok, id: 5 }, { ...ok, name: null }, { ...ok, expires_at: 5 }]) {
    assert.equal(parseCreated(bad), null, JSON.stringify(bad));
  }
});

test('list rows: malformed ones are dropped, newest first, no token or hash fields survive', () => {
  const rows = parseRows([
    row({ id: 'old', created_at: '2026-01-01T00:00:00Z' }),
    row({ id: 'new', created_at: '2026-10-10T00:00:00Z', token_hash: '\\xdeadbeef', token: 'rxd_pat_x' }),
    null, 5, 'x', { id: 1 }, { id: 'x', name: 'n' },
  ]);
  assert.deepEqual(rows.map((r) => r.id), ['new', 'old']);
  assert.ok(rows.every((r) => !('token_hash' in r) && !('token' in r)));
  assert.deepEqual(parseRows(null), []);
  assert.deepEqual(parseRows({}), []);
});

test('errors become plain sentences, never the database text', () => {
  assert.match(friendlyTokenError({ message: 'at most 10 active access tokens: revoke one first', code: '54000' }), /10 active tokens/);
  assert.match(friendlyTokenError({ message: 'at most 20 access tokens per day', code: '54000' }), /tomorrow/);
  assert.match(friendlyTokenError({ message: 'sign in to create an access token', code: '28000' }), /Sign in again/);
  assert.match(friendlyTokenError({ code: '22023', message: 'x' }), /not allowed/);
  assert.match(friendlyTokenError({ code: 'PGRST202', message: 'Could not find the function' }), /not available/);
  const generic = friendlyTokenError({ message: 'TypeError: Failed to fetch' });
  assert.match(generic, /Could not reach/);
  assert.ok(!generic.includes('TypeError'));
  assert.match(friendlyTokenError(null), /Could not reach/);
});
