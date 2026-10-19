/**
 * Terrain for the 3D "Realistic ground": a height map (metres above sea level) decoded from
 * Terrarium elevation tiles and registered through the same azimuthal-equidistant frame as the radar
 * data and the ground image (groundMap.ts), so hills sit where the echoes are.
 *
 * Source: Terrain Tiles on AWS (Terrarium PNG, no key). Each pixel stores the elevation in its RGB
 * bytes: metres = R * 256 + G + B / 256 - 32768. Sea level is the floor (bathymetry is clamped to 0).
 * The pure parts (decoding, tile maths, resampling) have unit tests (terrain.test.mjs); only
 * `buildTerrain` touches the network (cached by tileLoader.ts).
 */
import { dataSpaceToLatLon, latLonToTile } from './geoProjection.ts';
import { fetchTileBitmap, mapLimit } from './tileLoader.ts';

export const TERRARIUM_URL = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png';
export const TERRAIN_ATTRIBUTION =
  'Elevation: Terrain Tiles (Mapzen / Tilezen; USGS 3DEP and NED, SRTM, GMTED2010 and others)';
export const TERRAIN_ATTRIBUTION_URL = 'https://github.com/tilezen/joerd/blob/master/docs/attribution.md';

const TILE = 256;

/** Elevation in metres of one Terrarium pixel. */
export function decodeTerrarium(r: number, g: number, b: number): number {
  return r * 256 + g + b / 256 - 32768;
}

/** Decode a 256 x 256 RGBA Terrarium tile into metres (row-major, row 0 = north edge of the tile). */
export function decodeTerrariumTile(rgba: ArrayLike<number>, out: Float32Array): void {
  for (let i = 0; i < TILE * TILE; i++) out[i] = decodeTerrarium(rgba[i * 4], rgba[i * 4 + 1], rgba[i * 4 + 2]);
}

/** Ground size of one pixel of a Web Mercator tile at a latitude and zoom, metres. */
export function metersPerPixel(latDeg: number, zoom: number): number {
  return (156543.03392 * Math.cos((latDeg * Math.PI) / 180)) / 2 ** zoom;
}

export function tileUrl(template: string, z: number, x: number, y: number): string {
  return template.replace('{z}', String(z)).replace('{x}', String(x)).replace('{y}', String(y));
}

export interface TileSpan {
  tx0: number;
  tx1: number;
  ty0: number;
  ty1: number;
}

/**
 * Tiles covering the square [-halfM, halfM] around (lat, lon) in the azimuthal-equidistant frame
 * (the whole boundary is sampled: the square is curved in tile space).
 */
export function tileSpan(lat: number, lon: number, halfM: number, zoom: number): TileSpan {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  const N = 24;
  for (let i = 0; i <= N; i++) {
    const f = -halfM + (2 * halfM * i) / N;
    for (const [px, py] of [[f, -halfM], [f, halfM], [-halfM, f], [halfM, f]] as const) {
      const [la, lo] = dataSpaceToLatLon(px, py, lat, lon);
      const t = latLonToTile(la, lo, zoom);
      x0 = Math.min(x0, t.tx); x1 = Math.max(x1, t.tx);
      y0 = Math.min(y0, t.ty); y1 = Math.max(y1, t.ty);
    }
  }
  const n = 2 ** zoom;
  return {
    tx0: Math.max(0, Math.floor(x0)), tx1: Math.min(n - 1, Math.floor(x1)),
    ty0: Math.max(0, Math.floor(y0)), ty1: Math.min(n - 1, Math.floor(y1)),
  };
}

export function tileCount(s: TileSpan): number {
  return (s.tx1 - s.tx0 + 1) * (s.ty1 - s.ty0 + 1);
}

/**
 * Zoom for about one tile pixel per output texel, lowered until the tile count fits `maxTiles`.
 */
export function pickZoom(halfKm: number, size: number, lat: number, lon: number, minZoom: number, maxZoom: number, maxTiles: number): { zoom: number; span: TileSpan } {
  const texelM = (2 * halfKm * 1000) / size;
  let z = Math.floor(Math.log2((156543.03392 * Math.cos((lat * Math.PI) / 180)) / texelM));
  z = Math.max(minZoom, Math.min(maxZoom, z));
  let span = tileSpan(lat, lon, halfKm * 1000, z);
  while (tileCount(span) > maxTiles && z > minZoom) span = tileSpan(lat, lon, halfKm * 1000, --z);
  return { zoom: z, span };
}

/**
 * Resample a stitched tile mosaic of elevations (metres, `sw` x `sh`, origin = tile (tx0, ty0) at
 * `zoom`) onto the square [-half, half] km around (lat, lon): `size` x `size` kilometres above sea
 * level, row 0 = north. NaN (a missing tile) and sea (negative) become 0.
 */
export function resampleHeights(stitched: Float32Array, sw: number, sh: number, tx0: number, ty0: number, zoom: number, lat: number, lon: number, halfKm: number, size: number): Float32Array {
  const R = halfKm * 1000;
  const out = new Float32Array(size * size);
  for (let j = 0; j < size; j++) {
    const ey = R - ((j + 0.5) / size) * 2 * R;
    for (let i = 0; i < size; i++) {
      const ex = -R + ((i + 0.5) / size) * 2 * R;
      const [la, lo] = dataSpaceToLatLon(ex, ey, lat, lon);
      const t = latLonToTile(la, lo, zoom);
      const px = Math.max(0, Math.min(sw - 1.001, (t.tx - tx0) * TILE - 0.5));
      const py = Math.max(0, Math.min(sh - 1.001, (t.ty - ty0) * TILE - 0.5));
      const x0 = px | 0, y0 = py | 0;
      const fx = px - x0, fy = py - y0;
      const a = stitched[y0 * sw + x0], b = stitched[y0 * sw + x0 + 1];
      const c = stitched[(y0 + 1) * sw + x0], d = stitched[(y0 + 1) * sw + x0 + 1];
      const v = (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy;
      out[j * size + i] = Number.isFinite(v) ? Math.max(0, v) / 1000 : 0;
    }
  }
  return out;
}

export interface TerrainRequest {
  /** Centre of the square (the scene's ground map uses the same). */
  lat: number;
  lon: number;
  halfKm: number;
  /** Texels per side. */
  size: number;
  /** Terrarium URL template ({z} {x} {y}); default: TERRARIUM_URL. */
  url?: string;
  maxTiles?: number;
}

export interface TerrainResult {
  size: number;
  /** Elevation above sea level, km, row 0 = north. */
  heightsKm: Float32Array;
  minKm: number;
  maxKm: number;
  zoom: number;
  missing: number;
}

/** Fetch, decode and resample the terrain; null when nothing could be loaded or it was cancelled. */
export async function buildTerrain(req: TerrainRequest, isCancelled: () => boolean = () => false): Promise<TerrainResult | null> {
  const { lat, lon, halfKm, size } = req;
  const template = req.url ?? TERRARIUM_URL;
  const { zoom, span } = pickZoom(halfKm, size, lat, lon, 3, 12, req.maxTiles ?? 100);
  const nx = span.tx1 - span.tx0 + 1, ny = span.ty1 - span.ty0 + 1;
  const sw = nx * TILE, sh = ny * TILE;
  const stitched = new Float32Array(sw * sh).fill(NaN);
  const cx = (span.tx0 + span.tx1) / 2, cy = (span.ty0 + span.ty1) / 2;
  const jobs: { x: number; y: number }[] = [];
  for (let y = span.ty0; y <= span.ty1; y++) for (let x = span.tx0; x <= span.tx1; x++) jobs.push({ x, y });
  jobs.sort((a, b) => Math.hypot(a.x - cx, a.y - cy) - Math.hypot(b.x - cx, b.y - cy));
  const cv = document.createElement('canvas');
  cv.width = TILE;
  cv.height = TILE;
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  if (!ctx) return null;
  const tileM = new Float32Array(TILE * TILE);
  let loaded = 0, missing = 0;
  await mapLimit(jobs, 4, async (j) => {
    const bmp = await fetchTileBitmap(tileUrl(template, zoom, j.x, j.y));
    if (!bmp) { missing++; return; }
    ctx.clearRect(0, 0, TILE, TILE);
    ctx.drawImage(bmp, 0, 0, TILE, TILE);
    bmp.close?.();
    let px: ImageData;
    try { px = ctx.getImageData(0, 0, TILE, TILE); } catch { missing++; return; }
    decodeTerrariumTile(px.data, tileM);
    const ox = (j.x - span.tx0) * TILE, oy = (j.y - span.ty0) * TILE;
    for (let r = 0; r < TILE; r++) stitched.set(tileM.subarray(r * TILE, (r + 1) * TILE), (oy + r) * sw + ox);
    loaded++;
  }, isCancelled);
  if (isCancelled() || loaded === 0) return null;
  const heightsKm = resampleHeights(stitched, sw, sh, span.tx0, span.ty0, zoom, lat, lon, halfKm, size);
  let mn = Infinity, mx = -Infinity;
  for (let i = 0; i < heightsKm.length; i++) {
    const v = heightsKm[i];
    if (v < mn) mn = v;
    if (v > mx) mx = v;
  }
  return { size, heightsKm, minKm: mn, maxKm: mx, zoom, missing };
}
