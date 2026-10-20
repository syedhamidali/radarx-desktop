// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { isValidRange, safeRange, applyTypedLimit, colorbarTicks } from './colorRange.ts';

test('isValidRange: finite and strictly increasing', () => {
  assert.equal(isValidRange(-20, 75), true);
  assert.equal(isValidRange(5, 5), false);
  assert.equal(isValidRange(10, 0), false);
  assert.equal(isValidRange(NaN, 1), false);
  assert.equal(isValidRange(0, Infinity), false);
  assert.equal(isValidRange(-Infinity, 0), false);
});

test('safeRange keeps a valid range byte for byte and repairs the rest', () => {
  assert.deepEqual(safeRange(-20, 75), [-20, 75]);
  assert.deepEqual(safeRange(0.1, 0.30000000000000004), [0.1, 0.30000000000000004]);
  // constant field: a unit span around the value, never a zero divisor
  assert.deepEqual(safeRange(5, 5), [4.5, 5.5]);
  assert.deepEqual(safeRange(10, 0), [4.5, 5.5]);
  assert.deepEqual(safeRange(NaN, NaN), [0, 1]);
  assert.deepEqual(safeRange(3, NaN), [3, 4]);
  assert.deepEqual(safeRange(NaN, 3), [2, 3]);
  assert.deepEqual(safeRange(-Infinity, 3), [2, 3]);
  for (const [a, b] of [[5, 5], [NaN, 0], [Infinity, Infinity], [1, -1]]) {
    const [lo, hi] = safeRange(a, b);
    assert.ok(hi - lo > 0 && Number.isFinite(hi - lo));
  }
});

test('applyTypedLimit rejects non-finite and empty ranges', () => {
  assert.deepEqual(applyTypedLimit('-30', 'vmin', -20, 75), [-30, 75]);
  assert.deepEqual(applyTypedLimit(' 60.5 ', 'vmax', -20, 75), [-20, 60.5]);
  assert.deepEqual(applyTypedLimit('0,5', 'vmin', -20, 75), [0.5, 75]);
  // JS parses these as numbers; they would make the shader divide by infinity / NaN
  assert.equal(applyTypedLimit('-Infinity', 'vmin', -20, 75), null);
  assert.equal(applyTypedLimit('Infinity', 'vmax', -20, 75), null);
  assert.equal(applyTypedLimit('1e999', 'vmax', -20, 75), null);
  assert.equal(applyTypedLimit('NaN', 'vmin', -20, 75), null);
  assert.equal(applyTypedLimit('', 'vmin', -20, 75), null);
  assert.equal(applyTypedLimit('abc', 'vmin', -20, 75), null);
  // empty or inverted
  assert.equal(applyTypedLimit('75', 'vmin', -20, 75), null);
  assert.equal(applyTypedLimit('80', 'vmin', -20, 75), null);
  assert.equal(applyTypedLimit('-20', 'vmax', -20, 75), null);
});

test('colorbarTicks: labels carry the decimals their spacing needs', () => {
  const labels = (a, b) => colorbarTicks(a, b).map((t) => t.label);
  assert.deepEqual(labels(-20, 75), ['-20', '3.75', '27.5', '51.25', '75']); // was -20, 3.8, 27.5, 51.3, 75
  assert.deepEqual(labels(0, 1), ['0', '0.25', '0.5', '0.75', '1']); // was 0, 0.3, 0.5, 0.8, 1
  assert.deepEqual(labels(0, 100), ['0', '25', '50', '75', '100']);
  assert.deepEqual(labels(-30, 30), ['-30', '-15', '0', '15', '30']);
  // narrow ranges (rain rate, KDP) must not collapse to five identical labels
  assert.equal(new Set(labels(0, 0.02)).size, 5);
  assert.deepEqual(colorbarTicks(0, 100).map((t) => t.pos), [0, 25, 50, 75, 100]);
  assert.deepEqual(colorbarTicks(5, 5), []);
  assert.deepEqual(colorbarTicks(NaN, 5), []);
});
