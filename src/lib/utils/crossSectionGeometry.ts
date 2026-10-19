/**
 * Pure geometry for the large cross-section view: tick marks, the cells of the section, plot-box
 * layout with vertical exaggeration, zoom/pan windows and radar-relative (azimuth, range) maths.
 * No DOM and no imports, so it runs under `node --test` (see crossSectionGeometry.test.mjs).
 */

const DEG2RAD = Math.PI / 180;

// ---------------------------------------------------------------------------------------------
// Ticks
// ---------------------------------------------------------------------------------------------

/** A "nice" tick step (1, 2, 2.5, 5 x 10^k) giving at most about `maxTicks` ticks over `range`. */
export function niceStep(range: number, maxTicks: number): number {
  if (!(range > 0) || !(maxTicks > 0)) return 1;
  const rough = range / maxTicks;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const norm = rough / mag;
  const nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return nice * mag;
}

/** Tick values inside [lo, hi] at a nice step (always includes multiples of the step). */
export function niceTicks(lo: number, hi: number, maxTicks: number): number[] {
  if (!(hi > lo)) return [];
  const step = niceStep(hi - lo, maxTicks);
  const out: number[] = [];
  const first = Math.ceil(lo / step - 1e-9);
  for (let i = first; i * step <= hi + step * 1e-9; i++) {
    // multiply instead of accumulating: no drift (0.30000000000000004)
    out.push(Number((i * step).toPrecision(12)));
  }
  return out;
}

/** Tick label with just enough decimals for the step. */
export function formatTick(v: number, step: number): string {
  if (!Number.isFinite(v)) return '';
  let decimals = 0;
  while (decimals < 3 && Math.abs(step * 10 ** decimals - Math.round(step * 10 ** decimals)) > 1e-9) decimals++;
  const s = v.toFixed(decimals);
  return s === '-0' ? '0' : s;
}

// ---------------------------------------------------------------------------------------------
// Cells
// ---------------------------------------------------------------------------------------------

/** The part of a cross-section result the geometry needs. */
export interface SectionLike {
  distance_km: number[];
  height_km: (number | null)[][];
  values: (number | null)[][];
  n_sweeps: number;
  ground_range_km?: number[];
}

export interface SectionCell {
  x0: number;
  x1: number;
  /** y0 < y1, km above the radar */
  y0: number;
  y1: number;
  /** Height of the beam centre (km above the radar). */
  yc: number;
  v: number;
  sweep: number;
  col: number;
}

export interface SectionCells {
  cells: SectionCell[];
  /** The same cells, per distance column (ascending in height). */
  columns: SectionCell[][];
}

/** Beam width used for the vertical size of a cell (degrees, full width at half power). */
export const BEAM_WIDTH_DEG = 1.0;

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

/** Distance extent of column i: half way to each neighbour (half a step at the two ends). */
function columnExtents(dist: number[], i: number): [number, number] {
  const n = dist.length;
  if (n === 1) return [dist[0] - 0.5, dist[0] + 0.5];
  const left = i > 0 ? (dist[i - 1] + dist[i]) / 2 : dist[0] - (dist[1] - dist[0]) / 2;
  const right = i < n - 1 ? (dist[i] + dist[i + 1]) / 2 : dist[n - 1] + (dist[n - 1] - dist[n - 2]) / 2;
  return [left, right];
}

/**
 * The coloured cells of a section.
 *
 * Horizontally a cell reaches half way to its neighbours. Vertically it is the beam: `halfBeam`
 * above and below the beam centre, or (`fill`) half way to the next sweep above / below at this
 * distance, so the elevation gaps are closed. Without a neighbour the cell is just the beam, and
 * never goes below the radar level. Sweeps of one scan that share a beam height (split cuts) just
 * stack on the same place. Values the optional `masked` predicate hides get no cell at all: they
 * are missing exactly like gates without an echo, so anything built on the cells (the smoothed
 * grid in particular) treats them as empty and never blends across them.
 */
export function buildCells(
  data: SectionLike,
  opts: { fill: boolean; masked?: ((v: number) => boolean) | null },
): SectionCells {
  const dist = data.distance_km;
  const nCol = dist.length;
  const cells: SectionCell[] = [];
  const columns: SectionCell[][] = Array.from({ length: nCol }, () => []);
  if (nCol === 0) return { cells, columns };

  for (let col = 0; col < nCol; col++) {
    const [x0, x1] = columnExtents(dist, col);
    // The beam centres at this distance, one per sweep that has a position here.
    const hs: { sweep: number; h: number }[] = [];
    for (let s = 0; s < data.n_sweeps; s++) {
      const h = data.height_km[s]?.[col];
      if (isNum(h)) hs.push({ sweep: s, h });
    }
    const ground = data.ground_range_km?.[col] ?? Math.abs(dist[col]);
    for (let k = 0; k < hs.length; k++) {
      const { sweep, h } = hs[k];
      const v = data.values[sweep]?.[col];
      if (!isNum(v) || (opts.masked && opts.masked(v))) continue;
      const slant = Math.hypot(ground, h);
      const halfBeam = Math.max(0.5 * BEAM_WIDTH_DEG * DEG2RAD * slant, 0.03);
      // nearest beams below / above that are actually somewhere else
      let below = -Infinity;
      let above = Infinity;
      for (const o of hs) {
        if (o.h < h - 1e-6 && o.h > below) below = o.h;
        else if (o.h > h + 1e-6 && o.h < above) above = o.h;
      }
      const down = opts.fill && below > -Infinity ? (h - below) / 2 : halfBeam;
      const up = opts.fill && above < Infinity ? (above - h) / 2 : halfBeam;
      const cell: SectionCell = {
        x0,
        x1,
        y0: Math.max(h - down, Math.min(0, h)),
        y1: h + up,
        yc: h,
        v,
        sweep,
        col,
      };
      cells.push(cell);
      columns[col].push(cell);
    }
    columns[col].sort((a, b) => a.y0 - b.y0);
  }
  return { cells, columns };
}

/**
 * Height (km) a section should be drawn up to: just above the highest echo at a nice number, but
 * never above `cap`: the steep tilts of a volume run to 40 km and more, where a few noisy gates
 * would squash the troposphere into a sliver. (Pick a taller range by hand when it is wanted.)
 */
export function autoHeightTop(cells: SectionCell[], fallback = 15, cap = 20): number {
  let top = -Infinity;
  for (const c of cells) if (c.y1 > top) top = c.y1;
  if (!Number.isFinite(top) || top <= 0) return fallback;
  const step = niceStep(Math.min(top, cap), 5);
  return Math.min(cap, Math.max(2, Math.ceil(top / step - 1e-9) * step));
}

/** Index of the column whose sample is nearest to distance `d`; -1 outside the section. */
export function columnAt(dist: number[], d: number): number {
  const n = dist.length;
  if (n === 0) return -1;
  const [lo] = columnExtents(dist, 0);
  const [, hi] = columnExtents(dist, n - 1);
  if (d < lo || d > hi) return -1;
  let a = 0;
  let b = n - 1;
  while (b - a > 1) {
    const m = (a + b) >> 1;
    if (dist[m] <= d) a = m;
    else b = m;
  }
  return Math.abs(dist[a] - d) <= Math.abs(dist[b] - d) ? a : b;
}

/** The cell under (distance, height), or null (no echo there / outside the section). */
export function cellAt(sc: SectionCells, dist: number[], d: number, h: number): SectionCell | null {
  const col = columnAt(dist, d);
  if (col < 0) return null;
  let best: SectionCell | null = null;
  for (const c of sc.columns[col]) {
    if (h >= c.y0 && h <= c.y1) {
      // overlapping cells (shared beam height): keep the one centred closest
      if (!best || Math.abs(h - (c.y0 + c.y1) / 2) < Math.abs(h - (best.y0 + best.y1) / 2)) best = c;
    }
  }
  return best;
}

// ---------------------------------------------------------------------------------------------
// Layout and view
// ---------------------------------------------------------------------------------------------

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface PlotLayout {
  plot: Rect;
  colorbar: Rect;
}

export const MARGINS = { left: 64, top: 62, right: 104, bottom: 54 };

/**
 * Where the plot and its colour bar go in a `width` x `height` canvas.
 *
 * `ve` is the vertical exaggeration (1 = a kilometre is as long up as it is along); `null` fills
 * the whole drawing area and lets the exaggeration follow from the window shape.
 */
export function computeLayout(
  width: number,
  height: number,
  dSpan: number,
  hSpan: number,
  ve: number | null,
  margins = MARGINS,
): PlotLayout {
  const aw = Math.max(40, width - margins.left - margins.right);
  const ah = Math.max(40, height - margins.top - margins.bottom);
  let w = aw;
  let h = ah;
  if (ve != null && dSpan > 0 && hSpan > 0) {
    const aspect = (hSpan * ve) / dSpan; // plot height per unit of plot width
    w = Math.min(aw, ah / aspect);
    h = w * aspect;
  }
  const x = margins.left + (aw - w) / 2;
  const y = margins.top + (ah - h) / 2;
  const barH = Math.min(Math.max(h * 0.9, 60), 360);
  return {
    plot: { x, y, w, h },
    colorbar: { x: x + w + 18, y: y + (h - barH) / 2, w: 14, h: barH },
  };
}

/** Vertical exaggeration of a plot box over a data extent. */
export function effectiveExaggeration(plot: Rect, dSpan: number, hSpan: number): number {
  if (!(dSpan > 0) || !(hSpan > 0) || !(plot.w > 0)) return 1;
  return plot.h / hSpan / (plot.w / dSpan);
}

export interface View {
  d0: number;
  d1: number;
  h0: number;
  h1: number;
}

export const MAX_ZOOM = 40;

function clampWindow(lo: number, hi: number, fullLo: number, fullHi: number): [number, number] {
  const full = fullHi - fullLo;
  const size = Math.min(Math.max(hi - lo, full / MAX_ZOOM), full);
  let a = lo;
  if (a < fullLo) a = fullLo;
  if (a + size > fullHi) a = fullHi - size;
  return [a, a + size];
}

/** Zoom by `factor` (>1 in) keeping the data under the cursor fraction (fx right, fy up) fixed. */
export function zoomView(view: View, full: View, fx: number, fy: number, factor: number): View {
  const dw = view.d1 - view.d0;
  const hw = view.h1 - view.h0;
  const nd = dw / factor;
  const nh = hw / factor;
  const dc = view.d0 + fx * dw;
  const hc = view.h0 + fy * hw;
  const [d0, d1] = clampWindow(dc - fx * nd, dc - fx * nd + nd, full.d0, full.d1);
  const [h0, h1] = clampWindow(hc - fy * nh, hc - fy * nh + nh, full.h0, full.h1);
  return { d0, d1, h0, h1 };
}

/** Shift the window by a fraction of its own size (positive dx moves right, dy up). */
export function panView(view: View, full: View, dx: number, dy: number): View {
  const dw = view.d1 - view.d0;
  const hw = view.h1 - view.h0;
  const [d0, d1] = clampWindow(view.d0 + dx * dw, view.d0 + dx * dw + dw, full.d0, full.d1);
  const [h0, h1] = clampWindow(view.h0 + dy * hw, view.h0 + dy * hw + hw, full.h0, full.h1);
  return { d0, d1, h0, h1 };
}

export function isFullView(view: View, full: View): boolean {
  const eps = 1e-9;
  return (
    Math.abs(view.d0 - full.d0) < eps &&
    Math.abs(view.d1 - full.d1) < eps &&
    Math.abs(view.h0 - full.h0) < eps &&
    Math.abs(view.h1 - full.h1) < eps
  );
}

// ---------------------------------------------------------------------------------------------
// Radar-relative position of the line
// ---------------------------------------------------------------------------------------------

/** Radar-centred ground position (metres east, north) of a bearing / ground range. */
export function azRangeToXY(azimuthDeg: number, rangeKm: number): [number, number] {
  const a = azimuthDeg * DEG2RAD;
  return [rangeKm * 1000 * Math.sin(a), rangeKm * 1000 * Math.cos(a)];
}

/** Bearing (0-360, clockwise from north) and ground range (km) of a radar-centred (x, y) in metres. */
export function xyToAzRange(x: number, y: number): { azimuth: number; rangeKm: number } {
  const rangeKm = Math.hypot(x, y) / 1000;
  const azimuth = rangeKm < 1e-9 ? 0 : (((Math.atan2(x, y) / DEG2RAD) % 360) + 360) % 360;
  return { azimuth, rangeKm };
}

/** Wrap a bearing into [0, 360). */
export function wrapAzimuth(az: number): number {
  return ((az % 360) + 360) % 360;
}

// ---------------------------------------------------------------------------------------------
// Text
// ---------------------------------------------------------------------------------------------

export function fmtLatLon(lat: number, lon: number, decimals = 2): string {
  return `${Math.abs(lat).toFixed(decimals)}°${lat >= 0 ? 'N' : 'S'} ${Math.abs(lon).toFixed(decimals)}°${lon >= 0 ? 'E' : 'W'}`;
}

/** A file-name stem safe on every platform. */
export function safeStem(s: string): string {
  return s.replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^_+|_+$/g, '') || 'cross_section';
}

/** Great-circle length (km) between two lat/lon points. */
export function haversineKm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const p1 = lat1 * DEG2RAD;
  const p2 = lat2 * DEG2RAD;
  const dp = p2 - p1;
  const dl = (lon2 - lon1) * DEG2RAD;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * 6371.0088 * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** How many samples to take along a line: one per ~250 m (a gate), within sensible bounds. */
export function sectionPointCount(lengthKm: number, minPoints = 200, maxPoints = 1200): number {
  if (!Number.isFinite(lengthKm) || lengthKm <= 0) return minPoints;
  return Math.min(maxPoints, Math.max(minPoints, Math.ceil(lengthKm / 0.25)));
}

export interface SectionDescription {
  variable: string;
  instrument?: string | null;
  /** Scan start (UTC) or, failing that, the file name. */
  time?: string | null;
  fileName?: string | null;
  start?: [number, number] | null;
  end?: [number, number] | null;
  lengthKm?: number | null;
  /** RHI-style cut: bearing out of the radar. */
  rhiAzimuth?: number | null;
  maxRangeKm?: number | null;
}

/** Title and subtitle of the figure: variable, instrument, time and where the line runs. */
export function describeSection(d: SectionDescription): { title: string; subtitle: string } {
  const title = [`${d.variable || 'Cross-section'} cross-section`, d.instrument || null]
    .filter(Boolean)
    .join('  \u2014  ');
  const parts: string[] = [];
  if (d.time) parts.push(`${d.time} UTC`);
  else if (d.fileName) parts.push(d.fileName);
  if (d.rhiAzimuth != null && Number.isFinite(d.rhiAzimuth)) {
    const range = d.maxRangeKm ?? d.lengthKm;
    parts.push(
      `RHI-style cut, azimuth ${wrapAzimuth(d.rhiAzimuth).toFixed(1)}\u00B0` +
        (range != null && Number.isFinite(range) ? `, 0\u2013${range.toFixed(0)} km from the radar` : ''),
    );
  } else if (d.start && d.end) {
    parts.push(
      `A ${fmtLatLon(d.start[0], d.start[1])} \u2192 B ${fmtLatLon(d.end[0], d.end[1])}` +
        (d.lengthKm != null && Number.isFinite(d.lengthKm) ? `  (${d.lengthKm.toFixed(1)} km)` : ''),
    );
  }
  return { title, subtitle: parts.join('  \u00B7  ') };
}
