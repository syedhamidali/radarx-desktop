// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildArrowMesh, autoStrides, buildGlyphs, selectLevels, windStats, arrowPalette, lutFromCss, percentile, DEFAULT_GLYPH_OPTIONS,
} from './windGlyphs.ts';

/** nx x ny x nz grid, u = 10, v = 0, w = 1 everywhere except where overridden. */
function wind({ nx = 8, ny = 6, nz = 4 } = {}) {
  const n = nx * ny * nz;
  const mk = (v) => new Float32Array(n).fill(v);
  const fields = { u: mk(10), v: mk(0), w: mk(1), speed: mk(10), crossing_angle: mk(60), uncertainty: mk(1) };
  return { id: 'w', nx, ny, nz, x0: -4, y0: -3, dx: 1, dy: 1, z: Float64Array.from([1, 2, 3, 4]), fields, units: {}, originLat: 35, originLon: -97, sites: [], analysisTime: null };
}

const opts = (o = {}) => ({ ...DEFAULT_GLYPH_OPTIONS, strideH: 1, strideV: 1, minCrossing: 0, zMaxKm: 10, ...o });

test('the arrow mesh is closed, in range and wound counter-clockwise outwards', () => {
  const m = buildArrowMesh(6);
  const nv = m.vertices.length / m.floatsPerVertex;
  assert.ok(m.indices.every((i) => i < nv));
  assert.equal(m.indices.length % 3, 0);
  const f = m.floatsPerVertex;
  const rs = 0.05;
  const pos = (i) => {
    const x = m.vertices[i * f], c = m.vertices[i * f + 1], s = m.vertices[i * f + 2], r = m.vertices[i * f + 6] * rs;
    return [x, c * r, s * r];
  };
  const nrm = (i) => [m.vertices[i * f + 3], m.vertices[i * f + 4], m.vertices[i * f + 5]];
  let bad = 0;
  for (let t = 0; t < m.indices.length; t += 3) {
    const [a, b, c] = [m.indices[t], m.indices[t + 1], m.indices[t + 2]].map(pos);
    const u = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
    const v = [c[0] - a[0], c[1] - a[1], c[2] - a[2]];
    const g = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
    if (Math.hypot(...g) < 1e-12) continue; // degenerate sliver (tip)
    const ns = [m.indices[t], m.indices[t + 1], m.indices[t + 2]].map(nrm);
    const n = [0, 1, 2].map((k) => (ns[0][k] + ns[1][k] + ns[2][k]) / 3);
    if (g[0] * n[0] + g[1] * n[1] + g[2] * n[2] <= 0) bad++;
  }
  assert.equal(bad, 0, 'triangles facing against their normals');
  // spans the unit length centred on the origin
  const xs = Array.from({ length: nv }, (_, i) => m.vertices[i * f]);
  assert.equal(Math.min(...xs), -0.5);
  assert.equal(Math.max(...xs), 0.5);
});

test('levels: range filter and stride', () => {
  assert.deepEqual(selectLevels([1, 2, 3, 4, 5], 2, 4, 1), [1, 2, 3]);
  assert.equal(selectLevels([1, 2, 3, 4, 5, 6], 0, 10, 2).length, 3);
  assert.deepEqual(selectLevels([1, 2, 3], 10, 20, 1), []);
});

test('glyphs: one arrow per cell, position, unit direction and length', () => {
  const w = wind();
  const g = buildGlyphs(w, opts({ strideH: 1, strideV: 2, vexag: 6 }));
  assert.equal(g.count, 8 * 6 * g.levels.length);
  assert.equal(g.data.length, g.count * 8);
  const d = g.data;
  const dirLen = Math.hypot(d[4], d[5], d[6]);
  assert.ok(Math.abs(dirLen - 1) < 1e-5);
  assert.ok(d[4] > 0 && d[6] > 0 && d[5] === 0, 'east and up');
  assert.ok(d[3] > 0);
  // the level height is exaggerated in the scene
  assert.ok([1, 2, 3, 4].map((z) => z * 6).includes(d[2]));
  // length proportional to the 3D speed
  w.fields.u.fill(20, 0, 1); // cell 0 of level 0 is twice as fast
  const g2 = buildGlyphs(w, opts({ strideV: 1 }));
  const first = g2.data.slice(0, 8);
  const other = g2.data.slice(8, 16);
  assert.ok(first[3] > 1.5 * other[3] && first[3] < 2.1 * other[3]);
});

test('glyphs: crossing-angle, uncertainty and missing-data filters', () => {
  const w = wind();
  w.fields.crossing_angle[0] = 10;
  w.fields.u[1] = NaN;
  w.fields.uncertainty[2] = 9;
  const all = buildGlyphs(w, opts({ strideV: 1, minCrossing: 0 }));
  const strict = buildGlyphs(w, opts({ strideV: 1, minCrossing: 30 }));
  assert.equal(all.count, 8 * 6 * 4 - 1); // the NaN cell
  assert.equal(strict.count, all.count - 1); // and the weakly crossed one
  const lowUnc = buildGlyphs(w, opts({ strideV: 1, maxUncertainty: 2 }));
  assert.equal(lowUnc.count, all.count - 1);
});

test('glyphs: levels outside the chosen range are skipped', () => {
  const g = buildGlyphs(wind(), opts({ zMinKm: 2, zMaxKm: 3, strideV: 1 }));
  assert.deepEqual(g.levels, [1, 2]);
  assert.equal(g.count, 8 * 6 * 2);
});

test('glyphs: the stride grows until the cap fits', () => {
  const g = buildGlyphs(wind({ nx: 40, ny: 40, nz: 4 }), opts({ strideV: 1, maxGlyphs: 300 }));
  assert.ok(g.count <= 300);
  assert.ok(g.strideH > 1);
});

test('glyphs: colour value by speed or w, none by default', () => {
  const w = wind();
  assert.equal(buildGlyphs(w, opts({ colorBy: 'none' })).data[7], 0);
  const s = buildGlyphs(w, opts({ colorBy: 'speed' })).data[7];
  assert.ok(s > 0 && s <= 1);
  w.fields.w.fill(-3);
  const dn = buildGlyphs(w, opts({ colorBy: 'w' })).data[7];
  assert.ok(dn < 0.5, 'downdraft below the middle of the table');
});

test('stats and helpers', () => {
  const w = wind();
  const st = windStats(w);
  assert.equal(st.speedP95, 10);
  assert.equal(st.absWP98, 1);
  assert.equal(percentile([1, 2, 3, 4, NaN], 1), 4);
  assert.equal(percentile([], 0.5), 0);
  const lut = lutFromCss(['rgb(1,2,3)', 'rgb(4,5,6)']);
  assert.deepEqual([...lut.slice(0, 4)], [1, 2, 3, 255]);
  assert.deepEqual([...lut.slice(255 * 4, 255 * 4 + 3)], [4, 5, 6]);
});

test('arrow colours: white with a dark edge on a dark scene, dark with a light edge on a light one', () => {
  const lum = (c) => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  const dark = arrowPalette(false);
  assert.ok(lum(dark.fill) > 0.9 && lum(dark.outline) < 0.1);
  const light = arrowPalette(true);
  assert.ok(lum(light.fill) < 0.1 && lum(light.outline) > 0.9);
});

test('default density follows the cells that have wind, not the grid size', () => {
  const full = wind({ nx: 60, ny: 60, nz: 6 });
  const sparse = wind({ nx: 60, ny: 60, nz: 6 });
  for (let i = 0; i < sparse.fields.u.length; i++) if (i % 20 !== 0) sparse.fields.u[i] = NaN;
  const a = autoStrides(full);
  const b = autoStrides(sparse);
  assert.ok(a.strideH > b.strideH);
  assert.equal(a.strideV, 1);
  assert.ok(b.strideH >= 1);
});
