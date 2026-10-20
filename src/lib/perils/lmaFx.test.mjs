// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { compileFlashes, sourceBrightness, flashFrame, longestFlash, lightFrom, flashesInWindow, rateSeries, sourceColour, LMA_DEFAULTS } from './lmaFx.ts';

// a synthetic flashes.json in the shape of perils.json: [t (data s), x, y, z km, power dBW]
const flash = (id, t0, pts) => ({ id, t0, t1: t0 + 0.5, n: pts.length, s: pts.map((p, i) => [t0 + i * 0.05, p[0], p[1], p[2], p[3] ?? 5]) });
const F = [
  flash(1, 10, [[0, 0, 6], [0.5, 0, 6.2], [1.0, 0.2, 6.5], [10, 10, 6.6]]),
  flash(2, 30, [[5, 5, 8], [5.4, 5, 8.1]]),
];
const toClip = (td) => td / 6; // x6 time-lapse

test('every source goes to the clip time of its measured time; flashes are ordered by their first source', () => {
  const c = compileFlashes([F[1], F[0]], toClip);
  assert.equal(c[0].id, 1);
  assert.ok(Math.abs(c[0].tc[0] - 10 / 6) < 1e-9);
  assert.ok(Math.abs(c[0].tc[3] - (10.15) / 6) < 1e-9);
  assert.equal(c[1].id, 2);
  assert.ok(c[0].tc0 < c[1].tc0);
});

test('source brightness: nothing before it appears, a flare at once, an afterglow that ends', () => {
  const p = LMA_DEFAULTS;
  assert.equal(sourceBrightness(-0.01, p), 0);
  assert.ok(sourceBrightness(0, p) > 0.99);
  assert.ok(sourceBrightness(0.2, p) < sourceBrightness(0.05, p));
  assert.equal(sourceBrightness(p.glowS, p), 0);
  assert.equal(sourceBrightness(5, p), 0);
  let prev = 2;
  for (let a = 0; a < p.glowS; a += 0.01) {
    const b = sourceBrightness(a, p);
    assert.ok(b <= prev + 1e-12, 'monotone decay');
    prev = b;
  }
  assert.ok(p.glowS >= 0.3 && p.glowS <= 0.8, 'the afterglow is 0.3 to 0.8 s of clip time');
});

test('sources appear at their own time and not before; an old flash is gone', () => {
  const c = compileFlashes(F, toClip);
  const md = longestFlash(c);
  const t0 = c[0].tc[0];
  assert.equal(flashFrame(c, t0 - 0.05, md).sprites.length, 0, 'nothing before the first source');
  const f0 = flashFrame(c, t0 + 1e-6, md);
  assert.equal(f0.sprites.length, 1, 'only the first source at its own time');
  assert.equal(f0.active, 1);
  const f1 = flashFrame(c, c[0].tc[2] + 1e-6, md);
  assert.equal(f1.sprites.length, 3);
  assert.equal(flashFrame(c, c[0].tc[3] + 5, md).sprites.length, 0, 'gone after the afterglow');
});

test('links join consecutive sources but break over a long step in space or time (no channel is invented)', () => {
  const c = compileFlashes(F, toClip);
  const md = longestFlash(c);
  const fr = flashFrame(c, c[0].tc[3] + 1e-6, md, { ...LMA_DEFAULTS, glowS: 5 });
  // sources 0-1 and 1-2 are 0.5 km apart and linked; 2-3 is 10 km apart: not linked
  assert.equal(fr.links.length, 2);
  assert.equal(fr.sprites.length, 4);
  for (const l of fr.links) assert.ok(Math.hypot(l.ax - l.bx, l.ay - l.by, l.az - l.bz) <= LMA_DEFAULTS.maxLinkKm);
});

test('only the real sources are drawn: positions are exactly the measured ones', () => {
  const c = compileFlashes(F, toClip);
  const fr = flashFrame(c, c[1].tc[1] + 1e-6, longestFlash(c), { ...LMA_DEFAULTS, glowS: 0.8 });
  const real = new Set(F.flatMap((f) => f.s.map((s) => `${Math.fround(s[1])},${Math.fround(s[2])},${Math.fround(s[3])}`)));
  for (const s of fr.sprites) assert.ok(real.has(`${s.x},${s.y},${Math.fround(s.z)}`));
});

test('light for the cloud: at the flash, scaled by brightness, nothing when dark', () => {
  const c = compileFlashes(F, toClip);
  const md = longestFlash(c);
  const lit = lightFrom(flashFrame(c, c[0].tc[0] + 1e-6, md), 2);
  assert.equal(lit.points.length, 1);
  assert.ok(Math.abs(lit.points[0].x - 0) < 1e-6 && Math.abs(lit.points[0].z - 12) < 1e-5, 'z is exaggerated');
  assert.ok(lit.sky > 0);
  const dark = lightFrom(flashFrame(c, 0, md), 2);
  assert.equal(dark.points.length, 0);
  assert.equal(dark.sky, 0);
});

test('flash rate and the series', () => {
  const firsts = [1, 2, 3, 30, 31, 100];
  assert.equal(flashesInWindow(firsts, 31, 60), 5);
  assert.equal(flashesInWindow(firsts, 31, 2), 2);
  assert.deepEqual(rateSeries(firsts, 0, 120, 60), [5, 1]);
  assert.deepEqual(rateSeries([5], 10, 20, 5), [0, 0]);
});

test('colour by time runs from white to blue, by power from violet to white', () => {
  const a = sourceColour({ phase: 0, powerDbw: 0 }, 'time');
  const b = sourceColour({ phase: 1, powerDbw: 0 }, 'time');
  assert.ok(a[0] > b[0]);
  const lo = sourceColour({ phase: 0, powerDbw: -10 }, 'power');
  const hi = sourceColour({ phase: 0, powerDbw: 30 }, 'power');
  assert.ok(hi[0] > lo[0] && hi[1] > lo[1]);
});

import { smoothedFlashLight } from './lmaFx.ts';

test('the smoothed flash light is the mean over the window: a one-frame flash is dimmer, a steady one is unchanged', () => {
  const lit = { sprites: [{ flash: 1, x: 10, y: 20, z: 5, bright: 1 }, { flash: 1, x: 10.2, y: 20.1, z: 5.1, bright: 1 }, { flash: 1, x: 9.8, y: 19.9, z: 4.9, bright: 1 }], links: [], active: 1 };
  const dark = { sprites: [], links: [], active: 0 };
  const steady = smoothedFlashLight(() => lit, 5, 1);
  const raw = lightFrom(lit, 1);
  assert.ok(Math.abs(steady.points[0].i - raw.points[0].i) < 1e-9 && Math.abs(steady.sky - raw.sky) < 1e-9);
  // lit now, dark in the five samples before: one sixth of the light
  const blip = smoothedFlashLight((t) => (t === 5 ? lit : dark), 5, 1);
  assert.ok(Math.abs(blip.points[0].i - raw.points[0].i / 6) < 1e-9);
  assert.ok(Math.abs(blip.sky - raw.sky / 6) < 1e-9);
  // nothing in the window: no points
  assert.equal(smoothedFlashLight(() => dark, 5, 1).points.length, 0);
});
