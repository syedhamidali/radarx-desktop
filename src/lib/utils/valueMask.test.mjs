// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  NO_MASK,
  normalizeMask,
  isMaskActive,
  maskTest,
  maskPredicate,
  maskDescription,
  maskPresets,
  variableKind,
  maskOf,
} from '../stores/valueMask.ts';

const mk = (mode, min, max, inclusive = false) => ({ mode, min, max, inclusive });

test('off and incomplete masks hide nothing', () => {
  for (const v of [-50, 0, 12.5, 90]) assert.equal(maskTest(v, NO_MASK), false);
  assert.equal(maskTest(5, null), false);
  assert.equal(maskTest(5, mk('below', null, null)), false);
  assert.equal(maskTest(5, mk('between', 1, null)), false);
  assert.equal(isMaskActive(mk('above', null, 3)), true);
  assert.equal(isMaskActive(mk('above', 3, null)), false); // above needs max
  assert.equal(isMaskActive(mk('off', 1, 2)), false);
  assert.equal(maskPredicate(NO_MASK), null);
  assert.equal(maskPredicate(mk('below', 0, null))(-1), true);
});

test('below / above', () => {
  const below = mk('below', 0, null);
  assert.equal(maskTest(-0.1, below), true);
  assert.equal(maskTest(0, below), false);
  assert.equal(maskTest(0.1, below), false);
  assert.equal(maskTest(0, mk('below', 0, null, true)), true);
  const above = mk('above', null, 50);
  assert.equal(maskTest(50.1, above), true);
  assert.equal(maskTest(50, above), false);
  assert.equal(maskTest(50, mk('above', null, 50, true)), true);
  assert.equal(maskTest(10, above), false);
});

test('between hides the inside, outside hides the rest', () => {
  const between = mk('between', 20, 40);
  assert.deepEqual([10, 20, 30, 40, 50].map((v) => maskTest(v, between)), [false, false, true, false, false]);
  assert.deepEqual([10, 20, 30, 40, 50].map((v) => maskTest(v, mk('between', 20, 40, true))), [false, true, true, true, false]);
  const outside = mk('outside', 20, 40);
  assert.deepEqual([10, 20, 30, 40, 50].map((v) => maskTest(v, outside)), [true, false, false, false, true]);
  assert.deepEqual([10, 20, 30, 40, 50].map((v) => maskTest(v, mk('outside', 20, 40, true))), [true, true, false, true, true]);
  // limits given the wrong way round still mean the same range
  assert.equal(maskTest(30, mk('between', 40, 20)), true);
  assert.equal(maskTest(30, mk('outside', 40, 20)), false);
});

test('missing values are never "masked"', () => {
  for (const mask of [mk('below', 0, null), mk('above', null, 5), mk('between', 1, 2), mk('outside', 1, 2)]) {
    assert.equal(maskTest(NaN, mask), false);
    assert.equal(maskTest(null, mask), false);
    assert.equal(maskTest(undefined, mask), false);
    assert.equal(maskTest(Infinity, mask), false);
  }
});

test('normalising settings from storage or a form', () => {
  assert.deepEqual(normalizeMask(null), NO_MASK);
  assert.deepEqual(normalizeMask({ mode: 'bogus', min: 'x' }), NO_MASK);
  assert.deepEqual(normalizeMask({ mode: 'below', min: '5', max: '', inclusive: true }), mk('below', 5, null, true));
  assert.deepEqual(normalizeMask({ mode: 'between', min: 1, max: NaN, inclusive: 'yes' }), mk('between', 1, null, false));
  assert.equal(maskOf({ DBZH: mk('below', 0, null) }, 'DBZH').mode, 'below');
  assert.equal(maskOf({}, 'DBZH'), NO_MASK);
  assert.equal(maskOf({}, null), NO_MASK);
});

test('descriptions read as plain sentences', () => {
  assert.equal(maskDescription(NO_MASK, 'dBZ'), null);
  assert.equal(maskDescription(mk('below', 0, null), 'dBZ'), 'hide below 0 dBZ');
  assert.equal(maskDescription(mk('below', 0, null, true), 'dBZ'), 'hide 0 dBZ and below');
  assert.equal(maskDescription(mk('above', null, 50.5), 'dBZ'), 'hide above 50.5 dBZ');
  assert.equal(maskDescription(mk('above', null, 50, true), 'dBZ'), 'hide 50 dBZ and above');
  assert.equal(maskDescription(mk('between', 20, 40), 'dBZ'), 'hide between 20 and 40 dBZ');
  assert.equal(maskDescription(mk('between', 40, 20, true), 'dBZ'), 'hide 20 to 40 dBZ');
  assert.equal(maskDescription(mk('outside', -2, 6), 'dB'), 'hide outside -2 to 6 dB');
  assert.equal(maskDescription(mk('below', 0.7, null)), 'hide below 0.7');
  assert.equal(maskDescription(mk('below', 1 / 3, null), ''), 'hide below 0.333');
});

test('variable kinds and their presets', () => {
  const kinds = {
    DBZH: 'reflectivity', DBZ: 'reflectivity', DBZ_TOT: 'reflectivity', DZ: 'reflectivity', reflectivity: 'reflectivity',
    VRADH: 'velocity', VEL: 'velocity', VEL1: 'velocity', VT: 'velocity',
    WRADH: 'width', WIDTH: 'width',
    ZDR: 'zdr', RHOHV: 'rhohv', PHIDP: 'phidp', KDP: 'kdp', SQI: 'other',
  };
  for (const [name, kind] of Object.entries(kinds)) assert.equal(variableKind(name), kind, name);
  const refl = maskPresets('DBZH', 'dBZ');
  assert.deepEqual(refl.map((p) => p.mask.min), [0, 10, 20]);
  assert.ok(refl.every((p) => p.mask.mode === 'below'));
  assert.equal(refl[0].label, '< 0 dBZ');
  const vel = maskPresets('VRADH', 'm/s');
  assert.equal(vel[0].mask.mode, 'between');
  assert.equal(maskTest(1.5, vel[0].mask), true); // |V| < 2 hidden
  assert.equal(maskTest(-1.5, vel[0].mask), true);
  assert.equal(maskTest(3, vel[0].mask), false);
  assert.equal(maskPresets('SQI').length, 0);
  assert.ok(maskPresets('RHOHV').length >= 2);
  assert.ok(maskPresets('ZDR', 'dB').length >= 2);
  // every preset is a complete mask
  for (const name of Object.keys(kinds)) for (const p of maskPresets(name, 'u')) assert.ok(isMaskActive(p.mask), `${name} ${p.label}`);
});
