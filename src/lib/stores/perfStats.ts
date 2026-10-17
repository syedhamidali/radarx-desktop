/**
 * Writable store for performance timing data.
 *
 * Updated by the renderer / workers and read by PerformanceMonitor.
 */
import { writable } from 'svelte/store';

export interface PerfStatsData {
  /** Time spent in the WebGL render() call (ms) */
  renderTimeMs: number;
  /** Time spent building the triangle mesh in the worker (ms) */
  meshBuildTimeMs: number;
  /** Time transferring data between main thread and workers (ms) */
  dataTransferTimeMs: number;

  // ── 3D view (written by qualityGovernor) ──────────────────────────────
  /** Current 3D quality level, e.g. "medium (auto)"; empty until a 3D view exists. */
  quality: string;
  /** User preference: auto | high | medium | low */
  qualityMode: string;
  /** Smoothed GPU time of a complete 3D frame (ms) */
  frameMsEma: number;
  /** Fraction of canvas resolution the 3D volume is rendered at */
  renderScale: number;
  /** Size of the packed 3D volume texture (MB) */
  volumeTexMB: number;
  /** Unmasked GL renderer string (empty if unknown) */
  gpuRenderer: string;
}

export const perfStats = writable<PerfStatsData>({
  renderTimeMs: 0,
  meshBuildTimeMs: 0,
  dataTransferTimeMs: 0,
  quality: '',
  qualityMode: 'auto',
  frameMsEma: 0,
  renderScale: 1,
  volumeTexMB: 0,
  gpuRenderer: '',
});
