// @ts-nocheck - plain node:test file, not part of the app bundle
import test from 'node:test';
import assert from 'node:assert/strict';
import { History } from './history.ts';

test('undo and redo walk back and forth', () => {
  const h = new History(100, 800);
  h.push('a', null, 0); // a -> b
  h.push('b', null, 10_000); // b -> c
  assert.equal(h.undo('c'), 'b');
  assert.equal(h.undo('b'), 'a');
  assert.equal(h.undo('a'), undefined);
  assert.equal(h.redo('a'), 'b');
  assert.equal(h.redo('b'), 'c');
  assert.equal(h.redo('c'), undefined);
});

test('a new edit after an undo drops the redo branch', () => {
  const h = new History();
  h.push('a', null, 0);
  assert.equal(h.undo('b'), 'a');
  assert.equal(h.canRedo, true);
  h.push('a', null, 5_000);
  assert.equal(h.canRedo, false);
});

test('edits of one gesture (same key, close in time) undo in one step', () => {
  const h = new History(100, 800);
  h.push('v1', 'p:threshold', 0);
  h.push('v2', 'p:threshold', 200);
  h.push('v3', 'p:threshold', 500);
  assert.equal(h.undo('v4'), 'v1');
  assert.equal(h.canUndo, false);
});

test('a different key or a pause starts a new step', () => {
  const h = new History(100, 800);
  h.push('v1', 'p:a', 0);
  h.push('v2', 'p:b', 100);
  h.push('v3', 'p:b', 5_000);
  assert.equal(h.undo('v4'), 'v3');
  assert.equal(h.undo('v3'), 'v2');
  assert.equal(h.undo('v2'), 'v1');
});

test('history is bounded', () => {
  const h = new History(3, 0);
  for (let i = 0; i < 10; i++) h.push(i, null, i * 10_000);
  let n = 0;
  let cur = 10;
  for (let v = h.undo(cur); v !== undefined; v = h.undo(cur)) {
    cur = v;
    n++;
  }
  assert.equal(n, 3);
  assert.equal(cur, 7);
});

test('clear forgets everything', () => {
  const h = new History();
  h.push('a');
  h.clear();
  assert.equal(h.canUndo, false);
  assert.equal(h.canRedo, false);
});
