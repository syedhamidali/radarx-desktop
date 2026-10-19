// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { decodeSurface, sampleGrid, blendFields, tempColour, quantile, coldPoolLevel, swathUpTo } from './perilsSurface.ts';
import { contourSegments, outsideBoxAlpha, reflAlpha, codeAt } from './perilsGround.ts';

test('decoding: 0 is undefined, 1 and 255 are the ends of the range', () => {
  const meta = { nx: 2, ny: 1, fields: [{ name: 't', min: 10, max: 20 }] };
  const f = decodeSurface(meta, new Uint8Array([0, 255]));
  assert.ok(Number.isNaN(f.t[0]));
  assert.equal(f.t[1], 20);
  assert.equal(decodeSurface(meta, new Uint8Array([1, 128])).t[0], 10);
});

test('bilinear sampling, NaN outside and next to undefined cells', () => {
  const m = { x0: 0, y0: 0, dx: 2, nx: 2, ny: 2 };
  const f = Float32Array.from([0, 10, 20, 30]);
  assert.equal(sampleGrid(f, m, 1, 1), 15);
  assert.equal(sampleGrid(f, m, 0, 0), 0);
  assert.ok(Number.isNaN(sampleGrid(f, m, -1, 0)));
  assert.ok(Number.isNaN(sampleGrid(Float32Array.from([0, NaN, 20, 30]), m, 1, 1)));
});

test('cross-fade of two analyses keeps the side that exists', () => {
  const a = Float32Array.from([10, NaN]);
  const b = Float32Array.from([20, 5]);
  assert.equal(blendFields(a, b, 0.25)[0], 12.5);
  assert.ok(Number.isNaN(blendFields(a, b, 0.25)[1]));
  assert.equal(blendFields(a, b, 0.75)[1], 5);
  assert.equal(blendFields(null, b, 0.5), b);
  assert.equal(blendFields(null, null, 0.5), null);
});

test('temperature colour: cold is bluer than warm, inside 0..255', () => {
  const c = tempColour(10, 10, 30);
  const w = tempColour(30, 10, 30);
  assert.ok(c[2] > c[0] && w[0] > w[2]);
  assert.ok(tempColour(-99, 10, 30).every((v) => v >= 0 && v <= 255));
});

test('the cold-pool edge exists only where the analysis has a clearly colder region', () => {
  const warm = new Float32Array(1000).fill(24);
  assert.ok(Number.isNaN(coldPoolLevel(warm)), 'no cold pool in a uniform field');
  const f = new Float32Array(1000).fill(24);
  for (let i = 0; i < 300; i++) f[i] = 17;
  const lv = coldPoolLevel(f, 2);
  assert.ok(Math.abs(lv - 22) < 1e-6);
  assert.equal(quantile([1, 2, 3, 4, NaN], 0), 1);
});

test('marching squares finds the contour of a ramp at the right place', () => {
  const nx = 5;
  const f = [];
  for (let j = 0; j < 4; j++) for (let i = 0; i < nx; i++) f.push(i * 10);
  const segs = contourSegments(f, nx, 4, 0, 0, 1, 25);
  assert.equal(segs.length, 3);
  for (const s of segs) {
    assert.ok(Math.abs(s[0] - 2.5) < 1e-9 && Math.abs(s[2] - 2.5) < 1e-9);
  }
  assert.deepEqual(contourSegments(f, nx, 4, 0, 0, 1, 99), []);
});

test('the swath is the running maximum of the layers up to a step', () => {
  const l = [Uint8Array.from([0, 5, 0]), null, Uint8Array.from([3, 2, 9])];
  assert.deepEqual([...swathUpTo(l, 0)], [0, 5, 0]);
  assert.deepEqual([...swathUpTo(l, 1)], [0, 5, 0]);
  assert.deepEqual([...swathUpTo(l, 2)], [3, 5, 9]);
  assert.equal(swathUpTo([null], 0), null);
});

test('ground: the 2-D layers stay outside the radar box; reflectivity opacity ramps', () => {
  const box = { xMin: -90, xMax: 90, yMin: -90, yMax: 90 };
  assert.equal(outsideBoxAlpha(0, 0, box), 0);
  assert.equal(outsideBoxAlpha(89, 0, box), 0);
  assert.ok(outsideBoxAlpha(95, 0, box) > 0 && outsideBoxAlpha(95, 0, box) < 1);
  assert.equal(outsideBoxAlpha(120, 0, box), 1);
  assert.equal(reflAlpha(5), 0);
  assert.ok(reflAlpha(20) > 0.35 && reflAlpha(60) <= 0.85);
  const m = { x0: 0, y0: 0, dx: 1, nx: 3, ny: 3 };
  assert.equal(codeAt(Uint8Array.from([1, 2, 3, 4, 5, 6, 7, 8, 9]), m, 1.2, 1.1), 5);
  assert.equal(codeAt(Uint8Array.from([1, 2, 3, 4, 5, 6, 7, 8, 9]), m, 9, 9), 0);
});
