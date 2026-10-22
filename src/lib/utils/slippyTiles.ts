/**
 * A small bounded loader and in-memory cache for slippy-map tiles (not the persistent ground-tile cache
 * of tileLoader.ts), shared by the flat radar network map and the globe:
 * an LRU cache, at most six requests in flight, retries with back-off (four tries per tile), a pause after
 * six failures in a row (offline), and nothing at all when the basemap has no URL ("None").
 *
 * The images are requested with CORS (`crossOrigin = "anonymous"`) so that the globe may upload them as
 * a texture; the tile hosts the app uses (OpenStreetMap, OpenTopoMap) allow that. Attribution stays the
 * caller's duty: whoever draws tiles must keep the provider's attribution on screen.
 */
import { basemapTileUrl, type BasemapProvider } from './basemaps';

export interface TileWant {
  z: number;
  x: number;
  y: number;
}

export interface SlippyTilesOptions {
  /** Called whenever a tile arrived, failed or a retry is due (draw again). */
  onChange: () => void;
  maxInflight?: number;
  cacheSize?: number;
  /** For tests: how an image is made. */
  makeImage?: () => HTMLImageElement;
  now?: () => number;
}

interface Wanted extends TileWant {
  key: string;
}

export class SlippyTiles {
  private cache = new Map<string, HTMLImageElement>();
  private inflight = new Map<string, HTMLImageElement>();
  private failures = new Map<string, { n: number; until: number }>();
  private wanted: Wanted[] = [];
  private provider: BasemapProvider | null = null;
  private consecutiveFails = 0;
  private pausedUntil = 0;
  private gen = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private disposed = false;
  private readonly maxInflight: number;
  private readonly cacheSize: number;
  private readonly makeImage: () => HTMLImageElement;
  private readonly now: () => number;
  private readonly onChange: () => void;
  /** Number of image requests started since creation (for the "what was sent" statements and tests). */
  requests = 0;

  constructor(o: SlippyTilesOptions) {
    this.onChange = o.onChange;
    this.maxInflight = o.maxInflight ?? 6;
    this.cacheSize = o.cacheSize ?? 256;
    this.makeImage = o.makeImage ?? (() => new Image());
    this.now = o.now ?? (() => Date.now());
  }

  private keyOf(p: BasemapProvider, z: number, x: number, y: number): string {
    return `${p.id}/${z}/${x}/${y}`;
  }

  /** The cached image of a tile (and mark it recently used), or undefined. */
  get(p: BasemapProvider, z: number, x: number, y: number): HTMLImageElement | undefined {
    const key = this.keyOf(p, z, x, y);
    const img = this.cache.get(key);
    if (img) {
      this.cache.delete(key);
      this.cache.set(key, img);
    }
    return img;
  }

  has(p: BasemapProvider, z: number, x: number, y: number): boolean {
    return this.cache.has(this.keyOf(p, z, x, y));
  }

  /** Number of tiles in the cache for a provider and level. */
  count(p: BasemapProvider, z: number): number {
    const prefix = `${p.id}/${z}/`;
    let n = 0;
    for (const k of this.cache.keys()) if (k.startsWith(prefix)) n++;
    return n;
  }

  /**
   * Say which tiles are wanted now, most important first; those not cached are requested (within the limits).
   * A provider without a URL cancels everything and requests nothing.
   */
  want(p: BasemapProvider, tiles: readonly TileWant[]): void {
    if (this.disposed) return;
    if (!this.provider || this.provider.id !== p.id) {
      this.reset();
      this.provider = p;
    }
    if (!p.url) {
      this.wanted = [];
      return;
    }
    this.wanted = tiles
      .map((t) => ({ ...t, key: this.keyOf(p, t.z, t.x, t.y) }))
      .filter((t) => !this.cache.has(t.key));
    this.pump();
  }

  /** Forget everything in flight and cached (the provider changed). */
  reset(): void {
    this.gen++;
    for (const img of this.inflight.values()) img.src = '';
    this.inflight.clear();
    this.cache.clear();
    this.failures.clear();
    this.wanted = [];
    this.consecutiveFails = 0;
    this.pausedUntil = 0;
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }

  dispose(): void {
    this.disposed = true;
    this.reset();
    this.provider = null;
  }

  private pump(): void {
    const p = this.provider;
    if (this.disposed || !p || !p.url) return;
    const t = this.now();
    if (t < this.pausedUntil) {
      this.armRetry(this.pausedUntil - t);
      return;
    }
    let nextRetry = Infinity;
    for (const w of this.wanted) {
      if (this.inflight.size >= this.maxInflight) break;
      if (this.cache.has(w.key) || this.inflight.has(w.key)) continue;
      const f = this.failures.get(w.key);
      if (f) {
        if (f.n >= 4) continue;
        if (f.until > t) {
          nextRetry = Math.min(nextRetry, f.until - t);
          continue;
        }
      }
      const myGen = this.gen;
      const img = this.makeImage();
      img.crossOrigin = 'anonymous';
      img.decoding = 'async';
      this.inflight.set(w.key, img);
      this.requests++;
      img.onload = () => {
        if (myGen !== this.gen || this.disposed) return;
        this.inflight.delete(w.key);
        this.failures.delete(w.key);
        this.consecutiveFails = 0;
        while (this.cache.size >= this.cacheSize) {
          const first = this.cache.keys().next().value;
          if (first === undefined) break;
          this.cache.delete(first);
        }
        this.cache.set(w.key, img);
        this.onChange();
        this.pump();
      };
      img.onerror = () => {
        if (myGen !== this.gen || this.disposed) return;
        this.inflight.delete(w.key);
        const n = (this.failures.get(w.key)?.n ?? 0) + 1;
        this.failures.set(w.key, { n, until: this.now() + 2000 * 2 ** (n - 1) });
        if (++this.consecutiveFails >= 6) {
          this.pausedUntil = this.now() + 30000;
          this.consecutiveFails = 0;
        }
        this.onChange();
        this.pump();
      };
      img.src = basemapTileUrl(p, w.z, w.x, w.y);
    }
    if (nextRetry < Infinity) this.armRetry(nextRetry);
  }

  private armRetry(ms: number): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.pump();
    }, Math.max(50, ms));
  }
}
