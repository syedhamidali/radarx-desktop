// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { LruLoader, columnMax, codeOfDbz, dbzOfCode, lightningCandidates, summarise, dbzAt } from './clipSteps.ts';

function vol(nx, ny, nz, fill) {
  const data = new Uint8Array(nx * ny * nz);
  fill(data, nx, ny, nz);
  return { nx, ny, nz, x0: 0, y0: 0, dx: 1, dy: 1, z: Float64Array.from({ length: nz }, (_, k) => 1 + k * 2), vmin: -20, vmax: 75, data };
}

test('a long series is streamed: at most `max` steps stay in memory and each is read once while in use', async () => {
  const reads = [];
  const loader = new LruLoader(3, async (k) => { reads.push(k); return { k, big: new Uint8Array(1000) }; });
  // stream past 40 steps, as the loading pass does
  for (let k = 0; k < 40; k++) await loader.get(k);
  assert.equal(loader.size, 3, 'memory is bounded');
  assert.equal(reads.length, 40);
  // the pair of a frame (k, k + 1) is resident and costs no new read
  const before = reads.length;
  await loader.get(38);
  await loader.get(39);
  assert.equal(reads.length, before);
  // a step that was dropped is read again when the frame needs it
  await loader.get(5);
  assert.equal(reads.length, before + 1);
  assert.equal(loader.size, 3);
  // two requests for the same missing step share one read
  const n7 = reads.filter((k) => k === 7).length;
  const p1 = loader.get(7), p2 = loader.get(7);
  await Promise.all([p1, p2]);
  assert.equal(reads.filter((k) => k === 7).length, n7 + 1);
});

test('a failing read is not cached', async () => {
  let n = 0;
  const loader = new LruLoader(2, async (k) => { if (n++ === 0) throw new Error('boom'); return k; });
  await assert.rejects(loader.get(1), /boom/);
  assert.equal(await loader.get(1), 1);
});

test('codes and dBZ convert back and forth', () => {
  const v = { vmin: -20, vmax: 75 };
  assert.equal(codeOfDbz(v, -20), 1);
  assert.equal(codeOfDbz(v, 75), 255);
  for (const d of [0, 20, 45, 60]) assert.ok(Math.abs(dbzOfCode(v, codeOfDbz(v, d)) - d) < 0.4);
});

test('column maximum, lightning candidates above the -10 C level, and the step summary', () => {
  const strong = codeOfDbz({ vmin: -20, vmax: 75 }, 55);
  const weak = codeOfDbz({ vmin: -20, vmax: 75 }, 25);
  const v = vol(6, 4, 5, (d, nx, ny) => {
    const at = (k, j, i) => (k * ny + j) * nx + i;
    d[at(0, 1, 2)] = strong; // level 0 (z = 1 km, below -10 C at 5 km)
    d[at(3, 2, 2)] = strong; // level 3 (z = 7 km, above)
    d[at(3, 2, 4)] = strong;
    d[at(4, 2, 4)] = weak;
  });
  const cm = columnMax(v);
  assert.equal(cm[1 * 6 + 2], strong);
  assert.equal(cm[2 * 6 + 4], strong);
  assert.equal(cm[0], 0);
  const c = lightningCandidates(v, 5);
  assert.equal(c.length, 2, JSON.stringify(c));
  assert.ok(c.every((q) => q.baseKm >= 5 && q.topKm >= 7 && q.weight >= 1));
  assert.ok(c.some((q) => q.x === 2 && q.y === 2));
  // the column with strong echo only low (z = 1 km) is not a candidate
  assert.ok(!c.some((q) => q.x === 2 && q.y === 1));
  const s = summarise(v, 5);
  assert.equal(s.composite.length, 24);
  assert.equal(s.hist.length, 256);
  assert.ok(s.iceKm3 > 0);
  assert.equal(dbzAt(v, 2, 1, 1) > 50, true);
  assert.ok(Number.isNaN(dbzAt(v, 50, 1, 1)));
});
