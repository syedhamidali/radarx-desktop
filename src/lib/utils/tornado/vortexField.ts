/**
 * Tornado wind and pressure field (pure functions, no DOM, no GL; unit-tested in vortexField.test.mjs).
 *
 * The swirl is a Lamb-Oseen / Burgers-Rott vortex: tangential wind
 *     v(r) = Vt * f(r / rm),   f(x) = (1 - exp(-B x^2)) / (C x),   B = 1.2564, C = 1 - exp(-B) = 0.7153
 * which peaks at exactly Vt at r = rm (B is the root of d/dx[(1-exp(-B x^2))/x] = 0 at x = 1), is a solid
 * body (omega = 1.756 Vt / rm) inside the core and a potential vortex (v ~ 1/r) outside.
 *
 *  - Pressure: cyclostrophic balance dp/dr = rho v^2 / r, integrated inward from the far field:
 *        dp(r, z) = rho * Vt(z)^2 * G(r / rm(z)),   G(x) = integral_x^inf f(s)^2 / s ds    (Pa, positive = lower pressure)
 *  - Radial and vertical flow: a Burgers-style core inflow/updraft (stream function) plus a surface-layer
 *    inflow of depth DELTA whose updraft follows from mass continuity, so div(u) = 0 holds exactly for the
 *    inflow terms (tested numerically).
 *  - Height: the core widens with height, rm(z) = rm0 sqrt(1 + (z/Hf)^2), the swirl is weaker in the lowest
 *    tens of metres (surface friction) and decays above a few kilometres.
 *
 * Sense of rotation: `sense` +1 = cyclonic (counter-clockwise seen from above, +z), -1 = anticyclonic.
 * Frame: metres, x east, y north, z up, origin on the vortex axis at the ground.
 *
 * What is OBSERVED vs MODELLED: Vt and rm are seeded from the damage survey (EF rating, swath width), the
 * mesocyclone-scale instance from the radar rotation (Vrot); all the profile shapes here (the Lamb-Oseen
 * form, the inflow, the widening with height) are MODELLED, a radar grid does not resolve a 366 m vortex.
 */

export const LO_B = 1.2564; // Lamb-Oseen constant: peak of (1 - exp(-B x^2)) / x is at x = 1
export const LO_C = 1 - Math.exp(-LO_B); // f(1) normaliser, 0.7153
export const AIR_DENSITY = 1.15; // kg/m3 near the surface in a warm, moist air mass (rho = p / (Rd Tv))
export const GRAVITY = 9.80665;

/** Normalised Lamb-Oseen swirl profile, f(1) = 1 is the maximum. */
export function swirlShape(x: number): number {
  if (x < 1e-6) return (LO_B * x) / LO_C; // series: solid-body core
  return (1 - Math.exp(-LO_B * x * x)) / (LO_C * x);
}

// ---- G(x): the pressure-deficit shape, tabulated once ---------------------------------------------
const G_N = 512;
const G_XMAX = 40;
const G_LOGMIN = Math.log(1e-3);
const G_LOGMAX = Math.log(G_XMAX);
let GTAB: Float64Array | null = null;

/** G on a log grid x in [1e-3, 40]: integral from x to infinity of f(s)^2 / s ds (Simpson in ln s). */
export function pressureShapeTable(): Float64Array {
  if (GTAB) return GTAB;
  const n = G_N;
  const xs = new Float64Array(n);
  const h = (G_LOGMAX - G_LOGMIN) / (n - 1);
  for (let i = 0; i < n; i++) xs[i] = Math.exp(G_LOGMIN + h * i);
  // integrand in u = ln s:  f(s)^2 / s * s = f(s)^2
  const fu = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const f = swirlShape(xs[i]);
    fu[i] = f * f;
  }
  const g = new Float64Array(n);
  // tail beyond XMAX: f ~ 1/(C s) so integral_X^inf 1/(C^2 s^3) ds = 1/(2 C^2 X^2)
  g[n - 1] = 1 / (2 * LO_C * LO_C * G_XMAX * G_XMAX);
  for (let i = n - 2; i >= 0; i--) {
    // exact enough: trapezoid on a fine log grid (512 points over 10.6 e-folds)
    g[i] = g[i + 1] + 0.5 * (fu[i] + fu[i + 1]) * h;
  }
  GTAB = g;
  return g;
}

/** G(x) by cubic Hermite interpolation in ln x (the exact slope is dG/du = -f^2); G(0) is the central deficit shape. */
export function pressureShape(x: number): number {
  const g = pressureShapeTable();
  if (x <= 1e-3) return g[0];
  if (x >= G_XMAX) return 1 / (2 * LO_C * LO_C * x * x);
  const h = (G_LOGMAX - G_LOGMIN) / (G_N - 1);
  const u = (Math.log(x) - G_LOGMIN) / h;
  const i = Math.min(G_N - 2, Math.floor(u));
  const t = u - i;
  const f0 = swirlShape(Math.exp(G_LOGMIN + h * i));
  const f1 = swirlShape(Math.exp(G_LOGMIN + h * (i + 1)));
  const m0 = -f0 * f0 * h;
  const m1 = -f1 * f1 * h;
  const t2 = t * t;
  const t3 = t2 * t;
  return (2 * t3 - 3 * t2 + 1) * g[i] + (t3 - 2 * t2 + t) * m0 + (-2 * t3 + 3 * t2) * g[i + 1] + (t3 - t2) * m1;
}

export interface VortexParams {
  /** Peak tangential wind (m/s), reached at the core radius at the height of the swirl maximum. */
  vt: number;
  /** Core radius rm0 (m) at the ground: the radius of the maximum tangential wind. */
  rm: number;
  /** +1 cyclonic (counter-clockwise from above), -1 anticyclonic. */
  sense?: 1 | -1;
  /** Height scale of the widening of the core (m): rm(z) = rm0 sqrt(1 + (z/hf)^2). Default 4 rm0. */
  hf?: number;
  /** Height above which the swirl decays (m, gaussian scale). Default 2500. */
  hv?: number;
  /** Depth of the surface friction layer in which the swirl is reduced (m). Default 40. */
  hs?: number;
  /** Core updraft at the axis well above the surface layer, as a fraction of vt (Burgers part). Default 0.3. */
  wCore?: number;
  /** Depth of the Burgers inflow/updraft layer (m). Default 300. */
  ha?: number;
  /** Surface-layer inflow as a fraction of the local swirl (u_r = -beta v). Default 0.3. */
  beta?: number;
  /** Depth of the surface inflow layer (m). Default 30. */
  delta?: number;
}

export interface Vortex extends Required<VortexParams> {
  /** Normalisation so that max over z of the swirl height factor is 1. */
  gNorm: number;
  /** Strain rate a of the Burgers part (1/s). */
  a: number;
  /** Radius scale of the Burgers inflow/updraft core (m). */
  ra: number;
}

/** Height factor of the swirl before normalisation: weak at the ground, decaying aloft. */
function heightFactorRaw(z: number, hs: number, hv: number): number {
  const zz = Math.max(0, z);
  return (1 - 0.4 * Math.exp(-zz / hs)) * Math.exp(-(zz / hv) * (zz / hv));
}

export function makeVortex(p: VortexParams): Vortex {
  const hf = p.hf ?? 4 * p.rm;
  const hv = p.hv ?? 2500;
  const hs = p.hs ?? 40;
  const ha = p.ha ?? 300;
  const wCore = p.wCore ?? 0.3;
  let gmax = 0;
  for (let z = 0; z <= 3 * hv; z += 2) gmax = Math.max(gmax, heightFactorRaw(z, hs, hv));
  return {
    vt: p.vt,
    rm: p.rm,
    sense: p.sense ?? 1,
    hf,
    hv,
    hs,
    wCore,
    ha,
    beta: p.beta ?? 0.3,
    delta: p.delta ?? 30,
    gNorm: 1 / gmax,
    a: (wCore * p.vt) / ha,
    ra: 2.5 * p.rm,
  };
}

/** Peak-normalised swirl height factor g(z) (max 1). */
export function heightFactor(v: Vortex, z: number): number {
  return heightFactorRaw(z, v.hs, v.hv) * v.gNorm;
}

/** Core radius at height z (m). */
export function coreRadius(v: Vortex, z: number): number {
  const q = Math.max(0, z) / v.hf;
  return v.rm * Math.sqrt(1 + q * q);
}

/** Tangential wind magnitude (m/s, positive in the sense of rotation) at radius r, height z. */
export function tangential(v: Vortex, r: number, z: number): number {
  return v.vt * heightFactor(v, z) * swirlShape(r / coreRadius(v, z));
}

/** Angular velocity (rad/s, signed: +cyclonic) = sense * v / r. */
export function omega(v: Vortex, r: number, z: number): number {
  const rc = coreRadius(v, z);
  const x = r / rc;
  const f = swirlShape(x);
  // v / r = Vt g f / r ; for r -> 0 use f/x = B / C
  const fr = x < 1e-6 ? LO_B / LO_C : f / x;
  return v.sense * (v.vt * heightFactor(v, z) * fr) / rc;
}

/** Pressure deficit below the far-field value at (r, z), Pa, from cyclostrophic balance. */
export function pressureDeficit(v: Vortex, r: number, z: number): number {
  const vz = v.vt * heightFactor(v, z);
  return AIR_DENSITY * vz * vz * pressureShape(r / coreRadius(v, z));
}

/** Surface-layer swirl (ground profile, used for the inflow so the continuity is exact). */
function surfaceSwirl(v: Vortex, r: number): number {
  return v.vt * swirlShape(r / v.rm);
}

/** d(r v0)/dr / r for the ground profile v0 = Vt f(r/rm): 2 B Vt / (C rm) exp(-B x^2) * (1/rm)... see derivation in the docs. */
function surfaceConv(v: Vortex, r: number): number {
  const x = r / v.rm;
  // r v0 = Vt rm x f(x) = Vt rm (1 - exp(-B x^2)) / C ;  d/dr = Vt (2 B x / C) exp(-B x^2) ;  / r = (2 B Vt / (C rm)) exp(-B x^2)
  return ((2 * LO_B * v.vt) / (LO_C * v.rm)) * Math.exp(-LO_B * x * x);
}

/** Radial wind (m/s, negative = inflow) at (r, z). */
export function radialWind(v: Vortex, r: number, z: number): number {
  const zz = Math.max(0, z);
  const s = 1 / Math.cosh(zz / v.ha);
  const core = -0.5 * v.a * r * s * s * Math.exp(-(r * r) / (v.ra * v.ra));
  const bl = -v.beta * surfaceSwirl(v, r) * Math.exp(-zz / v.delta);
  return core + bl;
}

/** Vertical wind (m/s, positive up) at (r, z). Satisfies continuity with `radialWind`. */
export function verticalWind(v: Vortex, r: number, z: number): number {
  const zz = Math.max(0, z);
  const e = Math.exp(-(r * r) / (v.ra * v.ra));
  const core = v.a * v.ha * Math.tanh(zz / v.ha) * e * (1 - (r * r) / (v.ra * v.ra));
  const bl = v.beta * v.delta * (1 - Math.exp(-zz / v.delta)) * surfaceConv(v, r);
  return core + bl;
}

/** Full wind vector at (x, y, z) relative to the vortex axis (m/s). */
export function windAt(v: Vortex, x: number, y: number, z: number): [number, number, number] {
  const r = Math.hypot(x, y);
  if (r < 1e-6) return [0, 0, verticalWind(v, 0, z)];
  const vt = tangential(v, r, z) * v.sense;
  const ur = radialWind(v, r, z);
  const cx = x / r;
  const cy = y / r;
  // tangential direction for counter-clockwise rotation: (-y, x)/r
  return [ur * cx - vt * cy, ur * cy + vt * cx, verticalWind(v, r, z)];
}

/** Horizontal wind speed (m/s) at (r, z). */
export function horizontalSpeed(v: Vortex, r: number, z: number): number {
  return Math.hypot(tangential(v, r, z), radialWind(v, r, z));
}

/** Radius at which the swirl falls to `vEdge` outside the core (m), at height z. Used to size a core from a damage swath. */
export function radiusOfSpeed(vtPeak: number, rm: number, vEdge: number): number {
  // solve vt * f(r/rm) = vEdge for r > rm by bisection (f decreases monotonically beyond x = 1)
  if (vEdge >= vtPeak) return rm;
  let lo = 1;
  let hi = 200;
  for (let i = 0; i < 80; i++) {
    const mid = 0.5 * (lo + hi);
    if (vtPeak * swirlShape(mid) > vEdge) lo = mid;
    else hi = mid;
  }
  return rm * 0.5 * (lo + hi);
}

/**
 * Core radius rm (m) for which the swirl falls to `vEdge` (the damage threshold, EF0 = 29 m/s) at the
 * edge of the surveyed damage swath, half the path width: this ties the modelled core to the survey.
 */
export function coreRadiusFromSwath(vtPeak: number, widthM: number, vEdge = 29): number {
  const half = widthM / 2;
  const xEdge = radiusOfSpeed(vtPeak, 1, vEdge); // r / rm at the edge
  return half / xEdge;
}

/** EF-scale 3-s gust ranges (m/s) from 65-85, 86-110, 111-135, 136-165, 166-200, >200 mph. */
export const EF_RANGE_MS: Record<string, [number, number]> = {
  EF0: [29.1, 38.0],
  EF1: [38.4, 49.2],
  EF2: [49.6, 60.3],
  EF3: [60.7, 73.8],
  EF4: [74.2, 89.4],
  EF5: [89.8, 100],
};

/** Midpoint of the EF-scale wind range (m/s): the survey-implied peak wind used for the near-core field. */
export function efPeakWind(rating: string): number {
  const r = EF_RANGE_MS[(rating || '').toUpperCase().replace(/^EFU$/, 'EF0')] ?? EF_RANGE_MS.EF1;
  return 0.5 * (r[0] + r[1]);
}

/** Tracer advance: integrate a particle that follows the horizontal flow's rotation for dt, returns the new azimuth angle (rad). */
export function azimuthAfter(v: Vortex, r: number, z: number, phi0: number, dt: number): number {
  return phi0 + omega(v, r, z) * dt;
}

/** A mesoscale vortex (the radar couplet): same functional form with a km-scale core, no surface inflow, weak updraft. */
export function makeMesoVortex(vrot: number, radiusM: number, sense: 1 | -1 = 1): Vortex {
  return makeVortex({
    vt: vrot,
    rm: radiusM,
    sense,
    hf: 6 * radiusM,
    hv: 9000,
    hs: 150,
    wCore: 0.0,
    ha: 1500,
    beta: 0,
    delta: 50,
  });
}
