/**
 * Multi-Doppler wind helpers: result types, colour scales, field access and the canvas drawing of
 * the plan view (speed raster + wind arrows on the 2-D map) and of the vertical cross-section.
 *
 * The sidecar sends one float32 block of shape [n_fields, nz, ny, nx] (C order) next to a JSON
 * header; `fieldSlice` returns zero-copy views into it.
 */

export type WindFieldKey =
  | 'u'
  | 'v'
  | 'w'
  | 'speed'
  | 'divergence'
  | 'vorticity'
  | 'uncertainty'
  | 'crossing_angle'
  | 'n_radars'
  | 'dbz';

export interface WindFieldInfo {
  name: WindFieldKey;
  units: string;
  long_name: string;
}

export interface WindFieldStats {
  min: number | null;
  max: number | null;
  mean: number | null;
  p2: number | null;
  p98: number | null;
}

export interface WindRadar {
  file_id: string | null;
  name: string;
  latitude: number;
  longitude: number;
  altitude_m: number;
  volume_time: string | null;
}

export interface WindGrid {
  nx: number;
  ny: number;
  nz: number;
  x: [number, number];
  y: [number, number];
  dx: number;
  dy: number;
  z: number[];
  origin_lat: number;
  origin_lon: number;
  west: number;
  south: number;
  east: number;
  north: number;
}

export interface WindBackgroundInfo {
  mode: 'none' | 'vvp' | 'uniform' | 'manual';
  u?: number;
  v?: number;
  profile?: { height_m: number; u: number; v: number }[];
}

export interface WindHeader {
  type: 'multidoppler_result';
  request_id: string;
  wind_id: string;
  fields: WindFieldInfo[];
  shape: [number, number, number, number];
  grid: WindGrid;
  radars: WindRadar[];
  warnings: string[];
  method: string;
  boundary: string;
  coverage_pct: number;
  dual_coverage_pct: number;
  w_valid_pct: number;
  valid_cells: number;
  total_cells: number;
  crossing_angle: {
    min_required: number;
    min: number | null;
    mean: number | null;
    median: number | null;
    max: number | null;
  };
  field_stats: Record<string, WindFieldStats>;
  has_uncertainty: boolean;
  background: WindBackgroundInfo;
  fall_speed_correction: boolean;
  freezing_level: string | number;
  min_crossing_angle: number;
  z_min_auto: boolean;
  analysis_time: string | null;
  radarx: string;
  timing: Record<string, number>;
  elapsed_ms: number;
}

/** A decoded result: header, the float32 block and the lon/lat of the grid nodes. */
export interface WindResult extends WindHeader {
  data: Float32Array;
  /** Longitude / latitude of the (nx + 1) x (ny + 1) cell corners, row-major (y, x). */
  nodeLon: Float64Array;
  nodeLat: Float64Array;
  fieldIndex: Record<string, number>;
}

export interface WindLayer {
  res: WindResult;
  level: number;
  field: WindFieldKey;
  vmin: number;
  vmax: number;
  kind: ScaleKind;
  showRaster: boolean;
  showVectors: boolean;
  hidePpi: boolean;
  opacity: number;
  visible: boolean;
  minCrossing: number;
  maxUncertainty: number | null;
  radars: (WindRadar & { color: string })[];
}

// ── Colour scales ──────────────────────────────────────────────────────────

export type ScaleKind = 'sequential' | 'diverging' | 'magma';

const ANCHORS: Record<ScaleKind, [number, number, number][]> = {
  sequential: [
    [68, 1, 84],
    [59, 82, 139],
    [33, 145, 140],
    [94, 201, 98],
    [253, 231, 37],
  ],
  diverging: [
    [33, 102, 172],
    [103, 169, 207],
    [247, 247, 247],
    [239, 138, 98],
    [178, 24, 43],
  ],
  magma: [
    [0, 0, 4],
    [81, 18, 124],
    [183, 55, 121],
    [252, 137, 97],
    [252, 253, 191],
  ],
};

const lutCache = new Map<ScaleKind, string[]>();

/** 256 CSS colour strings for a scale (cached). */
export function scaleLut(kind: ScaleKind): string[] {
  const hit = lutCache.get(kind);
  if (hit) return hit;
  const a = ANCHORS[kind];
  const out: string[] = [];
  for (let i = 0; i < 256; i++) {
    const t = (i / 255) * (a.length - 1);
    const k = Math.min(a.length - 2, Math.floor(t));
    const f = t - k;
    const c = [0, 1, 2].map((j) => Math.round(a[k][j] + (a[k + 1][j] - a[k][j]) * f));
    out.push(`rgb(${c[0]},${c[1]},${c[2]})`);
  }
  lutCache.set(kind, out);
  return out;
}

/** CSS gradient string of a scale (for legends). */
export function scaleGradient(kind: ScaleKind): string {
  return `linear-gradient(90deg, ${ANCHORS[kind].map((c) => `rgb(${c[0]},${c[1]},${c[2]})`).join(', ')})`;
}

export interface FieldStyle {
  label: string;
  kind: ScaleKind;
  /** diverging fields are centred on zero */
  symmetric: boolean;
  /** lower bound for sequential fields (null: data minimum) */
  floor: number | null;
}

export const FIELD_STYLES: Record<WindFieldKey, FieldStyle> = {
  speed: { label: 'Wind speed', kind: 'sequential', symmetric: false, floor: 0 },
  u: { label: 'u (east)', kind: 'diverging', symmetric: true, floor: null },
  v: { label: 'v (north)', kind: 'diverging', symmetric: true, floor: null },
  w: { label: 'w (vertical)', kind: 'diverging', symmetric: true, floor: null },
  divergence: { label: 'Divergence', kind: 'diverging', symmetric: true, floor: null },
  vorticity: { label: 'Vorticity', kind: 'diverging', symmetric: true, floor: null },
  uncertainty: { label: 'Uncertainty', kind: 'magma', symmetric: false, floor: 0 },
  crossing_angle: { label: 'Crossing angle', kind: 'sequential', symmetric: false, floor: 0 },
  n_radars: { label: 'Radars', kind: 'sequential', symmetric: false, floor: 0 },
  dbz: { label: 'Reflectivity', kind: 'sequential', symmetric: false, floor: null },
};

/** Default colour range of a field from the header statistics. */
export function defaultRange(res: WindResult, field: WindFieldKey): [number, number] {
  const st = res.field_stats[field];
  const style = FIELD_STYLES[field];
  if (!st || st.p98 === null || st.p2 === null) return [0, 1];
  if (style.symmetric) {
    const m = Math.max(Math.abs(st.p2), Math.abs(st.p98), 1e-6);
    return [-m, m];
  }
  if (field === 'crossing_angle') return [0, 90];
  const lo = style.floor ?? st.p2;
  const hi = Math.max(st.p98, lo + 1e-6);
  return [lo, hi];
}

// ── Field access ───────────────────────────────────────────────────────────

/** Zero-copy view of one level (ny * nx) of a field. */
export function fieldSlice(res: WindResult, name: WindFieldKey, level: number): Float32Array {
  const fi = res.fieldIndex[name];
  const { nx, ny, nz } = res.grid;
  const size = nx * ny;
  const off = (fi * nz + level) * size;
  return res.data.subarray(off, off + size);
}

const DEG = Math.PI / 180;
const EARTH_R = 6371008.8;

/** Destination point (lat, lon in degrees) at `dist` metres and compass `bearingDeg` from a point. */
export function destination(lat: number, lon: number, dist: number, bearingDeg: number): [number, number] {
  const d = dist / EARTH_R;
  const b = bearingDeg * DEG;
  const la1 = lat * DEG;
  const lo1 = lon * DEG;
  const la2 = Math.asin(Math.sin(la1) * Math.cos(d) + Math.cos(la1) * Math.sin(d) * Math.cos(b));
  const lo2 =
    lo1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(la1), Math.cos(d) - Math.sin(la1) * Math.sin(la2));
  return [la2 / DEG, ((((lo2 / DEG + 540) % 360) + 360) % 360) - 180];
}

/** Lon/lat of the cell corners of the analysis grid (azimuthal equidistant about the origin). */
export function gridNodes(g: WindGrid): { lon: Float64Array; lat: Float64Array } {
  const nxn = g.nx + 1;
  const nyn = g.ny + 1;
  const lon = new Float64Array(nxn * nyn);
  const lat = new Float64Array(nxn * nyn);
  for (let j = 0; j < nyn; j++) {
    const y = g.y[0] + (j - 0.5) * g.dy;
    for (let i = 0; i < nxn; i++) {
      const x = g.x[0] + (i - 0.5) * g.dx;
      const dist = Math.hypot(x, y);
      const bearing = (Math.atan2(x, y) / DEG + 360) % 360;
      const [la, lo] = dist < 1e-6 ? [g.origin_lat, g.origin_lon] : destination(g.origin_lat, g.origin_lon, dist, bearing);
      lon[j * nxn + i] = lo;
      lat[j * nxn + i] = la;
    }
  }
  return { lon, lat };
}

/** Decode a header + float32 block into a result (validates the sizes). */
export function decodeResult(header: WindHeader, data: Float32Array): WindResult | null {
  const [nf, nz, ny, nx] = header.shape;
  if (data.length !== nf * nz * ny * nx || header.fields.length !== nf) return null;
  const nodes = gridNodes(header.grid);
  const fieldIndex: Record<string, number> = {};
  header.fields.forEach((f, i) => (fieldIndex[f.name] = i));
  return { ...header, data, nodeLon: nodes.lon, nodeLat: nodes.lat, fieldIndex };
}

/** Index of the level closest to a height (m). */
export function nearestLevel(z: number[], height: number): number {
  let best = 0;
  for (let k = 1; k < z.length; k++) if (Math.abs(z[k] - height) < Math.abs(z[best] - height)) best = k;
  return best;
}

// ── Plan-view drawing ──────────────────────────────────────────────────────

type Project = (lat: number, lon: number) => [number, number];

let scratch: HTMLCanvasElement | null = null;

/**
 * Draw the wind layer: the chosen field as a colour raster (cells projected through `project`),
 * wind arrows and the radar sites. `c` has the DPR transform set (CSS pixel units).
 */
export function drawWindLayer(
  c: CanvasRenderingContext2D,
  layer: WindLayer,
  project: Project,
  dpr: number,
  cw: number,
  ch: number,
): void {
  const { res } = layer;
  const { nx, ny } = res.grid;
  const k = Math.max(0, Math.min(res.grid.nz - 1, layer.level));
  const nxn = nx + 1;
  const px = new Float32Array(nxn * (ny + 1));
  const py = new Float32Array(nxn * (ny + 1));
  for (let n = 0; n < px.length; n++) {
    const [x, y] = project(res.nodeLat[n], res.nodeLon[n]);
    px[n] = x;
    py[n] = y;
  }
  const val = fieldSlice(res, layer.field, k);
  const cross = fieldSlice(res, 'crossing_angle', k);
  const unc = fieldSlice(res, 'uncertainty', k);
  const ok = (idx: number): boolean => {
    if (!Number.isFinite(val[idx])) return false;
    if (layer.minCrossing > 0 && !(cross[idx] >= layer.minCrossing)) return false;
    if (layer.maxUncertainty !== null && Number.isFinite(unc[idx]) && unc[idx] > layer.maxUncertainty) return false;
    return true;
  };

  if (layer.showRaster) {
    const lut = scaleLut(layer.kind);
    const span = layer.vmax - layer.vmin || 1;
    if (!scratch) scratch = document.createElement('canvas');
    if (scratch.width !== c.canvas.width || scratch.height !== c.canvas.height) {
      scratch.width = c.canvas.width;
      scratch.height = c.canvas.height;
    }
    const b = scratch.getContext('2d');
    if (b) {
      b.setTransform(1, 0, 0, 1, 0, 0);
      b.clearRect(0, 0, scratch.width, scratch.height);
      b.setTransform(dpr, 0, 0, dpr, 0, 0);
      b.lineWidth = 0.8;
      b.lineJoin = 'round';
      for (let j = 0; j < ny; j++) {
        for (let i = 0; i < nx; i++) {
          const idx = j * nx + i;
          if (!ok(idx)) continue;
          const a = j * nxn + i;
          const bb = a + 1;
          const d = a + nxn;
          const cc = d + 1;
          const minX = Math.min(px[a], px[bb], px[cc], px[d]);
          const maxX = Math.max(px[a], px[bb], px[cc], px[d]);
          const minY = Math.min(py[a], py[bb], py[cc], py[d]);
          const maxY = Math.max(py[a], py[bb], py[cc], py[d]);
          if (maxX < 0 || minX > cw || maxY < 0 || minY > ch) continue;
          const t = Math.max(0, Math.min(1, (val[idx] - layer.vmin) / span));
          const col = lut[Math.round(t * 255)];
          b.fillStyle = col;
          b.strokeStyle = col;
          b.beginPath();
          b.moveTo(px[a], py[a]);
          b.lineTo(px[bb], py[bb]);
          b.lineTo(px[cc], py[cc]);
          b.lineTo(px[d], py[d]);
          b.closePath();
          b.fill();
          b.stroke();
        }
      }
      c.save();
      c.setTransform(1, 0, 0, 1, 0, 0);
      c.globalAlpha = layer.opacity;
      c.drawImage(scratch, 0, 0);
      c.restore();
    }
  }

  if (layer.showVectors) drawArrows(c, layer, project, px, py, ok, k, cw, ch);
  drawWindSites(c, layer, project);
}

function drawArrows(
  c: CanvasRenderingContext2D,
  layer: WindLayer,
  project: Project,
  px: Float32Array,
  py: Float32Array,
  ok: (idx: number) => boolean,
  k: number,
  cw: number,
  ch: number,
): void {
  const { res } = layer;
  const { nx, ny } = res.grid;
  const nxn = nx + 1;
  const u = fieldSlice(res, 'u', k);
  const v = fieldSlice(res, 'v', k);
  const cross = fieldSlice(res, 'crossing_angle', k);
  // Cell size on screen decides the arrow spacing (about 22 px apart).
  const mid = Math.floor(ny / 2) * nxn + Math.floor(nx / 2);
  const cell = Math.max(0.5, Math.hypot(px[mid + 1] - px[mid], py[mid + 1] - py[mid]));
  const stride = Math.max(1, Math.ceil(22 / cell));
  c.save();
  c.lineCap = 'round';
  c.lineJoin = 'round';
  for (let j = Math.floor(stride / 2); j < ny; j += stride) {
    for (let i = Math.floor(stride / 2); i < nx; i += stride) {
      const idx = j * nx + i;
      const uu = u[idx];
      const vv = v[idx];
      if (!Number.isFinite(uu) || !Number.isFinite(vv)) continue;
      if (layer.minCrossing > 0 && !(cross[idx] >= layer.minCrossing)) continue;
      if (!ok(idx) && layer.showRaster) continue;
      const sx = 0.25 * (px[j * nxn + i] + px[j * nxn + i + 1] + px[(j + 1) * nxn + i] + px[(j + 1) * nxn + i + 1]);
      const sy = 0.25 * (py[j * nxn + i] + py[j * nxn + i + 1] + py[(j + 1) * nxn + i] + py[(j + 1) * nxn + i + 1]);
      if (sx < -20 || sx > cw + 20 || sy < -20 || sy > ch + 20) continue;
      const speed = Math.hypot(uu, vv);
      if (speed < 0.3) continue;
      // Screen direction of the wind: project a point a little downwind of the cell centre.
      const lat = 0.25 * (res.nodeLat[j * nxn + i] + res.nodeLat[j * nxn + i + 1] + res.nodeLat[(j + 1) * nxn + i] + res.nodeLat[(j + 1) * nxn + i + 1]);
      const lon = 0.25 * (res.nodeLon[j * nxn + i] + res.nodeLon[j * nxn + i + 1] + res.nodeLon[(j + 1) * nxn + i] + res.nodeLon[(j + 1) * nxn + i + 1]);
      const bearing = (Math.atan2(uu, vv) / DEG + 360) % 360;
      const [la2, lo2] = destination(lat, lon, 500, bearing);
      const [ex, ey] = project(la2, lo2);
      const dl = Math.hypot(ex - sx, ey - sy);
      if (dl < 1e-6) continue;
      const dirX = (ex - sx) / dl;
      const dirY = (ey - sy) / dl;
      const len = Math.min(30, 4 + speed * 1.1);
      const x0 = sx - (dirX * len) / 2;
      const y0 = sy - (dirY * len) / 2;
      const x1 = sx + (dirX * len) / 2;
      const y1 = sy + (dirY * len) / 2;
      const head = Math.min(7, len * 0.45);
      const hx = (a: number) => [x1 - head * (dirX * Math.cos(a) - dirY * Math.sin(a)), y1 - head * (dirY * Math.cos(a) + dirX * Math.sin(a))];
      const [lx, ly] = hx(0.45);
      const [rx, ry] = hx(-0.45);
      for (const pass of [0, 1]) {
        c.beginPath();
        c.moveTo(x0, y0);
        c.lineTo(x1, y1);
        c.moveTo(lx, ly);
        c.lineTo(x1, y1);
        c.lineTo(rx, ry);
        c.lineWidth = pass === 0 ? 3.4 : 1.5;
        c.strokeStyle = pass === 0 ? 'rgba(10,12,24,0.85)' : '#f6f7ff';
        c.stroke();
      }
    }
  }
  c.restore();
}

function drawWindSites(c: CanvasRenderingContext2D, layer: WindLayer, project: Project): void {
  c.save();
  c.lineJoin = 'round';
  for (const r of layer.radars) {
    const [x, y] = project(r.latitude, r.longitude);
    c.beginPath();
    c.arc(x, y, 5, 0, Math.PI * 2);
    c.fillStyle = r.color;
    c.fill();
    c.lineWidth = 1.5;
    c.strokeStyle = 'rgba(8, 10, 20, 0.95)';
    c.stroke();
    c.font = '600 11px Inter, system-ui, sans-serif';
    c.textBaseline = 'middle';
    c.lineWidth = 3;
    c.strokeStyle = 'rgba(8, 10, 20, 0.85)';
    c.strokeText(r.name, x + 9, y);
    c.fillStyle = '#f2f4ff';
    c.fillText(r.name, x + 9, y);
  }
  c.restore();
}

// ── Vertical cross-section ─────────────────────────────────────────────────

export interface SectionOptions {
  field: WindFieldKey;
  /** 'x': west-east slice at row `index` (fixed y); 'y': south-north slice at column `index`. */
  axis: 'x' | 'y';
  index: number;
  vmin: number;
  vmax: number;
  minCrossing: number;
  maxUncertainty: number | null;
}

/** Paint a vertical slice (distance along the slice vs height) of a field onto a canvas. */
export function drawSection(canvas: HTMLCanvasElement, res: WindResult, opt: SectionOptions, dark = true): void {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (w < 20 || h < 20) return;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const c = canvas.getContext('2d');
  if (!c) return;
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  const { nx, ny, nz } = res.grid;
  const along = opt.axis === 'x' ? nx : ny;
  const step = opt.axis === 'x' ? res.grid.dx : res.grid.dy;
  const start = opt.axis === 'x' ? res.grid.x[0] : res.grid.y[0];
  const zs = res.grid.z;
  const dz = nz > 1 ? zs[1] - zs[0] : 500;
  const left = 34;
  const bottom = 16;
  const top = 4;
  const right = 4;
  const pw = w - left - right;
  const ph = h - top - bottom;
  const zlo = zs[0] - dz / 2;
  const zhi = zs[nz - 1] + dz / 2;
  const lut = scaleLut(FIELD_STYLES[opt.field].kind);
  const span = opt.vmax - opt.vmin || 1;
  const fg = dark ? '#9da2c2' : '#444';
  for (let k = 0; k < nz; k++) {
    const val = fieldSlice(res, opt.field, k);
    const cross = fieldSlice(res, 'crossing_angle', k);
    const unc = fieldSlice(res, 'uncertainty', k);
    for (let a = 0; a < along; a++) {
      const idx = opt.axis === 'x' ? opt.index * nx + a : a * nx + opt.index;
      const v = val[idx];
      if (!Number.isFinite(v)) continue;
      if (opt.minCrossing > 0 && !(cross[idx] >= opt.minCrossing)) continue;
      if (opt.maxUncertainty !== null && Number.isFinite(unc[idx]) && unc[idx] > opt.maxUncertainty) continue;
      const t = Math.max(0, Math.min(1, (v - opt.vmin) / span));
      c.fillStyle = lut[Math.round(t * 255)];
      const x0 = left + (a / along) * pw;
      const x1 = left + ((a + 1) / along) * pw;
      const y1 = top + ph * (1 - (zs[k] - dz / 2 - zlo) / (zhi - zlo));
      const y0 = top + ph * (1 - (zs[k] + dz / 2 - zlo) / (zhi - zlo));
      c.fillRect(x0, y0, x1 - x0 + 0.6, y1 - y0 + 0.6);
    }
  }
  c.strokeStyle = fg;
  c.lineWidth = 1;
  c.strokeRect(left + 0.5, top + 0.5, pw, ph);
  c.fillStyle = fg;
  c.font = '10px Inter, system-ui, sans-serif';
  c.textBaseline = 'middle';
  c.textAlign = 'right';
  const nTicks = 4;
  for (let t = 0; t <= nTicks; t++) {
    const z = zlo + ((zhi - zlo) * t) / nTicks;
    const y = top + ph * (1 - t / nTicks);
    c.fillText((z / 1000).toFixed(1), left - 4, y);
  }
  c.textAlign = 'center';
  c.textBaseline = 'top';
  for (let t = 0; t <= 2; t++) {
    const km = (start - step / 2 + (step * along * t) / 2) / 1000;
    c.fillText(km.toFixed(0), left + (pw * t) / 2, top + ph + 3);
  }
}
