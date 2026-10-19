// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  decodeTerrarium, decodeTerrariumTile, metersPerPixel, tileUrl, tileSpan, tileCount, pickZoom, resampleHeights,
  TERRARIUM_URL,
} from './terrain.ts';
import { latLonToTile, tileToLatLon } from './geoProjection.ts';

test('Terrarium decoding: metres = R*256 + G + B/256 - 32768', () => {
  assert.equal(decodeTerrarium(128, 0, 0), 0); // sea level
  assert.equal(decodeTerrarium(128, 100, 0), 100);
  assert.equal(decodeTerrarium(127, 255, 0), -1);
  assert.equal(decodeTerrarium(128, 10, 128), 10.5); // the blue byte is the fraction
  assert.equal(decodeTerrarium(0, 0, 0), -32768);
  // a whole tile
  const rgba = new Uint8ClampedArray(256 * 256 * 4);
  for (let i = 0; i < 256 * 256; i++) { rgba[i * 4] = 128; rgba[i * 4 + 1] = i % 256; rgba[i * 4 + 3] = 255; }
  const out = new Float32Array(256 * 256);
  decodeTerrariumTile(rgba, out);
  assert.equal(out[0], 0);
  assert.equal(out[37], 37);
  assert.equal(out[256 * 256 - 1], 255);
});

test('tile maths: Web Mercator tiles, pixel size, URL templates', () => {
  // Mayfield, KY at zoom 11 (x from the longitude; y from the Mercator formula)
  const t = latLonToTile(36.74, -88.64, 11);
  assert.equal(Math.floor(t.tx), Math.floor(((-88.64 + 180) / 360) * 2048));
  assert.equal(Math.floor(t.tx), 519);
  const merc = Math.asinh(Math.tan((36.74 * Math.PI) / 180));
  assert.equal(Math.floor(t.ty), Math.floor(((1 - merc / Math.PI) / 2) * 2048));
  assert.equal(Math.floor(t.ty), 798);
  // round trip
  const [la, lo] = tileToLatLon(t.tx, t.ty, 11);
  assert.ok(Math.abs(la - 36.74) < 1e-9 && Math.abs(lo + 88.64) < 1e-9);
  // metres per pixel: 156543 m at the equator at zoom 0, halves per zoom, shrinks with cos(lat)
  assert.ok(Math.abs(metersPerPixel(0, 0) - 156543.03392) < 1e-6);
  assert.ok(Math.abs(metersPerPixel(0, 5) - 156543.03392 / 32) < 1e-6);
  assert.ok(Math.abs(metersPerPixel(60, 3) / metersPerPixel(0, 3) - 0.5) < 1e-9);
  assert.equal(tileUrl(TERRARIUM_URL, 9, 130, 200), 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/9/130/200.png');
  assert.equal(tileUrl('https://x/{z}/{y}/{x}.jpg', 3, 5, 6), 'https://x/3/6/5.jpg');
});

test('tile span of a square around a point covers it and respects the tile budget', () => {
  const lat = 36.15, lon = -89.9;
  const s = tileSpan(lat, lon, 117000, 10);
  // the centre tile is inside the span
  const c = latLonToTile(lat, lon, 10);
  assert.ok(Math.floor(c.tx) >= s.tx0 && Math.floor(c.tx) <= s.tx1);
  assert.ok(Math.floor(c.ty) >= s.ty0 && Math.floor(c.ty) <= s.ty1);
  // 234 km wide at zoom 10 (about 31.7 km tiles at this latitude): 8 or 9 tiles across
  assert.ok(s.tx1 - s.tx0 + 1 >= 8 && s.tx1 - s.tx0 + 1 <= 9);
  // a smaller square needs fewer tiles
  assert.ok(tileCount(tileSpan(lat, lon, 30000, 10)) < tileCount(s));
  // zoom choice: about one tile pixel per output texel, lowered to fit the budget
  const z = pickZoom(117, 1024, lat, lon, 3, 12, 100);
  assert.equal(z.zoom, 9);
  assert.ok(tileCount(z.span) <= 100);
  const tight = pickZoom(117, 4096, lat, lon, 3, 12, 30);
  assert.ok(tileCount(tight.span) <= 30 && tight.zoom < 12);
});

test('resampling puts north at row 0 and west at column 0, clamps the sea, ignores missing tiles', () => {
  const zoom = 8, lat = 36.15, lon = -89.9;
  const span = tileSpan(lat, lon, 20000, zoom);
  const nx = span.tx1 - span.tx0 + 1, ny = span.ty1 - span.ty0 + 1;
  const sw = nx * 256, sh = ny * 256;
  // elevation = 1 m per tile pixel eastward + 2 m per tile pixel southward, so it grows to the east and south
  const st = new Float32Array(sw * sh);
  for (let y = 0; y < sh; y++) for (let x = 0; x < sw; x++) st[y * sw + x] = x + 2 * y;
  const size = 16;
  const h = resampleHeights(st, sw, sh, span.tx0, span.ty0, zoom, lat, lon, 20, size);
  assert.equal(h.length, size * size);
  const km = (r, c) => h[r * size + c] * 1000;
  assert.ok(km(0, size - 1) > km(0, 0), 'east is higher than west');
  assert.ok(km(size - 1, 0) > km(0, 0), 'south is higher than north (row 0 = north)');
  // the centre sample is the elevation at the centre tile pixel
  const c = latLonToTile(lat, lon, zoom);
  const want = (c.tx - span.tx0) * 256 - 0.5 + 2 * ((c.ty - span.ty0) * 256 - 0.5);
  assert.ok(Math.abs((km(size / 2 - 1, size / 2 - 1) + km(size / 2, size / 2)) / 2 - want) < 40, `${km(7, 7)} ${km(8, 8)} vs ${want}`);
  // sea (negative) becomes 0 and NaN (a missing tile) becomes 0
  const neg = new Float32Array(sw * sh).fill(-50);
  neg[0] = NaN;
  const hn = resampleHeights(neg, sw, sh, span.tx0, span.ty0, zoom, lat, lon, 20, size);
  assert.ok(hn.every((v) => v === 0));
});
