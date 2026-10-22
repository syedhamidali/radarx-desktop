// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { SlippyTiles } from './slippyTiles.ts';
import { BASEMAPS } from './basemaps.ts';

const OSM = BASEMAPS.find((b) => b.id === 'osm');
const TOPO = BASEMAPS.find((b) => b.id === 'topo');
const NONE = BASEMAPS.find((b) => b.id === 'none');

/** A fake image: records the URL it was given; the test decides when it loads or fails. */
function harness(opts = {}) {
  const imgs = [];
  let clock = 1_000_000;
  let changes = 0;
  const tiles = new SlippyTiles({
    onChange: () => { changes++; },
    makeImage: () => {
      const img = { _src: '', crossOrigin: '', decoding: '', onload: null, onerror: null };
      Object.defineProperty(img, 'src', { get: () => img._src, set: (v) => { img._src = v; } });
      imgs.push(img);
      return img;
    },
    now: () => clock,
    ...opts,
  });
  return { tiles, imgs, tick: (ms) => { clock += ms; }, changes: () => changes };
}
const grid = (z, n) => Array.from({ length: n }, (_, i) => ({ z, x: i % 8, y: Math.floor(i / 8) }));

test('no request at all for a basemap without tiles ("None")', () => {
  const h = harness();
  h.tiles.want(NONE, grid(3, 64));
  assert.equal(h.imgs.length, 0);
  assert.equal(h.tiles.requests, 0);
});

test('at most six requests in flight; the rest follow as tiles arrive; all in the requested order', () => {
  const h = harness();
  h.tiles.want(OSM, grid(3, 64));
  assert.equal(h.imgs.length, 6);
  assert.match(h.imgs[0].src, /tile\.openstreetmap\.org\/3\/0\/0\.png$/);
  assert.match(h.imgs[5].src, /\/3\/5\/0\.png$/);
  assert.equal(h.imgs[0].crossOrigin, 'anonymous', 'CORS, so that the globe may upload the image as a texture');
  h.imgs[0].onload();
  assert.equal(h.imgs.length, 7, 'one slot freed, the next tile requested');
  assert.match(h.imgs[6].src, /\/3\/6\/0\.png$/);
  assert.equal(h.tiles.has(OSM, 3, 0, 0), true);
  assert.equal(h.tiles.get(OSM, 3, 0, 0), h.imgs[0]);
  assert.equal(h.tiles.count(OSM, 3), 1);
});

test('64 tiles of level 3 are requested exactly once each', () => {
  const h = harness();
  h.tiles.want(OSM, grid(3, 64));
  let guard = 0;
  while (h.tiles.count(OSM, 3) < 64 && guard++ < 200) {
    for (const img of h.imgs) if (img.onload && !img._done) { img._done = true; img.onload(); }
  }
  assert.equal(h.tiles.count(OSM, 3), 64);
  assert.equal(h.imgs.length, 64);
  assert.equal(new Set(h.imgs.map((i) => i.src)).size, 64);
  // asking again changes nothing
  h.tiles.want(OSM, grid(3, 64));
  assert.equal(h.imgs.length, 64);
});

test('a failed tile is retried after a back-off, four times at most', () => {
  const h = harness();
  h.tiles.want(OSM, [{ z: 3, x: 1, y: 1 }]);
  assert.equal(h.imgs.length, 1);
  h.imgs[0].onerror();
  h.tiles.want(OSM, [{ z: 3, x: 1, y: 1 }]); // too early
  assert.equal(h.imgs.length, 1);
  h.tick(2100);
  h.tiles.want(OSM, [{ z: 3, x: 1, y: 1 }]);
  assert.equal(h.imgs.length, 2, 'second try after 2 s');
  h.imgs[1].onerror();
  h.tick(4100);
  h.tiles.want(OSM, [{ z: 3, x: 1, y: 1 }]);
  assert.equal(h.imgs.length, 3);
  h.imgs[2].onerror();
  h.tick(8100);
  h.tiles.want(OSM, [{ z: 3, x: 1, y: 1 }]);
  assert.equal(h.imgs.length, 4);
  h.imgs[3].onerror();
  h.tick(100000);
  h.tiles.want(OSM, [{ z: 3, x: 1, y: 1 }]);
  assert.equal(h.imgs.length, 4, 'gives up after four tries');
});

test('six failures in a row pause the loader (offline)', () => {
  const h = harness();
  h.tiles.want(OSM, grid(3, 6));
  for (const img of h.imgs.slice(0, 6)) img.onerror();
  const n = h.imgs.length;
  h.tick(5000);
  h.tiles.want(OSM, grid(3, 12));
  assert.equal(h.imgs.length, n, 'paused for 30 s');
  h.tick(31000);
  h.tiles.want(OSM, grid(3, 12));
  assert.ok(h.imgs.length > n);
  h.tiles.dispose();
});

test('the cache is bounded and least recently used goes first', () => {
  const h = harness({ cacheSize: 3 });
  h.tiles.want(OSM, grid(3, 3));
  for (const img of h.imgs.slice(0, 3)) img.onload();
  assert.equal(h.tiles.count(OSM, 3), 3);
  h.tiles.get(OSM, 3, 0, 0); // touch the oldest
  h.tiles.want(OSM, [{ z: 3, x: 7, y: 7 }]);
  h.imgs[3].onload();
  assert.equal(h.tiles.count(OSM, 3), 3);
  assert.equal(h.tiles.has(OSM, 3, 0, 0), true, 'recently used stays');
  assert.equal(h.tiles.has(OSM, 3, 1, 0), false, 'least recently used goes');
});

test('changing the provider cancels what is in flight and clears the cache; nothing arrives late', () => {
  const h = harness();
  h.tiles.want(OSM, grid(3, 3));
  const first = h.imgs[0];
  first.onload();
  h.tiles.want(TOPO, grid(3, 2));
  assert.equal(h.tiles.count(OSM, 3), 0);
  assert.equal(h.imgs[1].src, '', 'in-flight request cancelled');
  const changes = h.changes();
  h.imgs[1].onload?.(); // a late arrival of the old provider is ignored
  assert.equal(h.changes(), changes);
  assert.match(h.imgs[3].src, /opentopomap\.org\/3\/0\/0\.png$/);
  // switching to "None" cancels everything
  h.tiles.want(NONE, []);
  assert.equal(h.tiles.count(TOPO, 3), 0);
});

test('dispose cancels everything and later wants do nothing', () => {
  const h = harness();
  h.tiles.want(OSM, grid(3, 4));
  h.tiles.dispose();
  for (const img of h.imgs) assert.equal(img.src, '');
  const n = h.imgs.length;
  h.tiles.want(OSM, grid(3, 4));
  assert.equal(h.imgs.length, n);
});
