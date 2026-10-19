/**
 * Range-height (RHI) geometry and rasteriser: pure functions, no DOM, no Svelte.
 *
 * An RHI sweep keeps the azimuth fixed and moves the antenna in elevation, so its rays are laid out
 * along ELEVATION instead of azimuth. This module
 *
 *  - generalises the ray-layout idea of meshBuilder.ts (typical step, scan direction, sector ends,
 *    holes that must stay holes) over any angle array, with or without wrap-around;
 *  - implements the standard beam-height model (4/3 effective Earth radius, same as the engine's
 *    `engine/volume.py` and radarx.fundamentals.geometry) and its closed-form inverse;
 *  - turns a sweep into pixels by INVERSE mapping: every output pixel (ground range, height) is
 *    converted back to (slant range, elevation) and looked up in the ray / gate grid. That keeps cell
 *    edges exact, handles elevation gaps and sector ends, and makes hover lookups use the very same
 *    code path as the picture.
 *
 * Data flow is deliberately split in three so a smoothing / masking step can be added later
 * without touching the geometry: `SweepDataEntry -> RhiField (values may be transformed) ->
 * rasterizeRhi() -> RGBA pixels`.
 */

/** A gate is "no data" when value <= -9998 or NaN (same convention as meshBuilder.ts). */
export const NODATA_LIMIT = -9998;
const DEG2RAD = Math.PI / 180;
const RAD2DEG = 180 / Math.PI;

export const EARTH_RADIUS_M = 6_371_000;
/** Effective Earth radius of the standard 4/3 beam model (metres). */
export const EFFECTIVE_RADIUS_M = (EARTH_RADIUS_M * 4) / 3;

// ─── Beam geometry ─────────────────────────────────────────────────────────

/**
 * Ground distance and height above the radar of the beam centre at slant range `rangeM` and
 * elevation `elDeg`: h = sqrt(r^2 + Re^2 + 2 r Re sin(el)) - Re, s = Re asin(r cos(el) / (Re + h))
 * (Doviak & Zrnic 1993, eq. 2.28b/c).
 */
export function beamPoint(
  rangeM: number,
  elDeg: number,
  re: number = EFFECTIVE_RADIUS_M,
): { groundM: number; heightM: number } {
  const el = elDeg * DEG2RAD;
  const heightM = Math.sqrt(rangeM * rangeM + re * re + 2 * rangeM * re * Math.sin(el)) - re;
  const arg = (rangeM * Math.cos(el)) / (re + heightM);
  const groundM = re * Math.asin(Math.max(-1, Math.min(1, arg)));
  return { groundM, heightM };
}

/** Inverse of {@link beamPoint}: slant range and elevation of the point (ground distance, height). */
export function beamInverse(
  groundM: number,
  heightM: number,
  re: number = EFFECTIVE_RADIUS_M,
): { rangeM: number; elDeg: number } {
  const alpha = groundM / re;
  const rh = re + heightM;
  const dx = rh * Math.sin(alpha);
  const dz = rh * Math.cos(alpha) - re;
  return { rangeM: Math.hypot(dx, dz), elDeg: Math.atan2(dz, dx) * RAD2DEG };
}

// ─── Ray layout over an angle array ────────────────────────────────────────

/** Typical spacing, scan direction and closure of an angle list. */
export interface AngleLayout {
  /** Typical angular step between neighbours, degrees (>0). */
  step: number;
  /** +1 when the angle increases with the index, -1 when it decreases. */
  dir: 1 | -1;
  /** True when the last entry is followed by the first (a full circle; only with a period). */
  closed: boolean;
}

/** A step up to this many times the typical spacing counts as a normal, continuous step. */
const BRIDGE_FACTOR = 1.5;
/** Holes are filled (split between the neighbours) up to this many typical steps, and at most this many degrees. */
const MAX_FILL_STEPS = 8;
const MAX_FILL_DEG = 10;

/** Angle difference wrapped into [-period/2, period/2); `period <= 0` means no wrapping. */
function wrapDiff(d: number, period: number): number {
  if (!(period > 0)) return d;
  return (((d % period) + 1.5 * period) % period) - period / 2;
}

/**
 * Generalisation of meshBuilder.rayLayout. `period` is 360 for azimuth (wraps; a full sweep is
 * `closed`) and 0 for elevation (never wraps).
 */
export function angleLayout(angles: ArrayLike<number>, n: number, period = 0): AngleLayout {
  if (n < 2) return { step: 1, dir: 1, closed: false };
  const mags = new Float64Array(n - 1);
  let signSum = 0;
  for (let i = 0; i < n - 1; i++) {
    const d = wrapDiff(angles[i + 1] - angles[i], period);
    mags[i] = Math.abs(d);
    signSum += Math.sign(d);
  }
  const sorted = Float64Array.from(mags).sort();
  let step = sorted[Math.floor((sorted.length - 1) / 2)];
  if (!(step > 0)) step = sorted.find((m) => m > 0) ?? 1;
  const dir: 1 | -1 = signSum >= 0 ? 1 : -1;
  const closed =
    period > 0 &&
    (() => {
      const back = wrapDiff(angles[0] - angles[n - 1], period) * dir;
      return back > 0 && back <= BRIDGE_FACTOR * step;
    })();
  return { step, dir, closed };
}

/** Angular step (>= 0) from entry `i` to the next, or -1 when no cell should span it. */
function stepToNext(angles: ArrayLike<number>, n: number, i: number, lay: AngleLayout, period: number): number {
  let next = i + 1;
  if (next >= n) {
    if (!lay.closed) return -1;
    next = 0;
  }
  const d = Math.abs(wrapDiff(angles[next] - angles[i], period));
  const limit = Math.max(BRIDGE_FACTOR * lay.step, Math.min(MAX_FILL_DEG, MAX_FILL_STEPS * lay.step));
  return d <= limit ? d : -1;
}

/**
 * How far entry `i` reaches on each side, in degrees: half-way to a neighbour, or half a normal
 * step at the end of a sector / next to a hole too big to fill. Generalisation of
 * meshBuilder.rayHalfWidths.
 */
export function angleHalfWidths(
  angles: ArrayLike<number>,
  n: number,
  i: number,
  lay: AngleLayout = angleLayout(angles, n),
  period = 0,
): { halfPrev: number; halfNext: number } {
  const toNext = stepToNext(angles, n, i, lay, period);
  const prevIdx = i > 0 ? i - 1 : n - 1;
  const fromPrev = i > 0 || lay.closed ? stepToNext(angles, n, prevIdx, lay, period) : -1;
  return {
    halfPrev: (fromPrev >= 0 ? fromPrev : lay.step) / 2,
    halfNext: (toNext >= 0 ? toNext : lay.step) / 2,
  };
}

/**
 * Index of the entry drawn at angle `a`, or -1 when that angle is empty (outside a sector, or in
 * a hole that is not filled). Generalisation of meshBuilder.rayAtAzimuth; O(n), for one-off lookups.
 */
export function angleIndexAt(angles: ArrayLike<number>, n: number, a: number, period = 0): number {
  if (n <= 0) return -1;
  const lay = angleLayout(angles, n, period);
  let best = 0;
  let bestDist = Infinity;
  for (let i = 0; i < n; i++) {
    const d = Math.abs(wrapDiff(a - angles[i], period));
    if (d < bestDist) {
      bestDist = d;
      best = i;
    }
  }
  const { halfPrev, halfNext } = angleHalfWidths(angles, n, best, lay, period);
  const offset = wrapDiff(a - angles[best], period) * lay.dir;
  const reach = offset >= 0 ? halfNext : halfPrev;
  return Math.abs(offset) <= reach + 1e-6 ? best : -1;
}

/** Per-ray cell bounds in elevation, for O(log n) lookups. Rays must be in ascending elevation. */
export interface RhiLayout {
  n: number;
  /** Ray elevations (ascending, degrees). */
  centers: Float64Array;
  /** Lower / upper elevation edge of every ray's cell. */
  lo: Float64Array;
  hi: Float64Array;
  /** Typical elevation step between rays, degrees. */
  step: number;
}

export function buildRhiLayout(elevation: ArrayLike<number>, n: number): RhiLayout {
  const centers = new Float64Array(n);
  for (let i = 0; i < n; i++) centers[i] = elevation[i];
  const lay = angleLayout(centers, n, 0);
  const lo = new Float64Array(n);
  const hi = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const { halfPrev, halfNext } = angleHalfWidths(centers, n, i, lay, 0);
    // dir is +1 for the ascending rays the engine sends; a descending list is mirrored.
    lo[i] = centers[i] - (lay.dir === 1 ? halfPrev : halfNext);
    hi[i] = centers[i] + (lay.dir === 1 ? halfNext : halfPrev);
  }
  return { n, centers, lo, hi, step: lay.step };
}

/** Index of the ray whose cell contains elevation `el`, or -1 (above / below the scan, or in a hole). */
export function rayAtElevation(layout: RhiLayout, el: number): number {
  const { n, centers, lo, hi } = layout;
  if (n === 0) return -1;
  // nearest centre by binary search (ascending)
  let a = 0;
  let b = n - 1;
  while (a < b) {
    const m = (a + b) >> 1;
    if (centers[m] < el) a = m + 1;
    else b = m;
  }
  let best = a;
  if (a > 0 && Math.abs(centers[a - 1] - el) <= Math.abs(centers[a] - el)) best = a - 1;
  return el >= lo[best] - 1e-9 && el <= hi[best] + 1e-9 ? best : -1;
}

// ─── Gate (range) axis ─────────────────────────────────────────────────────

/** Gate centres in metres, ascending. */
export interface RangeAxis {
  n: number;
  range: ArrayLike<number>;
  /** First gate centre and gate spacing when the gates are evenly spaced. */
  r0: number;
  dr: number;
  uniform: boolean;
  /** Edge of the first / last gate (m). */
  minM: number;
  maxM: number;
}

export function buildRangeAxis(range: ArrayLike<number>, n: number): RangeAxis {
  const r0 = n > 0 ? range[0] : 0;
  const dr = n > 1 ? (range[n - 1] - range[0]) / (n - 1) : 1;
  let uniform = n > 1 && dr > 0;
  if (uniform) {
    const tol = dr * 0.01;
    for (let i = 1; i < n; i += Math.max(1, (n / 64) | 0)) {
      if (Math.abs(range[i] - (r0 + dr * i)) > tol) {
        uniform = false;
        break;
      }
    }
  }
  const half = dr / 2;
  return { n, range, r0, dr, uniform, minM: r0 - half, maxM: (n > 0 ? range[n - 1] : 0) + half };
}

/** Gate containing slant range `r` (m), or -1 outside the first / last gate. */
export function gateAtRange(ax: RangeAxis, r: number): number {
  if (ax.n === 0 || r < ax.minM || r > ax.maxM) return -1;
  if (ax.uniform) {
    const i = Math.floor((r - ax.r0) / ax.dr + 0.5);
    return i < 0 ? 0 : i >= ax.n ? ax.n - 1 : i;
  }
  let a = 0;
  let b = ax.n - 1;
  while (a < b) {
    const m = (a + b) >> 1;
    if (ax.range[m] < r) a = m + 1;
    else b = m;
  }
  if (a > 0 && Math.abs(ax.range[a - 1] - r) <= Math.abs(ax.range[a] - r)) a -= 1;
  return a;
}

// ─── Field + sampling ──────────────────────────────────────────────────────

/** What the rasteriser draws: one RHI plane as a ray x gate grid (values may have been transformed). */
export interface RhiField {
  /** Ray elevations, ascending, degrees (length nRays). */
  elevation: ArrayLike<number>;
  /** Gate centres, metres (length nRange). */
  range: ArrayLike<number>;
  /** nRays x nRange, row-major. */
  values: ArrayLike<number>;
  nRays: number;
  nRange: number;
}

/** Layout lookups derived from a field; build once per field and reuse for drawing and hover. */
export interface RhiLookup {
  field: RhiField;
  rays: RhiLayout;
  gates: RangeAxis;
}

export function buildLookup(field: RhiField): RhiLookup {
  return {
    field,
    rays: buildRhiLayout(field.elevation, field.nRays),
    gates: buildRangeAxis(field.range, field.nRange),
  };
}

export interface RhiSample {
  ray: number;
  gate: number;
  /** NaN when the gate has no data. */
  value: number;
  rangeM: number;
  elDeg: number;
}

/** What is drawn at (ground distance, height above radar), or null when the point is outside the scan. */
export function sampleAt(lk: RhiLookup, groundM: number, heightM: number): RhiSample | null {
  const { rangeM, elDeg } = beamInverse(groundM, heightM);
  const ray = rayAtElevation(lk.rays, elDeg);
  if (ray < 0) return null;
  const gate = gateAtRange(lk.gates, rangeM);
  if (gate < 0) return null;
  const v = lk.field.values[ray * lk.field.nRange + gate];
  return { ray, gate, value: v > NODATA_LIMIT ? v : NaN, rangeM, elDeg };
}

// ─── Extents ───────────────────────────────────────────────────────────────

export interface Extent {
  xMinKm: number;
  xMaxKm: number;
  yMinKm: number;
  yMaxKm: number;
}

/**
 * Bounding box (km, ground x height above the radar) of the gates that carry data at or above
 * `minValue` (the colour range's lower end: gates below it are not drawn, so they must not stretch
 * the view); the radar (0, 0) is always included. Falls back to the full scan extent when nothing
 * qualifies.
 */
export function dataExtent(lk: RhiLookup, minValue = NODATA_LIMIT): Extent {
  const { field, rays, gates } = lk;
  let xMin = 0, xMax = 0, yMin = 0, yMax = 0;
  let any = false;
  const floor = Math.max(minValue, NODATA_LIMIT);
  const shown = (v: number) => v > NODATA_LIMIT && v >= floor;
  const take = (rangeM: number, el: number) => {
    const p = beamPoint(rangeM, el);
    if (p.groundM < xMin) xMin = p.groundM;
    if (p.groundM > xMax) xMax = p.groundM;
    if (p.heightM < yMin) yMin = p.heightM;
    if (p.heightM > yMax) yMax = p.heightM;
  };
  for (let i = 0; i < field.nRays; i++) {
    const base = i * field.nRange;
    let first = -1;
    let last = -1;
    for (let g = 0; g < field.nRange; g++) {
      if (shown(field.values[base + g])) {
        first = g;
        break;
      }
    }
    if (first < 0) continue;
    for (let g = field.nRange - 1; g >= first; g--) {
      if (shown(field.values[base + g])) {
        last = g;
        break;
      }
    }
    any = true;
    for (const el of [rays.lo[i], rays.hi[i]]) {
      take(Math.max(0, field.range[first] - gates.dr / 2), el);
      take(field.range[last] + gates.dr / 2, el);
    }
  }
  if (!any && field.nRays > 0 && field.nRange > 0) {
    for (const el of [rays.lo[0], rays.hi[rays.n - 1]]) take(gates.maxM, el);
  }
  return { xMinKm: xMin / 1000, xMaxKm: xMax / 1000, yMinKm: yMin / 1000, yMaxKm: yMax / 1000 };
}

// ─── Rasteriser ────────────────────────────────────────────────────────────

/** The visible window and its pixel size. Pixels are square in x; y may be exaggerated. */
export interface RhiViewport {
  /** Left edge ground range and top edge height above the radar, km. */
  xLeftKm: number;
  yTopKm: number;
  /** Kilometres per output pixel in x and in y (y = x / exaggeration). */
  kmPerPxX: number;
  kmPerPxY: number;
  width: number;
  height: number;
}

/** Colour of a value: 256 RGB triplets (a COLORMAP_DATA entry). */
export type ColormapLut = ArrayLike<number>;

/**
 * Paint the field into `out` (RGBA, width*height*4, row 0 = top). Pixels with no data, outside the
 * scan, or below `vmin` are left fully transparent. Returns the number of painted pixels.
 */
export function rasterizeRhi(
  lk: RhiLookup,
  vp: RhiViewport,
  lut: ColormapLut,
  vmin: number,
  vmax: number,
  out: Uint8ClampedArray,
): number {
  const { field, rays, gates } = lk;
  const { width, height } = vp;
  out.fill(0, 0, width * height * 4);
  if (field.nRays === 0 || field.nRange === 0 || width <= 0 || height <= 0) return 0;

  const re = EFFECTIVE_RADIUS_M;
  const sinA = new Float64Array(width);
  const cosA = new Float64Array(width);
  for (let px = 0; px < width; px++) {
    const alpha = ((vp.xLeftKm + (px + 0.5) * vp.kmPerPxX) * 1000) / re;
    sinA[px] = Math.sin(alpha);
    cosA[px] = Math.cos(alpha);
  }
  const rMin2 = Math.max(0, gates.minM) ** 2;
  const rMax2 = gates.maxM ** 2;
  const elLo = rays.lo[0];
  const elHi = rays.hi[rays.n - 1];
  const scale = vmax > vmin ? 255 / (vmax - vmin) : 0;
  const vals = field.values;
  const nRange = field.nRange;
  let painted = 0;

  for (let py = 0; py < height; py++) {
    const rh = re + (vp.yTopKm - (py + 0.5) * vp.kmPerPxY) * 1000;
    let o = py * width * 4;
    for (let px = 0; px < width; px++, o += 4) {
      const dx = rh * sinA[px];
      const dz = rh * cosA[px] - re;
      const r2 = dx * dx + dz * dz;
      if (r2 > rMax2 || r2 < rMin2) continue;
      const el = Math.atan2(dz, dx) * RAD2DEG;
      if (el < elLo || el > elHi) continue;
      const ray = rayAtElevation(rays, el);
      if (ray < 0) continue;
      const gate = gateAtRange(gates, Math.sqrt(r2));
      if (gate < 0) continue;
      const v = vals[ray * nRange + gate];
      if (!(v > NODATA_LIMIT) || v < vmin) continue;
      let t = ((v - vmin) * scale + 0.5) | 0;
      if (t > 255) t = 255;
      else if (t < 0) t = 0;
      const c = t * 3;
      out[o] = lut[c];
      out[o + 1] = lut[c + 1];
      out[o + 2] = lut[c + 2];
      out[o + 3] = 255;
      painted++;
    }
  }
  return painted;
}

// ─── Axes, grid and labels ─────────────────────────────────────────────────

/** "Nice" axis ticks (1, 2, 5 x 10^k) covering [min, max], about `target` of them. */
export function niceTicks(min: number, max: number, target = 6): number[] {
  const span = max - min;
  if (!(span > 0) || !isFinite(span)) return [];
  const raw = span / Math.max(1, target);
  const pow = 10 ** Math.floor(Math.log10(raw));
  const f = raw / pow;
  const step = (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * pow;
  const ticks: number[] = [];
  for (let k = Math.ceil(min / step - 1e-9); k * step <= max + step * 1e-9; k++) {
    ticks.push(k === 0 ? 0 : +(k * step).toPrecision(12));
  }
  return ticks;
}

/** Tick label without float noise (4 -> "4", 2.5 -> "2.5"). */
export function formatTick(v: number, step?: number): string {
  const decimals = step !== undefined && step < 1 ? Math.min(3, Math.ceil(-Math.log10(step))) : 0;
  return v.toFixed(decimals);
}

/**
 * Constant-range arc and constant-elevation ray: polylines (km) of the grid drawn over the field,
 * the RHI counterpart of a PPI's range rings.
 */
export interface RhiGridLine {
  kind: 'range' | 'elevation';
  /** Slant range in km, or elevation in degrees. */
  value: number;
  /** Polyline in (ground km, height km). */
  points: [number, number][];
}

export function gridLines(elMinDeg: number, elMaxDeg: number, maxRangeM: number): RhiGridLine[] {
  const lines: RhiGridLine[] = [];
  const maxKm = maxRangeM / 1000;
  if (!(maxKm > 0)) return lines;

  // Constant range arcs
  const lo = Math.min(0, elMinDeg);
  const hi = Math.max(elMaxDeg, elMinDeg + 1);
  for (const rKm of niceTicks(0, maxKm, 6)) {
    if (rKm <= 0) continue;
    const pts: [number, number][] = [];
    const nStep = Math.max(8, Math.ceil((hi - lo) / 0.5));
    for (let k = 0; k <= nStep; k++) {
      const p = beamPoint(rKm * 1000, lo + ((hi - lo) * k) / nStep);
      pts.push([p.groundM / 1000, p.heightM / 1000]);
    }
    lines.push({ kind: 'range', value: rKm, points: pts });
  }

  // Constant elevation rays
  const span = Math.max(hi - lo, 1);
  const elStep = span > 40 ? 10 : span > 18 ? 5 : span > 8 ? 2 : 1;
  for (let el = Math.ceil(elMinDeg / elStep) * elStep; el <= elMaxDeg + 1e-9; el += elStep) {
    if (el === 0 && elMinDeg > 0) continue;
    const pts: [number, number][] = [];
    for (let k = 0; k <= 24; k++) {
      const p = beamPoint((maxKm * 1000 * k) / 24, el);
      pts.push([p.groundM / 1000, p.heightM / 1000]);
    }
    lines.push({ kind: 'elevation', value: el, points: pts });
  }
  return lines;
}

// ─── Sweep list label ──────────────────────────────────────────────────────

/** The bits of a schema sweep entry the label needs. */
export interface SweepLabelInfo {
  index: number;
  elevation: number | null;
  kind?: string;
  azimuth?: number;
}

/**
 * Text of a sweep in selectors: "Sweep 3 (1.5°)" for a PPI, "RHI 135.0° (sweep 0)" for a range-height
 * scan (the sweep number keeps several RHIs of the same azimuth apart).
 */
export function sweepLabel(s: SweepLabelInfo): string {
  if (s.kind === 'rhi') {
    return `RHI ${s.azimuth != null ? s.azimuth.toFixed(1) : '?'}° (sweep ${s.index})`;
  }
  return `Sweep ${s.index} (${s.elevation != null ? s.elevation.toFixed(1) : '?'}°)`;
}
