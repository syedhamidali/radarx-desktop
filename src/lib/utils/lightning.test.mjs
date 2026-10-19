// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  mulberry32, jaggedPath, makeFlash, makeStrokes, flashLevels, activeFlashes, flashEnd, lightPoints, thunderDelay,
  SPEED_OF_SOUND_MS, modelFlashes, iceVolumeKm3,
} from './lightning.ts';

const same = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);

test('random numbers are deterministic for a seed and different between seeds', () => {
  const a = mulberry32(5), b = mulberry32(5), c = mulberry32(6);
  const xa = Array.from({ length: 5 }, a), xb = Array.from({ length: 5 }, b), xc = Array.from({ length: 5 }, c);
  assert.deepEqual(xa, xb);
  assert.notDeepEqual(xa, xc);
  assert.ok(xa.every((v) => v >= 0 && v < 1));
});

test('a cloud-to-ground flash: deterministic, jagged, branching, ends on the ground, starts in the cloud', () => {
  const init = { id: 3, t: 5, kind: 'CG', source: 'modelled', origin: [10, 20, 7.5], end: [12, 22, 0.1], seed: 7 };
  const f1 = makeFlash(init), f2 = makeFlash(init);
  assert.ok(same(f1.segments, f2.segments) && same(f1.level, f2.level), 'same input, same flash');
  assert.deepEqual(f1.strokes, f2.strokes);
  const other = makeFlash({ ...init, id: 4 });
  assert.ok(!same(f1.segments, other.segments), 'another flash, another shape');
  const n = f1.segments.length / 6;
  assert.ok(n > 40 && n < 3000, `${n} segments`);
  // contiguous main channel: each level-0 segment starts where the previous one ended
  const main = [];
  for (let i = 0; i < n; i++) if (f1.level[i] === 0) main.push(i);
  for (let k = 1; k < main.length; k++) {
    const a = main[k - 1], b = main[k];
    for (let j = 0; j < 3; j++) assert.ok(Math.abs(f1.segments[a * 6 + 3 + j] - f1.segments[b * 6 + j]) < 1e-5);
  }
  // starts at the charge region and reaches the ground
  assert.deepEqual([f1.segments[0], f1.segments[1], f1.segments[2]].map((v) => Math.round(v * 1e3) / 1e3), [10, 20, 7.5]);
  const last = main[main.length - 1];
  assert.ok(Math.abs(f1.segments[last * 6 + 5] - 0.1) < 1e-4, 'the main channel ends at the strike point');
  // tortuous: longer than the straight line
  let len = 0;
  for (const i of main) len += Math.hypot(f1.segments[i * 6 + 3] - f1.segments[i * 6], f1.segments[i * 6 + 4] - f1.segments[i * 6 + 1], f1.segments[i * 6 + 5] - f1.segments[i * 6 + 2]);
  assert.ok(len > 1.03 * Math.hypot(2, 2, 7.4), `tortuosity ${len / Math.hypot(2, 2, 7.4)}`);
  // branches exist, thinner levels too
  assert.ok(f1.level.some((l) => l === 1) && f1.level.some((l) => l === 2));
  assert.equal(f1.ground[2], 0.1);
});

test('an intracloud flash stays up in the cloud', () => {
  const f = makeFlash({ id: 1, t: 2, kind: 'IC', source: 'modelled', origin: [0, 0, 9], end: [15, 4, 9.5], seed: 3 });
  assert.equal(f.ground, undefined);
  let zmin = Infinity;
  for (let i = 0; i < f.segments.length / 3; i++) zmin = Math.min(zmin, f.segments[i * 3 + 2]);
  assert.ok(zmin > 5, `lowest point ${zmin} km`);
});

test('return strokes: 2 to 4 re-strikes in 0.15 to 0.5 s for cloud-to-ground, the first the brightest', () => {
  for (let seed = 1; seed < 40; seed++) {
    const s = makeStrokes(mulberry32(seed), 'CG');
    assert.ok(s.length >= 2 && s.length <= 4);
    assert.equal(s[0].dt, 0);
    assert.equal(s[0].amp, 1);
    assert.ok(s.every((q, i) => i === 0 || (q.dt > s[i - 1].dt && q.amp < 1)));
    const span = s[s.length - 1].dt;
    assert.ok(span >= 0.04 && span <= 0.5, `span ${span}`);
  }
});

test('brightness follows the strokes: zero before, peak at a stroke, fast decay, a bump at every re-strike', () => {
  const f = makeFlash({ id: 2, t: 10, kind: 'CG', source: 'modelled', origin: [0, 0, 7], end: [1, 1, 0], seed: 1, energy: 1 });
  assert.deepEqual(flashLevels(f, 9.9), { channel: 0, light: 0 });
  const peak = flashLevels(f, 10.0);
  assert.ok(peak.channel > 0.9 && peak.light > 0.5);
  const later = flashLevels(f, 10.04);
  assert.ok(later.channel < peak.channel * 0.5, 'the channel fades within a few frames');
  assert.ok(flashLevels(f, 10.5).channel < 0.05);
  // the second stroke brings it back up
  const t2 = 10 + f.strokes[1].dt;
  assert.ok(flashLevels(f, t2 + 0.001).channel > flashLevels(f, t2 - 0.01).channel);
  assert.ok(flashEnd(f) > 10 + f.strokes[f.strokes.length - 1].dt);
  assert.equal(activeFlashes([f], 9).length, 0);
  assert.equal(activeFlashes([f], 10.05).length, 1);
  assert.equal(activeFlashes([f], 30).length, 0);
  assert.equal(lightPoints(f).length, 3);
});

test('thunder delay is distance / 343 m/s (shortened by the time-lapse factor)', () => {
  assert.ok(Math.abs(thunderDelay(1.0) - 1000 / 343) < 1e-12);
  assert.ok(Math.abs(thunderDelay(20) - 20000 / SPEED_OF_SOUND_MS) < 1e-12);
  assert.ok(Math.abs(thunderDelay(20, 8) - 20000 / 343 / 8) < 1e-12);
});

const cand = (x, y) => ({ x, y, topKm: 12, baseKm: 5.5, weight: 1 });

test('modelled flashes: deterministic for a seed, only when the radar proxy says so, all marked modelled', () => {
  const input = { seed: 11, duration: 30, rate: () => 0.6, candidates: () => [cand(5, 5), cand(8, 3)], groundKm: () => 0.1, headingDeg: 55 };
  const a = modelFlashes(input), b = modelFlashes(input);
  assert.equal(a.length, b.length);
  assert.ok(a.length > 5 && a.length < 40, `${a.length} flashes`);
  assert.ok(a.every((f, i) => f.t === b[i].t && same(f.segments, b[i].segments)));
  assert.ok(a.every((f) => f.source === 'modelled'));
  assert.ok(a.every((f, i) => i === 0 || f.t - a[i - 1].t >= 0.34), 'a minimum gap between flashes');
  const c = modelFlashes({ ...input, seed: 12 });
  assert.ok(c.length !== a.length || c.some((f, i) => f.t !== a[i].t), 'another seed, other flashes');
  // no strong echo, no lightning
  assert.equal(modelFlashes({ ...input, rate: () => 0 }).length, 0);
  assert.equal(modelFlashes({ ...input, candidates: () => [] }).length, 0);
  // more echo, more flashes
  assert.ok(modelFlashes({ ...input, rate: () => 1.2 }).length > a.length);
  // they start in the strong columns, above the freezing level
  assert.ok(a.every((f) => f.origin[2] >= 5.5 && f.origin[2] <= 12));
  assert.ok(a.every((f) => Math.abs(f.origin[0] - 6.5) < 5 && Math.abs(f.origin[1] - 4) < 4));
});

test('the ice-phase proxy counts strong echo above the -10 C level only', () => {
  const nx = 4, ny = 4, nz = 4;
  const codes = new Uint8Array(nx * ny * nz);
  const z = [1, 3, 6, 9];
  // strong echo (about 50 dBZ = code ~ 1 + 70/95*254 = 188) at level 0 (below -10 C) and level 2 (above)
  for (let i = 0; i < 4; i++) codes[0 * 16 + i] = 190;
  for (let i = 0; i < 6; i++) codes[2 * 16 + i] = 190;
  // weaker echo above: not counted
  for (let i = 0; i < 5; i++) codes[3 * 16 + i] = 100;
  const v = iceVolumeKm3(codes, nx, ny, nz, z, 1, 1, -20, 75, 35, 5);
  assert.equal(v, 6 * 1 * 1 * (9 - 1) / 3); // 6 cells x dz of the level spacing
});
