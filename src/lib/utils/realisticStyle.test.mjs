// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { realisticSample, buildRealisticTf, lookFor, lightVector } from './realisticStyle.ts';
import { sunLight } from './sunPosition.ts';

const lum = (c) => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];

test('realistic look: opacity grows with reflectivity, nothing is drawn below the haze limit', () => {
  let prev = -1;
  for (let d = -20; d <= 75; d += 1) {
    const { sigma } = realisticSample(d);
    assert.ok(sigma >= prev - 1e-12, `sigma not monotone at ${d} dBZ`);
    prev = sigma;
  }
  assert.equal(realisticSample(0).sigma, 0);
  assert.ok(realisticSample(15).sigma < 0.01, 'light echo is a thin haze');
  assert.ok(realisticSample(35).sigma > 0.05 && realisticSample(35).sigma < 0.4, 'moderate rain is translucent');
  assert.ok(realisticSample(55).sigma > 2, 'heavy cores are dense');
});

test('realistic look: pale haze, grey-white rain, dark cores, a warm tint only for the strongest', () => {
  const light = realisticSample(15).rgb, rain = realisticSample(32).rgb, heavy = realisticSample(52).rgb, core = realisticSample(60).rgb;
  assert.ok(lum(light) > lum(rain) && lum(rain) > lum(heavy) && lum(heavy) >= lum(core) - 1e-9);
  assert.ok(lum(light) > 0.9 && lum(heavy) < 0.5 && lum(core) < 0.4);
  // no warm tint below 55 dBZ (red not above blue by more than a hair), a visible one at 66
  for (const d of [10, 25, 40, 52]) assert.ok(realisticSample(d).rgb[0] - realisticSample(d).rgb[2] < 0.02, `tint at ${d}`);
  const warm = realisticSample(66).rgb;
  assert.ok(warm[0] - warm[2] > 0.05);
});

test('the transfer function has 256 RGBA entries over [vmin, vmax] with a monotone alpha', () => {
  const tf = buildRealisticTf(-20, 75, 0.35);
  assert.equal(tf.length, 1024);
  let prev = -1;
  for (let i = 0; i < 256; i++) {
    assert.ok(tf[i * 4 + 3] >= prev, `alpha drops at ${i}`);
    prev = tf[i * 4 + 3];
  }
  assert.equal(tf[3], 0); // -20 dBZ
  assert.ok(tf[255 * 4 + 3] > 200); // 75 dBZ is nearly opaque within a step
  // entry 128 is about 27 dBZ: translucent, grey-white
  assert.ok(tf[128 * 4 + 3] > 0 && tf[128 * 4 + 3] < 60);
});

test('look: dusk light is warm, day light is whiter, the light vector follows the vertical stretch', () => {
  const dusk = lookFor(sunLight(new Date(Date.UTC(2021, 11, 11, 1, 50)), 36.15, -89.9));
  const day = lookFor(sunLight(new Date(Date.UTC(2021, 11, 11, 18, 0)), 36.15, -89.9));
  assert.ok(dusk.sunCol[0] / dusk.sunCol[2] > day.sunCol[0] / day.sunCol[2]);
  assert.equal(dusk.sunElevationDeg, 9);
  assert.ok(dusk.sunSteps >= 1);
  const v4 = lightVector(dusk, 4);
  const v1 = lightVector(dusk, 1);
  assert.ok(v4[2] > v1[2]);
  assert.ok(Math.abs(Math.hypot(...v4) - 1) < 1e-12);
});
