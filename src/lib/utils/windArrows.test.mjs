// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { arrowColors } from './wind.ts';

/** Relative luminance of "#rrggbb" or "rgba(r, g, b, a)". */
function luminance(css) {
  let rgb;
  if (css.startsWith('#')) {
    rgb = [1, 3, 5].map((i) => parseInt(css.slice(i, i + 2), 16));
  } else {
    rgb = css.match(/[\d.]+/g).slice(0, 3).map(Number);
  }
  return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255;
}

test('arrows are white on a dark basemap and dark on a light one', () => {
  assert.ok(luminance(arrowColors('dark').fill) > 0.9);
  assert.ok(luminance(arrowColors('light').fill) < 0.15);
});

test('the edge is always the opposite of the fill', () => {
  for (const theme of ['dark', 'light']) {
    const { fill, edge } = arrowColors(theme);
    assert.ok(Math.abs(luminance(fill) - luminance(edge)) > 0.75, theme);
  }
  assert.ok(luminance(arrowColors('dark').edge) < 0.2); // dark edge on white arrows
  assert.ok(luminance(arrowColors('light').edge) > 0.8); // light edge on dark arrows
});
