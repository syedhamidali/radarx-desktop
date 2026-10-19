/**
 * Raw sweep data cache (LRU, memory bounded) shared by the websocket layer, the file manager and the
 * renderers. Split out of ppiRenderer.ts so the always-loaded code does not drag the WebGL renderer
 * into the first-paint bundle.
 */
import { renderCache } from './renderCache';

export interface SweepDataEntry {
  variable: string;
  sweep: number;
  azimuth: Float32Array;
  range: Float32Array;
  values: Float32Array;
  nAz: number;
  nRange: number;
  maxRange: number;
  vmin: number;
  vmax: number;
  units: string;
  /** Set for variables produced by a plugin module (not stored in the file). */
  derived?: boolean;
  /** Human label of a derived variable. */
  label?: string;
  /** Class table of a categorical derived variable. */
  categories?: { value: number; label: string; color: string }[] | null;
  /** Colormap name the producer asked for (derived variables). */
  cmap?: string | null;
  /**
   * 'rhi' for a range-height sweep: its rays differ in elevation, not azimuth. `azimuth` then holds
   * the (nearly constant) ray azimuths, `elevation` the per-ray elevation (ascending, degrees) and
   * `rhiAzimuth` the plane azimuth. Absent for PPI sweeps.
   */
  kind?: 'rhi';
  elevation?: Float32Array;
  rhiAzimuth?: number;
}

// Insertion-ordered Map doubles as an LRU list (oldest first).
const sweepCache = new Map<string, SweepDataEntry>();
let sweepCacheBytes = 0;
/** Memory bound for cached raw sweeps (a full-resolution sweep is ~3-8 MB). */
const SWEEP_CACHE_MAX_BYTES = 384 * 1024 * 1024;
const SWEEP_CACHE_MAX_ENTRIES = 512;

function cacheKey(variable: string, sweep: number): string {
  return `${variable}:${sweep}`;
}

/** Bytes retained by an entry. Its arrays are usually views over one shared buffer. */
function entryBytes(e: SweepDataEntry): number {
  const buf = e.values.buffer;
  if (e.azimuth.buffer === buf && e.range.buffer === buf) return buf.byteLength;
  return e.azimuth.byteLength + e.range.byteLength + e.values.byteLength;
}

export function cacheSweepData(entry: SweepDataEntry): void {
  const key = cacheKey(entry.variable, entry.sweep);
  const existing = sweepCache.get(key);
  if (existing) {
    sweepCacheBytes -= entryBytes(existing);
    sweepCache.delete(key);
  }
  sweepCache.set(key, entry);
  sweepCacheBytes += entryBytes(entry);

  // Evict least-recently-used entries until under budget (never the one just added).
  while (
    (sweepCacheBytes > SWEEP_CACHE_MAX_BYTES || sweepCache.size > SWEEP_CACHE_MAX_ENTRIES) &&
    sweepCache.size > 1
  ) {
    const oldestKey = sweepCache.keys().next().value;
    if (oldestKey === undefined) break;
    const oldest = sweepCache.get(oldestKey)!;
    sweepCacheBytes -= entryBytes(oldest);
    sweepCache.delete(oldestKey);
  }
}

export function getCachedSweep(variable: string, sweep: number): SweepDataEntry | undefined {
  const key = cacheKey(variable, sweep);
  const entry = sweepCache.get(key);
  if (entry) {
    // Touch: move to the most-recently-used end.
    sweepCache.delete(key);
    sweepCache.set(key, entry);
  }
  return entry;
}

/** Drop every cached sweep (and mesh) of one variable, e.g. a derived variable that was recomputed. */
export function evictCachedVariable(variable: string): void {
  const prefix = `${variable}:`;
  for (const [k, e] of [...sweepCache]) {
    if (k.startsWith(prefix) && e.variable === variable) {
      sweepCacheBytes -= entryBytes(e);
      sweepCache.delete(k);
    }
  }
  renderCache.clear(); // meshes are keyed by variable:sweep with no per-variable eviction; rebuilt on demand
}

export function clearSweepCache(): void {
  sweepCache.clear();
  sweepCacheBytes = 0;
  renderCache.clear();
}
