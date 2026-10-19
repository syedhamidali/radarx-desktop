// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  niceTicks,
  formatNumber,
  formatPercent,
  signedPercent,
  scatterGeometry,
  seriesGeometry,
  utcMs,
  provenanceLabel,
  uncertaintyText,
  binMinutesFor,
} from './qpeView.ts';

test('nice ticks are 1-2-5 steps that cover the maximum', () => {
  assert.deepEqual(niceTicks(10, 5).map((t) => t.value), [0, 2, 4, 6, 8, 10]);
  assert.deepEqual(niceTicks(47, 4).map((t) => t.value), [0, 20, 40, 60]);
  assert.equal(niceTicks(0).length, 1);
  const t = niceTicks(0.9, 3);
  assert.ok(t[t.length - 1].value >= 0.9);
});

test('numbers are short and never print NaN', () => {
  assert.equal(formatNumber(0), '0');
  assert.equal(formatNumber(12.5), '12.5');
  assert.equal(formatNumber(0.125), '0.13');
  assert.equal(formatNumber(1250.4), '1250');
  assert.equal(formatNumber(NaN), '–');
  assert.equal(formatNumber(null), '–');
  assert.equal(formatPercent(0.873), '87 %');
  assert.equal(signedPercent(-0.182), '-18.2 %');
  assert.equal(signedPercent(0.05), '+5.0 %');
  assert.equal(signedPercent(undefined), '–');
});

test('scatter: equal axes, 1:1 line on the diagonal, values clamped', () => {
  const g = scatterGeometry([1, 5, 20, 8], 220);
  assert.ok(g.max >= 20);
  // the point (v, v) lies on the diagonal: x - left == top + size - y
  for (const v of [0, 3, 12, g.max]) {
    assert.ok(Math.abs(g.x(v) - g.left - (g.top + g.size - g.y(v))) < 1e-9);
  }
  assert.equal(g.x(-5), g.left); // clamped at the axis
  assert.equal(g.y(1e9), g.top);
  assert.deepEqual(scatterGeometry([]).ticks.map((t) => t.value)[0], 0);
});

test('series geometry maps time and amounts into the plot box', () => {
  const rows = [
    { time: '2021-12-11T02:00:00Z', mean_rate_mm_h: 0, cumulative_mm: 0 },
    { time: '2021-12-11T02:30:00Z', mean_rate_mm_h: 10, cumulative_mm: 2.5 },
    { time: '2021-12-11T03:00:00Z', mean_rate_mm_h: 20, cumulative_mm: 10 },
  ];
  const hy = {
    start: ['2021-12-11T02:00:00Z', '2021-12-11T02:30:00Z'],
    end: ['2021-12-11T02:30:00Z', '2021-12-11T03:00:00Z'],
    intensity_mm_h: [5, 15],
  };
  const g = seriesGeometry(rows, hy, 260, 120);
  assert.ok(g);
  assert.equal(g.tMax - g.tMin, 3600_000);
  assert.equal(g.bars.length, 2);
  assert.ok(g.bars[1].h > g.bars[0].h); // 15 mm/h bar is taller than 5 mm/h
  assert.ok(g.ratePath.startsWith('M30.0,'));
  assert.ok(g.rateMax >= 20 && g.cumMax >= 10);
  assert.equal(g.xTicks[1].label, '02:30');
  assert.equal(seriesGeometry(rows.slice(0, 1), hy), null);
});

test('times are UTC with or without the Z', () => {
  assert.equal(utcMs('2021-12-11T02:00:00Z'), utcMs('2021-12-11T02:00:00'));
});

test('an estimate is never called an observation', () => {
  assert.match(provenanceLabel('RETRIEVED'), /RETRIEVED/);
  assert.match(provenanceLabel('ADJUSTED'), /ADJUSTED/);
  assert.match(provenanceLabel(undefined), /RETRIEVED/);
  assert.doesNotMatch(provenanceLabel('RETRIEVED') + provenanceLabel('ADJUSTED'), /observ/i);
  assert.match(uncertaintyText(0.48), /48 %/);
  assert.equal(uncertaintyText(NaN), 'not available');
});

test('the hyetograph bin follows the length of the period', () => {
  assert.equal(binMinutesFor(17 * 60), 5);
  assert.equal(binMinutesFor(3 * 3600), 15);
  assert.equal(binMinutesFor(24 * 3600), 60);
  assert.equal(binMinutesFor(0), 60);
  assert.equal(binMinutesFor(NaN), 60);
});
