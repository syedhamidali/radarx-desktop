// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  thunderDelayS, relativeAzimuthDeg, equalPowerPan, DEFAULT_MIXER, layerGain, limiterGain, layerReport, flashStrength01,
  planReplay, synthThunder, synthTick, rmsDb, peakDb, normaliseTo, SOUND_LABEL,
} from './liveSound.ts';
import { decodeLightning } from './lightningLayer.ts';

const b64f = (arr) => Buffer.from(new Float32Array(arr).buffer).toString('base64');
const b64i = (arr) => Buffer.from(new Int32Array(arr).buffer).toString('base64');

/** Four flashes at t = 10, 20, 30, 40 s with energies 1e-13 .. 1e-11 J (GLM-like). */
function data(kind = 'glm') {
  const n = 4;
  return decodeLightning({
    kind, instrument: 'TEST', t_ref: '2021-12-11T02:00:00.000Z', files: [],
    points: { n: 0, t: '', lat: '', lon: '', alt_km: '', strength: '', brightness: '', stations: '', flash: '' },
    flashes: {
      n, t0: b64f([10, 20, 30, 40]), t1: b64f([10.5, 20.5, 30.5, 40.5]), lat: b64f([0, 0, 0, 0]), lon: b64f([0, 0, 0, 0]),
      alt_km: b64f(kind === 'lma' ? [8, 8, 8, 8] : [NaN, NaN, NaN, NaN]), n_points: b64i([5, 50, 500, 5000]),
      strength: b64f([1e-13, 1e-12, 1e-11, 1e-10]), area_km2: b64f([NaN, NaN, NaN, NaN]),
    },
    coverage: [[0, 100]], strength: { unit: 'J', what: '', brightness_scale: '' }, meta: {}, notes: [], provenance: null,
  });
}

test('thunder arrives after distance / 343 m/s, divided by the time-lapse factor', () => {
  assert.ok(Math.abs(thunderDelayS(3.43) - 10) < 1e-12);
  assert.ok(Math.abs(thunderDelayS(3.43, 10) - 1) < 1e-12);
  assert.equal(thunderDelayS(0), 0);
  assert.equal(thunderDelayS(-5), 0);
});

test('direction relative to where the listener faces', () => {
  const north = { x: 0, y: 0, yawDeg: 0 };
  assert.equal(relativeAzimuthDeg(north, 10, 0), 90); // east of a listener facing north: to the right
  assert.equal(relativeAzimuthDeg(north, -10, 0), -90);
  assert.equal(relativeAzimuthDeg(north, 0, 10), 0);
  assert.equal(relativeAzimuthDeg(north, 0, -10), 180);
  const east = { x: 0, y: 0, yawDeg: 90 };
  assert.equal(relativeAzimuthDeg(east, 0, 10), -90); // north of a listener facing east: to the left
  assert.ok(Math.abs(relativeAzimuthDeg({ x: 5, y: 5, yawDeg: 350 }, 5, 15) - 10) < 1e-9, 'wraps across north');
});

test('equal-power panning: unit power, centre, hard right, hard left', () => {
  for (let a = -180; a <= 180; a += 15) {
    const { left, right } = equalPowerPan(a);
    assert.ok(Math.abs(left * left + right * right - 1) < 1e-12, `power at ${a}`);
  }
  const c = equalPowerPan(0);
  assert.ok(Math.abs(c.left - Math.SQRT1_2) < 1e-12 && Math.abs(c.right - Math.SQRT1_2) < 1e-12);
  const r = equalPowerPan(90);
  assert.ok(r.right > 0.999999 && r.left < 1e-6);
  const l = equalPowerPan(-90);
  assert.ok(l.left > 0.999999 && l.right < 1e-6);
  let prev = -1;
  for (let a = -90; a <= 90; a += 10) {
    const { right } = equalPowerPan(a);
    assert.ok(right > prev, 'the right channel grows toward the right');
    prev = right;
  }
});

test('the mixer: mute is silence, levels are dB, the limiter never amplifies', () => {
  assert.equal(layerGain({ ...DEFAULT_MIXER, layers: { ...DEFAULT_MIXER.layers, thunder: { levelDb: 0, mute: true } } }, 'thunder'), 0);
  assert.ok(Math.abs(layerGain({ ...DEFAULT_MIXER, layers: { ...DEFAULT_MIXER.layers, thunder: { levelDb: -6.0206, mute: false } } }, 'thunder') - 0.5) < 1e-4);
  assert.equal(layerGain(DEFAULT_MIXER, 'ticks'), 0, 'ticks are off by default');
  assert.ok(Math.abs(limiterGain(1, -6) - 0.5011872336) < 1e-9);
  assert.equal(limiterGain(0.3, -6), 1);
  assert.equal(limiterGain(0, -6), 1);
  for (const p of [0.1, 0.5, 1, 2, 10]) {
    const g = limiterGain(p, -6);
    assert.ok(g <= 1 && p * g <= 0.5012 + 1e-9, `peak ${p}`);
  }
  assert.equal(limiterGain(2, 3), 0.5, 'a ceiling above 0 dBFS is taken as 0 dBFS');
});

test('the report says which instrument drives which layer, and what is made up', () => {
  const r = layerReport(data('glm'));
  assert.equal(r.length, 2);
  assert.match(r[0].driver, /TEST: flash time, position and radiant energy/);
  assert.match(r[0].driver, /343 m\/s/);
  assert.match(r[0].kind, /timbre made up/);
  assert.match(layerReport(data('lma'))[0].driver, /number of sources/);
  assert.match(SOUND_LABEL, /not a recording/);
  assert.ok(!r.some((l) => /tornado|hail|wind|rain/i.test(l.name)), 'no sound the data cannot drive');
});

test('flash strength is 0..1 on a log scale and increases with the energy', () => {
  const s = flashStrength01(data('glm'));
  assert.equal(s.length, 4);
  for (let i = 0; i < 4; i++) assert.ok(s[i] >= 0 && s[i] <= 1);
  for (let i = 1; i < 4; i++) assert.ok(s[i] >= s[i - 1]);
  assert.ok(s[3] > s[0]);
  const l = flashStrength01(data('lma')); // from the source count
  assert.ok(l[3] > l[0]);
});

test('the replay plan: flashes in the window, delays from distance, nearer is louder, in order of arrival', () => {
  const d = data('glm');
  // flashes 10 km, 20 km, 5 km and 40 km east of the radar
  const pos = [[10, 0], [20, 0], [5, 0], [40, 0]];
  const ev = planReplay(d, 0, 100, { x: 0, y: 0, yawDeg: 0 }, (i) => pos[i], { speed: 10 });
  assert.equal(ev.length, 4);
  for (const e of ev) {
    assert.ok(Math.abs(e.thunderAt - e.tickAt - (e.distKm * 1000) / 343 / 10) < 1e-9, 'delay = distance / 343 m/s / speed');
    assert.equal(e.azimuthDeg, 90, 'all east: to the right of a listener facing north');
  }
  for (let k = 1; k < ev.length; k++) assert.ok(ev[k].thunderAt >= ev[k - 1].thunderAt, 'sorted by arrival');
  // same strength, nearer is louder and brighter
  const same = planReplay(data('glm'), 0, 100, { x: 0, y: 0, yawDeg: 0 }, (i) => [[3, 0], [6, 0], [12, 0], [24, 0]][i], { speed: 1 });
  const byDist = same.slice().sort((a, b) => a.distKm - b.distKm);
  const e0 = flashStrength01(d);
  assert.ok(byDist[0].gain > 0 && byDist.length === 4);
  assert.ok(e0.length === 4);
  for (let k = 1; k < byDist.length; k++) assert.ok(byDist[k].cutoffHz < byDist[k - 1].cutoffHz && byDist[k].tailS > byDist[k - 1].tailS);
  // the window picks flashes by start time; the time of a tick is counted from the window start
  const part = planReplay(d, 15, 35, { x: 0, y: 0, yawDeg: 0 }, (i) => pos[i], { speed: 5 });
  assert.deepEqual(part.map((e) => e.flash).sort(), [1, 2]);
  assert.ok(Math.abs(part.find((e) => e.flash === 1).tickAt - 1) < 1e-9);
  // an LMA flash is 8 km up: the distance includes the height
  const lma = planReplay(data('lma'), 0, 100, { x: 0, y: 0, yawDeg: 0 }, () => [6, 0], { speed: 1 });
  assert.ok(Math.abs(lma[0].distKm - 10) < 1e-9);
  // deterministic
  assert.deepEqual(planReplay(d, 0, 100, { x: 0, y: 0, yawDeg: 0 }, (i) => pos[i], { speed: 10 }), ev);
  // a position that is not a number is skipped, never guessed
  assert.equal(planReplay(d, 0, 100, { x: 0, y: 0, yawDeg: 0 }, () => [NaN, 0], { speed: 1 }).length, 0);
});

test('synthetic thunder is deterministic and never clips', () => {
  const a = synthThunder({ seed: 7, distKm: 8, energy: 0.7, sampleRate: 8000 });
  const b = synthThunder({ seed: 7, distKm: 8, energy: 0.7, sampleRate: 8000 });
  assert.equal(a.length, b.length);
  assert.ok(a.every((v, i) => v === b[i]), 'same seed, same samples');
  const c = synthThunder({ seed: 8, distKm: 8, energy: 0.7, sampleRate: 8000 });
  assert.ok(a.some((v, i) => v !== c[i]), 'another seed, another thunder');
  for (const dist of [0.5, 2, 8, 30, 80]) {
    const s = synthThunder({ seed: 3, distKm: dist, energy: 1, sampleRate: 8000 });
    assert.ok(s.every(Number.isFinite));
    assert.ok(peakDb(s) <= 20 * Math.log10(0.95) + 1e-6, `peak at ${dist} km`);
  }
});

test('farther thunder is quieter in level, dull and longer', () => {
  const sr = 8000;
  const rms = [];
  const len = [];
  for (const d of [3, 6, 12, 24, 48]) {
    const s = synthThunder({ seed: 5, distKm: d, energy: 0.8, sampleRate: sr });
    rms.push(rmsDb(s.subarray(0, Math.floor(1.0 * sr)))); // the same first second of each
    len.push(s.length);
  }
  for (let k = 1; k < rms.length; k++) assert.ok(rms[k] < rms[k - 1], `level falls with distance (${rms.map((v) => v.toFixed(1))})`);
  for (let k = 1; k < len.length; k++) assert.ok(len[k] > len[k - 1], 'the tail lengthens with distance');
  // a stronger flash is louder at the same distance
  assert.ok(rmsDb(synthThunder({ seed: 5, distKm: 10, energy: 1, sampleRate: sr })) > rmsDb(synthThunder({ seed: 5, distKm: 10, energy: 0.1, sampleRate: sr })));
  // far thunder has less high-frequency content than near thunder: its sample-to-sample change is smaller relative to its level
  const rough = (s) => { let d = 0, e = 0; for (let i = 1; i < s.length; i++) { d += (s[i] - s[i - 1]) ** 2; e += s[i] ** 2; } return d / e; };
  assert.ok(rough(synthThunder({ seed: 5, distKm: 60, energy: 0.8, sampleRate: sr })) < rough(synthThunder({ seed: 5, distKm: 5, energy: 0.8, sampleRate: sr })));
});

test('loudness: normalising reaches the target level, and the ceiling wins when the peak would pass it', () => {
  const sr = 8000;
  const s = synthThunder({ seed: 2, distKm: 15, energy: 0.8, sampleRate: sr });
  const before = rmsDb(s);
  const g = normaliseTo(s, -30, -1);
  assert.ok(g > 0);
  assert.ok(Math.abs(rmsDb(s) - -30) < 1e-6 || peakDb(s) <= -1 + 1e-9);
  assert.ok(peakDb(s) <= -1 + 1e-6, 'no clipping');
  assert.ok(before !== rmsDb(s) || g === 1);
  // a target so loud that the ceiling must win: the peak sits at the ceiling and the RMS is below the target
  const t = synthThunder({ seed: 2, distKm: 15, energy: 0.8, sampleRate: sr });
  normaliseTo(t, -3, -12);
  assert.ok(Math.abs(peakDb(t) - -12) < 1e-6 && rmsDb(t) < -3);
  // silence is left alone
  const z = new Float32Array(100);
  assert.equal(normaliseTo(z, -20, -6), 1);
  assert.ok(z.every((v) => v === 0));
});

test('flash ticks are short, deterministic and louder for stronger flashes', () => {
  const a = synthTick({ seed: 1, strength: 0.2, sampleRate: 8000 });
  const b = synthTick({ seed: 1, strength: 0.9, sampleRate: 8000 });
  assert.equal(a.length, 400);
  assert.ok(peakDb(b) > peakDb(a));
  assert.ok(synthTick({ seed: 1, strength: 0.2, sampleRate: 8000 }).every((v, i) => v === a[i]));
});
