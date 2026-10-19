/**
 * The surface-based parcel path on the Skew-T of the movie (the app's Skew-T component does not draw
 * it: the engine returns only the numbers). Computed here from the same profile with the app's own
 * thermodynamics helpers (utils/skewT.ts): dry adiabat up to the lifting condensation level
 * (Bolton 1980), then the pseudo-adiabat through it. It is a path of the textbook parcel (no
 * entrainment, no ice), drawn thin; the CAPE / CIN numbers shown with it are the engine's.
 * Pure; unit-tested in perilsThermo.test.mjs.
 */
import { KAPPA, pseudoAdiabat } from '../utils/skewT.ts';
import type { SoundingLevels } from './perilsData.ts';

/** LCL temperature (C) and pressure (hPa) of a parcel at `tC`, dew point `tdC`, pressure `pHpa` (Bolton 1980, eq. 15 and Poisson). */
export function lcl(tC: number, tdC: number, pHpa: number): { tC: number; pHpa: number } {
  const T = tC + 273.15;
  const Td = Math.min(tdC, tC) + 273.15;
  const tl = 1 / (1 / (Td - 56) + Math.log(T / Td) / 800) + 56;
  return { tC: tl - 273.15, pHpa: pHpa * Math.pow(tl / T, 1 / KAPPA) };
}

/** Temperature (C) on the pseudo-adiabat labelled `t1000` (its temperature at 1000 hPa) at pressure `p`. */
function moistAt(t1000: number, p: number): number {
  const pa = pseudoAdiabat(t1000, 100, 5);
  for (let i = 1; i < pa.length; i++) {
    if (pa[i][0] <= p) {
      const [p0, t0] = pa[i - 1];
      const [p1, t1] = pa[i];
      return t0 + ((t1 - t0) * (p0 - p)) / (p0 - p1);
    }
  }
  return pa[pa.length - 1][1];
}

/** The pseudo-adiabat through (p, tC): its label (temperature at 1000 hPa), by bisection. */
export function pseudoLabel(tC: number, p: number): number {
  let lo = -60;
  let hi = 60;
  for (let i = 0; i < 40; i++) {
    const mid = (lo + hi) / 2;
    if (moistAt(mid, p) < tC) lo = mid;
    else hi = mid;
  }
  return (lo + hi) / 2;
}

export interface ParcelPath {
  /** [pressure hPa, temperature C] from the surface up. */
  path: [number, number][];
  lclHpa: number;
  lclC: number;
  start: { pHpa: number; tC: number; tdC: number };
}

/** The surface-based parcel path from the lowest level of a profile that has pressure, temperature and dew point. */
export function surfaceParcel(levels: SoundingLevels | null, pTop = 150): ParcelPath | null {
  if (!levels) return null;
  let i0 = -1;
  for (let i = 0; i < levels.pressure_hpa.length; i++) {
    if (levels.pressure_hpa[i] != null && levels.temperature_c[i] != null && levels.dewpoint_c[i] != null) {
      i0 = i;
      break;
    }
  }
  if (i0 < 0) return null;
  const p0 = levels.pressure_hpa[i0] as number;
  const t0 = levels.temperature_c[i0] as number;
  const td0 = levels.dewpoint_c[i0] as number;
  const l = lcl(t0, td0, p0);
  const path: [number, number][] = [];
  for (let p = p0; p > l.pHpa; p -= 10) path.push([p, (t0 + 273.15) * Math.pow(p / p0, KAPPA) - 273.15]);
  path.push([l.pHpa, l.tC]);
  const label = pseudoLabel(l.tC, l.pHpa);
  const pa = pseudoAdiabat(label, pTop, 10);
  for (const [p, t] of pa) if (p < l.pHpa) path.push([p, t]);
  return { path, lclHpa: l.pHpa, lclC: l.tC, start: { pHpa: p0, tC: t0, tdC: td0 } };
}
