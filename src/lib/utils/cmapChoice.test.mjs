// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { parseOverrides, resolveCmap, withChoice } from './cmapChoice.ts';
import { buildTf, recolorTf } from './volumeTf.ts';

const def = (v) => (v.startsWith('DBZ') ? 'NWSRef' : 'turbo');
const exists = (n) => ['NWSRef', 'turbo', 'viridis', 'magma'].includes(n);

test('the user choice wins over the default, per variable', () => {
  const o = { DBZH: 'magma' };
  assert.equal(resolveCmap('DBZH', o, def, exists), 'magma');
  assert.equal(resolveCmap('ZDR', o, def, exists), 'turbo');
});

test('a choice that no longer exists falls back to the default', () => {
  assert.equal(resolveCmap('DBZH', { DBZH: 'gone' }, def, exists), 'NWSRef');
});

test('categorical products always keep their class colours', () => {
  assert.equal(resolveCmap('HCA', { HCA: 'viridis' }, def, exists, true), 'turbo');
});

test('choosing the default clears the entry; other choices are stored', () => {
  let o = withChoice({}, 'DBZH', 'viridis', 'NWSRef');
  assert.deepEqual(o, { DBZH: 'viridis' });
  o = withChoice(o, 'DBZH', 'NWSRef', 'NWSRef');
  assert.deepEqual(o, {});
});

test('persisted JSON: garbage is ignored', () => {
  assert.deepEqual(parseOverrides('{"DBZH":"viridis","bad":3}'), { DBZH: 'viridis' });
  assert.deepEqual(parseOverrides('not json'), {});
  assert.deepEqual(parseOverrides('[1]'), {});
  assert.deepEqual(parseOverrides(null), {});
});

test('recolouring a transfer function keeps the opacity curve', () => {
  const grey = new Uint8Array(256 * 3).fill(100);
  const red = new Uint8Array(256 * 3);
  for (let i = 0; i < 256; i++) red[i * 3] = 255;
  const tf = buildTf('reflectivity', grey, -20, 75);
  tf[50 * 4 + 3] = 77; // a user-edited point
  const out = recolorTf(tf, red);
  for (let i = 0; i < 256; i++) {
    assert.equal(out[i * 4], 255);
    assert.equal(out[i * 4 + 1], 0);
    assert.equal(out[i * 4 + 3], tf[i * 4 + 3]);
  }
  assert.equal(out[50 * 4 + 3], 77);
  assert.equal(tf[0], 100, 'the input is not modified');
});
