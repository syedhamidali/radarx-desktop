// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { clampSidebarWidth, sidebarWidthAfterKey, SIDEBAR_MIN, SIDEBAR_MAX, SIDEBAR_DEFAULT } from './sidebarResize.ts';

test('arrow keys move the inner edge: left sidebar widens to the right, right sidebar to the left', () => {
  assert.equal(sidebarWidthAfterKey('left', 'ArrowRight', false, 300), 316);
  assert.equal(sidebarWidthAfterKey('left', 'ArrowLeft', false, 300), 284);
  assert.equal(sidebarWidthAfterKey('right', 'ArrowLeft', false, 300), 316);
  assert.equal(sidebarWidthAfterKey('right', 'ArrowRight', false, 300), 284);
  assert.equal(sidebarWidthAfterKey('left', 'ArrowRight', true, 300), 364);
});

test('widths stay within the limits', () => {
  assert.equal(sidebarWidthAfterKey('left', 'ArrowRight', true, SIDEBAR_MAX - 10), SIDEBAR_MAX);
  assert.equal(sidebarWidthAfterKey('left', 'ArrowLeft', true, SIDEBAR_MIN + 10), SIDEBAR_MIN);
  assert.equal(clampSidebarWidth(NaN), SIDEBAR_DEFAULT);
  assert.equal(clampSidebarWidth(-5), SIDEBAR_MIN);
  assert.equal(clampSidebarWidth(9999), SIDEBAR_MAX);
});

test('Home / Enter reset, End widens, other keys are left alone', () => {
  assert.equal(sidebarWidthAfterKey('left', 'Home', false, 420), SIDEBAR_DEFAULT);
  assert.equal(sidebarWidthAfterKey('right', 'Enter', false, 250), SIDEBAR_DEFAULT);
  assert.equal(sidebarWidthAfterKey('right', 'End', false, 250), SIDEBAR_MAX);
  assert.equal(sidebarWidthAfterKey('left', 'a', false, 300), null);
  assert.equal(sidebarWidthAfterKey('left', 'Tab', false, 300), null);
  assert.equal(sidebarWidthAfterKey('left', 'ArrowUp', false, 300), null);
});
