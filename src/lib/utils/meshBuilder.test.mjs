// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildSharpMesh, buildSmoothMesh, rayLayout } from './meshBuilder.ts';

const NR = 4; // gates
const range = Float32Array.from([1000, 2000, 3000, 4000]);

function sweep(azimuths) {
  const az = Float32Array.from(azimuths);
  const values = new Float32Array(az.length * NR).fill(10);
  return { az, values, n: az.length };
}

/** Bearing (deg, clockwise from north) of every vertex, as [min, max] per triangle-pair cell. */
function cellSpans(positions) {
  const spans = [];
  for (let c = 0; c < positions.length; c += 12) {
    const bearings = [];
    for (let k = 0; k < 6; k++) {
      const x = positions[c + k * 2], y = positions[c + k * 2 + 1];
      bearings.push(((Math.atan2(x, y) * 180) / Math.PI + 360) % 360);
    }
    spans.push(bearings);
  }
  return spans;
}
const width = (b) => {
  // angular width of a cell, robust to the 0/360 seam
  const ref = b[0];
  const rel = b.map((v) => ((v - ref + 540) % 360) - 180);
  return Math.max(...rel) - Math.min(...rel);
};

test('full circle is closed and every wedge is about one ray wide', () => {
  const { az, values, n } = sweep(Array.from({ length: 360 }, (_, i) => i));
  assert.equal(rayLayout(az, n).closed, true);
  for (const mesh of [buildSharpMesh(az, range, values, n, NR), buildSmoothMesh(az, range, values, n, NR)]) {
    assert.equal(mesh.vertexCount, 360 * (NR - 1) * 6); // every cell, seam included
    assert.ok(cellSpans(mesh.positions).every((b) => width(b) < 1.01));
  }
});

test('a sector scan is open: the last ray is not stretched back to the first', () => {
  const { az, values, n } = sweep(Array.from({ length: 131 }, (_, i) => 50 + i));
  assert.equal(rayLayout(az, n).closed, false);
  const sharp = buildSharpMesh(az, range, values, n, NR);
  const smooth = buildSmoothMesh(az, range, values, n, NR);
  for (const mesh of [sharp, smooth]) {
    const spans = cellSpans(mesh.positions);
    assert.ok(spans.every((b) => width(b) < 1.01), 'no wide wedge');
    for (const b of spans.flat()) assert.ok(b > 49 && b < 181, `bearing ${b} outside the sector`);
  }
  assert.equal(smooth.vertexCount, 130 * (NR - 1) * 6); // 130 gaps between 131 rays
});

test('a moderate hole is split between its neighbours, not stretched across', () => {
  const { az, values, n } = sweep([10, 11, 12, 13, 17.5, 18.5, 19.5]);
  for (const mesh of [buildSharpMesh(az, range, values, n, NR), buildSmoothMesh(az, range, values, n, NR)]) {
    const spans = cellSpans(mesh.positions);
    assert.ok(spans.every((b) => width(b) < 4.6), 'no wedge reaches past the hole');
  }
  // sharp: the ray at 13 reaches half-way (2.25 deg) into the hole, no further
  const sharp = cellSpans(buildSharpMesh(az, range, values, n, NR).positions);
  assert.ok(Math.max(...sharp.flat().filter((v) => v > 12.5 && v < 17)) < 15.3);
});

test('a big hole stays empty', () => {
  const { az, values, n } = sweep([10, 11, 12, 13, 40, 41, 42]);
  for (const mesh of [buildSharpMesh(az, range, values, n, NR), buildSmoothMesh(az, range, values, n, NR)]) {
    const spans = cellSpans(mesh.positions);
    assert.ok(spans.every((b) => width(b) < 1.6));
    assert.ok(!spans.some((b) => b.every((v) => v > 14 && v < 39)), 'nothing drawn in 14..39');
  }
});

test('sharp wedges are centred on the ray azimuth', () => {
  const { az, values, n } = sweep([90, 91, 92]);
  const mid = cellSpans(buildSharpMesh(az, range, values, n, NR).positions)
    .filter((b) => Math.min(...b) > 90.4 && Math.max(...b) < 91.6);
  assert.ok(mid.length > 0); // the middle ray covers 90.5 .. 91.5
});

test('descending scan direction works', () => {
  const { az, values, n } = sweep(Array.from({ length: 91 }, (_, i) => 180 - i));
  assert.equal(rayLayout(az, n).dir, -1);
  const spans = cellSpans(buildSharpMesh(az, range, values, n, NR).positions);
  assert.ok(spans.every((b) => width(b) < 1.01));
});
