// @ts-nocheck - plain node:test file, not part of the app bundle
import test from 'node:test';
import assert from 'node:assert/strict';
import { makeFlashEnv, flashLight, flashEnd } from './flashes.ts';

test('a flash is deterministic, dark before it starts, bright right after a stroke and gone after its tail', () => {
  const a = makeFlashEnv(7, 100), b = makeFlashEnv(7, 100), c = makeFlashEnv(8, 100);
  assert.deepEqual(a, b);
  assert.notDeepEqual(a.strokes, c.strokes);
  assert.equal(flashLight(a, 99.9), 0);
  const peak = Math.max(...[0, 0.02, 0.05, 0.1, 0.2].map((x) => flashLight(a, 100 + x)));
  assert.ok(peak > 0.15 && peak <= 1.2, `peak ${peak}`);
  assert.ok(flashLight(a, flashEnd(a) + 0.5) < 0.01);
  assert.ok(a.strokes.length >= 2 && a.strokes.length <= 4);
});
