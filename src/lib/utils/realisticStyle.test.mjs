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

test('realistic look: pale haze, grey rain, dark slate cores, no purple or pink anywhere, a green-grey hint only in the strongest', () => {
  for (const pal of ['white', 'soft', 'stormy']) {
    const light = realisticSample(15, pal).rgb, rain = realisticSample(32, pal).rgb, heavy = realisticSample(52, pal).rgb, core = realisticSample(60, pal).rgb;
    assert.ok(lum(light) >= lum(rain) - 1e-9 && lum(rain) > lum(heavy) && lum(heavy) >= lum(core) - 1e-9, pal);
    assert.ok(lum(light) > 0.9 && lum(heavy) < 0.9 && lum(core) < 0.5, pal);
    for (let d = -20; d <= 75; d += 1) {
      const [r, g, b] = realisticSample(d, pal).rgb;
      assert.ok(b - g < 0.08, `${pal} ${d} dBZ: violet (b ${b}, g ${g})`);
      assert.ok(r - g < 0.04 && r - b < 0.06, `${pal} ${d} dBZ: pink or orange (r ${r}, g ${g}, b ${b})`);
    }
  }
  // the green-grey hail hint: none below 55 dBZ, visible at 66
  const [r0, g0] = realisticSample(50).rgb;
  assert.ok(g0 - r0 < 0.03);
  const [r1, g1] = realisticSample(66).rgb;
  assert.ok(g1 - r1 > 0.03, `hint ${g1 - r1}`);
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

test('cloud body: the opacity of moderate echo is substantial and white', () => {
  const body = realisticSample(35, 'white');
  assert.ok(body.sigma > 0.1 && lum(body.rgb) > 0.95, `${body.sigma} ${lum(body.rgb)}`);
  assert.ok(realisticSample(10, 'white').sigma < 0.01);
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
