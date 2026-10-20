// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { movePanel } from './panelOrder.ts';

test('movePanel swaps a panel with its neighbour', () => {
  assert.deepEqual(movePanel(['a', 'b', 'c'], 'b', -1), ['b', 'a', 'c']);
  assert.deepEqual(movePanel(['a', 'b', 'c'], 'b', 1), ['a', 'c', 'b']);
});

test('movePanel does nothing past either end or for an unknown id, and never mutates its input', () => {
  const order = ['a', 'b', 'c'];
  assert.deepEqual(movePanel(order, 'a', -1), ['a', 'b', 'c']);
  assert.deepEqual(movePanel(order, 'c', 1), ['a', 'b', 'c']);
  assert.deepEqual(movePanel(order, 'zzz', 1), ['a', 'b', 'c']);
  assert.deepEqual(order, ['a', 'b', 'c']);
});
