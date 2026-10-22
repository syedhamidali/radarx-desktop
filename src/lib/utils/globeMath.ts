/**
 * Pure maths of the radar globe (no DOM, no WebGL, no Svelte): runs under plain node for the unit tests.
 *
 * Conventions
 *  - A view is the point of the sphere at the centre of the screen (`lon`, `lat`, degrees) and `roll`,
 *    the clockwise angle in degrees by which north is turned away from "up" on the screen (0 = north up).
 *  - The projection is orthographic: the sphere is a disc of `radius` pixels around the screen centre; a
 *    point is visible when it is on the near hemisphere (`depth` > 0).
 *  - World vectors: x towards (0 N, 0 E), y towards (0 N, 90 E), z towards the north pole.
 *  - Screen coordinates are CSS pixels, y down. "Disc coordinates" (a, b) are the pixel offset from the
 *    centre divided by the radius, b up: the unit disc.
 *
 * Nothing here knows a data source; positions are only turned into screen positions.
 */

export const DEG = Math.PI / 180;
/** Latitude beyond which a Web Mercator image has no rows (the poles are cut off). */
export const MERCATOR_MAX_LAT = 85.0511287798;
/** The centre of the view never reaches the pole exactly (north-up views are undefined there). */
export const MAX_CENTRE_LAT = 89.5;
/** Zoom limits as a multiple of the "whole globe fits" radius. */
export const MIN_SCALE = 0.6;
export const MAX_SCALE = 6;
/** From this scale on the globe offers the hand-over to the flat map. */
export const HANDOVER_SCALE = 3;

export interface GlobeView {
  lon: number;
  lat: number;
  roll: number;
}

export type Vec3 = [number, number, number];

const clamp = (x: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, x));

/** Longitude wrapped to [-180, 180). */
export function wrapLon(lon: number): number {
  return ((((lon + 180) % 360) + 360) % 360) - 180;
}

export function lonLatToVec(lon: number, lat: number): Vec3 {
  const cl = Math.cos(lat * DEG);
  return [cl * Math.cos(lon * DEG), cl * Math.sin(lon * DEG), Math.sin(lat * DEG)];
}

export function vecToLonLat(v: Vec3): [number, number] {
  const n = Math.hypot(v[0], v[1], v[2]) || 1;
  return [Math.atan2(v[1], v[0]) / DEG, Math.asin(clamp(v[2] / n, -1, 1)) / DEG];
}

export interface ViewBasis {
  /** Towards the viewer: the sphere point at the screen centre. */
  f: Vec3;
  /** Screen right. */
  r: Vec3;
  /** Screen up. */
  u: Vec3;
}

/** The three world-space axes of a view (right, up, towards the viewer). */
export function viewBasis(v: GlobeView): ViewBasis {
  const lam = v.lon * DEG;
  const phi = v.lat * DEG;
  const th = v.roll * DEG;
  const f: Vec3 = [Math.cos(phi) * Math.cos(lam), Math.cos(phi) * Math.sin(lam), Math.sin(phi)];
  const e: Vec3 = [-Math.sin(lam), Math.cos(lam), 0];
  const n: Vec3 = [-Math.sin(phi) * Math.cos(lam), -Math.sin(phi) * Math.sin(lam), Math.cos(phi)];
  const c = Math.cos(th);
  const s = Math.sin(th);
  return {
    f,
    r: [c * e[0] + s * n[0], c * e[1] + s * n[1], c * e[2] + s * n[2]],
    u: [c * n[0] - s * e[0], c * n[1] - s * e[1], c * n[2] - s * e[2]],
  };
}

export interface Projected {
  x: number;
  y: number;
  /** Cosine of the angle to the view centre: > 0 on the visible hemisphere. */
  depth: number;
}

/** Project a unit vector with a precomputed basis. */
export function projectVec(b: ViewBasis, v: Vec3, radius: number, cx: number, cy: number): Projected {
  return {
    x: cx + radius * (v[0] * b.r[0] + v[1] * b.r[1] + v[2] * b.r[2]),
    y: cy - radius * (v[0] * b.u[0] + v[1] * b.u[1] + v[2] * b.u[2]),
    depth: v[0] * b.f[0] + v[1] * b.f[1] + v[2] * b.f[2],
  };
}

export function project(v: GlobeView, lon: number, lat: number, radius: number, cx: number, cy: number): Projected {
  return projectVec(viewBasis(v), lonLatToVec(lon, lat), radius, cx, cy);
}

/** Back-face test: a point is hidden when it is on the far side of the sphere. */
export function isFrontFacing(depth: number, epsilon = 0): boolean {
  return depth > epsilon;
}

/** Longitude and latitude under a screen point, or null outside the disc. */
export function unproject(v: GlobeView, x: number, y: number, radius: number, cx: number, cy: number): [number, number] | null {
  const a = (x - cx) / radius;
  const b = (cy - y) / radius;
  const r2 = a * a + b * b;
  if (r2 > 1) return null;
  const d = Math.sqrt(1 - r2);
  const t = viewBasis(v);
  const w: Vec3 = [
    a * t.r[0] + b * t.u[0] + d * t.f[0],
    a * t.r[1] + b * t.u[1] + d * t.f[1],
    a * t.r[2] + b * t.u[2] + d * t.f[2],
  ];
  const [lon, lat] = vecToLonLat(w);
  return [lon, lat];
}

/**
 * The view in which the sphere point (grabLon, grabLat) appears at the disc coordinates (a, b): "grab the
 * globe and drag", the point under the pointer stays under it. When the roll of `start` allows it, the roll
 * is kept (north stays up when it was up). A point on a parallel cannot reach every screen position
 * with north up (a point at latitude L can only appear at |a| <= cos L): then the globe is turned about the
 * view axis as well, like a trackball, so the point still follows the pointer and the compass shows the
 * tilt. A target outside the disc is moved to the rim.
 */
export function dragView(start: GlobeView, grabLon: number, grabLat: number, a: number, b: number): GlobeView {
  const len = Math.hypot(a, b);
  if (len > 0.9999) {
    a *= 0.9999 / len;
    b *= 0.9999 / len;
  }
  return dragNorthKept(start, grabLon, grabLat, a, b) ?? dragTrackball(start, grabLon, grabLat, a, b);
}

/** Exact solution that keeps the roll, or null when no such view puts the point there (or past the allowed pole). */
function dragNorthKept(start: GlobeView, grabLon: number, grabLat: number, a: number, b: number): GlobeView | null {
  const th = start.roll * DEG;
  // The same target in the frame in which north is up.
  const a0 = Math.cos(th) * a - Math.sin(th) * b;
  const b0 = Math.sin(th) * a + Math.cos(th) * b;
  const lamG = grabLon * DEG;
  const phiG = grabLat * DEG;
  const cg = Math.cos(phiG);
  const sg = Math.sin(phiG);

  // a0 = cos(phiG) sin(lamG - lam0)
  let delta = 0;
  let lam0 = start.lon * DEG;
  if (cg > 1e-9) {
    const s = a0 / cg;
    if (Math.abs(s) > 1) return null;
    delta = Math.asin(s);
    lam0 = lamG - delta;
  } else if (Math.abs(a0) > 1e-9) {
    return null;
  }
  // b0 = cos(phi0) sin(phiG) - sin(phi0) cos(phiG) cos(delta) = C cos(phi0 + dl)
  const A = sg;
  const B = cg * Math.cos(delta);
  const C = Math.hypot(A, B);
  if (C < 1e-12 || Math.abs(b0) > C) return null;
  const dl = Math.atan2(B, A);
  const ac = Math.acos(b0 / C);
  const wrap = (x: number) => Math.atan2(Math.sin(x), Math.cos(x));
  const depthOf = (p: number) => Math.sin(p) * sg + Math.cos(p) * B;
  const cands = [wrap(ac - dl), wrap(-ac - dl)].filter((p) => Math.abs(p) <= MAX_CENTRE_LAT * DEG && depthOf(p) >= 0);
  if (!cands.length) return null;
  const cur = start.lat * DEG;
  const phi0 = cands.reduce((best, p) => (Math.abs(p - cur) < Math.abs(best - cur) ? p : best), cands[0]);
  return { lon: wrapLon(lam0 / DEG), lat: phi0 / DEG, roll: start.roll };
}

/** Trackball drag: rotate the view so that the grabbed point goes to (a, b) by the shortest turn. */
function dragTrackball(start: GlobeView, grabLon: number, grabLat: number, a: number, b: number): GlobeView {
  const t = viewBasis(start);
  const g = lonLatToVec(grabLon, grabLat);
  const dot3 = (p: Vec3, q: Vec3) => p[0] * q[0] + p[1] * q[1] + p[2] * q[2];
  const q0: Vec3 = [dot3(g, t.r), dot3(g, t.u), dot3(g, t.f)];
  const q1: Vec3 = [a, b, Math.sqrt(Math.max(0, 1 - a * a - b * b))];
  // Rotation taking q0 to q1 (Rodrigues).
  const ax: Vec3 = [q0[1] * q1[2] - q0[2] * q1[1], q0[2] * q1[0] - q0[0] * q1[2], q0[0] * q1[1] - q0[1] * q1[0]];
  const s = Math.hypot(ax[0], ax[1], ax[2]);
  const c = clamp(dot3(q0, q1), -1, 1);
  let Rv: number[];
  if (s < 1e-12) {
    Rv = [1, 0, 0, 0, 1, 0, 0, 0, 1];
  } else {
    const k: Vec3 = [ax[0] / s, ax[1] / s, ax[2] / s];
    const v = 1 - c;
    Rv = [
      c + k[0] * k[0] * v, k[0] * k[1] * v - k[2] * s, k[0] * k[2] * v + k[1] * s,
      k[1] * k[0] * v + k[2] * s, c + k[1] * k[1] * v, k[1] * k[2] * v - k[0] * s,
      k[2] * k[0] * v - k[1] * s, k[2] * k[1] * v + k[0] * s, c + k[2] * k[2] * v,
    ];
  }
  // New basis rows: row_i(M1) = sum_k Rv[i][k] * row_k(M0).
  const row = (i: number): Vec3 => [
    Rv[i * 3] * t.r[0] + Rv[i * 3 + 1] * t.u[0] + Rv[i * 3 + 2] * t.f[0],
    Rv[i * 3] * t.r[1] + Rv[i * 3 + 1] * t.u[1] + Rv[i * 3 + 2] * t.f[1],
    Rv[i * 3] * t.r[2] + Rv[i * 3 + 1] * t.u[2] + Rv[i * 3 + 2] * t.f[2],
  ];
  const r1 = row(0);
  const f1 = row(2);
  const [lon, lat0] = vecToLonLat(f1);
  const lat = clamp(lat0, -MAX_CENTRE_LAT, MAX_CENTRE_LAT);
  const nb = viewBasis({ lon, lat, roll: 0 });
  // r = cos(roll) e + sin(roll) n, with e = nb.r and n = nb.u at roll 0
  const roll = Math.atan2(dot3(r1, nb.u), dot3(r1, nb.r)) / DEG;
  return { lon: wrapLon(lon), lat, roll };
}

export function clampScale(s: number): number {
  return clamp(Number.isFinite(s) ? s : 1, MIN_SCALE, MAX_SCALE);
}

/**
 * Zoom about a screen point: the sphere point under (px, py) stays under it. `radiusOld` and `radiusNew`
 * are the disc radii in pixels before and after. A point off the disc zooms about the centre.
 */
export function zoomAt(v: GlobeView, px: number, py: number, cx: number, cy: number, radiusOld: number, radiusNew: number): GlobeView {
  const g = unproject(v, px, py, radiusOld, cx, cy);
  if (!g) return { ...v };
  return dragView(v, g[0], g[1], (px - cx) / radiusNew, (cy - py) / radiusNew);
}

/** Great-circle angle between two positions, degrees (haversine; stable for tiny and antipodal pairs). */
export function angularDistanceDeg(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const dLat = (lat2 - lat1) * DEG;
  const dLon = (lon2 - lon1) * DEG;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * DEG) * Math.cos(lat2 * DEG) * Math.sin(dLon / 2) ** 2;
  return (2 * Math.asin(Math.min(1, Math.sqrt(h)))) / DEG;
}

export const EARTH_RADIUS_KM = 6371.0088;
export function greatCircleKm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  return angularDistanceDeg(lat1, lon1, lat2, lon2) * DEG * EARTH_RADIUS_KM;
}

/** The point a fraction t of the way along the great circle from a to b (spherical interpolation). */
export function slerpLonLat(lon1: number, lat1: number, lon2: number, lat2: number, t: number): [number, number] {
  const a = lonLatToVec(lon1, lat1);
  const b = lonLatToVec(lon2, lat2);
  const dot = clamp(a[0] * b[0] + a[1] * b[1] + a[2] * b[2], -1, 1);
  const om = Math.acos(dot);
  if (om < 1e-9) return [wrapLon(lon2), lat2];
  const so = Math.sin(om);
  if (so < 1e-9) {
    // antipodal: go over the equator
    const lon = lon1 + (wrapLon(lon2 - lon1) || 180) * t;
    return [wrapLon(lon), lat1 + (lat2 - lat1) * t];
  }
  const w1 = Math.sin((1 - t) * om) / so;
  const w2 = Math.sin(t * om) / so;
  return vecToLonLat([w1 * a[0] + w2 * b[0], w1 * a[1] + w2 * b[1], w1 * a[2] + w2 * b[2]]);
}

/** Shortest signed turn from roll `from` to roll `to`, degrees in (-180, 180]. */
export function rollDelta(from: number, to: number): number {
  const d = ((((to - from) % 360) + 540) % 360) - 180;
  return d === -180 ? 180 : d;
}

/** Ease in and out. */
export const easeInOut = (t: number): number => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);

// ── Hand-over to the flat (Web Mercator) map ──────────────────────────────────────────────────

/**
 * The slippy-map zoom whose pixels per degree at the equator equal the globe's at its centre:
 * flat: 256 * 2^z / 360 px per degree; globe: radius * pi / 180.
 */
export function globeToFlatZoom(radiusPx: number, minZoom = 1, maxZoom = 12): number {
  return clamp(Math.log2((radiusPx * 2 * Math.PI) / 256), minZoom, maxZoom);
}

/** Inverse of {@link globeToFlatZoom}: the globe radius in pixels that matches a flat zoom. */
export function flatZoomToGlobeRadius(zoom: number): number {
  return (256 * 2 ** zoom) / (2 * Math.PI);
}

/** Radius in pixels of the whole-globe fit for a viewport (scale 1). */
export function fitRadius(width: number, height: number): number {
  return Math.max(40, (Math.min(width, height) / 2) * 0.9);
}

// ── Sampling coordinates of the basemap texture ───────────────────────────────────────────────

/** Web Mercator image coordinates of a position: u across (0 at -180), v down (0 at 85.05 N). v is clamped. */
export function mercatorUV(lon: number, lat: number): [number, number] {
  const phi = clamp(lat, -MERCATOR_MAX_LAT, MERCATOR_MAX_LAT) * DEG;
  const u = (wrapLon(lon) + 180) / 360;
  const v = 0.5 - Math.atanh(Math.sin(phi)) / (2 * Math.PI);
  return [u, clamp(v, 0, 1)];
}

/** Inverse of {@link mercatorUV} for v in [0, 1]. */
export function mercatorUVToLonLat(u: number, v: number): [number, number] {
  return [u * 360 - 180, Math.atan(Math.sinh(Math.PI * (1 - 2 * v))) / DEG];
}

/** Plate-carree image coordinates (the plain land texture): u across, v down from the north pole. */
export function equirectUV(lon: number, lat: number): [number, number] {
  return [(wrapLon(lon) + 180) / 360, 0.5 - clamp(lat, -90, 90) / 180];
}

/**
 * Weight (0..1) with which the polar colour replaces the image near the poles of a Mercator image: 0 below
 * `from` degrees of latitude, 1 beyond the last image row, smooth in between. The last row of a Mercator
 * tile set is one row of pixels stretched over the whole polar cap, which would show as streaks pinching
 * into the pole; the cap is painted with the mean colour of that row instead.
 */
export function poleBlend(lat: number, from = 83.5): number {
  const a = Math.abs(lat);
  const t = clamp((a - from) / (MERCATOR_MAX_LAT - from), 0, 1);
  return t * t * (3 - 2 * t);
}

export interface TileXY {
  z: number;
  x: number;
  y: number;
}

/** All tiles of level z, nearest to (lon, lat) first (the tiles you look at load first). */
export function tilesByDistance(z: number, lon: number, lat: number): TileXY[] {
  const n = 2 ** z;
  const [u, v] = mercatorUV(lon, lat);
  const cx = u * n;
  const cy = v * n;
  const out: Array<TileXY & { d: number }> = [];
  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) {
      let dx = Math.abs(x + 0.5 - cx);
      dx = Math.min(dx, n - dx); // around the antimeridian
      out.push({ z, x, y, d: dx * dx + (y + 0.5 - cy) ** 2 });
    }
  }
  out.sort((p, q) => p.d - q.d || p.y - q.y || p.x - q.x);
  return out.map(({ z: zz, x, y }) => ({ z: zz, x, y }));
}

// ── CSS filter as a colour matrix (the dark tile theme, applied in the shader) ────────────────

export interface ColourAffine {
  /** Row-major 3x3. */
  m: number[];
  /** Added after the matrix. */
  t: number[];
}

function saturateMatrix(s: number): ColourAffine {
  return {
    m: [
      0.213 + 0.787 * s, 0.715 - 0.715 * s, 0.072 - 0.072 * s,
      0.213 - 0.213 * s, 0.715 + 0.285 * s, 0.072 - 0.072 * s,
      0.213 - 0.213 * s, 0.715 - 0.715 * s, 0.072 + 0.928 * s,
    ],
    t: [0, 0, 0],
  };
}

function hueMatrix(deg: number): ColourAffine {
  const c = Math.cos(deg * DEG);
  const s = Math.sin(deg * DEG);
  return {
    m: [
      0.213 + c * 0.787 - s * 0.213, 0.715 - c * 0.715 - s * 0.715, 0.072 - c * 0.072 + s * 0.928,
      0.213 - c * 0.213 + s * 0.143, 0.715 + c * 0.285 + s * 0.14, 0.072 - c * 0.072 - s * 0.283,
      0.213 - c * 0.213 - s * 0.787, 0.715 - c * 0.715 + s * 0.715, 0.072 + c * 0.928 + s * 0.072,
    ],
    t: [0, 0, 0],
  };
}

/** Most stages the shader takes (the dark map filter has five). */
export const MAX_FILTER_STAGES = 6;

/**
 * Parse a CSS `filter` made of invert(), hue-rotate(), brightness(), contrast() and saturate() into its
 * stages, one affine colour map each, in order. The stages are kept apart because browsers clamp to
 * 0..1 between two filter functions (hue-rotate of a dark inverted map overshoots and is clamped), and
 * the globe has to look like the flat map. "none" or anything unknown gives no stages.
 */
export function parseColourFilter(filter: string): ColourAffine[] {
  const stages: ColourAffine[] = [];
  const re = /([a-z-]+)\(\s*([-0-9.]+)\s*(deg|%)?\s*\)/g;
  for (const m of filter.matchAll(re)) {
    let x = parseFloat(m[2]);
    if (m[3] === '%') x /= 100;
    let op: ColourAffine | null = null;
    switch (m[1]) {
      case 'invert': op = { m: [1 - 2 * x, 0, 0, 0, 1 - 2 * x, 0, 0, 0, 1 - 2 * x], t: [x, x, x] }; break;
      case 'brightness': op = { m: [x, 0, 0, 0, x, 0, 0, 0, x], t: [0, 0, 0] }; break;
      case 'contrast': op = { m: [x, 0, 0, 0, x, 0, 0, 0, x], t: [0.5 - 0.5 * x, 0.5 - 0.5 * x, 0.5 - 0.5 * x] }; break;
      case 'saturate': op = saturateMatrix(x); break;
      case 'hue-rotate': op = hueMatrix(x); break;
    }
    if (!op) return [];
    stages.push(op);
  }
  return stages.slice(0, MAX_FILTER_STAGES);
}

/** Apply the stages in order, clamping to 0..1 after each (what the browser's filter does). */
export function applyColourFilter(stages: readonly ColourAffine[], rgb: [number, number, number]): [number, number, number] {
  let c = rgb;
  for (const a of stages) {
    const o: [number, number, number] = [0, 0, 0];
    for (let i = 0; i < 3; i++) o[i] = clamp(a.m[i * 3] * c[0] + a.m[i * 3 + 1] * c[1] + a.m[i * 3 + 2] * c[2] + a.t[i], 0, 1);
    c = o;
  }
  return c;
}

// ── Drawing helpers: polylines on the sphere ──────────────────────────────────────────────────

/** Unit vectors (x, y, z interleaved) of a list of [lon, lat] pairs given as a flat array. */
export function polylineVectors(lonLat: ArrayLike<number>): Float32Array {
  const n = Math.floor(lonLat.length / 2);
  const out = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    const v = lonLatToVec(lonLat[2 * i], lonLat[2 * i + 1]);
    out[3 * i] = v[0];
    out[3 * i + 1] = v[1];
    out[3 * i + 2] = v[2];
  }
  return out;
}

/**
 * Walk a polyline of unit vectors and report only its visible part: `move(x, y)` starts a stroke,
 * `line(x, y)` continues it. Segments crossing the horizon are cut exactly at it (interpolated on the
 * sphere), so strokes end on the rim instead of leaving gaps or leaking over the back.
 */
export function traceVisible(
  vecs: Float32Array,
  closed: boolean,
  b: ViewBasis,
  radius: number,
  cx: number,
  cy: number,
  move: (x: number, y: number) => void,
  line: (x: number, y: number) => void,
): void {
  const n = vecs.length / 3;
  if (n < 2) return;
  const total = closed ? n + 1 : n;
  let prevDepth = 0;
  let px = 0, py = 0, pz = 0;
  let pen = false;
  for (let k = 0; k < total; k++) {
    const i = k % n;
    const x = vecs[3 * i], y = vecs[3 * i + 1], z = vecs[3 * i + 2];
    const d = x * b.f[0] + y * b.f[1] + z * b.f[2];
    const sx = cx + radius * (x * b.r[0] + y * b.r[1] + z * b.r[2]);
    const sy = cy - radius * (x * b.u[0] + y * b.u[1] + z * b.u[2]);
    if (k === 0) {
      if (d > 0) { move(sx, sy); pen = true; }
    } else if (d > 0 && prevDepth > 0) {
      if (!pen) { move(sx, sy); pen = true; } else line(sx, sy);
    } else if (d > 0 && prevDepth <= 0) {
      // coming round the horizon: start at the crossing
      const t = prevDepth / (prevDepth - d);
      const cxv = px + (x - px) * t, cyv = py + (y - py) * t, czv = pz + (z - pz) * t;
      const nn = Math.hypot(cxv, cyv, czv) || 1;
      const hx = cx + radius * ((cxv * b.r[0] + cyv * b.r[1] + czv * b.r[2]) / nn);
      const hy = cy - radius * ((cxv * b.u[0] + cyv * b.u[1] + czv * b.u[2]) / nn);
      move(hx, hy);
      line(sx, sy);
      pen = true;
    } else if (d <= 0 && prevDepth > 0) {
      // going over the horizon: end at the crossing
      const t = prevDepth / (prevDepth - d);
      const cxv = px + (x - px) * t, cyv = py + (y - py) * t, czv = pz + (z - pz) * t;
      const nn = Math.hypot(cxv, cyv, czv) || 1;
      line(cx + radius * ((cxv * b.r[0] + cyv * b.r[1] + czv * b.r[2]) / nn), cy - radius * ((cxv * b.u[0] + cyv * b.u[1] + czv * b.u[2]) / nn));
      pen = false;
    } else {
      pen = false;
    }
    prevDepth = d;
    px = x; py = y; pz = z;
  }
}

/** Parallels and meridians every `step` degrees, as lon/lat polylines (flat arrays), sampled every `res` degrees. */
export function graticuleLines(step = 30, res = 3): Float64Array[] {
  const out: Float64Array[] = [];
  for (let lat = -90 + step; lat < 90; lat += step) {
    const pts: number[] = [];
    for (let lon = -180; lon <= 180; lon += res) pts.push(lon, lat);
    out.push(Float64Array.from(pts));
  }
  for (let lon = -180; lon < 180; lon += step) {
    const pts: number[] = [];
    for (let lat = -90; lat <= 90; lat += res) pts.push(lon, lat);
    out.push(Float64Array.from(pts));
  }
  return out;
}

/** Degrees the centre moves for one arrow-key press (smaller when zoomed in). */
export function keyStepDeg(scale: number): number {
  return clamp(14 / scale, 1.5, 18);
}
