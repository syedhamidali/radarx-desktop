/**
 * Cartesian 3D volumes for the 3D view: the merged mosaic field and the multi-Doppler wind grids.
 *
 * Pure helpers (no DOM, no GL; unit-tested with node:test): decoding the sidecar messages
 * (`mosaic_volume`, `wind_volume`), 8-bit quantisation with the same packing as the polar volume
 * (code 0 = no data, 1..255 over [vmin, vmax]), the box the volume occupies in the scene, the
 * texture-budget downsampling and the packing into the RG8 texture the shader samples
 * (R = code, G = 255 where there is data, so hardware trilinear filtering can renormalise at
 * the edges of the echo).
 *
 * Scene units are km. x = east, y = north of the volume's AEQD origin; z is the height above sea
 * level (unexaggerated: the viewer multiplies by the vertical exaggeration).
 */

// ─── Types ──────────────────────────────────────────────────────────────────

export interface SceneSite {
  name: string;
  /** km east / north of the scene origin. */
  x: number;
  y: number;
  /** km above sea level. */
  alt: number;
}

export interface SceneBox {
  xMin: number;
  xMax: number;
  yMin: number;
  yMax: number;
  zMin: number;
  zMax: number;
}

/** A scalar field on a regular grid, 8-bit quantised. */
export interface CartVolume {
  id: string;
  kind: 'mosaic' | 'wind';
  label: string;
  variable: string;
  units: string;
  vmin: number;
  vmax: number;
  cmap: string;
  nx: number;
  ny: number;
  nz: number;
  /** Centre of the first cell (km) and cell size (km). */
  x0: number;
  y0: number;
  dx: number;
  dy: number;
  /** Level heights (km ASL), ascending. */
  z: Float64Array;
  /** (nz, ny, nx), x fastest: 0 = no data, 1..255 -> vmin + (code - 1) / 254 * (vmax - vmin). */
  data: Uint8Array;
  originLat: number;
  originLon: number;
  sites: SceneSite[];
  analysisTime: string | null;
}

export interface WindVolume {
  id: string;
  nx: number;
  ny: number;
  nz: number;
  x0: number;
  y0: number;
  dx: number;
  dy: number;
  z: Float64Array;
  /** One (nz, ny, nx) float32 block per field (views into the received buffer). */
  fields: Record<string, Float32Array>;
  units: Record<string, string>;
  originLat: number;
  originLon: number;
  sites: SceneSite[];
  analysisTime: string | null;
}

// ─── Decoding the sidecar messages ──────────────────────────────────────────

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

function sitesFrom(radars: any, altKey = 'altitude_m'): SceneSite[] {
  if (!Array.isArray(radars)) return [];
  const out: SceneSite[] = [];
  for (const r of radars) {
    if (!r || !isNum(r.x_m) || !isNum(r.y_m)) continue;
    out.push({ name: String(r.name ?? ''), x: r.x_m / 1000, y: r.y_m / 1000, alt: (isNum(r[altKey]) ? r[altKey] : 0) / 1000 });
  }
  return out;
}

/** `mosaic_volume` header + uint8 payload -> volume, or null when the sizes do not agree. */
export function decodeMosaicVolume(h: any, bytes: Uint8Array): CartVolume | null {
  const g = h?.grid;
  if (!g || !isNum(g.nx) || !isNum(g.ny) || !isNum(g.nz) || !Array.isArray(g.z)) return null;
  const { nx, ny, nz } = g;
  if (nx < 1 || ny < 1 || nz < 1 || bytes.length !== nx * ny * nz || g.z.length !== nz) return null;
  if (!isNum(h.vmin) || !isNum(h.vmax) || h.vmax <= h.vmin) return null;
  const cell = (isNum(g.cell_m) ? g.cell_m : 1000) / 1000;
  return {
    id: String(h.mosaic_id ?? ''),
    kind: 'mosaic',
    label: `${h.variable ?? 'Mosaic'} mosaic`,
    variable: String(h.variable ?? 'DBZH'),
    units: String(h.units ?? ''),
    vmin: h.vmin,
    vmax: h.vmax,
    cmap: String(h.cmap ?? ''),
    nx,
    ny,
    nz,
    x0: g.x0 / 1000,
    y0: g.y0 / 1000,
    dx: cell,
    dy: cell,
    z: Float64Array.from(g.z as number[], (m) => m / 1000),
    data: bytes,
    originLat: g.origin_lat,
    originLon: g.origin_lon,
    sites: sitesFrom(h.radars),
    analysisTime: h.analysis_time ?? null,
  };
}

/** `wind_volume` header + float32 payload [n_fields, nz, ny, nx] -> wind volume (null on mismatch). */
export function decodeWindVolume(h: any, data: Float32Array): WindVolume | null {
  const g = h?.grid;
  if (!g || !Array.isArray(h.fields) || !Array.isArray(h.shape) || h.shape.length !== 4) return null;
  const [nf, nz, ny, nx] = h.shape as number[];
  if (![nf, nz, ny, nx].every((n) => isNum(n) && n >= 1) || data.length !== nf * nz * ny * nx) return null;
  if (h.fields.length !== nf || !Array.isArray(g.z) || g.z.length !== nz || !Array.isArray(g.x)) return null;
  const block = nz * ny * nx;
  const fields: Record<string, Float32Array> = {};
  const units: Record<string, string> = {};
  h.fields.forEach((f: any, i: number) => {
    fields[String(f.name)] = data.subarray(i * block, (i + 1) * block);
    units[String(f.name)] = String(f.units ?? '');
  });
  if (!fields.u || !fields.v) return null;
  return {
    id: String(h.wind_id ?? ''),
    nx,
    ny,
    nz,
    x0: g.x[0] / 1000,
    y0: g.y[0] / 1000,
    dx: g.dx / 1000,
    dy: g.dy / 1000,
    z: Float64Array.from(g.z as number[], (m) => m / 1000),
    fields,
    units,
    originLat: g.origin_lat,
    originLon: g.origin_lon,
    sites: sitesFrom(h.radars),
    analysisTime: h.analysis_time ?? null,
  };
}

// ─── Quantisation (same packing as the polar volume) ────────────────────────

/** float -> 0 = no data (NaN or below `lo`), 1..255 over [lo, hi]. */
export function quantiseField(src: ArrayLike<number>, lo: number, hi: number): Uint8Array {
  const out = new Uint8Array(src.length);
  const sc = 254 / (hi - lo || 1);
  for (let i = 0; i < src.length; i++) {
    const v = src[i];
    if (!(v >= lo)) continue; // NaN and values below the range
    const t = (v - lo) * sc;
    out[i] = t >= 254 ? 255 : (t + 1.5) | 0;
  }
  return out;
}

/** The wind analysis' own reflectivity grid as a volume (same layout as a mosaic volume). */
export function windDbzVolume(w: WindVolume, lo = -20, hi = 75): CartVolume | null {
  const dbz = w.fields.dbz;
  if (!dbz) return null;
  return {
    id: `${w.id}:dbz`,
    kind: 'wind',
    label: 'Wind-grid reflectivity',
    variable: 'DBZH',
    units: w.units.dbz || 'dBZ',
    vmin: lo,
    vmax: hi,
    cmap: '',
    nx: w.nx,
    ny: w.ny,
    nz: w.nz,
    x0: w.x0,
    y0: w.y0,
    dx: w.dx,
    dy: w.dy,
    z: w.z,
    data: quantiseField(dbz, lo, hi),
    originLat: w.originLat,
    originLon: w.originLon,
    sites: w.sites,
    analysisTime: w.analysisTime,
  };
}

// ─── Geometry ───────────────────────────────────────────────────────────────

/** Spacing of the vertical levels (km); a single level gets a nominal 0.5 km. */
export function levelSpacing(z: ArrayLike<number>): number {
  return z.length > 1 ? (z[z.length - 1] - z[0]) / (z.length - 1) : 0.5;
}

/** The box a gridded field occupies: cell edges, levels padded by half a spacing. */
export function gridBox(g: { nx: number; ny: number; x0: number; y0: number; dx: number; dy: number; z: ArrayLike<number> }): SceneBox {
  const dz = levelSpacing(g.z);
  return {
    xMin: g.x0 - g.dx / 2,
    xMax: g.x0 + (g.nx - 0.5) * g.dx,
    yMin: g.y0 - g.dy / 2,
    yMax: g.y0 + (g.ny - 0.5) * g.dy,
    zMin: g.z[0] - dz / 2,
    zMax: g.z[g.z.length - 1] + dz / 2,
  };
}

export function unionBox(a: SceneBox | null, b: SceneBox | null): SceneBox | null {
  if (!a) return b;
  if (!b) return a;
  return {
    xMin: Math.min(a.xMin, b.xMin),
    xMax: Math.max(a.xMax, b.xMax),
    yMin: Math.min(a.yMin, b.yMin),
    yMax: Math.max(a.yMax, b.yMax),
    zMin: Math.min(a.zMin, b.zMin),
    zMax: Math.max(a.zMax, b.zMax),
  };
}

export function translateBox(b: SceneBox, dx: number, dy: number): SceneBox {
  return { ...b, xMin: b.xMin + dx, xMax: b.xMax + dx, yMin: b.yMin + dy, yMax: b.yMax + dy };
}

/** Half-extent (km) of the smallest origin-centred square holding the box. */
export function halfExtent(b: SceneBox): number {
  return Math.max(Math.abs(b.xMin), Math.abs(b.xMax), Math.abs(b.yMin), Math.abs(b.yMax));
}

const EARTH_R_KM = 6371.0088;

/**
 * Where (lat, lon) lies in the azimuthal-equidistant frame centred on (lat0, lon0): [east, north]
 * in km. Used to place a wind grid (its own AEQD frame) into the scene frame of the mosaic. Only
 * the translation is applied (the grid axes of two nearby AEQD frames differ by a fraction of a
 * degree for the distances involved).
 */
export function aeqdOffsetKm(lat0: number, lon0: number, lat: number, lon: number): [number, number] {
  const rad = Math.PI / 180;
  const p0 = lat0 * rad;
  const p1 = lat * rad;
  const dl = (lon - lon0) * rad;
  const cosc = Math.sin(p0) * Math.sin(p1) + Math.cos(p0) * Math.cos(p1) * Math.cos(dl);
  const c = Math.acos(Math.max(-1, Math.min(1, cosc)));
  if (c < 1e-12) return [0, 0];
  const k = c / Math.sin(c);
  return [
    EARTH_R_KM * k * Math.cos(p1) * Math.sin(dl),
    EARTH_R_KM * k * (Math.cos(p0) * Math.sin(p1) - Math.sin(p0) * Math.cos(p1) * Math.cos(dl)),
  ];
}

/** Light scene? `mode` is the UI theme setting; 'auto' follows the system preference. */
export function sceneIsLight(mode: string | undefined, systemPrefersLight: boolean): boolean {
  if (mode === 'light') return true;
  if (mode === 'dark') return false;
  return systemPrefersLight;
}

// ─── Texture packing ────────────────────────────────────────────────────────

/** Integer per-axis factors that bring the grid under `maxVoxels` and the 3D texture size limit. */
export function chooseDownsample(
  dims: [number, number, number],
  maxVoxels: number,
  maxTex: number,
): [number, number, number] {
  const f: [number, number, number] = [1, 1, 1];
  const out = () => dims.map((d, i) => Math.ceil(d / f[i]));
  for (let guard = 0; guard < 400; guard++) {
    const o = out();
    if (o[0] * o[1] * o[2] <= maxVoxels && o[0] <= maxTex && o[1] <= maxTex && o[2] <= maxTex) break;
    const axis = o[0] >= o[1] && o[0] >= o[2] ? 0 : o[1] >= o[2] ? 1 : 2;
    f[axis]++;
  }
  return f;
}

/**
 * Pack output slab `oz` of the (downsampled) RG8 volume into `out` (length onx * ony * 2): R = the
 * largest code of the source block (max-pooling keeps thin echo), G = 255 where any source voxel
 * has data. `codes` is (nz, ny, nx), x fastest.
 */
export function packRG8Slab(
  codes: Uint8Array,
  nx: number,
  ny: number,
  nz: number,
  f: [number, number, number],
  oz: number,
  out: Uint8Array,
): void {
  const [fx, fy, fz] = f;
  const onx = Math.ceil(nx / fx);
  const ony = Math.ceil(ny / fy);
  const z0 = oz * fz;
  const z1 = Math.min(nz, z0 + fz);
  const nxy = nx * ny;
  let o = 0;
  for (let oy = 0; oy < ony; oy++) {
    const y0 = oy * fy;
    const y1 = Math.min(ny, y0 + fy);
    for (let ox = 0; ox < onx; ox++) {
      const x0 = ox * fx;
      const x1 = Math.min(nx, x0 + fx);
      let m = 0;
      for (let z = z0; z < z1; z++) {
        for (let y = y0; y < y1; y++) {
          const row = z * nxy + y * nx;
          for (let x = x0; x < x1; x++) {
            const c = codes[row + x];
            if (c > m) m = c;
          }
        }
      }
      out[o++] = m;
      out[o++] = m > 0 ? 255 : 0;
    }
  }
}

// ─── Misc ───────────────────────────────────────────────────────────────────

/** A nice ground-grid step (km) for an extent. */
export function gridStepFor(extentKm: number): number {
  if (extentKm <= 80) return 10;
  if (extentKm <= 160) return 20;
  if (extentKm <= 400) return 50;
  return 100;
}

type V3 = [number, number, number];

/** Camera basis for the orbit angles used by the renderer (degrees; Z up, theta 180 = looking north). */
function orbitBasis(theta: number, phi: number, dist: number, target: V3) {
  const th = (theta * Math.PI) / 180;
  const ph = (phi * Math.PI) / 180;
  const eye: V3 = [
    target[0] + dist * Math.cos(ph) * Math.sin(th),
    target[1] + dist * Math.cos(ph) * Math.cos(th),
    target[2] + dist * Math.sin(ph),
  ];
  let f: V3 = [target[0] - eye[0], target[1] - eye[1], target[2] - eye[2]];
  const fl = Math.hypot(...f) || 1;
  f = [f[0] / fl, f[1] / fl, f[2] / fl];
  const upHint: V3 = phi < 0 ? [0, 0, -1] : [0, 0, 1];
  let r: V3 = [f[1] * upHint[2] - f[2] * upHint[1], f[2] * upHint[0] - f[0] * upHint[2], f[0] * upHint[1] - f[1] * upHint[0]];
  const rl = Math.hypot(...r) || 1;
  r = [r[0] / rl, r[1] / rl, r[2] / rl];
  const u: V3 = [r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0]];
  return { eye, f, r, u };
}

/** Screen extent (NDC) of the box corners seen from the pose; null when a corner is behind the eye. */
function projectedExtent(b: SceneBox, vexag: number, theta: number, phi: number, dist: number, target: V3, tanX: number, tanY: number) {
  const { eye, f, r, u } = orbitBasis(theta, phi, dist, target);
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const x of [b.xMin, b.xMax]) {
    for (const y of [b.yMin, b.yMax]) {
      for (const z of [0, b.zMax * vexag]) {
        const v: V3 = [x - eye[0], y - eye[1], z - eye[2]];
        const d = v[0] * f[0] + v[1] * f[1] + v[2] * f[2];
        if (d < 1e-3) return null;
        const sx = (v[0] * r[0] + v[1] * r[1] + v[2] * r[2]) / d / tanX;
        const sy = (v[0] * u[0] + v[1] * u[1] + v[2] * u[2]) / d / tanY;
        x0 = Math.min(x0, sx); x1 = Math.max(x1, sx); y0 = Math.min(y0, sy); y1 = Math.max(y1, sy);
      }
    }
  }
  return { x0, x1, y0, y1, r, u };
}

/**
 * Pose of the default camera framing a box (km, scene z exaggerated by `vexag`): the viewing
 * direction of the preset, the distance found by projecting the eight corners so that they all fit
 * (with a margin for the labels), and the target shifted to centre the result on screen.
 */
export function framePose(
  b: SceneBox,
  vexag: number,
  aspect: number,
  fovY: number,
  preset: 'oblique' | 'top' | 'side',
): { theta: number; phi: number; dist: number; target: V3 } {
  const cx = (b.xMin + b.xMax) / 2;
  const cy = (b.yMin + b.yMax) / 2;
  const zTop = Math.max(b.zMax, 1) * vexag;
  const tanY = Math.tan(fovY / 2);
  const tanX = tanY * Math.max(0.5, aspect);
  const theta = preset === 'oblique' ? 192 : 180;
  const phi = preset === 'top' ? 89 : preset === 'side' ? 3 : 34;
  let target: V3 = [cx, cy, preset === 'top' ? 0 : zTop * (preset === 'side' ? 0.4 : 0.2)];
  const span = Math.hypot(b.xMax - b.xMin, b.yMax - b.yMin, zTop);
  const MX = 0.88;
  const MY = 0.86;
  let dist = span;
  for (let pass = 0; pass < 3; pass++) {
    let lo = span * 0.05;
    let hi = span * 12;
    for (let i = 0; i < 40; i++) {
      const mid = (lo + hi) / 2;
      const e = projectedExtent(b, vexag, theta, phi, mid, target, tanX, tanY);
      const fits = !!e && Math.max(Math.abs(e.x0), Math.abs(e.x1)) <= MX && Math.max(Math.abs(e.y0), Math.abs(e.y1)) <= MY;
      if (fits) hi = mid; else lo = mid;
    }
    dist = hi;
    const e = projectedExtent(b, vexag, theta, phi, dist, target, tanX, tanY);
    if (!e) break;
    // centre the projected box: move the target along the screen axes
    const sx = ((e.x0 + e.x1) / 2) * tanX * dist;
    const sy = ((e.y0 + e.y1) / 2) * tanY * dist;
    target = [
      target[0] + e.r[0] * sx + e.u[0] * sy,
      target[1] + e.r[1] * sx + e.u[1] * sy,
      Math.max(0, target[2] + e.r[2] * sx + e.u[2] * sy),
    ];
  }
  return { theta, phi, dist, target };
}
