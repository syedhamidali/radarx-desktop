/**
 * Condensation funnel from physics (pure functions; unit-tested in funnel.test.mjs).
 *
 * A parcel of the surrounding near-surface air (temperature T0, dew point Td0, pressure p0) that is carried
 * to a lower pressure p cools dry-adiabatically, T = T0 (p/p0)^kappa, while its vapour pressure falls in step
 * with p (mixing ratio conserved), so its dew point falls too, more slowly. Where the temperature reaches the
 * dew point the air saturates: a cloud droplet forms. That happens at a critical pressure ratio pi_c that
 * depends only on T0 and Td0. The pressure of the parcel at (r, z) is
 *     p = p_amb(z) - dp(r, z)
 * (hydrostatic ambient pressure at height z minus the cyclostrophic deficit of the vortex), so the visible
 * funnel is the region where   (p0 - p_amb(z)) + dp(r, z) >= (1 - pi_c) p0.
 * With dp = 0 this is the lifting condensation level (the cloud base); with the tornado's deficit the
 * boundary dips below it, to the ground when dp(0, 0) >= (1 - pi_c) p0. Nothing here is a drawn cone.
 *
 * The condensed water follows the excess: LWC ~ (deficit - critical) / (rho g) * LWC_AD with the adiabatic
 * liquid water gradient of about 2 g m^-3 per km (modelled, order of magnitude).
 */

import { AIR_DENSITY, GRAVITY, pressureDeficit, type Vortex } from './vortexField.ts';

export const RD = 287.04; // J/(kg K)
export const CP = 1004.7;
export const KAPPA = RD / CP;
export const EPS = 0.622;

/** Saturation vapour pressure (hPa) over water, Bolton (1980). T in deg C. */
export function satVaporPressure(tc: number): number {
  return 6.112 * Math.exp((17.67 * tc) / (tc + 243.5));
}

/** Dew point (deg C) for a vapour pressure e (hPa). */
export function dewpointFromVapor(e: number): number {
  const l = Math.log(e / 6.112);
  return (243.5 * l) / (17.67 - l);
}

export interface Thermo {
  /** Surface (inflow) temperature, deg C. */
  t0: number;
  /** Surface dew point, deg C. */
  td0: number;
  /** Surface pressure, hPa. */
  p0: number;
}

/** Espy's rule of thumb: LCL height above the surface, m. */
export function lclEspy(t0: number, td0: number): number {
  return 125 * Math.max(0, t0 - td0);
}

/** Bolton (1980) LCL temperature (K) and the height at the dry adiabatic lapse rate (m). */
export function lclBolton(t0: number, td0: number): number {
  const T = t0 + 273.15;
  const Td = td0 + 273.15;
  if (T <= Td) return 0;
  const tl = 1 / (1 / (Td - 56) + Math.log(T / Td) / 800) + 56;
  return ((T - tl) * CP) / GRAVITY;
}

/** Hydrostatic pressure (hPa) at height z (m) for a layer of mean virtual temperature tv (K). */
export function hydrostaticPressure(p0: number, z: number, tv: number): number {
  return p0 * Math.exp((-GRAVITY * z) / (RD * tv));
}

/** Parcel temperature (deg C) and dew point (deg C) after dry-adiabatic movement from p0 to p, mixing ratio conserved. */
export function parcelAt(th: Thermo, p: number): { t: number; td: number } {
  const pi = p / th.p0;
  const t = (th.t0 + 273.15) * Math.pow(pi, KAPPA) - 273.15;
  const e0 = satVaporPressure(th.td0);
  const r = (EPS * e0) / (th.p0 - e0); // mixing ratio
  const e = (r * p) / (EPS + r);
  return { t, td: dewpointFromVapor(e) };
}

/** Critical pressure ratio pi_c: the parcel saturates at p = pi_c p0 (bisection; undefined -> 0 when already saturated). */
export function condensationPressureRatio(th: Thermo): number {
  if (th.t0 <= th.td0) return 1;
  let lo = 0.3; // saturated side
  let hi = 1; // unsaturated side
  for (let i = 0; i < 60; i++) {
    const mid = 0.5 * (lo + hi);
    const pc = parcelAt(th, mid * th.p0);
    if (pc.t > pc.td) hi = mid; // still unsaturated at mid: the critical ratio is lower
    else lo = mid;
  }
  return 0.5 * (lo + hi);
}

export interface FunnelCondition {
  thermo: Thermo;
  /** Critical pressure ratio. */
  piC: number;
  /** Critical total pressure deficit, Pa. */
  dpCrit: number;
  /** Mean virtual temperature of the lowest layer, K (for the hydrostatic height term). */
  tv: number;
  /** Cloud base (LCL) above the surface for zero vortex deficit, m. */
  lcl: number;
}

export function makeCondition(th: Thermo): FunnelCondition {
  const piC = condensationPressureRatio(th);
  const dpCrit = (1 - piC) * th.p0 * 100; // hPa -> Pa
  const tv = (th.t0 + 273.15) * (1 + 0.378 * satVaporPressure(th.td0) / th.p0) - 1.5; // a layer slightly cooler than the surface
  const c: FunnelCondition = { thermo: th, piC, dpCrit, tv, lcl: 0 };
  c.lcl = baseHeight(c, 0);
  return c;
}

/** Pressure drop from the surface to height z in the ambient air, Pa. */
export function heightDeficit(c: FunnelCondition, z: number): number {
  const th = c.thermo;
  return (th.p0 - hydrostaticPressure(th.p0, z, c.tv)) * 100;
}

/** Total deficit that matters for condensation at (r, z): ambient height drop plus the vortex deficit (Pa). */
export function totalDeficit(c: FunnelCondition, v: Vortex | null, r: number, z: number): number {
  return heightDeficit(c, z) + (v ? pressureDeficit(v, r, z) : 0);
}

/** Lowest height (m) on the axis at which the air is saturated: the funnel / cloud base. 0 = reaches the ground. */
export function baseHeight(c: FunnelCondition, dpAxis: number | ((z: number) => number)): number {
  const dp = typeof dpAxis === 'function' ? dpAxis : () => dpAxis;
  if (heightDeficit(c, 0) + dp(0) >= c.dpCrit) return 0;
  let lo = 0;
  let hi = 8000;
  for (let i = 0; i < 70; i++) {
    const mid = 0.5 * (lo + hi);
    if (heightDeficit(c, mid) + dp(mid) >= c.dpCrit) hi = mid;
    else lo = mid;
  }
  return 0.5 * (lo + hi);
}

/**
 * Radius (m) at height z where the VORTEX deficit alone cools the surface air to its dew point
 * (dp(r, z) = dpCrit of the nearest-station air; no ambient height term): the visible condensation funnel
 * that the renderer draws. Below the ambient cloud base this is the funnel, above it the cloud takes over.
 */
export function vortexOnlyRadius(c: FunnelCondition, v: Vortex, z: number): number {
  if (pressureDeficit(v, 0, z) < c.dpCrit) return 0;
  let lo = 0;
  let hi = 60 * v.rm;
  for (let i = 0; i < 60; i++) {
    const mid = 0.5 * (lo + hi);
    if (pressureDeficit(v, mid, z) >= c.dpCrit) lo = mid;
    else hi = mid;
  }
  return 0.5 * (lo + hi);
}

/** Funnel base on the axis of vortex v (m above the surface). */
export function funnelBase(c: FunnelCondition, v: Vortex): number {
  return baseHeight(c, (z) => pressureDeficit(v, 0, z));
}

/** Radius (m) of the saturated region at height z (0 if the axis is unsaturated there). Bisection on r. */
export function funnelRadius(c: FunnelCondition, v: Vortex, z: number): number {
  if (totalDeficit(c, v, 0, z) < c.dpCrit) return 0;
  let lo = 0;
  let hi = 60 * v.rm;
  if (totalDeficit(c, v, hi, z) >= c.dpCrit) return hi; // saturated everywhere (cloud, not a funnel)
  for (let i = 0; i < 60; i++) {
    const mid = 0.5 * (lo + hi);
    if (totalDeficit(c, v, mid, z) >= c.dpCrit) lo = mid;
    else hi = mid;
  }
  return 0.5 * (lo + hi);
}

/** Condensed water (kg/m3, order of magnitude) from the excess deficit. LWC_AD = 2e-3 kg/m3 per km. */
export function condensate(c: FunnelCondition, deficit: number): number {
  const excess = Math.max(0, deficit - c.dpCrit);
  const dz = excess / (AIR_DENSITY * GRAVITY); // metres of equivalent adiabatic ascent
  return Math.min(3e-3, 2e-6 * dz);
}

/** Shape class from the width of the funnel relative to its height (descriptive only). */
export function shapeClass(topRadius: number, groundRadius: number, height: number): 'rope' | 'cone' | 'wedge' {
  const aspect = (2 * topRadius) / Math.max(1, height);
  if (aspect < 0.25) return 'rope';
  if (aspect < 0.8 || groundRadius < 0.4 * topRadius) return 'cone';
  return 'wedge';
}
