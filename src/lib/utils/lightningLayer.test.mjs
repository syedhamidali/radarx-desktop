// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  f32, i32, decodeLightning, windowSeconds, lowerBound, fade, MIN_FADE, visiblePoints, channelsOf, coveredFraction,
  windowCoverageText, rateSeries, dataSpan, niceBin, flashRate, pointRate, placeAll, scenePoints, windowStats, flashesText,
  coverageNote, DEFAULT_WINDOW, boxAround, buildOpenRequest, DEFAULT_OPEN,
} from './lightningLayer.ts';

const b64f = (arr) => Buffer.from(new Float32Array(arr).buffer).toString('base64');
const b64i = (arr) => Buffer.from(new Int32Array(arr).buffer).toString('base64');

/** A wire message with 2 flashes: A has 3 points (t = 10, 12, 14 s), B has 2 (t = 100, 101 s); the data cover 0..60 and 90..150 s. */
function wire(over = {}) {
  return {
    kind: 'lma',
    instrument: 'LMA (test)',
    t_ref: '2021-12-11T02:00:00.000Z',
    files: ['a.dat'],
    points: {
      n: 5,
      t: b64f([10, 12, 14, 100, 101]),
      lat: b64f([36.5, 36.5, 36.5, 36.5, 36.5]),
      lon: b64f([-89.0, -88.99, -88.98, -88.5, -88.49]),
      alt_km: b64f([7, 7.5, 8, 9, 9]),
      strength: b64f([10, 11, 12, 5, 6]),
      brightness: b64f([1, 0.8, 0.6, 0.2, 0.4]),
      stations: b64i([6, 6, 7, 8, 8]),
      flash: b64i([0, 0, 0, 1, 1]),
    },
    flashes: {
      n: 2,
      t0: b64f([10, 100]),
      t1: b64f([14, 101]),
      lat: b64f([36.5, 36.5]),
      lon: b64f([-89.0, -88.5]),
      alt_km: b64f([7, 9]),
      n_points: b64i([3, 2]),
      strength: b64f([NaN, NaN]),
      area_km2: b64f([NaN, NaN]),
    },
    coverage: [[0, 60], [90, 150]],
    strength: { unit: 'dBW', what: 'power', brightness_scale: 'linear' },
    meta: { stations: [{}, {}, {}] },
    notes: [],
    provenance: { kind: 'observed', source: 'x' },
    ...over,
  };
}

test('columns decode exactly (little-endian float32 / int32)', () => {
  assert.deepEqual([...f32(b64f([1.5, -2, 0.25]))], [1.5, -2, 0.25]);
  assert.deepEqual([...i32(b64i([1, -2, 300000]))], [1, -2, 300000]);
  assert.equal(f32(undefined).length, 0);
  const d = decodeLightning(wire());
  assert.equal(d.n, 5);
  assert.equal(d.nf, 2);
  assert.equal(d.tRefMs, Date.UTC(2021, 11, 11, 2, 0, 0));
  assert.deepEqual([...d.flash], [0, 0, 0, 1, 1]);
  assert.deepEqual(d.coverage, [[0, 60], [90, 150]]);
});

test('an answer that is not lightning data is refused with a readable error', () => {
  assert.throws(() => decodeLightning(null), /does not know/);
  assert.throws(() => decodeLightning({ kind: 'radar' }), /does not know/);
  assert.throws(() => decodeLightning(wire({ t_ref: 'never' })), /reference time/);
  const bad = wire();
  bad.points = { ...bad.points, n: 4 };
  assert.throws(() => decodeLightning(bad), /inconsistent/);
});

test('the window is tied to the volume time: start offset and length in minutes', () => {
  const d = decodeLightning(wire());
  const volume = d.tRefMs + 30_000; // the volume starts 30 s after the reference time
  assert.deepEqual(windowSeconds(d, volume, { startMin: 0, lengthMin: 1, decayMin: 1 }), { start: 30, end: 90 });
  assert.deepEqual(windowSeconds(d, volume, { startMin: -0.5, lengthMin: 5, decayMin: 1 }), { start: 0, end: 300 });
  assert.equal(DEFAULT_WINDOW.lengthMin, 5);
});

test('lowerBound finds the first index at or above a value', () => {
  const a = [1, 2, 2, 5];
  assert.equal(lowerBound(a, 0), 0);
  assert.equal(lowerBound(a, 2), 1);
  assert.equal(lowerBound(a, 3), 3);
  assert.equal(lowerBound(a, 9), 4);
  assert.equal(lowerBound([], 1), 0);
});

test('brightness decays exponentially with the age; the future and zero decay are 0', () => {
  assert.equal(fade(0, 60), 1);
  assert.ok(Math.abs(fade(60, 60) - Math.exp(-1)) < 1e-12);
  assert.ok(Math.abs(fade(120, 60) - Math.exp(-2)) < 1e-12);
  let prev = 2;
  for (let a = 0; a < 600; a += 30) {
    const v = fade(a, 90);
    assert.ok(v < prev, 'strictly decreasing');
    prev = v;
  }
  assert.equal(fade(-1, 60), 0);
  assert.equal(fade(10, 0), 0);
});

test('only points inside the window are visible, newest brightest, and faint ones are dropped', () => {
  const d = decodeLightning(wire());
  // window 8..20 s, decay 10 s: points at 10, 12, 14 s have ages 10, 8, 6 s
  const v = visiblePoints(d, 8, 20, 10 / 60);
  assert.deepEqual(v.idx, [0, 1, 2]);
  assert.deepEqual(v.age, [10, 8, 6]);
  // brightness = (0.25 + 0.75 * strength brightness) * fade
  const want = (i, a) => Math.min(1, (0.25 + 0.75 * d.brightness[i]) * Math.exp(-a / 10));
  for (let k = 0; k < 3; k++) assert.ok(Math.abs(v.b[k] - want(v.idx[k], v.age[k])) < 1e-6);
  assert.ok(v.b[2] > v.b[1] * 0.5, 'a younger point is not dimmer for the same strength');
  // a window that holds none of the points
  assert.equal(visiblePoints(d, 20, 30, 1).idx.length, 0);
  // a point whose fade is below the floor is not drawn (age 100 s with a 10 s decay: e^-10)
  const faint = visiblePoints(d, 8, 114, 10 / 60);
  assert.ok(!faint.idx.includes(0));
  assert.ok(Math.exp(-10) < MIN_FADE);
  // the whole window including both flashes
  assert.equal(visiblePoints(d, 0, 200, 1000).idx.length, 5);
});

test('the points of one flash are joined in time order', () => {
  const d = decodeLightning(wire());
  const v = visiblePoints(d, 0, 200, 1000);
  const ch = channelsOf(d, v.idx);
  assert.deepEqual([...ch.keys()], [0, 1]);
  assert.deepEqual(ch.get(0), [0, 1, 2]);
  assert.deepEqual(ch.get(1), [3, 4]);
  for (const list of ch.values()) for (let k = 1; k < list.length; k++) assert.ok(d.t[list[k]] >= d.t[list[k - 1]]);
});

test('coverage: how much of a window the files cover, and what the panel says', () => {
  const cov = [[0, 60], [90, 150]];
  assert.equal(coveredFraction(cov, 0, 60), 1);
  assert.ok(Math.abs(coveredFraction(cov, 30, 120) - 2 / 3) < 1e-12); // 30 s + 30 s of 90 s
  assert.equal(coveredFraction(cov, 60, 90), 0);
  assert.equal(coveredFraction(cov, 5, 5), 0);
  const d = decodeLightning(wire());
  assert.equal(windowCoverageText(d, 0, 60), '');
  assert.match(windowCoverageText(d, 60, 90), /outside the time the loaded files cover/);
  assert.match(windowCoverageText(d, 30, 120), /67 %/);
  assert.match(windowCoverageText(d, 30, 120), /not "no lightning"/);
});

test('rate series: hand-counted events, gaps and a short last bin', () => {
  // events at 5, 10, 70, 71, 72, 200 s in bins of 60 s over 0..240; the data cover 0..180 and 200..240
  const s = rateSeries([5, 10, 70, 71, 72, 200], 0, 240, 60, [[0, 180], [200, 240]]);
  assert.deepEqual(s.edges, [0, 60, 120, 180, 240]);
  assert.deepEqual(s.count.slice(0, 3), [2, 3, 0]);
  assert.deepEqual(s.rate.slice(0, 3), [2, 3, 0]);
  assert.ok(Number.isNaN(s.count[3]) && Number.isNaN(s.rate[3]), 'a bin the data do not fully cover is a gap, not zero');
  // 90 s: the last bin lasts 30 s, so two events there are 4 per minute
  const t = rateSeries([65, 80], 0, 90, 60);
  assert.deepEqual(t.count, [0, 2]);
  assert.equal(t.rate[1], 4);
  // an event exactly at the end is counted in the last bin
  assert.deepEqual(rateSeries([90], 0, 90, 60).count, [0, 1]);
  assert.throws(() => rateSeries([], 5, 5), /end > start/);
});

test('the flash rate and the point rate of the loaded data use the span of the data', () => {
  const d = decodeLightning(wire());
  assert.deepEqual(dataSpan(d), [0, 150]);
  const fr = flashRate(d, 60);
  assert.deepEqual(fr.edges, [0, 60, 120, 150]);
  // flash A starts at 10 s (bin 0), flash B at 100 s (bin 1); bin 1 (60..120) is not fully covered: a gap
  assert.equal(fr.count[0], 1);
  assert.ok(Number.isNaN(fr.count[1]));
  const pr = pointRate(d, 60);
  assert.equal(pr.count[0], 3);
  assert.equal(niceBin(600), 10);
  assert.equal(niceBin(3600), 60);
  assert.equal(niceBin(3 * 3600), 300);
});

test('window statistics: flashes by start time, points, a rate only over fully covered windows', () => {
  const d = decodeLightning(wire());
  const a = windowStats(d, 0, 60);
  assert.deepEqual([a.flashes, a.points, a.meanPointsPerFlash], [1, 3, 3]);
  assert.equal(a.ratePerMin, 1);
  const b = windowStats(d, 0, 150);
  assert.equal(b.flashes, 2);
  assert.ok(Number.isNaN(b.ratePerMin), 'part of the window has no data: no rate');
  const none = windowStats(d, 20, 50);
  assert.deepEqual([none.flashes, none.points, none.ratePerMin], [0, 0, 0]);
  assert.equal(flashesText(1), '1 flash');
  assert.equal(flashesText(3), '3 flashes');
});

test('scene points: heights above the radar ground, GLM on the ground, range limit', () => {
  const d = decodeLightning(wire());
  const placed = placeAll(d, 36.5, -89.0);
  // 0.01 degree of longitude at 36.5 N is about 0.89 km: the second point is ~0.9 km east of the radar
  assert.ok(Math.abs(placed.x[1] / 1000 - 0.89) < 0.02 && Math.abs(placed.y[1]) < 50);
  const v = visiblePoints(d, 0, 20, 1000);
  const pts = scenePoints(d, v, placed, 0.2, 100); // radar at 200 m ASL
  assert.equal(pts.length, 3);
  assert.deepEqual(pts.map((p) => Number(p.z.toFixed(3))), [6.8, 7.3, 7.8]);
  assert.equal(scenePoints(d, v, placed, 0.2, 0.5).length, 1, 'only the first point is within 0.5 km');
  // a point below the radar's ground is clamped to the ground, not drawn underground
  assert.equal(scenePoints(d, v, placed, 7.5, 100)[0].z, 0);
  // GLM has no height: the points sit on the ground
  const g = decodeLightning(wire({ kind: 'glm', points: { ...wire().points, alt_km: b64f([NaN, NaN, NaN, NaN, NaN]) } }));
  assert.ok(scenePoints(g, visiblePoints(g, 0, 200, 1000), placeAll(g, 36.5, -89), 0.2, 100).every((p) => p.z === 0));
});

test('the coverage note says what is not known instead of drawing a footprint', () => {
  const lma = decodeLightning(wire());
  assert.match(coverageNote(lma), /3 stations/);
  assert.match(coverageNote(lma), /does not compute a detection efficiency/);
  const glm = decodeLightning(wire({ kind: 'glm', meta: { field_of_view: { lat: [-66.56, 66.56], lon: [-141.56, -8.44] } } }));
  assert.match(coverageNote(glm), /8 km at nadir/);
  assert.match(coverageNote(glm), /does not give a detection efficiency/);
  assert.match(coverageNote(glm), /-142 to -8 degrees longitude/);
});

test('the read box: 450 km around the radar, longitude widened with latitude, wrapped at the date line', () => {
  const [s, w, n, e] = boxAround(37.0, -89.0, 450);
  assert.ok(Math.abs(n - s - 2 * (450 / 111.2)) < 1e-9);
  assert.ok(Math.abs((e - w) / 2 - 450 / (111.2 * Math.cos((n * Math.PI) / 180))) < 1e-9, 'half-width in degrees of longitude at the northern edge');
  assert.ok(w < -89 && e > -89);
  const [, w2, , e2] = boxAround(10, 179, 450);
  assert.ok(w2 > 170 && e2 < -170, 'crosses the date line: west > east');
  const [s3, , n3] = boxAround(89.5, 0, 450);
  assert.ok(s3 > 80 && n3 <= 89.9);
});

test('the open request carries every parameter the user sees', () => {
  const r = buildOpenRequest('r1', ['/a.nc'], { lat: 37, lon: -89, rangeKm: 450 }, DEFAULT_OPEN);
  assert.equal(r.type, 'lightning_open');
  assert.equal(r.request_id, 'r1');
  assert.equal(r.box.length, 4);
  assert.equal('parallax_to_km' in r, false, 'no parallax correction unless asked');
  assert.equal(r.max_chi2, 1);
  assert.equal(r.min_stations, 6);
  assert.deepEqual(r.link, { distance_m: 3000, time_s: 0.15, min_points: 1, max_duration_s: 3, metric: 'ellipsoid' });
  const p = buildOpenRequest('r2', ['/a.nc'], null, { ...DEFAULT_OPEN, parallaxOn: true, parallaxKm: 9.5 });
  assert.equal(p.parallax_to_km, 9.5);
  assert.equal('box' in p, false, 'without a radar the whole file is read');
});
