/**
 * Satellite imagery and terrain for the tornado ground, from the existing tile cache.
 *
 * It uses the same tile sources as the storm clip (`tools/storm_clip`, `engine/animate_tiles.py`): the clip
 * server answers `/tiles/<source>/<z>/<x>/<y>` from the disk cache (EOX Sentinel-2 cloudless, Terrain Tiles
 * Terrarium) and, when allowed, fetches missing ones once. Here the tiles are mosaicked into one local square
 * (metres east / north of the scene origin) at the zoom the pixel size asks for, up to z15 near the vortex
 * (a source that stops at a lower zoom, EOX z13, is simply upsampled). When too few tiles are present the loader
 * resolves null and the scene uses its procedural ground ("no satellite imagery available offline"): dropping
 * tiles into the cache and reloading improves the look with no other change.
 *
 * Browser code (OffscreenCanvas / createImageBitmap); the pure parts are exported for tests.
 */

import { latLonToTile } from '../geoProjection.ts';
import { fetchTileBitmap, mapLimit } from '../tileLoader.ts';
import { fromLocal } from './seed.ts';

export interface GroundTileRequest {
  origin: { lat: number; lon: number };
  /** Centre of the mosaic in local metres. */
  cx: number;
  cy: number;
  /** Half width (m). */
  half: number;
  /** Output imagery size in pixels (square). */
  imagerySize?: number;
  /** Output height size in samples. */
  heightSize?: number;
  /** Highest zoom to request (default 15). */
  maxZoom?: number;
  /** Most tiles per source (default 48). */
  maxTiles?: number;
  /** Base URL of the tile server ('' = same origin). */
  base?: string;
  /** Fraction of the wanted tiles that must be present to use the layer (default 0.6). */
  minCoverage?: number;
}

export interface GroundTileResult {
  imagery: { size: number; rgba: Uint8ClampedArray; zoom: number; coverage: number } | null;
  terrain: { size: number; metres: Float32Array; zoom: number; coverage: number; min: number; max: number } | null;
  attribution: string[];
  log: string[];
}

/** Zoom at which one tile pixel is no larger than `pxM` metres at latitude `lat`, capped to [minZ, maxZ]. */
export function zoomForPixel(pxM: number, lat: number, minZ: number, maxZ: number): number {
  const z = Math.ceil(Math.log2((156543.03392 * Math.cos((lat * Math.PI) / 180)) / pxM));
  return Math.max(minZ, Math.min(maxZ, z));
}

/** Tile range covering a local square (sampling the boundary). */
export function tileRangeFor(req: GroundTileRequest, zoom: number): { tx0: number; tx1: number; ty0: number; ty1: number } {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  const N = 16;
  for (let i = 0; i <= N; i++) {
    const f = -req.half + (2 * req.half * i) / N;
    for (const [px, py] of [[f, -req.half], [f, req.half], [-req.half, f], [req.half, f]] as const) {
      const [la, lo] = fromLocal(req.origin, req.cx + px, req.cy + py);
      const t = latLonToTile(la, lo, zoom);
      x0 = Math.min(x0, t.tx); x1 = Math.max(x1, t.tx); y0 = Math.min(y0, t.ty); y1 = Math.max(y1, t.ty);
    }
  }
  return { tx0: Math.floor(x0), tx1: Math.floor(x1), ty0: Math.floor(y0), ty1: Math.floor(y1) };
}

/** Terrarium pixel to metres. */
export function terrariumMetres(r: number, g: number, b: number): number {
  return r * 256 + g + b / 256 - 32768;
}

async function stitch(source: string, zoom: number, rng: { tx0: number; tx1: number; ty0: number; ty1: number }, base: string): Promise<{ canvas: OffscreenCanvas; loaded: number; wanted: number }> {
  const nx = rng.tx1 - rng.tx0 + 1, ny = rng.ty1 - rng.ty0 + 1;
  const canvas = new OffscreenCanvas(nx * 256, ny * 256);
  const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
  const jobs: { tx: number; ty: number }[] = [];
  for (let ty = rng.ty0; ty <= rng.ty1; ty++) for (let tx = rng.tx0; tx <= rng.tx1; tx++) jobs.push({ tx, ty });
  let loaded = 0;
  await mapLimit(jobs, 4, async (j) => {
    const bmp = await fetchTileBitmap(`${base}/tiles/${source}/${zoom}/${j.tx}/${j.ty}`);
    if (!bmp) return;
    ctx.drawImage(bmp, (j.tx - rng.tx0) * 256, (j.ty - rng.ty0) * 256);
    loaded++;
  });
  return { canvas, loaded, wanted: jobs.length };
}

/** Load the imagery and terrain mosaics (null parts when the tiles are not in the cache). */
export async function loadGroundTiles(req: GroundTileRequest): Promise<GroundTileResult> {
  const res: GroundTileResult = { imagery: null, terrain: null, attribution: [], log: [] };
  if (typeof OffscreenCanvas === 'undefined') { res.log.push('no OffscreenCanvas: procedural ground'); return res; }
  const base = req.base ?? '';
  const isz = req.imagerySize ?? 2048;
  const hsz = req.heightSize ?? 512;
  const maxZ = req.maxZoom ?? 15;
  const minCov = req.minCoverage ?? 0.6;
  const maxTiles = req.maxTiles ?? 48;
  const [lat0] = fromLocal(req.origin, req.cx, req.cy);

  const layer = async (source: string, outSize: number) => {
    const pxM = (2 * req.half) / outSize;
    let z = zoomForPixel(pxM, lat0, 8, maxZ);
    let rng = tileRangeFor(req, z);
    while ((rng.tx1 - rng.tx0 + 1) * (rng.ty1 - rng.ty0 + 1) > maxTiles && z > 8) { z--; rng = tileRangeFor(req, z); }
    // try the best zoom, then coarser ones, until the coverage is acceptable
    for (let zz = z; zz >= Math.max(8, z - 3); zz--) {
      const r2 = zz === z ? rng : tileRangeFor(req, zz);
      const st = await stitch(source, zz, r2, base);
      const cov = st.loaded / st.wanted;
      res.log.push(`${source} z${zz}: ${st.loaded}/${st.wanted} tiles`);
      if (cov >= minCov) return { st, rng: r2, zoom: zz, cov };
      if (st.loaded === 0 && zz === z) {
        // a source without any tile at the finest zoom may still have coarser ones
        continue;
      }
    }
    return null;
  };

  /** Bilinear resample of a stitched mosaic into the local square; `decode` turns a pixel into `n` values (interpolated after decoding). */
  const sampleMosaic = (canvas: OffscreenCanvas, rng: { tx0: number; ty0: number }, zoom: number, outSize: number, n: number, decode: (img: Uint8ClampedArray, q: number, out: number[]) => void) => {
    const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
    const img = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    const W = canvas.width, H = canvas.height;
    const out = new Float32Array(outSize * outSize * n);
    const v = new Array<number>(n).fill(0);
    const acc = new Array<number>(n).fill(0);
    for (let j = 0; j < outSize; j++) {
      const y = req.cy - req.half + ((j + 0.5) / outSize) * 2 * req.half;
      for (let i = 0; i < outSize; i++) {
        const x = req.cx - req.half + ((i + 0.5) / outSize) * 2 * req.half;
        const [la, lo] = fromLocal(req.origin, x, y);
        const t = latLonToTile(la, lo, zoom);
        const px = (t.tx - rng.tx0) * 256 - 0.5, py = (t.ty - rng.ty0) * 256 - 0.5;
        const ix = Math.max(0, Math.min(W - 2, Math.floor(px))), iy = Math.max(0, Math.min(H - 2, Math.floor(py)));
        const fx = Math.max(0, Math.min(1, px - ix)), fy = Math.max(0, Math.min(1, py - iy));
        acc.fill(0);
        for (const [dx, dy, w] of [[0, 0, (1 - fx) * (1 - fy)], [1, 0, fx * (1 - fy)], [0, 1, (1 - fx) * fy], [1, 1, fx * fy]] as const) {
          decode(img, ((iy + dy) * W + ix + dx) * 4, v);
          for (let c = 0; c < n; c++) acc[c] += w * v[c];
        }
        const o = (j * outSize + i) * n;
        for (let c = 0; c < n; c++) out[o + c] = acc[c];
      }
    }
    return out;
  };

  try {
    const im = await layer('eox', isz);
    if (im) {
      const f = sampleMosaic(im.st.canvas, im.rng, im.zoom, isz, 4, (d, q, o) => { o[0] = d[q]; o[1] = d[q + 1]; o[2] = d[q + 2]; o[3] = 255; });
      const rgba = new Uint8ClampedArray(isz * isz * 4);
      for (let k = 0; k < rgba.length; k++) rgba[k] = f[k];
      res.imagery = { size: isz, rgba, zoom: im.zoom, coverage: im.cov };
      res.attribution.push('Imagery: Sentinel-2 cloudless (c) EOX IT Services GmbH (contains modified Copernicus Sentinel data 2021), CC BY-NC-SA 4.0');
    }
    const te = await layer('terrarium', hsz);
    if (te) {
      const m = sampleMosaic(te.st.canvas, te.rng, te.zoom, hsz, 1, (d, q, o) => { o[0] = terrariumMetres(d[q], d[q + 1], d[q + 2]); });
      let mn = Infinity, mx = -Infinity;
      for (let k = 0; k < m.length; k++) {
        mn = Math.min(mn, m[k]); mx = Math.max(mx, m[k]);
      }
      res.terrain = { size: hsz, metres: m, zoom: te.zoom, coverage: te.cov, min: mn, max: mx };
      res.attribution.push('Elevation: Terrain Tiles (Mapzen / Tilezen: USGS 3DEP, NED, SRTM, GMTED2010)');
    }
  } catch (e) {
    res.log.push(`tile mosaic failed: ${String(e)}`);
  }
  return res;
}
