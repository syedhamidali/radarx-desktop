/**
 * Pure helpers of the rainfall (QPE) panel: chart scales, SVG paths and number formatting.
 * No DOM, no stores, so they can be unit-tested with `node --test`.
 */

export interface GaugePoint {
  station: string;
  gauge_mm: number;
  radar_mm: number;
  residual_mm: number;
  range_km: number | null;
}

export interface Tick {
  value: number;
  label: string;
}

/** About `n` round tick values covering [0, max] (1-2-5 steps). */
export function niceTicks(max: number, n = 4): Tick[] {
  if (!Number.isFinite(max) || max <= 0) return [{ value: 0, label: '0' }];
  const raw = max / n;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out: Tick[] = [];
  for (let i = 0; i < 100; i++) {
    const v = i * step;
    out.push({ value: v, label: formatNumber(v) });
    if (v >= max - step * 1e-9) break;
  }
  return out;
}

/** 0 -> "0", 12.5 -> "12.5", 0.125 -> "0.13", 1250 -> "1250". */
export function formatNumber(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '–';
  const a = Math.abs(v);
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1).replace(/\.0$/, '');
  return v.toFixed(digits).replace(/0+$/, '').replace(/\.$/, '');
}

export function formatPercent(v: number | null | undefined, digits = 0): string {
  return v === null || v === undefined || !Number.isFinite(v) ? '–' : `${(100 * v).toFixed(digits)} %`;
}

export function signedPercent(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '–';
  const p = 100 * v;
  return `${p > 0 ? '+' : ''}${p.toFixed(digits)} %`;
}

export interface ScatterGeometry {
  /** Upper bound of both axes (equal axes: the 1:1 line is the diagonal). */
  max: number;
  ticks: Tick[];
  x: (gauge: number) => number;
  y: (radar: number) => number;
  /** Plot rectangle inside the SVG viewBox. */
  left: number;
  top: number;
  size: number;
}

/** Square scatter plot geometry: gauge on x, radar on y, same scale, margins for the axes. */
export function scatterGeometry(
  values: number[],
  box = 220,
  margin = { left: 34, top: 8, right: 8, bottom: 26 },
): ScatterGeometry {
  const finite = values.filter((v) => Number.isFinite(v) && v >= 0);
  const hi = finite.length ? Math.max(...finite) : 1;
  const ticks = niceTicks(hi * 1.05, 4);
  const max = ticks[ticks.length - 1].value || 1;
  const size = box - margin.left - Math.max(margin.right, margin.bottom);
  return {
    max,
    ticks,
    left: margin.left,
    top: margin.top,
    size,
    x: (g) => margin.left + (Math.min(Math.max(g, 0), max) / max) * size,
    y: (r) => margin.top + size - (Math.min(Math.max(r, 0), max) / max) * size,
  };
}

export interface SeriesRow {
  time: string;
  mean_rate_mm_h: number | null;
  cumulative_mm: number | null;
}

export interface SeriesGeometry {
  tMin: number;
  tMax: number;
  rateMax: number;
  cumMax: number;
  ratePath: string;
  cumPath: string;
  bars: { x: number; w: number; y: number; h: number }[];
  xTicks: { x: number; label: string }[];
}

/** Epoch ms of an ISO time with or without the trailing Z (always UTC). */
export function utcMs(iso: string): number {
  return Date.parse(iso.endsWith('Z') ? iso : `${iso}Z`);
}

function path(points: [number, number][]): string {
  return points.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join('');
}

/** SVG paths of a basin series: mean rate (left axis), cumulative depth (right axis), hyetograph bars. */
export function seriesGeometry(
  rows: SeriesRow[],
  hyetograph: { start: string[]; end: string[]; intensity_mm_h: number[] },
  width = 260,
  height = 120,
  pad = { left: 30, right: 30, top: 6, bottom: 18 },
): SeriesGeometry | null {
  const pts = rows.filter((r) => Number.isFinite(utcMs(r.time)));
  if (pts.length < 2) return null;
  const t0 = utcMs(pts[0].time);
  const t1 = utcMs(pts[pts.length - 1].time);
  if (!(t1 > t0)) return null;
  const rates = pts.map((r) => r.mean_rate_mm_h ?? 0);
  const cums = pts.map((r) => r.cumulative_mm ?? 0);
  const rateMax = Math.max(niceTicks(Math.max(...rates, ...hyetograph.intensity_mm_h, 0.1) * 1.05, 3).at(-1)!.value, 0.1);
  const cumMax = Math.max(niceTicks(Math.max(...cums, 0.1) * 1.05, 3).at(-1)!.value, 0.1);
  const w = width - pad.left - pad.right;
  const h = height - pad.top - pad.bottom;
  const X = (t: number) => pad.left + ((t - t0) / (t1 - t0)) * w;
  const YR = (v: number) => pad.top + h - (Math.min(v, rateMax) / rateMax) * h;
  const YC = (v: number) => pad.top + h - (Math.min(v, cumMax) / cumMax) * h;
  const bars = hyetograph.start.map((s, i) => {
    const a = utcMs(s);
    const b = utcMs(hyetograph.end[i]);
    const v = hyetograph.intensity_mm_h[i] ?? 0;
    return { x: X(a), w: Math.max(X(b) - X(a) - 1, 1), y: YR(v), h: pad.top + h - YR(v) };
  });
  const xTicks = [0, 0.5, 1].map((f) => ({
    x: X(t0 + f * (t1 - t0)),
    label: new Date(t0 + f * (t1 - t0)).toISOString().slice(11, 16),
  }));
  return {
    tMin: t0,
    tMax: t1,
    rateMax,
    cumMax,
    ratePath: path(pts.map((r, i) => [X(utcMs(r.time)), YR(rates[i])] as [number, number])),
    cumPath: path(pts.map((r, i) => [X(utcMs(r.time)), YC(cums[i])] as [number, number])),
    bars,
    xTicks,
  };
}

/** The label that goes next to every number that is an estimate: never "observed". */
export function provenanceLabel(type: string | undefined | null): string {
  return type === 'ADJUSTED' ? 'ADJUSTED to gauges' : 'RETRIEVED from radar';
}

/** One-line description of the uncertainty for the result card. */
export function uncertaintyText(medianRel: number | null | undefined): string {
  if (medianRel === null || medianRel === undefined || !Number.isFinite(medianRel)) return 'not available';
  return `median ${(100 * medianRel).toFixed(0)} % (1 sigma, fully correlated in time)`;
}
