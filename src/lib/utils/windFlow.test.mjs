// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildFlowField, cellConfidence, sampleFlow, rk2Step, advect, trailPoints, seedCells, seedParticles, positionInCell,
  particleFade, colorValue, defaultVisualSpeed, describeVisualSpeed, activeParticles, physicsCheck, formatUtc,
  hasRetrievedW, levelsUniform, lifetimeFor, mulberry32, animPhases, driftSource,
} from './windFlow.ts';

/** A flow field on [xlo, xhi] x [ylo, yhi] x [zlo, zhi] km with 1 km cells; fn(x, y, z) -> [u, v, w] m/s or null. */
function field(fn, { x = [-20, 20], y = [-20, 20], z = [0, 10], d = 1 } = {}) {
  const nx = Math.round((x[1] - x[0]) / d) + 1, ny = Math.round((y[1] - y[0]) / d) + 1, nz = Math.round((z[1] - z[0]) / d) + 1;
  const data = new Float32Array(nx * ny * nz * 4);
  let valid = 0;
  for (let k = 0; k < nz; k++) for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const q = (k * ny + j) * nx + i;
    const r = fn(x[0] + i * d, y[0] + j * d, z[0] + k * d);
    if (!r) continue;
    data.set([r[0], r[1], r[2], 1], q * 4);
    valid++;
  }
  return { nx, ny, nz, x0: x[0], y0: y[0], z0: z[0], dx: d, dy: d, dz: d, data, validCells: valid, totalCells: nx * ny * nz, wRetrieved: true };
}

test('uniform flow: a straight line at the right speed', () => {
  const f = field(() => [10, 0, 0]);
  const r = advect(f, [-15, 2, 5], 1000); // 10 m/s for 1000 s = 10 km
  assert.ok(r.ok);
  assert.ok(Math.abs(r.pos[0] - -5) < 1e-4, `x ${r.pos[0]}`);
  assert.ok(Math.abs(r.pos[1] - 2) < 1e-6 && Math.abs(r.pos[2] - 5) < 1e-6);
  const g = field(() => [3, -4, 0]); // speed 5: direction preserved
  const q = advect(g, [0, 0, 5], 2000);
  assert.ok(Math.abs(q.pos[0] - 6) < 1e-4 && Math.abs(q.pos[1] + 8) < 1e-4);
});

test('solid-body rotation: a circle of the right radius and period', () => {
  const T = 3600;
  const om = (2 * Math.PI) / T;
  const f = field((x, y) => [-om * y * 1000, om * x * 1000, 0], { x: [-30, 30], y: [-30, 30] });
  const p0 = [10, 0, 5];
  let p = p0;
  const steps = 120; // 30 s each
  for (let i = 0; i < steps; i++) {
    const r = advect(f, p, T / steps);
    assert.ok(r.ok);
    p = r.pos;
    const radius = Math.hypot(p[0], p[1]);
    assert.ok(Math.abs(radius - 10) < 0.05, `radius ${radius} at step ${i}`);
  }
  assert.ok(Math.hypot(p[0] - p0[0], p[1] - p0[1]) < 0.1, `after one period the particle is at ${p}`);
  // a quarter period turns it by 90 degrees (counter-clockwise: east -> north)
  let q = p0;
  for (let i = 0; i < steps / 4; i++) q = advect(f, q, T / steps).pos;
  assert.ok(Math.abs(q[0]) < 0.1 && Math.abs(q[1] - 10) < 0.1, `quarter period at ${q}`);
});

test('the vertical component moves the particle at w', () => {
  const f = field(() => [0, 0, 2]);
  const r = advect(f, [0, 0, 1], 1500); // 2 m/s for 1500 s = 3 km
  assert.ok(r.ok && Math.abs(r.pos[2] - 4) < 1e-4);
  const g = field(() => [10, 0, -1]);
  const s = advect(g, [-10, 0, 8], 1000);
  assert.ok(Math.abs(s.pos[0] - 0) < 1e-4 && Math.abs(s.pos[2] - 7) < 1e-4);
});

test('a particle never enters a cell without wind: it stops instead', () => {
  const f = field((x) => (x <= 10 ? [10, 0, 0] : null)); // no wind east of x = 10 km
  let p = [0, 0, 5];
  let r = advect(f, p, 3000); // would reach x = 30 km
  assert.equal(r.ok, false);
  assert.ok(r.pos[0] <= 10 + 1e-9, `stopped at ${r.pos}`);
  assert.ok(sampleFlow(f, ...r.pos) !== null, 'it stops on a valid sample');
  // from the stop position it cannot move on (the next step needs invalid wind)
  const again = advect(f, r.pos, 100);
  assert.ok(again.pos[0] <= 10 + 1e-9);
  // a single step towards the void is refused as a whole
  assert.equal(rk2Step(f, [9.9, 0, 5], 1000), null);
});

test('a sample with any invalid corner is invalid; elsewhere it is exact', () => {
  const f = field((x, y, z) => (x === 3 && y === 4 && z === 5 ? null : [x, y, z]));
  assert.equal(sampleFlow(f, 2.5, 3.5, 4.5), null); // touches the bad node
  assert.equal(sampleFlow(f, 3.5, 4.5, 5.5), null);
  const ok = sampleFlow(f, 7.25, 1.5, 2.75); // trilinear of a linear field is exact
  assert.ok(ok && Math.abs(ok[0] - 7.25) < 1e-5 && Math.abs(ok[1] - 1.5) < 1e-5 && Math.abs(ok[2] - 2.75) < 1e-5);
  assert.equal(sampleFlow(f, 25, 0, 5), null, 'outside the grid');
  assert.equal(sampleFlow(f, 0, 0, -0.5), null);
  assert.ok(sampleFlow(f, 20, 20, 10) !== null, 'the last node is inside');
});

test('trails follow the streamline backwards and are cut where the wind ends', () => {
  const f = field((x) => (x >= -5 ? [10, 0, 0] : null));
  const t = trailPoints(f, [0, 0, 5], 8, 100); // 1 km per step, backwards = west
  assert.equal(t.length, 8);
  assert.ok(Math.abs(t[1].p[0] + 1) < 1e-4 && Math.abs(t[3].p[0] + 3) < 1e-4);
  assert.ok(t.slice(0, 6).every((e) => e.conf > 0));
  assert.equal(t[6].conf, 0, 'beyond x = -5 km there is no wind: no trail');
  assert.equal(t[7].conf, 0);
  assert.ok(t[7].p[0] >= -5 - 1e-9, 'and nothing is extrapolated');
  assert.equal(trailPoints(f, [-9, 0, 5], 4, 100)[0].conf, 0, 'a head outside the wind has no trail');
});

test('seeding: only inside cells whose corners all have wind, deterministic, level range honoured', () => {
  const f = field((x, y) => (x < 5 && y > -5 ? [1, 0, 0] : null));
  const cells = seedCells(f, 0, 100);
  assert.ok(cells.length > 100);
  const init = seedParticles(f, cells, 500, 6, 7);
  for (let i = 0; i < 500; i++) {
    const s = sampleFlow(f, init.state[i * 4], init.state[i * 4 + 1], init.state[i * 4 + 2]);
    assert.ok(s !== null, `particle ${i} was born without wind`);
    assert.ok(init.state[i * 4 + 3] >= 0 && init.state[i * 4 + 3] < init.aux[i * 4]);
    assert.equal(init.aux[i * 4 + 1], -1);
  }
  const again = seedParticles(f, cells, 500, 6, 7);
  assert.deepEqual([...again.state.slice(0, 40)], [...init.state.slice(0, 40)]);
  const other = seedParticles(f, cells, 500, 6, 8);
  assert.notDeepEqual([...other.state.slice(0, 40)], [...init.state.slice(0, 40)]);
  // level range: cells whose centre is inside [2, 4] km
  const rng = seedCells(f, 2, 4);
  const q = positionInCell(f, rng[0], 0.5, 0.5, 0.5);
  assert.ok(q[2] >= 2 && q[2] <= 4);
  assert.ok(rng.length < cells.length);
  // nothing to seed: the particles never draw
  const none = seedParticles(f, new Uint32Array(0), 10, 6, 1);
  assert.ok([...none.aux].filter((_, i) => i % 4 === 0).every((l) => l === 0));
});

test('lifetime and fades: in, out, and out after stopping', () => {
  const life = lifetimeFor(6, 0.5);
  assert.ok(Math.abs(life - 6) < 1e-9);
  assert.ok(lifetimeFor(6, 0) < 6 && lifetimeFor(6, 0.999) > 6);
  assert.equal(particleFade(0, 6, -1, 0.5, 0.5), 0);
  assert.equal(particleFade(3, 6, -1, 0.5, 0.5), 1);
  assert.ok(particleFade(5.75, 6, -1, 0.5, 0.5) <= 0.5);
  assert.ok(particleFade(0.25, 6, -1, 0.5, 0.5) > 0 && particleFade(0.25, 6, -1, 0.5, 0.5) < 1);
  assert.equal(particleFade(3, 6, 0.5, 0.5, 0.5), 0, 'fully faded once the fade-out time since stopping has passed');
  assert.ok(particleFade(3, 6, 0.1, 0.5, 0.5) > particleFade(3, 6, 0.3, 0.5, 0.5));
  const r = mulberry32(3);
  assert.ok([r(), r(), r()].every((x) => x >= 0 && x < 1));
});

function windVol(over = {}) {
  const n = 2 * 2 * 2;
  const mk = (v) => new Float32Array(n).fill(v);
  return {
    nx: 2, ny: 2, nz: 2, x0: 0, y0: 0, dx: 1, dy: 1, z: Float64Array.from([1, 2]),
    fields: { u: mk(5), v: mk(1), w: mk(0.5), crossing_angle: mk(60), n_radars: mk(2), uncertainty: mk(1), divergence: mk(0.4), speed: mk(5) },
    ...over,
  };
}

test('the mask: crossing angle, number of radars, uncertainty, finite u / v / w', () => {
  const w = windVol();
  const o = { minCrossing: 30, maxUncertainty: null, minRadars: 2 };
  assert.ok(cellConfidence(w, o, 0, true) > 0);
  w.fields.crossing_angle[1] = 20;
  assert.equal(cellConfidence(w, o, 1, true), 0);
  w.fields.n_radars[2] = 1;
  assert.equal(cellConfidence(w, o, 2, true), 0);
  w.fields.u[3] = NaN;
  assert.equal(cellConfidence(w, o, 3, true), 0);
  w.fields.w[4] = NaN;
  assert.equal(cellConfidence(w, o, 4, true), 0, 'strict: w must be finite when the analysis retrieved w');
  assert.ok(cellConfidence(w, o, 4, false) > 0, 'horizontal flow does not need w');
  w.fields.uncertainty[5] = 8;
  assert.equal(cellConfidence(w, { ...o, maxUncertainty: 5 }, 5, true), 0, 'hide where uncertainty > X');
  assert.ok(cellConfidence(w, { ...o, maxUncertainty: 20 }, 5, true) > 0);
});

test('confidence falls with weak crossing and with uncertainty', () => {
  const w = windVol();
  const o = { minCrossing: 30, maxUncertainty: null, minRadars: 2 };
  w.fields.crossing_angle[0] = 90;
  w.fields.crossing_angle[1] = 32;
  const strong = cellConfidence(w, o, 0, true);
  const weak = cellConfidence(w, o, 1, true);
  assert.ok(strong > weak && weak > 0, `${strong} vs ${weak}`);
  w.fields.uncertainty[0] = 0.5;
  w.fields.uncertainty[2] = 9;
  assert.ok(cellConfidence(w, o, 0, true) > cellConfidence(w, o, 2, true));
});

test('the flow field has no wind where the mask says so, and w is 0 only when it was not retrieved', () => {
  const w = windVol();
  w.fields.crossing_angle[0] = 10;
  const f = buildFlowField(w, { minCrossing: 30, maxUncertainty: null, minRadars: 2 });
  assert.equal(f.wRetrieved, true);
  assert.equal(f.data[3], 0);
  assert.equal(f.validCells, 7);
  assert.equal(f.data[4 * 1 + 2], 0.5);
  const noW = windVol();
  noW.fields.w.fill(NaN);
  const h = buildFlowField(noW, { minCrossing: 30, maxUncertainty: null, minRadars: 2 });
  assert.equal(hasRetrievedW(noW), false);
  assert.equal(h.wRetrieved, false);
  assert.equal(h.validCells, 8);
  assert.equal(h.data[2], 0);
  assert.ok(levelsUniform([1, 2, 3]) && !levelsUniform([1, 2, 4]));
});

test('physics read-out: extremes, valid share, mean divergence and warnings', () => {
  const w = windVol();
  w.fields.u[0] = 95; // close to the QC limit
  const f = buildFlowField(w, { minCrossing: 30, maxUncertainty: null, minRadars: 2 });
  const p = physicsCheck(w, f, { max_speed: 100, max_abs_w: 40, masked_cells: 12 });
  assert.ok(Math.abs(p.maxSpeed - Math.hypot(95, 1)) < 1e-3);
  assert.equal(p.maxAbsW, 0.5);
  assert.equal(p.validShare, 100);
  assert.ok(Math.abs(p.meanDivergence - 0.4) < 1e-6);
  assert.ok(p.warnings.some((s) => s.includes('QC removed 12')));
  assert.ok(p.warnings.some((s) => s.includes('QC limit of 100')));
  const noW = windVol();
  noW.fields.w.fill(NaN);
  const q = physicsCheck(noW, buildFlowField(noW, { minCrossing: 0, maxUncertainty: null, minRadars: 2 }), null);
  assert.equal(q.maxAbsW, null);
  assert.ok(q.warnings.some((s) => s.includes('horizontal only')));
});

test('visual speed: a 30 m/s wind crosses the domain in about 10 animation seconds', () => {
  const extent = 200;
  const k = defaultVisualSpeed(extent);
  const crossingSeconds = (extent * 1000) / (30 * k);
  assert.ok(Math.abs(crossingSeconds - 10) < 0.1, `${crossingSeconds}`);
  assert.equal(describeVisualSpeed(667), '1 s = 11 min of wind motion');
  assert.equal(describeVisualSpeed(30), '1 s = 30 s of wind motion');
});

test('colour value, particle count by quality level, UTC label', () => {
  assert.equal(colorValue('none', 10, 0, 0, 50), 0);
  assert.equal(colorValue('speed', 30, 40, 0, 50), 1);
  assert.equal(colorValue('speed', 15, 0, 0, 30), 0.5);
  assert.equal(colorValue('w', 0, 0, 0, 5), 0.5);
  assert.equal(colorValue('w', 0, 0, -5, 5), 0);
  assert.equal(colorValue('w', 0, 0, 50, 5), 1);
  assert.equal(activeParticles(40000, 4), 40000);
  assert.ok(activeParticles(40000, 1) < activeParticles(40000, 3));
  assert.equal(activeParticles(40000, 0), 8000);
  assert.equal(formatUtc('2021-12-11T02:04:18'), '02:04 UTC');
  assert.equal(formatUtc('2021-12-11T02:04:18+00:00'), '02:04 UTC');
  assert.equal(formatUtc(null), null);
});

test('field animation: the two phases cross-fade without a seam, and cells drift with the wind only', () => {
  for (let i = 0; i <= 20; i++) {
    const [p1, p2, w1, w2] = animPhases(i / 20);
    assert.ok(Math.abs(w1 + w2 - 1) < 1e-12);
    assert.ok(Math.abs(p2 - ((p1 + 0.5) % 1)) < 1e-12);
  }
  // a phase has no weight at its reset, so the jump of the sawtooth is never visible
  assert.equal(animPhases(0)[2], 0);
  assert.equal(animPhases(0.5)[3], 0);
  assert.equal(animPhases(0.5)[2], 1);
  // the echo comes from upstream: a 10 m/s west wind for 1000 s moves the echo 10 km east
  const src = driftSource([0, 0, 5], [10, 0, 0], 1000);
  assert.deepEqual(src, [-10, 0, 5]);
  // no wind, no drift
  assert.deepEqual(driftSource([1, 2, 3], [0, 0, 0], 5000), [1, 2, 3]);
  // the vertical component is shown in the exaggerated scene
  assert.equal(driftSource([0, 0, 5], [0, 0, 2], 1000, 6)[2], 5 - 12);
});
