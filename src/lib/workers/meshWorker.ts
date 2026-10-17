/**
 * Web Worker: polar-to-Cartesian triangle mesh computation.
 *
 * Moves the heaviest CPU work (the nested gate loop) off the main thread.
 * Every request carries an `id` that is echoed in the response, so results can
 * be matched to requests regardless of ordering or errors.
 *
 * The result arrays are exactly sized, so they are transferred without copying.
 */

import { buildSharpMesh, buildSmoothMesh } from '../utils/meshBuilder';

export interface MeshWorkerInput {
  /** Correlation id, echoed in the output. */
  id: number;
  /** 'sharp' (default): flat-colour mesh. 'smooth': bilinear mesh. */
  mode?: 'sharp' | 'smooth';
  azimuth: Float32Array;
  range: Float32Array;
  values: Float32Array;
  nAz: number;
  nRange: number;
}

export interface MeshWorkerOutput {
  id: number;
  mode: 'sharp' | 'smooth';
  positions: Float32Array;
  /** sharp mode: one value per vertex (empty in smooth mode) */
  vals: Float32Array;
  /** smooth mode only */
  polars?: Float32Array;
  /** smooth mode only */
  values4?: Float32Array;
  vertexCount: number;
  buildTimeMs: number;
}

self.onmessage = (e: MessageEvent<MeshWorkerInput>) => {
  const { id, azimuth, range, values, nAz, nRange } = e.data;
  const mode = e.data.mode ?? 'sharp';
  const t0 = performance.now();

  let output: MeshWorkerOutput;
  let transfer: ArrayBuffer[];
  if (mode === 'smooth') {
    const m = buildSmoothMesh(azimuth, range, values, nAz, nRange);
    output = {
      id, mode, positions: m.positions, vals: new Float32Array(0),
      polars: m.polars, values4: m.values4,
      vertexCount: m.vertexCount, buildTimeMs: performance.now() - t0,
    };
    transfer = [m.positions.buffer as ArrayBuffer, m.polars.buffer as ArrayBuffer, m.values4.buffer as ArrayBuffer];
  } else {
    const m = buildSharpMesh(azimuth, range, values, nAz, nRange);
    output = {
      id, mode, positions: m.positions, vals: m.vals,
      vertexCount: m.vertexCount, buildTimeMs: performance.now() - t0,
    };
    transfer = [m.positions.buffer as ArrayBuffer, m.vals.buffer as ArrayBuffer];
  }

  (self as unknown as Worker).postMessage(output, transfer);
};
