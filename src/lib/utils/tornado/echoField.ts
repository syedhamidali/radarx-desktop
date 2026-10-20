/**
 * The radar echo under the tornado simulation, through time (pure; unit-tested in echoField.test.mjs).
 *
 * The seed carries the lowest-level reflectivity of every observed volume of the survey's life on one grid (local metres).
 * Between two volumes the echo is carried along the observed pattern motion (the same motion the film's 3-D echo uses to
 * interpolate) and the two are blended, so the rain curtains of the simulation move and change with the radar echo instead
 * of standing still: the earlier snapshot is shifted forward by the motion times the time since it, the later one back by
 * the time to it. Reflectivity is turned into rain rate (Marshall-Palmer Z = 200 R^1.6, capped at 120 mm/h) before it is
 * blended.
 */

export interface EchoGrid {
  x0: number;
  y0: number;
  dx: number;
  nx: number;
  ny: number;
  /** Row-major (y rows) reflectivity (dBZ); null / NaN / below -30 = no echo. */
  dbz: (number | null)[];
  /** Time on the data clock (s). */
  t: number;
}

/** Rain rate (mm/h) of a reflectivity (dBZ); 0 below 5 dBZ and for missing values. */
export function rainRate(dbz: number | null | undefined): number {
  if (dbz == null || !Number.isFinite(dbz) || dbz <= 5) return 0;
  return Math.min(120, Math.pow(Math.pow(10, dbz / 10) / 200, 1 / 1.6));
}

/** Bilinear sample of a rain-rate array on the grid of `g` at local (x, y) metres; 0 outside by more than a cell. */
function sample(R: Float32Array, g: EchoGrid, x: number, y: number): number {
  const fx = (x - g.x0) / g.dx - 0.5; // cell centres at integer positions
  const fy = (y - g.y0) / g.dx - 0.5;
  if (fx < -1 || fy < -1 || fx > g.nx || fy > g.ny) return 0;
  const i = Math.max(0, Math.min(g.nx - 2, Math.floor(fx)));
  const j = Math.max(0, Math.min(g.ny - 2, Math.floor(fy)));
  const ax = Math.max(0, Math.min(1, fx - i));
  const ay = Math.max(0, Math.min(1, fy - j));
  const o = j * g.nx + i;
  return (R[o] * (1 - ax) + R[o + 1] * ax) * (1 - ay) + (R[o + g.nx] * (1 - ax) + R[o + g.nx + 1] * ax) * ay;
}

export interface EchoSeries {
  /** The grids sorted by time (all on the same geometry), their rain rates and the motion (m/s east, north). */
  grids: EchoGrid[];
  rates: Float32Array[];
  motion: [number, number];
}

export function makeEchoSeries(grids: EchoGrid[], motion: [number, number] = [0, 0]): EchoSeries | null {
  if (!grids.length) return null;
  const sorted = grids.slice().sort((a, b) => a.t - b.t);
  const rates = sorted.map((g) => Float32Array.from(g.dbz, (v) => rainRate(v)));
  return { grids: sorted, rates, motion };
}

/**
 * Rain rate (mm/h) on the grid of the series at data time `t`, written into `out` (length nx * ny; allocated when absent).
 * Before the first and after the last volume the nearest one is carried along the motion.
 */
export function echoRainAt(s: EchoSeries, t: number, out?: Float32Array): Float32Array {
  const g0 = s.grids[0];
  const res = out && out.length === g0.nx * g0.ny ? out : new Float32Array(g0.nx * g0.ny);
  const n = s.grids.length;
  let k = 0;
  while (k < n - 2 && t > s.grids[k + 1].t) k++;
  const a = s.grids[Math.min(k, n - 1)];
  const b = s.grids[Math.min(k + 1, n - 1)];
  const ra = s.rates[Math.min(k, n - 1)];
  const rb = s.rates[Math.min(k + 1, n - 1)];
  const span = b.t - a.t;
  const w = n < 2 || span <= 0 ? 0 : Math.max(0, Math.min(1, (t - a.t) / span));
  const [u, v] = s.motion;
  const da = t - a.t; // time since the earlier volume
  const db = t - b.t; // (negative) time to the later one
  for (let j = 0; j < g0.ny; j++) {
    const y = g0.y0 + (j + 0.5) * g0.dx;
    for (let i = 0; i < g0.nx; i++) {
      const x = g0.x0 + (i + 0.5) * g0.dx;
      const A = sample(ra, a, x - u * da, y - v * da);
      const B = sample(rb, b, x - u * db, y - v * db);
      res[j * g0.nx + i] = (1 - w) * A + w * B;
    }
  }
  return res;
}
