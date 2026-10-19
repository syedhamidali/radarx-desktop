// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  CONTROLS, GROUPS, defaultLook, validateLook, parseLookText, loadLookText, lookToJson, lookToToml, changedLook, suggestKey,
  PRESETS, presetNamed, applyPreset, matchingPreset, loadUserPresets, saveUserPreset, deleteUserPreset, LookError,
  postProcess, postParams, whiteBalance, hexToRgb, rgbToHex, colourOr,
} from './lookConfig.ts';
import { sampleOptions, buildRealisticTf, realisticSample, lookFor, lookWith } from './realisticStyle.ts';
import { sunLight } from './sunPosition.ts';

class Store {
  constructor() { this.m = new Map(); }
  getItem(k) { return this.m.has(k) ? this.m.get(k) : null; }
  setItem(k, v) { this.m.set(k, v); }
}

test('every control has a group, a range with its default inside, and a help line', () => {
  const keys = new Set();
  for (const q of CONTROLS) {
    assert.ok(!keys.has(q.key), `duplicate key ${q.key}`);
    keys.add(q.key);
    assert.ok(GROUPS.includes(q.group), `${q.key}: group ${q.group}`);
    assert.ok(q.help.length > 10 && q.label.length > 2);
    if (q.kind === 'number') {
      assert.ok(q.min < q.max && q.step > 0, q.key);
      assert.ok(q.def >= q.min && q.def <= q.max, `${q.key}: default ${q.def} outside ${q.min}..${q.max}`);
    } else if (q.kind === 'choice') {
      assert.ok(q.choices.includes(q.def), q.key);
    } else {
      assert.ok(q.def === '' || /^#[0-9a-f]{6}$/.test(q.def), q.key);
    }
  }
  assert.ok(CONTROLS.length >= 40);
  // the keys asked for
  for (const k of ['exposure', 'brightness', 'contrast', 'saturation', 'gamma', 'temperature', 'tint', 'cloudAlbedo', 'densityScale', 'lowDbzCutoff',
    'shadowStrength', 'shadowSoftness', 'ambient', 'sunElevation', 'sunAzimuth', 'sunColor', 'sunIntensity', 'rim', 'detail', 'hazeColor', 'hazeDistanceKm',
    'skyZenith', 'skyHorizon', 'sunGlow', 'oceanColor', 'whitecaps', 'rainStreaks', 'rainColor', 'rampLow', 'rampMid', 'rampHeavy', 'rampHail',
    'lightningBrightness', 'lightningColor', 'terrainBrightness', 'terrainSaturation', 'bloom', 'vignette', 'grain', 'fov', 'shake']) {
    assert.ok(keys.has(k), `missing control ${k}`);
  }
});

test('an unknown key is a clear error with a suggestion; wrong types, ranges, choices and colours are errors naming the key', () => {
  assert.throws(() => validateLook({ exposur: 1 }), (e) => e instanceof LookError && /unknown look key "exposur"/.test(e.message) && /did you mean "exposure"/.test(e.message));
  assert.throws(() => validateLook({ saturaton: 1 }), /did you mean "saturation"/);
  assert.throws(() => validateLook({ qqqqqqqq: 1 }), (e) => /unknown look key "qqqqqqqq"/.test(e.message) && !/did you mean/.test(e.message));
  assert.throws(() => validateLook({ exposure: 'bright' }), /"exposure" must be a number/);
  assert.throws(() => validateLook({ exposure: 9 }), /"exposure" = 9 is out of range 0.3 to 3/);
  assert.throws(() => validateLook({ lowDbzCutoff: -5 }), /out of range 0 to 40 dBZ/);
  assert.throws(() => validateLook({ light: 'sunrise' }), /one of daylight, dusk, overcast, night/);
  assert.throws(() => validateLook({ sunColor: 'orange' }), /"sunColor" must be a colour/);
  assert.equal(suggestKey('whitecap'), 'whitecaps');
  assert.equal(suggestKey('Exposure'), 'exposure');
  // valid input is merged on the defaults and case of colours is normalised
  const v = validateLook({ exposure: 1.5, sunColor: '#FFAA00' });
  assert.equal(v.exposure, 1.5);
  assert.equal(v.sunColor, '#ffaa00');
  assert.equal(v.contrast, 1);
});

test('JSON and TOML look files parse to the same settings; errors point to the line', () => {
  const json = '{ "exposure": 1.6, "light": "overcast", "sunColor": "#ffcc88", "whitecaps": 0.5 }';
  const toml = `# my look
[scene]
light = "overcast"
[exposure_and_colour]
exposure = 1.6     # brighter
[light]
sunColor = '#ffcc88'
[ground_and_ocean]
whitecaps = 0.5
`;
  assert.deepEqual(loadLookText(json, 'json'), loadLookText(toml, 'toml'));
  assert.throws(() => loadLookText('exposure 1.6', 'toml'), /line 1: cannot read/);
  assert.throws(() => loadLookText('exposure = high', 'toml'), /line 1: value of "exposure"/);
  assert.throws(() => loadLookText('exposur = 1.6', 'toml'), /did you mean "exposure"/);
  assert.throws(() => loadLookText('{ nope', 'json'), /not valid JSON/);
  assert.throws(() => loadLookText('[1,2]', 'json'), /must be a JSON object/);
  // a saved preset file {name, look} is accepted
  assert.equal(loadLookText('{"name":"x","look":{"exposure":1.9}}', 'json').exposure, 1.9);
});

test('every built-in preset validates, differs from the others, and round-trips through JSON and TOML', () => {
  assert.deepEqual(PRESETS.map((p) => p.name), [
    'Daylight cumulonimbus', 'Golden hour', 'Dusk', 'Stormy overcast', 'Night + lightning', 'Hurricane (daylight)', 'Hurricane (stormy)', 'Satellite white', 'Scientific (dBZ)',
  ]);
  const seen = new Set();
  for (const p of PRESETS) {
    const cfg = applyPreset(p.name);
    const j = lookToJson(cfg, p.name);
    assert.deepEqual(loadLookText(j, 'json'), cfg, `${p.name} via JSON`);
    assert.deepEqual(loadLookText(lookToToml(cfg), 'toml'), cfg, `${p.name} via TOML`);
    assert.equal(matchingPreset(cfg), p.name);
    const sig = JSON.stringify(changedLook(cfg));
    assert.ok(!seen.has(sig), `${p.name} equals another preset`);
    seen.add(sig);
  }
  assert.equal(applyPreset('Scientific (dBZ)').style, 'scientific');
  assert.equal(applyPreset('Night + lightning').light, 'night');
  assert.equal(applyPreset('Hurricane (stormy)').palette, 'cyclone');
  assert.throws(() => applyPreset('Nope'), /unknown look preset "Nope"; presets: Daylight cumulonimbus/);
  // a tweaked preset is a custom look
  assert.equal(matchingPreset({ ...applyPreset('Dusk'), exposure: 2 }), null);
});

test('saved presets: stored locally, listed, replaced, deleted, and a built-in name is refused', () => {
  const st = new Store();
  assert.deepEqual(loadUserPresets(st), []);
  const cfg = { ...defaultLook(), exposure: 1.9, temperature: 0.2, rampHeavy: '#334455' };
  saveUserPreset(st, 'My storm', cfg);
  let list = loadUserPresets(st);
  assert.equal(list.length, 1);
  assert.deepEqual(applyPreset('My storm', list), cfg);
  assert.equal(matchingPreset(cfg, list), 'My storm');
  saveUserPreset(st, 'My storm', { ...cfg, exposure: 1.1 });
  list = loadUserPresets(st);
  assert.equal(list.length, 1);
  assert.equal(applyPreset('My storm', list).exposure, 1.1);
  assert.throws(() => saveUserPreset(st, 'Dusk', cfg), /built-in preset name/);
  assert.throws(() => saveUserPreset(st, '  ', cfg), /needs a name/);
  deleteUserPreset(st, 'My storm');
  assert.deepEqual(loadUserPresets(st), []);
  // a corrupt or outdated store never breaks loading
  st.setItem('xradar.look.presets', '{oops');
  assert.deepEqual(loadUserPresets(st), []);
  st.setItem('xradar.look.presets', JSON.stringify([{ name: 'old', look: { removedKey: 1 } }, { name: 'ok', look: { exposure: 1.2 } }]));
  assert.deepEqual(loadUserPresets(st).map((p) => p.name), ['ok']);
});

test('colours: hex round trip, automatic (empty) falls back', () => {
  assert.deepEqual(hexToRgb('#ff8000'), [1, 128 / 255, 0]);
  assert.equal(rgbToHex([1, 0.5, 0]), '#ff8000');
  assert.deepEqual(colourOr({ sunColor: '' }, 'sunColor', [1, 2, 3]), [1, 2, 3]);
  assert.deepEqual(colourOr({ sunColor: '#000000' }, 'sunColor', [1, 2, 3]), [0, 0, 0]);
});

test('the finishing stage is monotonic: brightness, gamma and exposure-like gain make a test frame lighter', () => {
  const grey = (v) => [v, v, v];
  const lum = (c) => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  const cfg = defaultLook();
  const run = (over, v = 0.4) => lum(postProcess(grey(v), postParams({ ...cfg, bloom: 0, vignette: 0, ...over })));
  let prev = -1;
  for (const b of [-0.3, -0.15, 0, 0.1, 0.25, 0.4]) { const l = run({ brightness: b }); assert.ok(l > prev, `brightness ${b}`); prev = l; }
  prev = -1;
  for (const g of [0.6, 0.8, 1, 1.3, 1.8]) { const l = run({ gamma: g }); assert.ok(l > prev, `gamma ${g}`); prev = l; }
  // contrast: darker below mid grey, brighter above
  assert.ok(run({ contrast: 1.6 }, 0.7) > run({ contrast: 1.0 }, 0.7) && run({ contrast: 1.6 }, 0.3) < run({ contrast: 1.0 }, 0.3));
  // saturation 0 is grey; 2 is more colourful than 1
  const c0 = postProcess([0.8, 0.4, 0.2], postParams({ ...cfg, bloom: 0, vignette: 0, saturation: 0 }));
  assert.ok(Math.abs(c0[0] - c0[1]) < 1e-9 && Math.abs(c0[1] - c0[2]) < 1e-9);
  const sp = (s) => { const c = postProcess([0.8, 0.4, 0.2], postParams({ ...cfg, bloom: 0, vignette: 0, saturation: s })); return c[0] - c[2]; };
  assert.ok(sp(2) > sp(1) && sp(1) > sp(0.5));
  // warm white balance raises red over blue, cool the opposite
  const warm = whiteBalance(0.6, 0), cool = whiteBalance(-0.6, 0);
  assert.ok(warm[0] > warm[2] && cool[0] < cool[2]);
  assert.deepEqual(whiteBalance(0, 0), [1, 1, 1]);
  // vignette darkens the corners, not the middle; no value goes outside 0..1
  const vg = postParams({ ...cfg, bloom: 0, vignette: 0.8 });
  assert.ok(lum(postProcess(grey(0.5), vg, [0, 0])) < lum(postProcess(grey(0.5), vg, [0.5, 0.5])));
  for (const v of [0, 0.5, 1]) for (const c of postProcess(grey(v), postParams({ ...cfg, brightness: 0.4, contrast: 1.8 }))) assert.ok(c >= 0 && c <= 1);
});

test('the exposure control scales the scene light monotonically; the look never changes the echo opacity law except by its own density controls', () => {
  const sun = sunLight(new Date(Date.UTC(2021, 11, 11, 18, 0)), 36.15, -89.9);
  const base = lookFor(sun, 4);
  let prev = -1;
  for (const e of [0.3, 0.6, 1, 1.6, 2.4, 3]) {
    const lk = lookWith(base, { ...defaultLook(), exposure: e });
    assert.ok(lk.exposure > prev);
    prev = lk.exposure;
  }
  // sun elevation / colour / intensity overrides reach the light; automatic (-1, '') leaves it alone
  const auto = lookWith(base, defaultLook());
  assert.equal(auto.sunElevationDeg, base.sunElevationDeg);
  const set = lookWith(base, { ...defaultLook(), sunElevation: 12, sunAzimuth: 250, sunColor: '#ff0000', sunIntensity: 2 });
  assert.equal(set.sunElevationDeg, 12);
  assert.equal(set.sunAzimuthDeg, 250);
  assert.deepEqual(set.sunCol, [1, 0, 0]);
  assert.equal(set.sunGain, 2 * base.sunGain);
  assert.equal(lookWith(base, { ...defaultLook(), hazeDistanceKm: 100 }).fog, 0.01);
  // density and cutoff controls shape the opacity; the cutoff hides faint echo
  const cfg = defaultLook();
  const tf1 = buildRealisticTf(-20, 75, 0.35, 'white', sampleOptions(cfg));
  const tf2 = buildRealisticTf(-20, 75, 0.35, 'white', sampleOptions({ ...cfg, densityScale: 2 }));
  const tf3 = buildRealisticTf(-20, 75, 0.35, 'white', sampleOptions({ ...cfg, lowDbzCutoff: 30 }));
  const idx = (dbz) => Math.round(((dbz + 20) / 95) * 255);
  assert.ok(tf2[idx(40) * 4 + 3] > tf1[idx(40) * 4 + 3]);
  assert.equal(tf3[idx(25) * 4 + 3], 0);
  assert.ok(tf1[idx(25) * 4 + 3] > 0);
  // echo colours: ramp colour overrides land in the ramp
  const heavy = realisticSample(45, 'white', sampleOptions({ ...cfg, rampHeavy: '#ff0000', rampMidDbz: 30 }));
  assert.ok(heavy.rgb[0] > heavy.rgb[1] + 0.3, 'a red heavy-rain colour shows');
  // the whiteness control scales the colour
  assert.ok(realisticSample(30, 'white', sampleOptions({ ...cfg, cloudAlbedo: 0.6 })).rgb[0] < 0.7);
});
