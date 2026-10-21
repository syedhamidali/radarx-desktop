// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { nightLightingText, soundRowLayout, facingStorm } from './clipLabels.ts';

test('the night line names modelled lightning only when modelled flashes are drawn', () => {
  assert.match(nightLightingText('modelled'), /lit by modelled lightning/);
  assert.doesNotMatch(nightLightingText('off'), /modelled/i);
  assert.match(nightLightingText('off'), /no lightning is drawn/);
  assert.doesNotMatch(nightLightingText('observed'), /modelled/i);
  assert.match(nightLightingText('observed'), /observed flash places/);
});

test('the sound rows sit above the rows already drawn, top row first, and lift the progress bar', () => {
  const both = soundRowLayout(2, true, true);
  assert.deepEqual(both.fromBottom, [96, 76]);
  assert.equal(both.lift, 91);
  const only = soundRowLayout(2, false, false);
  assert.deepEqual(only.fromBottom, [56, 36]);
  assert.equal(only.lift, 51);
  const none = soundRowLayout(0, true, true);
  assert.deepEqual(none.fromBottom, []);
  assert.equal(none.lift, 50, 'no sound rows: the progress bar stays where it always was');
  assert.deepEqual(soundRowLayout(2, false, true).fromBottom, [76, 56]);
});

test('a virtual listener faces the storm centre (bearing clockwise from north)', () => {
  assert.equal(facingStorm(0, 0, 0, 10), 0);
  assert.equal(facingStorm(0, 0, 10, 0), 90);
  assert.equal(facingStorm(0, 0, 0, -10), 180);
  assert.equal(facingStorm(0, 0, -10, 0), 270);
  assert.ok(Math.abs(facingStorm(3, 4, 6, 8) - 36.8699) < 1e-3);
  assert.equal(facingStorm(1, 1, 1, 1), 0, 'standing on the storm centre: no bearing, north');
});
