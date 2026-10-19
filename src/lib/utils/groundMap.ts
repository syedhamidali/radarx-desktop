/**
 * Ground map texture for the 3D radar volume.
 *
 * Builds a square RGBA image covering [-R, R] km around the radar (east/north), from the same
 * keyless slippy-map tiles the 2D view uses (see basemaps.ts). The image is registered through the
 * radar's azimuthal-equidistant projection (the one the echoes themselves use), so ground features
 * line up with the data at any range, and restyled for a dark scene.
 *
 * Tiles are fetched with a tiny bounded loader (CORS-enabled so they can be read back), kept in a
 * small LRU, and a failed or offline fetch simply yields no map: the caller falls back to a grid.
 * All pixel work is time-sliced so the page stays responsive.
 */
import { basemapTileUrl, type BasemapProvider } from './basemaps';
import { dataSpaceToLatLon, latLonToTile } from './geoProjection';
import { yieldToMain } from './qualityGovernor';
import { fetchTileBitmap } from './tileLoader';

const TILE = 256;
const MAX_TILES = 64;
const CACHE_MAX = 160;
const CONCURRENCY = 4; // tile servers are shared: a few requests at once, nearest tiles first

const tileCache = new Map<string, ImageBitmap>();

export interface GroundMapRequest {
  lat: number;
  lon: number;
  radiusKm: number;
  /** Output size in texels (square). */
  size: number;
  provider: BasemapProvider;
  /** 'dark' restyles the (light) tiles for the dark 3D scene. */
  theme: 'dark' | 'light';
  /** Highest tile zoom to use (default 10) and the most tiles to fetch (default 64): satellite imagery asks for more. */
  maxZoom?: number;
  maxTiles?: number;
}

export interface GroundMapResult {
  size: number;
  data: Uint8ClampedArray; // RGBA, row 0 = north edge
  /** Tiles that could not be fetched (their area is left neutral grey). */
  missing: number;
}

function loadTile(provider: BasemapProvider, z: number, x: number, y: number): Promise<ImageBitmap | null> {
  const key = `${provider.id}/${z}/${x}/${y}`;
  const hit = tileCache.get(key);
  if (hit) {
    tileCache.delete(key);
    tileCache.set(key, hit);
    return Promise.resolve(hit);
  }
  // persistent cache (Cache API) first, then the network; one fetch per tile
  return fetchTileBitmap(basemapTileUrl(provider, z, x, y)).then((bmp) => {
    if (!bmp) return null;
    while (tileCache.size >= CACHE_MAX) {
      const first = tileCache.keys().next().value;
      if (first === undefined) break;
      tileCache.delete(first);
    }
    tileCache.set(key, bmp);
    return bmp;
  });
}

/** CSS `invert hue-rotate(180) brightness .78 contrast .92 saturate .55`, as one per-pixel function. */
function darken(r: number, g: number, b: number, out: Uint8ClampedArray, o: number): void {
  const ir = 255 - r, ig = 255 - g, ib = 255 - b;
  // hue-rotate(180deg)
  let hr = -0.574 * ir + 1.43 * ig + 0.144 * ib;
  let hg = 0.426 * ir + 0.43 * ig + 0.144 * ib;
  let hb = 0.426 * ir + 1.43 * ig - 0.856 * ib;
  // saturate(.55)
  const l = 0.213 * hr + 0.715 * hg + 0.072 * hb;
  hr = l + (hr - l) * 0.55;
  hg = l + (hg - l) * 0.55;
  hb = l + (hb - l) * 0.55;
  // brightness(.78), contrast(.92)
  out[o] = (hr * 0.78 - 128) * 0.92 + 128;
  out[o + 1] = (hg * 0.78 - 128) * 0.92 + 128;
  out[o + 2] = (hb * 0.78 - 128) * 0.92 + 128;
  out[o + 3] = 255;
}

/**
 * Build the ground image. `isCancelled` is polled between slices; resolves null when cancelled or
 * when no tile could be loaded at all.
 */
export async function buildGroundMap(req: GroundMapRequest, isCancelled: () => boolean): Promise<GroundMapResult | null> {
  const { lat, lon, radiusKm, size, provider } = req;
  if (!provider.url || !(radiusKm > 1)) return null;
  const R = radiusKm * 1000;

  // Tile zoom: about one tile pixel per texel (a little coarser is fine), never more than MAX_TILES.
  const texelM = (2 * R) / size;
  const mPerPx0 = (156543.03392 * Math.cos((lat * Math.PI) / 180));
  let z = Math.floor(Math.log2(mPerPx0 / texelM));
  const maxTiles = req.maxTiles ?? MAX_TILES;
  z = Math.max(Math.max(3, provider.minZoom), Math.min(Math.min(req.maxZoom ?? 10, provider.maxZoom), z));

  // Bounding box of the square in tile space (sample the whole boundary: the projection curves).
  const bounds = (zoom: number) => {
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    const N = 24;
    for (let i = 0; i <= N; i++) {
      const f = -R + (2 * R * i) / N;
      for (const [px, py] of [[f, -R], [f, R], [-R, f], [R, f]] as const) {
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
  };
  let b = bounds(z);
  while ((b.tx1 - b.tx0 + 1) * (b.ty1 - b.ty0 + 1) > maxTiles && z > 3) b = bounds(--z);

  const nx = b.tx1 - b.tx0 + 1;
  const ny = b.ty1 - b.ty0 + 1;
  const stitch = document.createElement('canvas');
  stitch.width = nx * TILE;
  stitch.height = ny * TILE;
  const sctx = stitch.getContext('2d', { willReadFrequently: true });
  if (!sctx) return null;
  sctx.fillStyle = '#e9e8ee';
  sctx.fillRect(0, 0, stitch.width, stitch.height);

  // Fetch tiles, nearest the centre first, with bounded concurrency.
  const cx = (b.tx0 + b.tx1) / 2, cy = (b.ty0 + b.ty1) / 2;
  const jobs: { x: number; y: number }[] = [];
  for (let y = b.ty0; y <= b.ty1; y++) for (let x = b.tx0; x <= b.tx1; x++) jobs.push({ x, y });
  jobs.sort((a, c) => Math.hypot(a.x - cx, a.y - cy) - Math.hypot(c.x - cx, c.y - cy));
  let missing = 0;
  let loaded = 0;
  let cursor = 0;
  const worker = async () => {
    while (cursor < jobs.length && !isCancelled()) {
      const j = jobs[cursor++];
      const img = await loadTile(provider, z, j.x, j.y);
      if (img) {
        sctx.drawImage(img, (j.x - b.tx0) * TILE, (j.y - b.ty0) * TILE);
        loaded++;
      } else {
        missing++;
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, jobs.length) }, worker));
  if (isCancelled() || loaded === 0) return null;

  let src: ImageData;
  try {
    src = sctx.getImageData(0, 0, stitch.width, stitch.height); // throws if the canvas got tainted
  } catch {
    return null;
  }
  const sw = src.width, sh = src.height;
  const sd = src.data;

  // Coarse grid of source-pixel coordinates (projection is smooth: interpolate in between).
  const STEP = 16;
  const G = Math.ceil(size / STEP) + 1;
  const gx = new Float32Array(G * G);
  const gy = new Float32Array(G * G);
  for (let j = 0; j < G; j++) {
    const ey = R - ((j * STEP + 0.5) / size) * 2 * R;
    for (let i = 0; i < G; i++) {
      const ex = -R + ((i * STEP + 0.5) / size) * 2 * R;
      const [la, lo] = dataSpaceToLatLon(ex, ey, lat, lon);
      const t = latLonToTile(la, lo, z);
      gx[j * G + i] = (t.tx - b.tx0) * TILE;
      gy[j * G + i] = (t.ty - b.ty0) * TILE;
    }
  }

  const out = new Uint8ClampedArray(size * size * 4);
  const dark = req.theme === 'dark';
  let t0 = performance.now();
  for (let y = 0; y < size; y++) {
    const fj = y / STEP;
    const j0 = Math.min(G - 2, Math.floor(fj));
    const tj = fj - j0;
    for (let x = 0; x < size; x++) {
      const fi = x / STEP;
      const i0 = Math.min(G - 2, Math.floor(fi));
      const ti = fi - i0;
      const a = j0 * G + i0;
      const sx = (gx[a] * (1 - ti) + gx[a + 1] * ti) * (1 - tj) + (gx[a + G] * (1 - ti) + gx[a + G + 1] * ti) * tj;
      const sy = (gy[a] * (1 - ti) + gy[a + 1] * ti) * (1 - tj) + (gy[a + G] * (1 - ti) + gy[a + G + 1] * ti) * tj;
      // bilinear fetch
      const px = Math.max(0, Math.min(sw - 1.001, sx - 0.5));
      const py = Math.max(0, Math.min(sh - 1.001, sy - 0.5));
      const x0 = px | 0, y0 = py | 0;
      const fx = px - x0, fy = py - y0;
      const p00 = (y0 * sw + x0) * 4, p10 = p00 + 4, p01 = p00 + sw * 4, p11 = p01 + 4;
      const w00 = (1 - fx) * (1 - fy), w10 = fx * (1 - fy), w01 = (1 - fx) * fy, w11 = fx * fy;
      const r = sd[p00] * w00 + sd[p10] * w10 + sd[p01] * w01 + sd[p11] * w11;
      const g = sd[p00 + 1] * w00 + sd[p10 + 1] * w10 + sd[p01 + 1] * w01 + sd[p11 + 1] * w11;
      const bl = sd[p00 + 2] * w00 + sd[p10 + 2] * w10 + sd[p01 + 2] * w01 + sd[p11 + 2] * w11;
      const o = (y * size + x) * 4;
      if (dark) darken(r, g, bl, out, o);
      else { out[o] = r; out[o + 1] = g; out[o + 2] = bl; out[o + 3] = 255; }
    }
    if ((y & 15) === 15 && performance.now() - t0 > 8) {
      await yieldToMain();
      if (isCancelled()) return null;
      t0 = performance.now();
    }
  }
  return { size, data: out, missing };
}
