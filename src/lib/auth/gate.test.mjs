import test from 'node:test';
import assert from 'node:assert/strict';
import { enabledProviders } from './config.ts';
import { gateState, graceDaysLeft, looksLikeEmail, passwordProblem, friendlyAuthError } from './gate.ts';

const DAY = 86_400_000;
const now = 1_800_000_000_000;
const base = { hasSession: true, lastVerifiedAt: now, now, online: true, graceDays: 30 };

test('first run needs an account', () => {
  assert.equal(gateState({ ...base, hasSession: false }), 'needs-account');
  assert.equal(gateState({ ...base, hasSession: false, online: false }), 'needs-account');
});
test('signed in and online opens', () => assert.equal(gateState(base), 'open'));
test('offline inside the grace period opens', () => {
  assert.equal(gateState({ ...base, online: false, lastVerifiedAt: now - 29 * DAY }), 'open-offline');
  assert.equal(gateState({ ...base, online: false, lastVerifiedAt: now - 30 * DAY }), 'open-offline');
});
test('offline past the grace period asks to reconnect', () => {
  assert.equal(gateState({ ...base, online: false, lastVerifiedAt: now - 31 * DAY }), 'reconnect');
  assert.equal(gateState({ ...base, online: false, lastVerifiedAt: null }), 'reconnect');
});
test('a clock moved far backwards does not extend the grace period', () => {
  assert.equal(gateState({ ...base, online: false, lastVerifiedAt: now + 40 * DAY }), 'reconnect');
});
test('days left', () => {
  assert.equal(graceDaysLeft(now - 10 * DAY, now, 30), 20);
  assert.equal(graceDaysLeft(now - 40 * DAY, now, 30), 0);
  assert.equal(graceDaysLeft(null, now, 30), 0);
});
test('email and password checks', () => {
  assert.ok(looksLikeEmail('a@b.co'));
  assert.ok(!looksLikeEmail('a@b'));
  assert.ok(!looksLikeEmail('a b@c.de'));
  assert.equal(passwordProblem('abc12345'), null);
  assert.ok(passwordProblem('short1'));
  assert.ok(passwordProblem('onlyletters'));
});
test('errors become sentences', () => {
  assert.match(friendlyAuthError('Invalid login credentials'), /do not match/);
  assert.match(friendlyAuthError('User already registered'), /already an account/);
  assert.match(friendlyAuthError('TypeError: Failed to fetch'), /Cannot reach/);
  assert.equal(friendlyAuthError(''), 'Something went wrong. Please try again.');
});
test('social buttons appear only for providers that were listed', () => {
  assert.deepEqual(enabledProviders(undefined), []);
  assert.deepEqual(enabledProviders(''), []);
  assert.deepEqual(enabledProviders('google, Apple').map((p) => p.id), ['google', 'apple']);
  assert.deepEqual(enabledProviders('myspace'), []);
});
