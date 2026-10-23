// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { basinPath, labelPoint, colorOnLegend, basinMean, isClick, basinTitle, polygonsOf } from './basinMap.ts';

const SQUARE = { type: 'Polygon', coordinates: [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]] };
const WITH_HOLE = {
  type: 'Polygon',
  coordinates: [
    [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
    [[4, 4], [4, 6], [6, 6], [6, 4], [4, 4]],
  ],
};
// lon, lat -> pixels: 10 px per degree, y down
const project = (lat, lon) => [lon * 10, 100 - lat * 10];

test('a polygon becomes one closed sub-path per ring, in screen pixels', () => {
  assert.equal(basinPath(SQUARE, project), 'M0.0,100.0L100.0,100.0L100.0,0.0L0.0,0.0L0.0,100.0Z');
  const d = basinPath(WITH_HOLE, project);
  assert.equal(d.split('Z').filter(Boolean).length, 2);
  assert.ok(d.includes('M40.0,60.0'));
});

test('a multipolygon keeps every part and the label sits on the largest one', () => {
  const g = {
    type: 'MultiPolygon',
    coordinates: [SQUARE.coordinates, [[[20, 0], [21, 0], [21, 1], [20, 1], [20, 0]]]],
  };
  assert.equal(polygonsOf(g).length, 2);
  assert.equal(basinPath(g, project).split('Z').filter(Boolean).length, 2);
  assert.deepEqual(labelPoint(g), [5, 5]);
  assert.equal(labelPoint({ type: 'Polygon', coordinates: [[]] }), null);
});

test('the colour of a basin comes from the legend of the map layer', () => {
  const legend = [[0, '#000000'], [10, '#ff0000'], [20, '#ffffff']];
  assert.equal(colorOnLegend(0, legend), '#000000');
  assert.equal(colorOnLegend(5, legend), 'rgb(128,0,0)');
  assert.equal(colorOnLegend(15, legend), 'rgb(255,128,128)');
  assert.equal(colorOnLegend(99, legend), '#ffffff');
  assert.equal(colorOnLegend(-3, legend), '#000000');
  assert.equal(colorOnLegend(null, legend), null);
  assert.equal(colorOnLegend(NaN, legend), null);
  assert.equal(colorOnLegend(5, []), null);
});

test('adjusted means are used only when they exist', () => {
  const f = { properties: { id: 'a', accumulation_mean_mm: 4, accumulation_adjusted_mean_mm: 6 } };
  assert.equal(basinMean(f, false), 4);
  assert.equal(basinMean(f, true), 6);
  assert.equal(basinMean({ properties: { id: 'a', accumulation_mean_mm: 4 } }, true), 4);
  assert.equal(basinMean({ properties: { id: 'a', accumulation_mean_mm: null } }, false), null);
});

test('a drag is not a click', () => {
  assert.equal(isClick({ x: 10, y: 10 }, { x: 12, y: 11 }), true);
  assert.equal(isClick({ x: 10, y: 10 }, { x: 40, y: 10 }), false);
  assert.equal(isClick(null, { x: 1, y: 1 }), true);
});

test('the tooltip states units, uncertainty and coverage and never prints NaN', () => {
  const f = {
    properties: {
      id: 'H1', name: 'Test Creek', area_km2: 1234.5, accumulation_mean_mm: 12.34,
      accumulation_sigma_mm: 3.2, accumulation_max_mm: 40, coverage: 0.87,
    },
  };
  const t = basinTitle(f, false);
  assert.match(t, /H1 \(Test Creek\)/);
  assert.match(t, /1235 km²/);
  assert.match(t, /mean 12\.3 mm/);
  assert.match(t, /±3\.2 mm \(1σ\)/);
  assert.match(t, /87 % of the basin/);
  assert.doesNotMatch(basinTitle({ properties: { id: 'x' } }, false), /NaN|undefined/);
});
