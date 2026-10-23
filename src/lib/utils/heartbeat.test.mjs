// @ts-nocheck - plain node:test file, not part of the app bundle
import test from 'node:test';
import assert from 'node:assert/strict';
import { Heartbeat } from './heartbeat.ts';

test('answers within the limit never warn', () => {
  const h = new Heartbeat(30_000, 0);
  for (let t = 10_000; t <= 120_000; t += 10_000) {
    assert.equal(h.tick(t), null);
    assert.equal(h.pong(t), null);
  }
});

test('no pong for longer than the limit warns exactly once', () => {
  const h = new Heartbeat(30_000, 0);
  assert.equal(h.tick(20_000), null);
  assert.equal(h.tick(30_000), 'silent');
  assert.equal(h.tick(40_000), null);
  assert.equal(h.tick(50_000), null);
  assert.equal(h.isSilent, true);
});

test('a pong after the warning reports the recovery once', () => {
  const h = new Heartbeat(30_000, 0);
  h.tick(35_000);
  assert.equal(h.pong(36_000), 'recovered');
  assert.equal(h.pong(37_000), null);
  assert.equal(h.tick(40_000), null);
  // and it can warn again later
  assert.equal(h.tick(70_000), 'silent');
});

test('reset (a new connection) clears a pending warning', () => {
  const h = new Heartbeat(30_000, 0);
  h.tick(31_000);
  h.reset(32_000);
  assert.equal(h.isSilent, false);
  assert.equal(h.tick(40_000), null);
});
