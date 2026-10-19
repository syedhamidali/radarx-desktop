// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { trackEngine } from './engineIdentity.ts';

test('the first pong only records the engine', () => {
  assert.deepEqual(trackEngine(null, 'a1'), { engineId: 'a1', restarted: false });
});

test('the same engine after a reconnect is not a restart', () => {
  assert.deepEqual(trackEngine('a1', 'a1'), { engineId: 'a1', restarted: false });
});

test('a different engine id means the sidecar was restarted', () => {
  assert.deepEqual(trackEngine('a1', 'b2'), { engineId: 'b2', restarted: true });
});

test('a pong without an id never reports a restart and keeps the known id', () => {
  for (const bad of [undefined, null, '', 5, {}]) {
    assert.deepEqual(trackEngine('a1', bad), { engineId: 'a1', restarted: false });
  }
});
