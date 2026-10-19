// @ts-nocheck - plain node:test file, not part of the app bundle
import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveSeed, axisAt } from './seed.ts';
import { treeState } from './treeDamage.ts';
import { buildLandscape, sampleLand } from './landscape.ts';
import { buildNoiseVolume, gradientNoise } from './noise3d.ts';

const R = resolveSeed({
  width: 366, ef: 'EF2',
  track: [{ t: 240, lat: 33.1008, lon: -88.4691 }, { t: 960, lat: 33.2105, lon: -88.3316 }],
  thermo: { t0: 15.7, td0: 15.4, p0: 994.4 },
});

test('trees on the track go down as the vortex passes, trees far from it stay up', () => {
  const mid = axisAt(R, 600);
  // a tree 75 m to the side of the track, hit at t = 600
  const nx = -R.translation[1] / R.speed, ny = R.translation[0] / R.speed;
  const x = mid.x + 75 * nx, y = mid.y + 75 * ny;
  assert.equal(treeState(R, x, y, false, 560).fall, 0, 'upright well before');
  const during = treeState(R, x, y, false, 600);
  assert.ok(during.fall > 0.3);
  const after = treeState(R, x, y, false, 620);
  assert.ok(after.fall === 1 && Math.abs(after.tilt - 1.45) < 1e-9, 'lying after');
  assert.ok(Math.hypot(after.dir[0], after.dir[1]) > 0.99);
  // 500 m away: never falls
  const far = treeState(R, mid.x + 500 * nx, mid.y + 500 * ny, false, 700);
  assert.equal(far.fall, 0);
  assert.ok(far.peak < 30);
  // pines fail a little earlier than hardwoods: a tree at the edge of the hardwood failure distance
  let rEdge = 0;
  for (let d = 70; d < 400; d += 1) if (treeState(R, mid.x + d * nx, mid.y + d * ny, false, 700).fall === 0) { rEdge = d; break; }
  assert.ok(rEdge > 60 && rEdge < 300, `hardwoods fail within ${rEdge} m`);
  assert.ok(treeState(R, mid.x + rEdge * nx, mid.y + rEdge * ny, true, 700).fall > 0, 'a pine at that distance has failed');
});

test('the procedural landscape is deterministic and has the scale cues (fields, woods, roads, trees, farms)', () => {
  const a = buildLandscape({ cx: 0, cy: 0, half: 1500, size: 256, seed: 7, focus: [0, 0], treeRadius: 500 });
  const b = buildLandscape({ cx: 0, cy: 0, half: 1500, size: 256, seed: 7, focus: [0, 0], treeRadius: 500 });
  assert.deepEqual(Array.from(a.data.slice(0, 4000)), Array.from(b.data.slice(0, 4000)));
  assert.deepEqual(Array.from(a.trees.slice(0, 60)), Array.from(b.trees.slice(0, 60)));
  const classes = new Set();
  let roads = 0, trees = 0;
  for (let i = 0; i < a.data.length; i += 4) { classes.add(a.data[i]); if (a.data[i + 2] > 128) roads++; if (a.data[i + 3] > 128) trees++; }
  assert.ok(classes.size >= 4, `classes ${[...classes]}`);
  assert.ok(roads > 100 && roads < 0.2 * 256 * 256, `road pixels ${roads}`);
  assert.ok(trees > 1000);
  assert.ok(a.trees.length / 6 > 100, `trees ${a.trees.length / 6}`);
  assert.ok(sampleLand(a, 0, 0) !== null && sampleLand(a, 99999, 0) === null);
});

test('the noise volume is tileable (opposite faces match) and deterministic', () => {
  const v = buildNoiseVolume(16, 3);
  assert.equal(v.length, 16 * 16 * 16 * 4);
  const w = buildNoiseVolume(16, 3);
  assert.deepEqual(Array.from(v.slice(0, 500)), Array.from(w.slice(0, 500)));
  // periodicity of the generator: noise at x and x + period are equal
  assert.ok(Math.abs(gradientNoise(0.37, 1.2, 2.9, 4, 1) - gradientNoise(4.37, 1.2, 2.9, 4, 1)) < 1e-12);
  let mean = 0;
  for (let i = 0; i < v.length; i += 4) mean += v[i];
  mean /= v.length / 4;
  assert.ok(mean > 100 && mean < 160, `mean ${mean}`);
});
