// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  decodeMosaicVolume, decodeWindVolume, quantiseField, windDbzVolume, gridBox, unionBox, translateBox,
  aeqdOffsetKm, chooseDownsample, packRG8Slab, sceneIsLight, framePose, levelSpacing,
} from './cartesianVolume.ts';

function mosaicHeader(nx = 4, ny = 3, nz = 2) {
  return {
    mosaic_id: 'm1', variable: 'DBZH', units: 'dBZ', vmin: -20, vmax: 75, cmap: 'NWSRef',
    grid: { x0: -2000, y0: -1000, cell_m: 1000, nx, ny, nz, z: [1000, 1500], origin_lat: 35, origin_lon: -97 },
    radars: [{ name: 'A', x_m: 500, y_m: -250, altitude_m: 200 }, { name: 'B' }],
    analysis_time: null,
  };
}

test('mosaic volume: decoded geometry in km, sites with a grid position only', () => {
  const v = decodeMosaicVolume(mosaicHeader(), new Uint8Array(24));
  assert.equal(v.nx, 4); assert.equal(v.nz, 2);
  assert.equal(v.x0, -2); assert.equal(v.dx, 1);
  assert.deepEqual([...v.z], [1, 1.5]);
  assert.equal(v.sites.length, 1);
  assert.deepEqual([v.sites[0].x, v.sites[0].y, v.sites[0].alt], [0.5, -0.25, 0.2]);
});

test('mosaic volume: a payload of the wrong size or range is refused', () => {
  assert.equal(decodeMosaicVolume(mosaicHeader(), new Uint8Array(23)), null);
  assert.equal(decodeMosaicVolume({ ...mosaicHeader(), vmax: -20 }, new Uint8Array(24)), null);
  assert.equal(decodeMosaicVolume({ ...mosaicHeader(), grid: null }, new Uint8Array(24)), null);
});

test('grid box: cell edges and half a level of padding', () => {
  const v = decodeMosaicVolume(mosaicHeader(), new Uint8Array(24));
  const b = gridBox(v);
  assert.deepEqual([b.xMin, b.xMax, b.yMin, b.yMax], [-2.5, 1.5, -1.5, 1.5]);
  assert.deepEqual([b.zMin, b.zMax], [0.75, 1.75]);
  assert.equal(levelSpacing([1, 1.5]), 0.5);
  const u = unionBox(b, translateBox(b, 10, 0));
  assert.equal(u.xMax, 11.5);
});

test('quantiser: 0 = no data, 1..255 over the range, same packing as the polar volume', () => {
  const q = quantiseField([NaN, -30, -20, 27.5, 75, 99], -20, 75);
  assert.deepEqual([...q], [0, 0, 1, 128, 255, 255]);
});

function windHeader(nf = 3, nz = 2, ny = 2, nx = 3) {
  const names = ['u', 'v', 'dbz', 'crossing_angle'].slice(0, nf);
  return {
    wind_id: 'w1', fields: names.map((n) => ({ name: n, units: n === 'dbz' ? 'dBZ' : 'm/s' })),
    shape: [nf, nz, ny, nx],
    grid: { x: [-1000, 1000], y: [-500, 500], dx: 1000, dy: 1000, z: [1000, 2000], origin_lat: 35, origin_lon: -97 },
    radars: [{ name: 'A', x_m: 0, y_m: 0 }],
  };
}

test('wind volume: one view per field, mismatched sizes refused', () => {
  const nf = 3;
  const data = Float32Array.from({ length: nf * 12 }, (_, i) => i);
  const w = decodeWindVolume(windHeader(nf), data);
  assert.equal(w.fields.u.length, 12);
  assert.equal(w.fields.v[0], 12);
  assert.equal(w.fields.dbz[0], 24);
  assert.equal(w.x0, -1); assert.equal(w.dx, 1);
  assert.equal(decodeWindVolume(windHeader(nf), data.subarray(1)), null);
  assert.equal(decodeWindVolume({ ...windHeader(2), fields: [{ name: 'speed' }, { name: 'w' }] }, new Float32Array(24)), null); // no u / v
});

test('the wind grid reflectivity becomes a quantised volume', () => {
  const data = new Float32Array(3 * 12);
  data.fill(NaN);
  data.fill(30, 24, 30); // dbz of the first level
  const w = decodeWindVolume(windHeader(3), data);
  const v = windDbzVolume(w);
  assert.equal(v.nx, 3); assert.equal(v.data.length, 12);
  assert.ok(v.data.slice(0, 6).every((c) => c > 100 && c < 150));
  assert.ok(v.data.slice(6).every((c) => c === 0));
});

test('aeqd offset: east and north of the origin, zero at the origin', () => {
  assert.deepEqual(aeqdOffsetKm(35, -97, 35, -97), [0, 0]);
  const [x, y] = aeqdOffsetKm(35, -97, 36, -97);
  assert.ok(Math.abs(x) < 1e-6);
  assert.ok(Math.abs(y - 111.19) < 0.1);
  const [xe] = aeqdOffsetKm(35, -97, 35, -96);
  assert.ok(xe > 90 && xe < 92);
});

test('downsample: stays under the voxel budget and the texture limit', () => {
  const f = chooseDownsample([800, 800, 30], 4_000_000, 2048);
  const o = [800 / f[0], 800 / f[1], 30 / f[2]].map(Math.ceil);
  assert.ok(o[0] * o[1] * o[2] <= 4_000_000);
  assert.deepEqual(chooseDownsample([100, 100, 10], 1e9, 2048), [1, 1, 1]);
  assert.ok(chooseDownsample([5000, 10, 10], 1e9, 2048)[0] >= 3);
});

test('RG8 packing: max-pooling of the codes, G marks where there is data', () => {
  const nx = 4, ny = 2, nz = 1;
  const codes = Uint8Array.from([0, 5, 9, 0, 0, 0, 0, 7]);
  const out = new Uint8Array(2 * 1 * 2);
  packRG8Slab(codes, nx, ny, nz, [2, 2, 1], 0, out);
  assert.deepEqual([...out], [5, 255, 9, 255]);
  const full = new Uint8Array(nx * ny * 2);
  packRG8Slab(codes, nx, ny, nz, [1, 1, 1], 0, full);
  assert.equal(full[2], 5); assert.equal(full[3], 255); assert.equal(full[0], 0); assert.equal(full[1], 0);
});

test('scene theme follows the app theme; auto follows the system', () => {
  assert.equal(sceneIsLight('light', false), true);
  assert.equal(sceneIsLight('dark', true), false);
  assert.equal(sceneIsLight('auto', true), true);
  assert.equal(sceneIsLight(undefined, false), false);
});

test('default camera frames the whole box', () => {
  const b = { xMin: -250, xMax: 250, yMin: -250, yMax: 250, zMin: 0.5, zMax: 15 };
  const p = framePose(b, 6, 1.6, Math.PI / 4, 'oblique');
  assert.ok(Math.abs(p.target[0]) < 60 && Math.abs(p.target[1]) < 200, `target ${p.target}`);
  assert.ok(p.dist > 500 && p.dist < 2500, `dist ${p.dist}`);
  const top = framePose(b, 6, 1.6, Math.PI / 4, 'top');
  assert.equal(top.phi, 89);
  // looking straight down the square needs a distance of about its half width / tan(fov/2) / margin
  assert.ok(top.dist > 250 / Math.tan(Math.PI / 8) / 1.6 && top.dist < 1500, `top dist ${top.dist}`);
  // a wider window needs a shorter distance than a narrow one (the box is wider than tall)
  assert.ok(framePose(b, 6, 2.4, Math.PI / 4, 'oblique').dist < framePose(b, 6, 1.0, Math.PI / 4, 'oblique').dist);
});
