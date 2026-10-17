/**
 * Pure mesh builders for the PPI renderer (polar gates -> triangle lists).
 *
 * Shared by the mesh Web Worker and the main-thread fallback so both produce
 * identical geometry. Each builder makes one cheap counting pass and then fills
 * exactly-sized typed arrays: no growable `number[]`, no trim/copy step, and the
 * resulting buffers can be transferred or uploaded as they are.
 *
 * A gate is "no data" when value <= -9998 or NaN (`!(v > -9998)`).
 */

const NODATA_LIMIT = -9998;
const DEG2RAD = Math.PI / 180;

export interface SharpMesh {
  /** x,y per vertex (metres, radar-centred) */
  positions: Float32Array;
  /** one value per vertex */
  vals: Float32Array;
  vertexCount: number;
}

export interface SmoothMesh {
  positions: Float32Array;
  /** (az, range) fraction inside the cell, per vertex */
  polars: Float32Array;
  /** The four corner values of the cell, repeated for every vertex of the cell */
  values4: Float32Array;
  vertexCount: number;
}

/** Flat-colour mesh: 2 triangles per valid gate, value constant over the gate. */
export function buildSharpMesh(
  azimuth: Float32Array,
  range: Float32Array,
  values: Float32Array,
  nAz: number,
  nRange: number,
): SharpMesh {
  const nCols = nRange - 1;
  if (nAz <= 0 || nCols <= 0) {
    return { positions: new Float32Array(0), vals: new Float32Array(0), vertexCount: 0 };
  }

  // Pass 1: count valid gates so the output arrays are allocated exactly once.
  let cells = 0;
  for (let ai = 0; ai < nAz; ai++) {
    const base = ai * nRange;
    for (let ri = 0; ri < nCols; ri++) {
      if (values[base + ri] > NODATA_LIMIT) cells++;
    }
  }

  const vertexCount = cells * 6;
  const positions = new Float32Array(vertexCount * 2);
  const vals = new Float32Array(vertexCount);

  let p = 0; // write index into positions
  let q = 0; // write index into vals
  for (let ai = 0; ai < nAz; ai++) {
    const az0 = azimuth[ai] * DEG2RAD;
    const az1 = (ai + 1 < nAz ? azimuth[ai + 1] : azimuth[0] + 360) * DEG2RAD;
    const s0 = Math.sin(az0), c0 = Math.cos(az0);
    const s1 = Math.sin(az1), c1 = Math.cos(az1);
    const base = ai * nRange;

    for (let ri = 0; ri < nCols; ri++) {
      const v = values[base + ri];
      if (!(v > NODATA_LIMIT)) continue;

      const r0 = range[ri];
      const r1 = range[ri + 1];
      const x00 = r0 * s0, y00 = r0 * c0;
      const x01 = r1 * s0, y01 = r1 * c0;
      const x10 = r0 * s1, y10 = r0 * c1;
      const x11 = r1 * s1, y11 = r1 * c1;

      // Triangle 1: (0,0) (0,1) (1,0)
      positions[p] = x00; positions[p + 1] = y00;
      positions[p + 2] = x01; positions[p + 3] = y01;
      positions[p + 4] = x10; positions[p + 5] = y10;
      // Triangle 2: (0,1) (1,1) (1,0)
      positions[p + 6] = x01; positions[p + 7] = y01;
      positions[p + 8] = x11; positions[p + 9] = y11;
      positions[p + 10] = x10; positions[p + 11] = y10;
      p += 12;

      vals[q] = v; vals[q + 1] = v; vals[q + 2] = v;
      vals[q + 3] = v; vals[q + 4] = v; vals[q + 5] = v;
      q += 6;
    }
  }

  return { positions, vals, vertexCount };
}

// Per-cell constant data for the smooth mesh (two triangles).
const SMOOTH_POLAR = new Float32Array([0, 0, 0, 1, 1, 0, 0, 1, 1, 1, 1, 0]);

/** Bilinear mesh: each cell carries its 4 corner values, interpolated in the fragment shader. */
export function buildSmoothMesh(
  azimuth: Float32Array,
  range: Float32Array,
  values: Float32Array,
  nAz: number,
  nRange: number,
): SmoothMesh {
  const nCols = nRange - 1;
  if (nAz <= 0 || nCols <= 0) {
    return {
      positions: new Float32Array(0),
      polars: new Float32Array(0),
      values4: new Float32Array(0),
      vertexCount: 0,
    };
  }

  let cells = 0;
  for (let ai = 0; ai < nAz; ai++) {
    const b0 = ai * nRange;
    const b1 = ((ai + 1) % nAz) * nRange;
    for (let ri = 0; ri < nCols; ri++) {
      if (
        values[b0 + ri] > NODATA_LIMIT || values[b0 + ri + 1] > NODATA_LIMIT ||
        values[b1 + ri] > NODATA_LIMIT || values[b1 + ri + 1] > NODATA_LIMIT
      ) cells++;
    }
  }

  const vertexCount = cells * 6;
  const positions = new Float32Array(vertexCount * 2);
  const polars = new Float32Array(vertexCount * 2);
  const values4 = new Float32Array(vertexCount * 4);

  let p = 0;
  let c = 0;
  for (let ai = 0; ai < nAz; ai++) {
    const b0 = ai * nRange;
    const b1 = ((ai + 1) % nAz) * nRange;
    const az0 = azimuth[ai] * DEG2RAD;
    const az1 = (ai + 1 < nAz ? azimuth[ai + 1] : azimuth[0] + 360) * DEG2RAD;
    const s0 = Math.sin(az0), c0 = Math.cos(az0);
    const s1 = Math.sin(az1), c1 = Math.cos(az1);

    for (let ri = 0; ri < nCols; ri++) {
      let v00 = values[b0 + ri];
      let v01 = values[b0 + ri + 1];
      let v10 = values[b1 + ri];
      let v11 = values[b1 + ri + 1];
      if (!(v00 > NODATA_LIMIT) && !(v01 > NODATA_LIMIT) && !(v10 > NODATA_LIMIT) && !(v11 > NODATA_LIMIT)) continue;
      // Normalise NaN / sentinel corners so the shader sees a clean no-data value.
      if (!(v00 > NODATA_LIMIT)) v00 = -9999;
      if (!(v01 > NODATA_LIMIT)) v01 = -9999;
      if (!(v10 > NODATA_LIMIT)) v10 = -9999;
      if (!(v11 > NODATA_LIMIT)) v11 = -9999;

      const r0 = range[ri];
      const r1 = range[ri + 1];
      const x00 = r0 * s0, y00 = r0 * c0;
      const x01 = r1 * s0, y01 = r1 * c0;
      const x10 = r0 * s1, y10 = r0 * c1;
      const x11 = r1 * s1, y11 = r1 * c1;

      positions[p] = x00; positions[p + 1] = y00;
      positions[p + 2] = x01; positions[p + 3] = y01;
      positions[p + 4] = x10; positions[p + 5] = y10;
      positions[p + 6] = x01; positions[p + 7] = y01;
      positions[p + 8] = x11; positions[p + 9] = y11;
      positions[p + 10] = x10; positions[p + 11] = y10;

      polars.set(SMOOTH_POLAR, p);

      const v = c * 4; // 6 vertices * 4 floats
      for (let k = 0; k < 6; k++) {
        const o = v + k * 4;
        values4[o] = v00; values4[o + 1] = v01; values4[o + 2] = v10; values4[o + 3] = v11;
      }

      p += 12;
      c += 6;
    }
  }

  return { positions, polars, values4, vertexCount };
}
