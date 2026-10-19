// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { COLORMAP_DATA, COLORMAP_NAMES, COLORMAP_CATEGORIES } from './colormaps.ts';

test('every built-in colormap is 256 RGB triplets', () => {
  assert.equal(COLORMAP_NAMES.length, 29);
  for (const name of COLORMAP_NAMES) {
    assert.ok(COLORMAP_DATA[name] instanceof Uint8Array, name);
    assert.equal(COLORMAP_DATA[name].length, 768, name);
  }
});

test('the colormap tables are byte-identical to the original number-list form', () => {
  // sha256 over (name, bytes) of every map, taken from the pre-base64 source.
  const h = createHash('sha256');
  for (const name of COLORMAP_NAMES) {
    h.update(name);
    h.update(COLORMAP_DATA[name]);
  }
  assert.equal(h.digest('hex'), '2546728ef4d604621b0f68e3e8ab20a07ba8ed3bd6eed3f60bff8127a280b79d');
});

test('every categorised colormap exists', () => {
  for (const names of Object.values(COLORMAP_CATEGORIES)) {
    for (const n of names) assert.ok(COLORMAP_DATA[n], n);
  }
});
