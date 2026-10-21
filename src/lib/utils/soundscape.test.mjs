// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  windGain, windCutoffHz, rainGain, thunderGain, thunderCutoffHz, thunderTail, thunderOf, gustSeries, sampleSeries,
  AUDIO_LABEL, sumTree,
} from './soundscape.ts';
import { makeFlash, thunderDelay } from './lightning.ts';

const flash = (x, y, kind = 'CG') => makeFlash({ id: 1, t: 5, kind, source: 'modelled', origin: [x, y, 7], end: [x + 1, y + 1, 0.1], seed: 4, energy: 1 });

test('the wind gets louder and brighter with the wind speed', () => {
  let g = -1, c = -1;
  for (let v = 0; v <= 40; v += 2) {
    assert.ok(windGain(v) >= g && windCutoffHz(v) >= c, `not monotone at ${v} m/s`);
    g = windGain(v);
    c = windCutoffHz(v);
  }
  assert.equal(windGain(0), 0);
  assert.ok(windGain(30) > 3 * windGain(10));
  assert.ok(windGain(-5) === 0);
  // rain follows its level
  assert.ok(rainGain(0.9) > rainGain(0.4) && rainGain(0) === 0);
});

test('thunder is quieter, duller and longer the farther the flash', () => {
  let g = 2, c = 1e9, t = -1;
  for (const d of [1, 2, 5, 10, 20, 40, 80]) {
    assert.ok(thunderGain(d) < g && thunderCutoffHz(d) < c && thunderTail(d) > t, `at ${d} km`);
    g = thunderGain(d); c = thunderCutoffHz(d); t = thunderTail(d);
  }
  assert.ok(thunderCutoffHz(0.5) > 2000 && thunderCutoffHz(40) < 400, 'far thunder is a low rumble');
  assert.ok(thunderGain(15, 1) > thunderGain(15, 0.1), 'a stronger flash is louder');
  assert.ok(thunderGain(0) <= 1);
});

test('thunder reaches the listener after distance / 343 m/s, part by part along the channel', () => {
  const f = flash(10, 0); // 10 km east of the listener
  const th = thunderOf(f, [0, 0, 0.01], 1);
  const d = th.distKm;
  assert.ok(d > 8 && d < 12.5);
  assert.ok(Math.abs(th.arrival - (f.t + (d * 1000) / 343)) < 1e-9, 'first arrival = flash time + nearest distance / c');
  assert.ok(Math.abs(th.arrival - f.t - thunderDelay(d)) < 1e-12);
  // the delay is shortened by the time-lapse factor
  const th8 = thunderOf(f, [0, 0, 0.01], 8);
  assert.ok(Math.abs(th8.arrival - f.t - thunderDelay(d, 8)) < 1e-12);
  assert.ok(th8.arrival < th.arrival);
  // louder at one km than at twenty, with the same flash
  const near = thunderOf(f, [8, 0, 0.01], 1), far = thunderOf(f, [-10, 0, 0.01], 1);
  assert.ok(near.gain > th.gain && th.gain > far.gain);
  assert.ok(near.cutoffHz > far.cutoffHz);
});

test('the rumble of a far flash is longer than that of a near one; its loudest part is normalised to 1', () => {
  const f = flash(0, 0);
  const near = thunderOf(f, [3, 0, 0.01], 1), far = thunderOf(f, [60, 0, 0.01], 1);
  const dur = (t) => Math.max(...t.parts.map((p) => p.env.length)) / t.rate;
  assert.ok(dur(far) > dur(near), `${dur(far)} vs ${dur(near)}`);
  for (const th of [near, far]) {
    const peak = Math.max(...th.parts.map((p) => Math.max(...p.env)));
    assert.ok(Math.abs(peak - 1) < 1e-6);
    assert.ok(th.parts.length >= 2 && th.parts.every((p) => p.env.every((v) => v >= 0)));
  }
});

test('the parts come from the right side: a flash to the east is panned east', () => {
  const f = flash(15, 0);
  const th = thunderOf(f, [0, 0, 0.01], 1);
  for (const p of th.parts) {
    assert.ok(p.pos[0] > 3, `part at x = ${p.pos[0]}`);
    assert.ok(Math.abs(p.pos[1]) < 6);
  }
  // parts arrive in order of distance
  const t0s = th.parts.map((p) => p.t0);
  assert.ok(t0s.every((v) => v >= th.arrival - 1e-9));
});

test('gusts are smooth, positive and about 1 on average; series interpolate linearly', () => {
  const g = gustSeries(3, 600, 0.1, 0.25);
  const mean = g.reduce((s, v) => s + v, 0) / g.length;
  assert.ok(mean > 0.85 && mean < 1.15, `mean ${mean}`);
  assert.ok(g.every((v) => v > 0.2 && v < 2.2));
  const step = Math.max(...Array.from(g, (v, i) => (i ? Math.abs(v - g[i - 1]) : 0)));
  assert.ok(step < 0.12, `a smooth series (largest step ${step})`);
  assert.deepEqual(gustSeries(3, 50, 0.1), gustSeries(3, 50, 0.1));
  assert.equal(sampleSeries([0, 10], 1, 0.5), 5);
  assert.equal(sampleSeries([0, 10], 1, 3), 10);
  assert.match(AUDIO_LABEL, /not a recording/i);
});

test('sources are summed by a pairwise tree: no node has more than two inputs and every source reaches the output', () => {
  for (const n of [0, 1, 2, 3, 5, 8, 17, 140]) {
    const fanIn = new Map();
    const mk = () => {
      const node = { id: Symbol(), to: [], connect(t) { this.to.push(t); fanIn.set(t, (fanIn.get(t) ?? 0) + 1); } };
      return node;
    };
    const ctx = { createGain: mk };
    const leaves = Array.from({ length: n }, mk);
    const dest = { dest: true };
    const made = sumTree(ctx, leaves, dest);
    assert.ok([...fanIn.values()].every((c) => c <= 2), `${n} sources: a node has more than two inputs`);
    // every leaf reaches the destination
    const reaches = (node) => node === dest || node.to.some(reaches);
    assert.ok(leaves.every(reaches), `${n} sources: one does not reach the output`);
    assert.equal(made, Math.max(0, n - 1), 'a binary tree of n leaves has n - 1 sums');
  }
});
