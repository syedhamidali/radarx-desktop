// @ts-nocheck - plain node:test file, not part of the app bundle
import test from 'node:test';
import assert from 'node:assert/strict';
import { mapPixel, panelHeight, insetHeight } from './perilsFocusInset.ts';

test('a km position is a pixel of the map: the centre in the middle, north up, east right, the scale from the half-width', () => {
  // a 400 px map whose top left is (1000, 50), centred on (12, -48) km, 10 km half-width: 20 px per km
  const c = mapPixel(12, -48, 1000, 50, 400, 12, -48, 10);
  assert.deepEqual(c, [1200, 250]);
  const e = mapPixel(13, -48, 1000, 50, 400, 12, -48, 10);
  assert.equal(e[0], 1220);
  assert.equal(e[1], 250);
  const n = mapPixel(12, -47, 1000, 50, 400, 12, -48, 10);
  assert.equal(n[0], 1200);
  assert.equal(n[1], 230); // north is up
  const corner = mapPixel(2, -38, 1000, 50, 400, 12, -48, 10);
  assert.deepEqual(corner, [1000, 50]);
});

test('the two panels fit in the frame at the largest size, and the compact ones are smaller', () => {
  const u = 1;
  assert.ok(52 + insetHeight(400, u, false) < 1080);
  assert.ok(insetHeight(288, u, true) < insetHeight(400, u, false));
  assert.ok(panelHeight(400, u, false) > 400);
  assert.equal(insetHeight(400, u, false), 2 * panelHeight(400, u, false) + 10 * u);
});
