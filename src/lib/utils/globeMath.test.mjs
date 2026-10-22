// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DEG,
  MIN_SCALE,
  MAX_SCALE,
  MAX_CENTRE_LAT,
  MERCATOR_MAX_LAT,
  wrapLon,
  lonLatToVec,
  vecToLonLat,
  viewBasis,
  project,
  projectVec,
  unproject,
  isFrontFacing,
  dragView,
  zoomAt,
  clampScale,
  angularDistanceDeg,
  greatCircleKm,
  slerpLonLat,
  rollDelta,
  globeToFlatZoom,
  flatZoomToGlobeRadius,
  fitRadius,
  mercatorUV,
  mercatorUVToLonLat,
  equirectUV,
  poleBlend,
  tilesByDistance,
  parseColourFilter,
  applyColourAffine,
  polylineVectors,
  traceVisible,
  graticuleLines,
  keyStepDeg,
} from './globeMath.ts';

const near = (a, b, eps = 1e-6, msg = '') => assert.ok(Math.abs(a - b) <= eps, `${msg} ${a} vs ${b}`);
const CX = 400, CY = 300, R = 250;

// ── Projection ───────────────────────────────────────────────────────────────────────────────

test('the view centre projects to the screen centre, north is up at roll 0', () => {
  const v = { lon: 12, lat: 40, roll: 0 };
  const p = project(v, 12, 40, R, CX, CY);
  near(p.x, CX); near(p.y, CY); near(p.depth, 1);
  const north = project(v, 12, 41, R, CX, CY);
  assert.ok(north.y < CY, 'a point to the north is above the centre');
  near(north.x, CX, 1e-6, 'due north stays on the vertical');
  const east = project(v, 13, 40, R, CX, CY);
  assert.ok(east.x > CX, 'a point to the east is to the right');
});

test('roll turns north clockwise on the screen', () => {
  const v = { lon: 0, lat: 0, roll: 90 };
  const north = project(v, 0, 10, R, CX, CY);
  assert.ok(north.x > CX + 1 && Math.abs(north.y - CY) < 1e-6, 'north points right after a 90 degree clockwise roll');
});

test('orthographic round trip: unproject(project(p)) = p for visible points', () => {
  const views = [
    { lon: 0, lat: 0, roll: 0 },
    { lon: 77.3, lat: 23.5, roll: 0 },
    { lon: -120, lat: -45, roll: 33 },
    { lon: 179.5, lat: 60, roll: -80 },
  ];
  for (const v of views) {
    for (let lon = -180; lon < 180; lon += 17) {
      for (let lat = -85; lat <= 85; lat += 11) {
        const p = project(v, lon, lat, R, CX, CY);
        if (p.depth < 0.05) continue; // near the limb the round trip is ill-conditioned by design
        const q = unproject(v, p.x, p.y, R, CX, CY);
        assert.ok(q, 'inside the disc');
        near(angularDistanceDeg(lat, lon, q[1], q[0]), 0, 1e-4, `${lon},${lat}`);
      }
    }
  }
});

test('unproject is null outside the disc and defined on the rim', () => {
  const v = { lon: 0, lat: 0, roll: 0 };
  assert.equal(unproject(v, CX + R + 1, CY, R, CX, CY), null);
  assert.ok(unproject(v, CX + R - 0.5, CY, R, CX, CY));
  const [lon] = unproject(v, CX + R - 1e-6, CY, R, CX, CY);
  near(lon, 90, 0.5);
});

test('back-face test: the antipode and the far side are hidden, the near side shown', () => {
  const v = { lon: 10, lat: 20, roll: 0 };
  assert.ok(isFrontFacing(project(v, 10, 20, R, CX, CY).depth));
  assert.ok(!isFrontFacing(project(v, 190, -20, R, CX, CY).depth), 'antipode');
  assert.ok(!isFrontFacing(project(v, 10 + 100, 20, R, CX, CY).depth), '100 degrees of longitude away at the same latitude');
  // depth is the cosine of the angular distance to the view centre
  near(project(v, 50, -10, R, CX, CY).depth, Math.cos(angularDistanceDeg(20, 10, -10, 50) * DEG), 1e-9);
  // the horizon: 90 degrees away is exactly depth 0 and not front facing
  near(project(v, 100, 0, R, CX, CY).depth, Math.cos(angularDistanceDeg(20, 10, 0, 100) * DEG), 1e-9);
  assert.ok(!isFrontFacing(0));
});

test('lon/lat <-> unit vector', () => {
  for (const [lon, lat] of [[0, 0], [90, 0], [-180, 10], [45, -89], [0, 90]]) {
    const v = lonLatToVec(lon, lat);
    near(Math.hypot(...v), 1);
    const [lo, la] = vecToLonLat(v);
    near(la, lat, 1e-9);
    if (Math.abs(lat) < 89.9) near(wrapLon(lo - lon), 0, 1e-9);
  }
  near(wrapLon(190), -170); near(wrapLon(-190), 170); near(wrapLon(180), -180); near(wrapLon(540), -180);
});

test('the view basis is orthonormal for any roll', () => {
  for (const roll of [0, 33, 90, -170]) {
    const b = viewBasis({ lon: 31, lat: -47, roll });
    const dot = (a, c) => a[0] * c[0] + a[1] * c[1] + a[2] * c[2];
    near(dot(b.r, b.r), 1); near(dot(b.u, b.u), 1); near(dot(b.f, b.f), 1);
    near(dot(b.r, b.u), 0); near(dot(b.r, b.f), 0); near(dot(b.u, b.f), 0);
  }
});

// ── Drag to rotation ─────────────────────────────────────────────────────────────────────────

/** Drag from screen point A to B starting at view v (what the component does on every move). */
function drag(v, ax, ay, bx, by, radius = R) {
  const g = unproject(v, ax, ay, radius, CX, CY);
  assert.ok(g, 'grab inside the disc');
  return { g, view: dragView(v, g[0], g[1], (bx - CX) / radius, (CY - by) / radius) };
}

test('dragging keeps the grabbed point under the pointer, and keeps north up where it can', () => {
  const starts = [
    { lon: 0, lat: 0, roll: 0 },
    { lon: 10, lat: 52, roll: 0 },
    { lon: -100, lat: -30, roll: 0 },
  ];
  let keptNorth = 0, total = 0;
  for (const v of starts) {
    for (const [dx, dy] of [[40, 0], [-90, 25], [0, -60], [120, 80], [-20, -20], [200, 10]]) {
      const ax = CX + 30, ay = CY - 20;
      const { g, view } = drag(v, ax, ay, ax + dx, ay + dy);
      const p = project(view, g[0], g[1], R, CX, CY);
      near(p.x, ax + dx, 0.05, `${JSON.stringify(v)} ${dx},${dy} x`);
      near(p.y, ay + dy, 0.05, 'y');
      assert.ok(p.depth > 0, 'still on the near side');
      total++;
      if (view.roll === 0) keptNorth++;
    }
  }
  assert.ok(keptNorth >= total * 0.6, `north stayed up in ${keptNorth} of ${total} drags`);
  // at the equator, horizontal drags never need a roll
  const eq = drag({ lon: 0, lat: 0, roll: 0 }, CX, CY, CX + 100, CY).view;
  assert.equal(eq.roll, 0);
});

test('dragging works with a rolled view and keeps the roll where it can', () => {
  const v = { lon: 20, lat: 10, roll: 40 };
  const { g, view } = drag(v, CX + 10, CY + 10, CX + 90, CY - 30);
  const p = project(view, g[0], g[1], R, CX, CY);
  near(p.x, CX + 90, 0.05); near(p.y, CY - 30, 0.05);
  assert.equal(view.roll, 40);
});

test('a drag to the right brings western longitudes into view; zero drag is a no-op', () => {
  const v = { lon: 0, lat: 0, roll: 0 };
  const { view } = drag(v, CX, CY, CX + 60, CY);
  assert.ok(view.lon < 0, 'centre moved west');
  near(view.lat, 0, 1e-6);
  const same = drag(v, CX + 5, CY + 5, CX + 5, CY + 5).view;
  near(same.lon, 0, 1e-9); near(same.lat, 0, 1e-9); near(same.roll, 0, 1e-9);
  // 1 pixel at the centre is 1/R radians
  const one = drag(v, CX, CY, CX + 1, CY).view;
  near(one.lon, -(1 / R) / DEG, 1e-3);
});

test('a drag that north-up cannot follow (high latitude, far sideways) falls back to a roll and still follows the pointer', () => {
  const v = { lon: 0, lat: 75, roll: 0 };
  const { g, view } = drag(v, CX, CY, CX + 200, CY);
  const p = project(view, g[0], g[1], R, CX, CY);
  near(p.x, CX + 200, 0.05); near(p.y, CY, 0.05);
  assert.ok(Math.abs(view.roll) > 1, `roll ${view.roll}`);
  // dragging back to the start restores the start view
  const back = dragView(v, g[0], g[1], 0, 0);
  near(back.lon, 0, 1e-6); near(back.lat, 75, 1e-6); near(back.roll, 0, 1e-6);
});

test('drag target outside the disc goes to the rim, never NaN; the centre never reaches the pole', () => {
  const v = { lon: 5, lat: 80, roll: 0 };
  const g = unproject(v, CX, CY - 100, R, CX, CY);
  const out = dragView(v, g[0], g[1], 0, -3); // far below the disc
  assert.ok(Number.isFinite(out.lon) && Number.isFinite(out.lat) && Number.isFinite(out.roll));
  assert.ok(Math.abs(out.lat) <= MAX_CENTRE_LAT);
  // keep pulling the globe over the pole, step by step
  let cur = { lon: 0, lat: 70, roll: 0 };
  for (let i = 0; i < 60; i++) {
    const gg = unproject(cur, CX, CY - 150, R, CX, CY);
    if (!gg) break;
    cur = dragView(cur, gg[0], gg[1], 0, 0.1);
    assert.ok(Math.abs(cur.lat) <= MAX_CENTRE_LAT && Number.isFinite(cur.lon) && Number.isFinite(cur.roll));
  }
});

test('dragging the pole itself is defined', () => {
  const v = { lon: 30, lat: 85, roll: 0 };
  const p = project(v, 0, 90, R, CX, CY);
  const g = unproject(v, p.x, p.y, R, CX, CY);
  const out = dragView(v, g[0], g[1], (p.x - CX + 20) / R, (CY - p.y) / R);
  assert.ok(Number.isFinite(out.lon) && Number.isFinite(out.lat) && Number.isFinite(out.roll));
  const q = project(out, g[0], g[1], R, CX, CY);
  near(q.x, p.x + 20, 0.05); near(q.y, p.y, 0.05);
});

test('zoom clamp', () => {
  assert.equal(clampScale(0.01), MIN_SCALE);
  assert.equal(clampScale(100), MAX_SCALE);
  assert.equal(clampScale(2), 2);
  assert.equal(clampScale(NaN), 1);
  assert.equal(clampScale(Infinity), 1);
});

test('zoom about the pointer keeps the point under it', () => {
  const v = { lon: 15, lat: 35, roll: 0 };
  const px = CX + 70, py = CY - 40;
  const g = unproject(v, px, py, R, CX, CY);
  const v2 = zoomAt(v, px, py, CX, CY, R, R * 2);
  const p = project(v2, g[0], g[1], R * 2, CX, CY);
  near(p.x, px, 0.05); near(p.y, py, 0.05);
  // outside the disc: zoom about the centre, view unchanged
  assert.deepEqual(zoomAt(v, CX + 9999, CY, CX, CY, R, R * 2), v);
});

// ── Distances, interpolation, hand-over ──────────────────────────────────────────────────────

test('great-circle distance: known values', () => {
  near(angularDistanceDeg(0, 0, 0, 90), 90, 1e-9);
  near(angularDistanceDeg(0, 0, 0, 180), 180, 1e-9);
  near(angularDistanceDeg(-90, 0, 90, 123), 180, 1e-9);
  near(angularDistanceDeg(10, 20, 10, 20), 0, 1e-12);
  near(angularDistanceDeg(0, 179, 0, -179), 2, 1e-9, 'across the antimeridian');
  // Berlin - Paris is about 878 km
  near(greatCircleKm(52.52, 13.405, 48.857, 2.352), 878, 3);
  // one degree of latitude is about 111.2 km
  near(greatCircleKm(0, 0, 1, 0), 111.195, 0.05);
  // symmetric
  near(greatCircleKm(10, 20, -30, 140), greatCircleKm(-30, 140, 10, 20), 1e-9);
});

test('slerp follows the great circle', () => {
  const [lo, la] = slerpLonLat(0, 0, 90, 0, 0.5);
  near(lo, 45, 1e-9); near(la, 0, 1e-9);
  const [lo1, la1] = slerpLonLat(10, 20, 10, 20, 0.3);
  near(lo1, 10); near(la1, 20);
  // ends
  const a = slerpLonLat(-170, 30, 170, 40, 0);
  near(angularDistanceDeg(a[1], a[0], 30, -170), 0, 1e-6);
  const b = slerpLonLat(-170, 30, 170, 40, 1);
  near(angularDistanceDeg(b[1], b[0], 40, 170), 0, 1e-6);
  // the midpoint is half way
  const m = slerpLonLat(-170, 30, 170, 40, 0.5);
  const total = angularDistanceDeg(30, -170, 40, 170);
  near(angularDistanceDeg(30, -170, m[1], m[0]), total / 2, 1e-6);
});

test('roll delta takes the short way round', () => {
  near(rollDelta(350, 10), 20); near(rollDelta(10, 350), -20); near(rollDelta(0, 180), 180); near(rollDelta(-170, 170), -20);
});

test('hand-over zoom matches pixels per degree and inverts', () => {
  for (const radius of [200, 600, 1500]) {
    const z = globeToFlatZoom(radius, 0, 20);
    const flatPxPerDeg = (256 * 2 ** z) / 360;
    near(flatPxPerDeg, (radius * Math.PI) / 180, 1e-9);
    near(flatZoomToGlobeRadius(z), radius, 1e-6);
  }
  assert.equal(globeToFlatZoom(1, 1, 12), 1, 'clamped to the flat map minimum');
  assert.equal(globeToFlatZoom(1e9, 1, 12), 12);
  assert.ok(fitRadius(1000, 600) < 300 && fitRadius(1000, 600) > 200);
  assert.ok(fitRadius(10, 10) >= 40);
});

test('key step shrinks with zoom', () => {
  assert.ok(keyStepDeg(1) > keyStepDeg(4));
  assert.ok(keyStepDeg(100) >= 1.5);
});

// ── Texture sampling coordinates ─────────────────────────────────────────────────────────────

test('Mercator image coordinates: corners, centre, wrap, clamp', () => {
  let [u, v] = mercatorUV(0, 0);
  near(u, 0.5); near(v, 0.5);
  [u, v] = mercatorUV(-180, MERCATOR_MAX_LAT);
  near(u, 0); near(v, 0, 1e-9);
  [u, v] = mercatorUV(180, -MERCATOR_MAX_LAT);
  near(u, 0, 1e-9, '+180 wraps to -180'); near(v, 1, 1e-9);
  // beyond the last row: clamped, not extrapolated, not NaN
  [u, v] = mercatorUV(20, 89.9);
  near(v, 0, 1e-9); assert.ok(Number.isFinite(u));
  assert.equal(mercatorUV(20, 89.9)[1], mercatorUV(20, MERCATOR_MAX_LAT)[1], 'same row as the last one');
  [, v] = mercatorUV(20, -90);
  near(v, 1, 1e-9);
  // 540 = -180
  near(mercatorUV(540, 10)[0], 0);
});

test('Mercator image coordinates agree with the slippy-map formula (tile row of Berlin at z3)', () => {
  const [u, v] = mercatorUV(13.405, 52.52);
  assert.equal(Math.floor(u * 8), 4);
  assert.equal(Math.floor(v * 8), 2);
  // exact inverse for rows inside the image
  for (const [lon, lat] of [[0, 0], [100, 45], [-75, -33], [20, 80]]) {
    const [uu, vv] = mercatorUV(lon, lat);
    const [lo, la] = mercatorUVToLonLat(uu, vv);
    near(lo, lon, 1e-9); near(la, lat, 1e-9);
  }
});

test('Mercator rows are monotone from north to south', () => {
  let prev = -1;
  for (let lat = 85; lat >= -85; lat -= 5) {
    const v = mercatorUV(0, lat)[1];
    assert.ok(v > prev, `${lat}`);
    prev = v;
  }
});

test('plate carree image coordinates', () => {
  assert.deepEqual(equirectUV(-180, 90), [0, 0]);
  near(equirectUV(0, 0)[0], 0.5); near(equirectUV(0, 0)[1], 0.5);
  near(equirectUV(180, -90)[0], 0); near(equirectUV(180, -90)[1], 1);
  near(equirectUV(90, 45)[0], 0.75); near(equirectUV(90, 45)[1], 0.25);
});

test('pole blend: none at mid latitudes, full beyond the last image row, smooth and symmetric', () => {
  assert.equal(poleBlend(0), 0);
  assert.equal(poleBlend(80), 0);
  near(poleBlend(MERCATOR_MAX_LAT), 1, 1e-12);
  assert.equal(poleBlend(89.99), 1);
  assert.equal(poleBlend(-89.99), 1);
  assert.equal(poleBlend(-60), 0);
  let prev = 0;
  for (let lat = 80; lat <= 90; lat += 0.25) {
    const w = poleBlend(lat);
    assert.ok(w >= prev - 1e-12 && w >= 0 && w <= 1);
    prev = w;
  }
  near(poleBlend(84.2), poleBlend(-84.2));
});

test('tile order: all 64 tiles of level 3 exactly once, nearest first, antimeridian-aware', () => {
  const t = tilesByDistance(3, 13.4, 52.5);
  assert.equal(t.length, 64);
  assert.equal(new Set(t.map((q) => `${q.x}/${q.y}`)).size, 64);
  assert.deepEqual([t[0].x, t[0].y], [4, 2], 'Berlin is in tile 3/4/2');
  const pac = tilesByDistance(3, 179.9, 0);
  assert.ok([0, 7].includes(pac[0].x) && [0, 7].includes(pac[1].x), 'tiles on both sides of the date line come first');
  assert.ok(tilesByDistance(1, 0, 0).length === 4);
});

// ── Colour filter as a matrix ────────────────────────────────────────────────────────────────

test('colour filter: none is the identity, invert flips, brightness scales', () => {
  const id = parseColourFilter('none');
  assert.deepEqual(applyColourAffine(id, [0.2, 0.5, 0.9]), [0.2, 0.5, 0.9]);
  assert.deepEqual(applyColourAffine(parseColourFilter('invert(1)'), [1, 1, 1]), [0, 0, 0]);
  const half = applyColourAffine(parseColourFilter('brightness(0.5)'), [1, 0.5, 0]);
  near(half[0], 0.5); near(half[1], 0.25); near(half[2], 0);
  near(applyColourAffine(parseColourFilter('contrast(0.5)'), [1, 1, 1])[0], 0.75);
  const grey = applyColourAffine(parseColourFilter('saturate(0)'), [1, 0, 0]);
  near(grey[0], grey[1], 1e-9); near(grey[1], grey[2], 1e-9);
  assert.deepEqual(applyColourAffine(parseColourFilter('bogus(3)'), [0.1, 0.2, 0.3]), [0.1, 0.2, 0.3]);
  assert.deepEqual(applyColourAffine(parseColourFilter('hue-rotate(0deg)'), [0.1, 0.2, 0.3]).map((x) => +x.toFixed(9)), [0.1, 0.2, 0.3]);
});

test('the dark map filter turns the light OSM paper dark and keeps the order of lightness', () => {
  const dark = parseColourFilter('invert(1) hue-rotate(196deg) brightness(0.74) contrast(0.95) saturate(0.62)');
  const paper = applyColourAffine(dark, [0.914, 0.91, 0.933]);
  assert.ok(paper[0] < 0.3 && paper[1] < 0.3 && paper[2] < 0.35, `paper became ${paper}`);
  const white = applyColourAffine(dark, [1, 1, 1]);
  const black = applyColourAffine(dark, [0, 0, 0]);
  assert.ok(white[0] < black[0] && white[1] < black[1], 'invert: white becomes darker than black');
});

// ── Polylines on the sphere ──────────────────────────────────────────────────────────────────

function collect(vecs, closed, view) {
  const b = viewBasis(view);
  const strokes = [];
  let cur = null;
  traceVisible(vecs, closed, b, R, CX, CY, (x, y) => { cur = [[x, y]]; strokes.push(cur); }, (x, y) => cur.push([x, y]));
  return strokes;
}

test('polylines: the far side is dropped, strokes end on the rim', () => {
  // the equator, seen from lon 0: one visible stroke from -90 to +90
  const eq = polylineVectors(Float64Array.from({ length: 73 * 2 }, (_, i) => (i % 2 === 0 ? -180 + 5 * (i / 2) : 0)));
  const s = collect(eq, false, { lon: 0, lat: 0, roll: 0 });
  assert.equal(s.length, 1);
  const [a, b] = [s[0][0], s[0][s[0].length - 1]];
  near(Math.hypot(a[0] - CX, a[1] - CY), R, 1e-3, 'start on the rim');
  near(Math.hypot(b[0] - CX, b[1] - CY), R, 1e-3, 'end on the rim');
  for (const p of s[0]) assert.ok(Math.hypot(p[0] - CX, p[1] - CY) <= R + 1e-3);
  // all hidden from the other side of a small ring
  const ring = polylineVectors([180, 0, 181, 1, 182, 0]);
  assert.equal(collect(ring, true, { lon: 0, lat: 0, roll: 0 }).length, 0);
  // a closed ring on the near side is one closed stroke
  const near1 = polylineVectors([0, 0, 5, 0, 5, 5, 0, 5]);
  const c = collect(near1, true, { lon: 2, lat: 2, roll: 0 });
  assert.equal(c.length, 1);
  assert.equal(c[0].length, 5);
});

test('graticule has parallels and meridians that all stay within the longitude/latitude range', () => {
  const g = graticuleLines(30, 3);
  assert.equal(g.length, 5 + 12);
  for (const l of g) for (let i = 0; i < l.length; i += 2) assert.ok(Math.abs(l[i]) <= 180 && Math.abs(l[i + 1]) <= 90);
});

test('projectVec equals project', () => {
  const v = { lon: 3, lat: -7, roll: 12 };
  const a = project(v, 40, 50, R, CX, CY);
  const b = projectVec(viewBasis(v), lonLatToVec(40, 50), R, CX, CY);
  assert.deepEqual(a, b);
});
