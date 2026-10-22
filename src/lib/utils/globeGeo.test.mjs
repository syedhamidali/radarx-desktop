// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, statSync } from 'node:fs';
import { decodeGeo, decodeLine, coastRuns, buildGlobeLines, paintPlainLand } from './globeGeo.ts';

const FILE = new URL('../data/globeGeo.json', import.meta.url);
const DOC = JSON.parse(readFileSync(FILE, 'utf8'));
const geo = decodeGeo(DOC);

/** Even-odd point in polygon over every land ring (rings close implicitly). */
function onLand(lon, lat) {
  let inside = false;
  for (const ring of geo.land) {
    const n = ring.length / 2;
    for (let i = 0, j = n - 1; i < n; j = i++) {
      const xi = ring[2 * i], yi = ring[2 * i + 1], xj = ring[2 * j], yj = ring[2 * j + 1];
      if (yi > lat !== yj > lat && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
    }
  }
  return inside;
}

test('the bundled outlines are small (under 300 KB) and say what they are', () => {
  assert.ok(statSync(FILE).size < 300 * 1024, `${statSync(FILE).size} bytes`);
  assert.equal(DOC.v, 1);
  assert.equal(DOC.unit, 0.05);
  assert.ok(DOC.land.length > 100 && DOC.borders.length > 100);
});

test('delta decoding restores the points', () => {
  const line = decodeLine([100, 200, 1, -2, 3, 4], 0.05);
  assert.deepEqual(Array.from(line).map((x) => +x.toFixed(6)), [5, 10, 5.05, 9.9, 5.2, 10.1]);
  assert.equal(decodeLine([], 0.05).length, 0);
});

test('every point is a valid position', () => {
  for (const set of [geo.land, geo.borders]) {
    for (const l of set) {
      for (let i = 0; i < l.length; i += 2) {
        assert.ok(Math.abs(l[i]) <= 180.0001 && Math.abs(l[i + 1]) <= 90.0001, `${l[i]},${l[i + 1]}`);
      }
    }
  }
});

test('known places are on land or in water', () => {
  const land = [[13.4, 52.5, 'Berlin'], [134, -25, 'Australia'], [20, 5, 'central Africa'], [-100, 40, 'USA'], [80, 62, 'Siberia'], [0, -80, 'Antarctica'], [78, 22, 'India']];
  const sea = [[-30, 30, 'mid-Atlantic'], [-150, 0, 'Pacific'], [80, -40, 'Indian Ocean'], [0, 0, 'Gulf of Guinea']];
  for (const [lo, la, n] of land) assert.ok(onLand(lo, la), `${n} should be land`);
  for (const [lo, la, n] of sea) assert.ok(!onLand(lo, la), `${n} should be water`);
});

test('coastlines have no artificial edges (the south pole line and the date line)', () => {
  const runs = geo.land.flatMap((r) => coastRuns(r));
  assert.ok(runs.length >= geo.land.length);
  for (const r of runs) {
    for (let i = 2; i < r.length; i += 2) {
      const bothPole = r[i - 1] <= -89.9 && r[i + 1] <= -89.9;
      const bothDateLine = Math.abs(r[i - 2]) >= 179.95 && Math.abs(r[i]) >= 179.95 && Math.sign(r[i - 2]) === Math.sign(r[i]);
      assert.ok(!bothPole && !bothDateLine, 'artificial edge kept');
    }
  }
  // Antarctica really is split: its ring reaches the pole and the date line
  const ant = geo.land.find((r) => Math.min(...Array.from(r).filter((_, i) => i % 2 === 1)) <= -89.9);
  assert.ok(ant, 'Antarctica ring present');
  assert.ok(coastRuns(ant).length >= 1);
  // a plain square ring has nothing to cut
  const sq = Float64Array.from([0, 0, 10, 0, 10, 10, 0, 10]);
  const sqRuns = coastRuns(sq);
  assert.equal(sqRuns.length, 1);
  assert.equal(sqRuns[0].length, 10, 'closed back to the first point');
});

test('line sets for the overlay: unit vectors', () => {
  const lines = buildGlobeLines(geo);
  assert.ok(lines.coast.length > 100 && lines.borders.length > 100 && lines.graticule.length === 17);
  const v = lines.coast[0];
  assert.equal(v.length % 3, 0);
  for (let i = 0; i < v.length; i += 3) assert.ok(Math.abs(Math.hypot(v[i], v[i + 1], v[i + 2]) - 1) < 1e-5);
});

test('the plain land texture fills every ring even-odd on an ocean, and draws nothing without geography', () => {
  const calls = [];
  const ctx = new Proxy({}, {
    get: (_, k) => (...a) => { calls.push([k, ...a]); },
    set: (_, k, v) => { calls.push(['set', k, v]); return true; },
  });
  paintPlainLand(ctx, 2048, 1024, null, { ocean: 'blue', land: 'grey' });
  assert.equal(calls.filter((c) => c[0] === 'fill').length, 0);
  assert.ok(calls.some((c) => c[0] === 'fillRect'));
  calls.length = 0;
  paintPlainLand(ctx, 2048, 1024, geo, { ocean: 'blue', land: 'grey' });
  assert.equal(calls.filter((c) => c[0] === 'closePath').length, geo.land.length);
  const fill = calls.find((c) => c[0] === 'fill');
  assert.equal(fill[1], 'evenodd');
  // coordinates stay inside the image
  for (const c of calls) if (c[0] === 'moveTo' || c[0] === 'lineTo') assert.ok(c[1] >= -1 && c[1] <= 2049 && c[2] >= -1 && c[2] <= 1025);
});
