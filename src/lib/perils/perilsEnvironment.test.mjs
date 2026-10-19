// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { chooseEnvironmentPair, sbCape, pickEnvSounding } from './perilsData.ts';

const snd = (id, site, launch, x, y, cape, hasLevels = true) => ({
  id, site, launch, launchIso: id, x, y, drift: [], levels: hasLevels ? { pressure_hpa: [1000], height_msl_m: [0], temperature_c: [20], dewpoint_c: [10], u_ms: [0], v_ms: [0] } : null,
  diagnostics: { parcels: { 'surface-based': { cape } } },
});

test('the environment is the pre-storm sounding with the most CAPE near the line; the post-storm one is the same site later', () => {
  const list = [
    snd('far-high', 'FAR', -10000, 400, 400, 800),
    snd('near-low', 'NEAR', -9000, 10, 0, 100),
    snd('near-high', 'S5', -8000, 20, 10, 560),
    snd('in-window', 'S5', 100, 20, 10, 5), // launched inside the window: not pre-storm
    snd('s5-after', 'S5', 2000, 20, 10, 0),
    snd('s5-later', 'S5', 4000, 20, 10, 0),
    snd('other-after', 'NEAR', 2500, 10, 0, 0),
  ];
  const r = chooseEnvironmentPair(list, 960, [25, 0]);
  assert.equal(r.pre.id, 'near-high');
  assert.equal(r.post.id, 's5-after');
});

test('no pre-storm sounding with a profile gives nothing; no post-storm one gives a pre-storm sounding alone', () => {
  assert.equal(chooseEnvironmentPair([snd('x', 'A', 100, 0, 0, 100)], 960, [0, 0]), null);
  assert.equal(chooseEnvironmentPair([snd('x', 'A', -5000, 0, 0, 100, false)], 960, [0, 0]), null);
  const r = chooseEnvironmentPair([snd('x', 'A', -5000, 0, 0, 100)], 960, [0, 0]);
  assert.equal(r.pre.id, 'x');
  assert.equal(r.post, null);
});

test('CAPE from the engine diagnostics, null when missing', () => {
  assert.equal(sbCape(snd('a', 'A', 0, 0, 0, 321)), 321);
  assert.equal(sbCape({ diagnostics: {} }), null);
  assert.equal(sbCape({ diagnostics: { parcels: { 'surface-based': { cape: null } } } }), null);
});

test('the environment numbers come from the sounding nearest in time and place that has them', () => {
  const env = [
    { id: 'a', site: 'A', launch: '', launch_t_s: -3600, x_km: [0, 0], sbcape: 400 },
    { id: 'b', site: 'B', launch: '', launch_t_s: -3600, x_km: [200, 0], sbcape: 900 },
    { id: 'c', site: 'C', launch: '', launch_t_s: -60, x_km: [0, 0], sbcape: null, mlcape: null },
  ];
  assert.equal(pickEnvSounding(env, 0, [0, 0]).id, 'a');
  assert.equal(pickEnvSounding([env[2]], 0, [0, 0]), null);
});
