// @ts-nocheck - plain node:test file, not part of the app bundle
// With the account gate switched off at build time (RADARX_AUTH_DISABLED=1: UI tests, offline-only builds)
// the app must open at once. It once stayed on "Checking your account..." because the profile state started
// at 'loading' and nothing ever moved it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { initialProfileState, appMayOpen, profileMissing } from './openRule.ts';

const V = '2026-10-21';
const complete = { affiliation: 'NCAR', terms_version: V };

test('a disabled gate starts with a profile that is not waited for', () => {
  assert.equal(initialProfileState(true), 'ready');
  assert.equal(initialProfileState(false), 'loading');
});

test('the state a disabled gate starts in opens the app', () => {
  const s = { gate: 'open', email: null, profileState: initialProfileState(true), profile: null };
  assert.equal(appMayOpen(s, V), true);
  // the regression: the same state with the old initial value stays shut
  assert.equal(appMayOpen({ ...s, profileState: 'loading' }, V), false);
});

test('a closed gate or a loading profile keeps the app shut, a failed profile load does not', () => {
  const base = { email: 'a@b.test', profile: complete };
  assert.equal(appMayOpen({ ...base, gate: 'needs-account', profileState: 'ready' }, V), false);
  assert.equal(appMayOpen({ ...base, gate: 'open', profileState: 'loading' }, V), false);
  assert.equal(appMayOpen({ ...base, gate: 'open', profileState: 'ready' }, V), true);
  assert.equal(appMayOpen({ ...base, gate: 'open-offline', profileState: 'failed' }, V), true);
});

test('a signed-in account missing affiliation or the current terms is held at the extra step', () => {
  const s = { gate: 'open', email: 'a@b.test', profileState: 'ready' };
  assert.equal(profileMissing({ ...s, profile: null }, V), true);
  assert.equal(profileMissing({ ...s, profile: { affiliation: '', terms_version: V } }, V), true);
  assert.equal(profileMissing({ ...s, profile: { affiliation: 'X', terms_version: '2026-10' } }, V), true);
  assert.equal(profileMissing({ ...s, profile: complete }, V), false);
  assert.equal(appMayOpen({ ...s, profile: null }, V), false);
});
