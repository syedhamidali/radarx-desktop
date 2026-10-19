// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { chipLevel, chipText, formatValue, whereText, parseHidden, withHidden } from './dataCheckLogic.ts';

test('chip level is the worst severity that is not a fact', () => {
  assert.equal(chipLevel(null), 'none');
  assert.equal(chipLevel({ warning: 0, notice: 0, info: 4 }), 'ok');
  assert.equal(chipLevel({ warning: 0, notice: 2 }), 'notice');
  assert.equal(chipLevel({ warning: 1, notice: 9 }), 'warning');
});

test('chip text counts by severity and says OK when only facts are left', () => {
  assert.equal(chipText({ warning: 1, notice: 5 }, false), 'Data check: 1 warning, 5 notices');
  assert.equal(chipText({ warning: 2, notice: 1 }, false), 'Data check: 2 warnings, 1 notice');
  assert.equal(chipText({ warning: 0, notice: 0, info: 3 }, false), 'Data check: OK');
  assert.equal(chipText(null, true), 'Checking data...');
  assert.equal(chipText(null, false), '');
});

test('evidence values keep three significant digits', () => {
  assert.equal(formatValue(0.4973, 'dB'), '0.497 dB');
  assert.equal(formatValue(46491), '46500');
  assert.equal(formatValue(120000), '1.20e+5');
  assert.equal(formatValue(0.004), '4.00e-3');
  assert.equal(formatValue(0), '0');
  assert.equal(formatValue('S', ''), 'S');
  assert.equal(formatValue(null), 'n/a');
  assert.equal(formatValue(NaN), 'n/a');
});

test('where text names sweeps, azimuth and range, and falls back to the whole file', () => {
  assert.equal(whereText('file', [], null), 'whole file');
  assert.equal(
    whereText('sweep', [1, 3, 5, 6, 7, 8, 9], { azimuth_deg: [11.5, 220.5], range_km: [33.4, 142.9] }),
    'sweeps 1, 3, 5, 6, 7 +2 · azimuth 11.5–220.5° · range 33.4–142.9 km',
  );
  assert.equal(whereText('sweep', [2], { azimuth_deg: [0, 360], range_km: null }), 'sweep 2 · all azimuths');
});

test('the hidden-file list survives bad storage and is capped', () => {
  assert.deepEqual(parseHidden(null), []);
  assert.deepEqual(parseHidden('not json'), []);
  assert.deepEqual(parseHidden('{"a":1}'), []);
  assert.deepEqual(parseHidden('["a", 3, "b"]'), ['a', 'b']);
  assert.deepEqual(withHidden(['a'], 'a'), ['a']);
  assert.deepEqual(withHidden(['a', 'b'], 'c', 2), ['b', 'c']);
});
