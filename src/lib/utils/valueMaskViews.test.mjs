// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
//
// The value mask must mean the same in every view: the 2D shader, the 3D volume shader, the wind
// map, the cross-section and the engine's exports. The viewer-side rules are tested here; the
// engine's (python/engine/valuemask.py) are tested against the same table in
// python/tests/test_valuemask.py.
import test from 'node:test';
import assert from 'node:assert/strict';
import { maskTest, maskPredicate, float32Mask, NO_MASK } from '../stores/valueMask.ts';
import { maskVec, maskVecNorm, hiddenByVec, MASK_OFF, GLSL_VALUE_MASK } from './valueMaskGl.ts';
import { buildGlyphs, DEFAULT_GLYPH_OPTIONS } from './windGlyphs.ts';
import { engineCleanOptions, cleanField, DEFAULT_NOISE } from './rhiClean.ts';
import { readFileSync } from 'node:fs';

const mk = (mode, min = null, max = null, inclusive = false) => ({ mode, min, max, inclusive });

/** The same table as CASES in python/tests/test_valuemask.py: [mask, value, hidden]. */
const TABLE = [
  [mk('below', 0), -1, true],
  [mk('below', 0), 0, false],
  [mk('below', 0, null, true), 0, true],
  [mk('above', null, 10), 10, false],
  [mk('above', null, 10), 10.5, true],
  [mk('above', null, 10, true), 10, true],
  [mk('between', -2, 2), 0, true],
  [mk('between', -2, 2), 2, false],
  [mk('between', 2, -2), 0, true],
  [mk('between', -2, 2, true), 2, true],
  [mk('outside', -2, 6), 7, true],
  [mk('outside', -2, 6), 6, false],
  [mk('outside', -2, 6, true), 6, true],
  [mk('outside', -2, 6), 0, false],
  [mk('off', 0, 1), 0.5, false],
  [mk('below', null), -50, false],
  [mk('between', 1, null), 5, false],
];

test('the shared rule table (same as the engine tests)', () => {
  for (const [mask, value, hidden] of TABLE) assert.equal(maskTest(value, mask), hidden, JSON.stringify([mask, value]));
});

test('the GPU form hides exactly what maskTest hides (all modes, edges and random values)', () => {
  const modes = ['below', 'above', 'between', 'outside'];
  let n = 0;
  let seed = 12345;
  const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32);
  for (const mode of modes) {
    for (const inclusive of [false, true]) {
      for (let k = 0; k < 40; k++) {
        const a = Math.fround(rnd() * 100 - 30);
        const b = Math.fround(rnd() * 100 - 30);
        const mask = mk(mode, mode === 'above' ? null : a, mode === 'below' ? null : b, inclusive);
        const vec = maskVec(mask);
        // values: the limits themselves, around them, and random ones (all float32, as in the data)
        const values = [a, b, Math.fround(a + 1e-3), Math.fround(a - 1e-3), Math.fround(b + 1e-3), Math.fround(b - 1e-3)];
        for (let i = 0; i < 20; i++) values.push(Math.fround(rnd() * 120 - 40));
        for (const v of values) {
          assert.equal(hiddenByVec(v, vec), maskTest(v, float32Mask(mask)), `${mode} inc=${inclusive} v=${v} ${a},${b}`);
          n++;
        }
      }
    }
  }
  assert.ok(n > 1000);
  assert.deepEqual(maskVec(NO_MASK), MASK_OFF);
  assert.deepEqual(maskVec(mk('below', null)), MASK_OFF); // incomplete = off
  assert.equal(hiddenByVec(NaN, maskVec(mk('below', 0))), false);
});

test('float32: the float32 form of a limit is not hidden by an exclusive mask, in every path', () => {
  const v = Math.fround(0.7); // 0.699999988...
  const mask = mk('below', 0.7);
  assert.equal(maskTest(v, mask), true); // double compare (documented): why float data uses the f32 forms
  assert.equal(maskPredicate(mask, { f32: true })(v), false);
  assert.equal(hiddenByVec(v, maskVec(mask)), false);
  assert.equal(maskPredicate(mk('below', 0.7, null, true), { f32: true })(v), true);
  assert.equal(maskPredicate(NO_MASK, { f32: true }), null);
});

test('the volume form maps limits onto the stored 0..1 range', () => {
  const vmin = -20;
  const vmax = 75;
  const norm = (x) => Math.fround((x - vmin) / (vmax - vmin));
  const mask = mk('below', 20);
  const vec = maskVecNorm(mask, vmin, vmax);
  assert.equal(vec[0], 1);
  assert.equal(vec[1], norm(20));
  // a stored byte b (1..255) decodes to (b-1)/254: bytes under 20 dBZ are hidden, the rest stay
  const bytesHidden = [];
  for (let b = 1; b <= 255; b++) if (hiddenByVec(Math.fround((b - 1) / 254), vec)) bytesHidden.push(b);
  const dbzOf = (b) => vmin + ((b - 1) / 254) * (vmax - vmin);
  assert.ok(bytesHidden.length > 0);
  assert.ok(bytesHidden.every((b) => dbzOf(b) < 20 + 1e-4));
  assert.ok(dbzOf(Math.max(...bytesHidden) + 1) >= 20 - 1e-4);
  // a limit above the range hides everything stored; one below it hides nothing
  assert.equal(hiddenByVec(0.5, maskVecNorm(mk('below', 500), vmin, vmax)), true);
  assert.equal(hiddenByVec(0.5, maskVecNorm(mk('below', -500), vmin, vmax)), false);
  assert.deepEqual(maskVecNorm(mask, 5, 5), MASK_OFF);
  const between = maskVecNorm(mk('between', 40, 20), vmin, vmax);
  assert.equal(hiddenByVec(norm(30), between), true);
  assert.equal(hiddenByVec(norm(10), between), false);
});

test('the shader snippet declares the function every shader calls', () => {
  assert.match(GLSL_VALUE_MASK, /bool valueHidden\(float v, vec4 m\)/);
});

function wind() {
  const nx = 6, ny = 4, nz = 2, n = nx * ny * nz;
  const f = (x) => new Float32Array(n).fill(x);
  const u = f(10);
  const v = f(0);
  // the second half of the cells is slow
  for (let i = n / 2; i < n; i++) u[i] = 2;
  return { id: 'w', nx, ny, nz, x0: 0, y0: 0, dx: 1, dy: 1, z: Float64Array.from([1, 2]),
    fields: { u, v, w: f(0), speed: f(10), crossing_angle: f(60), uncertainty: f(1) }, units: {}, originLat: 35, originLon: -97, sites: [], analysisTime: null };
}

test('3D wind arrows: the speed mask hides arrows; the count follows', () => {
  const o = { ...DEFAULT_GLYPH_OPTIONS, strideH: 1, strideV: 1, minCrossing: 0, zMaxKm: 10 };
  const all = buildGlyphs(wind(), o);
  const test5 = maskPredicate(mk('below', 5), { f32: true });
  const some = buildGlyphs(wind(), { ...o, hidden: (u, v) => test5(Math.hypot(u, v)) });
  assert.equal(all.count, 48);
  assert.equal(some.count, 24); // the slow half is hidden
});

test('the RHI cleaning hides with the same mask, in float32', () => {
  const o = engineCleanOptions(DEFAULT_NOISE, mk('below', 10));
  assert.equal(o.mask.mode, 'below');
  const plain = { ...DEFAULT_NOISE, sqi: { ...DEFAULT_NOISE.sqi, on: false }, snr: { ...DEFAULT_NOISE.snr, on: false },
    rhohv: { ...DEFAULT_NOISE.rhohv, on: false }, despeckle: { ...DEFAULT_NOISE.despeckle, on: false }, smoothing: { ...DEFAULT_NOISE.smoothing, mode: 'off' } };
  const values = Float32Array.from([5, 10, 10.5, 20, Math.fround(0.7)]);
  const run = (mask) => cleanField({ values, nRays: 1, nRange: values.length, quality: {}, mask }, plain, 'dBZ');
  const res = run(mk('below', 10));
  const kept = Array.from(res.values ?? res.field ?? []).filter((x) => x > -9000);
  assert.deepEqual(kept.map((x) => Math.fround(x)), [10, 10.5, 20, Math.fround(0.7)].filter((x) => x >= 10));
  // 0.7 as float32 against "below 0.7": kept (the float32 form of the limit is not below it)
  const r2 = cleanField({ values: Float32Array.from([Math.fround(0.7), 0.5]), nRays: 1, nRange: 2, quality: {}, mask: mk('below', 0.7) }, plain, '');
  const k2 = Array.from(r2.values ?? r2.field ?? []).filter((x) => x > -9000);
  assert.equal(k2.length, 1);
});

test('the map toggle is a persisted display setting with a shortcut', () => {
  // (read as text: importing the store touches localStorage, which Node does not have)
  const settingsSrc = readFileSync(new URL('../stores/settings.ts', import.meta.url), 'utf8');
  assert.match(settingsSrc, /hideMap: boolean;/);
  assert.match(settingsSrc, /hideMap: false,/);
  const src = readFileSync(new URL('./shortcuts.ts', import.meta.url), 'utf8');
  const keys = [...src.matchAll(/defaultKey: '([^']+)'/g)].map((m) => m[1]);
  assert.ok(src.includes("id: 'toggle-map'") && keys.includes('mod+shift+m'));
  assert.equal(keys.filter((k) => k === 'mod+shift+m').length, 1, 'no other shortcut uses the same keys');
});
