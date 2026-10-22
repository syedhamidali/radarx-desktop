/**
 * The image on the globe's surface: where it comes from and what it costs.
 *
 *  - Basemap OpenStreetMap / OpenTopoMap: the 64 tiles of level 3 (8 x 8, 256 px) are fetched through the
 *    same bounded tile cache the flat map uses (SlippyTiles: at most six requests at once, retries with
 *    back-off, nothing when offline), nearest to the view first, and composited into one 2048 x 2048 canvas
 *    that the renderer samples in Web Mercator. That is all the globe ever asks the tile server for.
 *  - Basemap "None": nothing is requested. The bundled land outlines are painted into a plate carree canvas
 *    (ocean and land in the theme's colours); the renderer samples that, and the caller strokes the
 *    coastlines, borders and graticule on top.
 *
 * Attribution is the caller's: tiles are only on the globe while the provider's attribution is shown.
 */
import { type ResolvedBasemap } from './basemaps';
import { tilesByDistance, parseColourFilter } from './globeMath';
import { paintPlainLand, type GlobeGeo, type PlainColours } from './globeGeo';
import type { GlobeRenderer } from './globeRenderer';
import type { SlippyTiles } from './slippyTiles';

export const GLOBE_TILE_Z = 3;
const TILES = 2 ** GLOBE_TILE_Z; // 8 per side
const TILE_PX = 256;
const SIDE = TILES * TILE_PX; // 2048
/** Paper colour under the tiles (goes through the same dark filter as the tiles). */
const PAPER = '#e9e8ee';

export interface SurfaceStatus {
  /** 'tiles' | 'plain' | 'none-yet' */
  kind: 'tiles' | 'plain';
  loaded: number;
  total: number;
}

export class GlobeSurface {
  private tileCanvas: HTMLCanvasElement | null = null;
  private plainCanvas: HTMLCanvasElement | null = null;
  private edge: HTMLCanvasElement | null = null;
  private drawn = new Set<string>();
  private basemap: ResolvedBasemap | null = null;
  private uploadTimer: ReturnType<typeof setTimeout> | null = null;
  private lastUpload = 0;
  private disposed = false;
  private geo: GlobeGeo | null = null;
  private colours: PlainColours | null = null;
  status: SurfaceStatus = { kind: 'plain', loaded: 0, total: 0 };

  constructor(
    private readonly renderer: GlobeRenderer,
    private readonly tiles: SlippyTiles,
    private readonly changed: () => void,
  ) {}

  /** Choose what the sphere shows. Safe to call again whenever the setting, the theme or the geography changed. */
  setBasemap(b: ResolvedBasemap, plain: PlainColours, geo: GlobeGeo | null, lon: number, lat: number): void {
    if (this.disposed) return;
    const prev = this.basemap;
    this.basemap = b;
    this.geo = geo;
    this.colours = plain;
    if (!b.provider.url) {
      // Nothing may be sent: cancel anything in flight and paint the plain world.
      this.tiles.want(b.provider, []);
      this.paintPlain();
      this.renderer.setSurface(this.plainCanvas, 'equirect', [0, 0, 0], [0, 0, 0], []);
      this.status = { kind: 'plain', loaded: 0, total: 0 };
      this.changed();
      return;
    }
    if (!this.tileCanvas) {
      this.tileCanvas = document.createElement('canvas');
      this.tileCanvas.width = SIDE;
      this.tileCanvas.height = SIDE;
    }
    if (!prev || prev.provider.id !== b.provider.id) {
      this.drawn.clear();
      const ctx = this.tileCanvas.getContext('2d')!;
      ctx.fillStyle = PAPER;
      ctx.fillRect(0, 0, SIDE, SIDE);
    }
    this.pushTiles(lon, lat);
    this.syncTiles();
    this.applyTileSurface();
  }

  /** Ask for the tiles again in the order of distance to a new centre (cheap; the cache answers what it has). */
  focus(lon: number, lat: number): void {
    if (this.disposed || !this.basemap?.provider.url) return;
    this.pushTiles(lon, lat);
  }

  private pushTiles(lon: number, lat: number): void {
    if (!this.basemap) return;
    this.tiles.want(this.basemap.provider, tilesByDistance(GLOBE_TILE_Z, lon, lat));
  }

  /** Composite the tiles that arrived since the last call; upload at most about every 150 ms. */
  syncTiles(): void {
    if (this.disposed || !this.basemap?.provider.url || !this.tileCanvas) return;
    const p = this.basemap.provider;
    const ctx = this.tileCanvas.getContext('2d')!;
    let fresh = 0;
    for (let y = 0; y < TILES; y++) {
      for (let x = 0; x < TILES; x++) {
        const key = `${x}/${y}`;
        if (this.drawn.has(key)) continue;
        const img = this.tiles.get(p, GLOBE_TILE_Z, x, y);
        if (!img) continue;
        ctx.drawImage(img, x * TILE_PX, y * TILE_PX, TILE_PX, TILE_PX);
        this.drawn.add(key);
        fresh++;
      }
    }
    this.status = { kind: 'tiles', loaded: this.drawn.size, total: TILES * TILES };
    if (fresh === 0) return;
    const wait = this.drawn.size === TILES * TILES ? 0 : Math.max(0, 150 - (performance.now() - this.lastUpload));
    if (this.uploadTimer) return;
    this.uploadTimer = setTimeout(() => {
      this.uploadTimer = null;
      if (this.disposed) return;
      this.applyTileSurface();
      this.changed();
    }, wait);
  }

  private applyTileSurface(): void {
    if (!this.tileCanvas || !this.basemap) return;
    this.lastUpload = performance.now();
    const filter = parseColourFilter(this.basemap.filter);
    this.renderer.setSurface(this.tileCanvas, 'mercator', this.rowMean(0), this.rowMean(SIDE - 4), filter);
  }

  /** Mean colour (0..1 RGB) of four rows of the tile canvas starting at `y`: the polar cap colour. */
  private rowMean(y: number): [number, number, number] {
    if (!this.tileCanvas) return [0.9, 0.9, 0.9];
    if (!this.edge) {
      this.edge = document.createElement('canvas');
      this.edge.width = 1;
      this.edge.height = 1;
    }
    const c = this.edge.getContext('2d', { willReadFrequently: true })!;
    c.imageSmoothingEnabled = true;
    c.imageSmoothingQuality = 'high';
    c.clearRect(0, 0, 1, 1);
    c.drawImage(this.tileCanvas, 0, y, SIDE, 4, 0, 0, 1, 1);
    const d = c.getImageData(0, 0, 1, 1).data;
    return [d[0] / 255, d[1] / 255, d[2] / 255];
  }

  private paintPlain(): void {
    if (!this.plainCanvas) {
      this.plainCanvas = document.createElement('canvas');
      this.plainCanvas.width = 2048;
      this.plainCanvas.height = 1024;
    }
    const ctx = this.plainCanvas.getContext('2d')!;
    paintPlainLand(ctx, 2048, 1024, this.geo, this.colours ?? { ocean: '#101820', land: '#243040' });
  }

  dispose(): void {
    this.disposed = true;
    if (this.uploadTimer) clearTimeout(this.uploadTimer);
    this.uploadTimer = null;
    this.tileCanvas = null;
    this.plainCanvas = null;
    this.edge = null;
    this.drawn.clear();
  }
}
