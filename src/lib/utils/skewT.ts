/**
 * Geometry of the Skew-T log-p diagram and the hodograph: pure functions, no DOM.
 *
 * Skew-T: pressure is the vertical axis on a log scale (1050 hPa at the bottom, 100 hPa at the top);
 * temperature is the horizontal axis, skewed so isotherms lean to the right with height. The skew is
 * a fixed number of degrees per unit of ln(p), so the diagram is a linear map of (T, ln p).
 *
 * The background curves (isotherms, dry adiabats, saturation mixing-ratio lines, pseudo-adiabats)
 * are computed here from the textbook formulas. They are reference lines only; every number shown
 * next to the diagram (CAPE, LCL, ...) comes from the engine (radarx), never from this file.
 */

export const P_BOTTOM = 1050;
export const P_TOP = 100;
/** Degrees C of horizontal shift per unit of ln(p0 / p). */
export const SKEW = 37;
export const T_MIN = -40; // left edge at the bottom of the diagram, degC
export const T_MAX = 50; // right edge at the bottom
export const PRESSURE_TICKS = [1000, 925, 850, 700, 500, 400, 300, 250, 200, 150, 100];
export const KAPPA = 0.2854; // Rd/cp as used in Bolton (1980)

const LN_SPAN = Math.log(P_BOTTOM / P_TOP);

export interface Frame {
  width: number;
  height: number;
  left: number;
  right: number;
  top: number;
  bottom: number;
}

export function makeFrame(width: number, height: number, m = { left: 38, right: 46, top: 10, bottom: 24 }): Frame {
  return { width, height, left: m.left, right: width - m.right, top: m.top, bottom: height - m.bottom };
}

/** 0 at P_BOTTOM, 1 at P_TOP. */
export const yFrac = (p: number): number => Math.log(P_BOTTOM / p) / LN_SPAN;

/** Horizontal position as a fraction of the diagram width. */
export function xFrac(tC: number, p: number): number {
  return (tC + SKEW * Math.log(P_BOTTOM / p) - T_MIN) / (T_MAX - T_MIN);
}

export function project(f: Frame, tC: number, p: number): [number, number] {
  return [f.left + xFrac(tC, p) * (f.right - f.left), f.bottom - yFrac(p) * (f.bottom - f.top)];
}

/** Inverse of `project` for the pressure: used to place the height scale and read the cursor. */
export function pressureAtY(f: Frame, y: number): number {
  const frac = (f.bottom - y) / (f.bottom - f.top);
  return P_BOTTOM / Math.exp(frac * LN_SPAN);
}

export function pressureLevels(step = 25): number[] {
  const out: number[] = [];
  for (let p = P_BOTTOM; p >= P_TOP; p -= step) out.push(p);
  return out;
}

/** An SVG path "M x y L x y ..." from points, breaking the line where a value is missing. */
export function pathOf(f: Frame, tC: (number | null)[], p: (number | null)[]): string {
  let d = '';
  let pen = false;
  for (let i = 0; i < p.length; i++) {
    const t = tC[i];
    const pp = p[i];
    if (t === null || t === undefined || pp === null || pp === undefined || !Number.isFinite(t) || !Number.isFinite(pp) || pp > P_BOTTOM || pp < P_TOP) {
      pen = false;
      continue;
    }
    const [x, y] = project(f, t, pp);
    d += `${pen ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`;
    pen = true;
  }
  return d;
}

// ── Background curves ───────────────────────────────────────────────────────────────────────────

/** Saturation vapour pressure over liquid water, hPa (Bolton 1980, eq. 10). */
export const satVapourHpa = (tC: number): number => 6.112 * Math.exp((17.67 * tC) / (tC + 243.5));

/** Temperature (degC) along the dry adiabat of potential temperature theta (K) at pressure p (hPa). */
export const dryAdiabatC = (thetaK: number, p: number): number => thetaK * Math.pow(p / 1000, KAPPA) - 273.15;

/** Dew point (degC) of air with saturation mixing ratio w (g/kg) at p (hPa). */
export function mixingRatioLineC(wGkg: number, p: number): number {
  const w = wGkg / 1000;
  const e = (w * p) / (0.622 + w);
  const ln = Math.log(e / 6.112);
  return (243.5 * ln) / (17.67 - ln);
}

/**
 * Pseudo-adiabat through (tStartC, 1000 hPa): integrates the moist-adiabatic lapse rate in
 * pressure with RK4 (Rogers and Yau 1989). Returns [pressure, temperature degC] pairs, upward.
 */
export function pseudoAdiabat(tStartC: number, pTop = P_TOP, dp = 10): [number, number][] {
  const Rd = 287.04;
  const cp = 1005.7;
  const Lv = 2.501e6;
  const eps = 0.622;
  const dTdp = (p: number, T: number): number => {
    const es = satVapourHpa(T - 273.15);
    const rs = (eps * es) / (p - es);
    const num = (Rd * T + Lv * rs) / p;
    const den = cp + (Lv * Lv * rs * eps) / (Rd * T * T);
    return num / den; // K per hPa (dp negative going up gives cooling)
  };
  const out: [number, number][] = [[1000, tStartC]];
  let T = tStartC + 273.15;
  let p = 1000;
  while (p - dp >= pTop - 1e-9) {
    const h = -dp;
    const k1 = dTdp(p, T);
    const k2 = dTdp(p + h / 2, T + (k1 * h) / 2);
    const k3 = dTdp(p + h / 2, T + (k2 * h) / 2);
    const k4 = dTdp(p + h, T + k3 * h);
    T += (h / 6) * (k1 + 2 * k2 + 2 * k3 + k4);
    p += h;
    out.push([p, T - 273.15]);
  }
  return out;
}

// ── Heights ─────────────────────────────────────────────────────────────────────────────────────

/** Height (m) at pressure p by interpolating the profile in ln(p); null outside it. */
export function heightAtPressure(pHpa: (number | null)[], zM: (number | null)[], p: number): number | null {
  let best: number | null = null;
  for (let i = 0; i + 1 < pHpa.length; i++) {
    const p0 = pHpa[i];
    const p1 = pHpa[i + 1];
    const z0 = zM[i];
    const z1 = zM[i + 1];
    if (p0 == null || p1 == null || z0 == null || z1 == null) continue;
    if (p <= p0 && p >= p1 && p0 > p1) {
      const w = Math.log(p0 / p) / Math.log(p0 / p1);
      best = z0 + w * (z1 - z0);
      break;
    }
  }
  return best;
}

// ── Hodograph ───────────────────────────────────────────────────────────────────────────────────

export interface HodoFrame {
  cx: number;
  cy: number;
  scale: number; // px per m/s
  max: number; // m/s at the outer ring
}

/** The outer ring is the smallest multiple of 10 m/s that holds every point (and the storm motions). */
export function hodographFrame(size: number, u: (number | null)[], v: (number | null)[], extra: [number, number][] = []): HodoFrame {
  let m = 0;
  for (let i = 0; i < u.length; i++) {
    const a = u[i];
    const b = v[i];
    if (a == null || b == null) continue;
    m = Math.max(m, Math.hypot(a, b));
  }
  for (const [a, b] of extra) m = Math.max(m, Math.hypot(a, b));
  const max = Math.max(20, Math.ceil((m + 1) / 10) * 10);
  const r = size / 2 - 14;
  return { cx: size / 2, cy: size / 2, scale: r / max, max };
}

/** SVG coordinates of a wind (u east, v north); north is up. */
export const hodoPoint = (f: HodoFrame, u: number, v: number): [number, number] => [f.cx + u * f.scale, f.cy - v * f.scale];

/** Height classes of the hodograph trace, m AGL (upper bound exclusive). */
export const HODO_LAYERS: { top: number; label: string; token: string }[] = [
  { top: 1000, label: '0-1 km', token: 'var(--c-alarm)' },
  { top: 3000, label: '1-3 km', token: 'var(--c-caution)' },
  { top: 6000, label: '3-6 km', token: 'var(--c-nominal)' },
  { top: 9000, label: '6-9 km', token: 'var(--c-accent)' },
  { top: Infinity, label: '> 9 km', token: 'var(--c-derived)' },
];

export const layerOf = (zAgl: number): number => HODO_LAYERS.findIndex((l) => zAgl < l.top);

/** The trace split into one SVG path per height class (consecutive points keep the line joined). */
export function hodographPaths(
  f: HodoFrame,
  u: (number | null)[],
  v: (number | null)[],
  zAgl: (number | null)[],
  maxAgl = 12000,
): { layer: number; d: string }[] {
  const paths: { layer: number; d: string }[] = [];
  let prev: [number, number] | null = null;
  let prevLayer = -1;
  for (let i = 0; i < u.length; i++) {
    const a = u[i];
    const b = v[i];
    const z = zAgl[i];
    if (a == null || b == null || z == null || z > maxAgl) {
      prev = null;
      continue;
    }
    const layer = layerOf(z);
    const pt = hodoPoint(f, a, b);
    if (prev && layer === prevLayer && paths.length) {
      paths[paths.length - 1].d += `L${pt[0].toFixed(1)} ${pt[1].toFixed(1)}`;
    } else {
      // start a new class; join to the previous point so the trace has no gap
      const start = prev && layer !== prevLayer ? `M${prev[0].toFixed(1)} ${prev[1].toFixed(1)}L` : 'M';
      paths.push({ layer, d: `${start}${pt[0].toFixed(1)} ${pt[1].toFixed(1)}` });
    }
    prev = pt;
    prevLayer = layer;
  }
  return paths;
}

/** Heights AGL of the levels of a profile given heights above sea level and the launch height. */
export const aglOf = (zMsl: (number | null)[], ground: number): (number | null)[] => zMsl.map((z) => (z == null ? null : z - ground));
