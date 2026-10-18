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

/** A step up to this many times the typical spacing counts as a normal, continuous step. */
const BRIDGE_FACTOR = 1.5;
/**
 * Missing rays (a hole in the azimuth list) are filled by splitting the hole between the two
 * neighbours, like a nearest-neighbour plot, up to this many typical steps (and at most 10 deg).
 * Anything bigger stays empty instead of smearing a ray across a wide wedge.
 */
const MAX_FILL_STEPS = 8;
const MAX_FILL_DEG = 10;

/** Wrap an angle difference into (-180, 180]. */
function wrap180(d: number): number {
  return (((d % 360) + 540) % 360) - 180;
}

/**
 * How the rays of a sweep are laid out: the typical spacing, the scan direction and whether the
 * sweep closes into a full circle. A sector scan (e.g. 50 deg to 180 deg) is NOT closed: its last ray
 * must not be stretched around to the first one, and a hole in the azimuth list (lost rays) must
 * stay a hole instead of smearing the previous ray across it.
 */
interface RayLayout {
  /** Typical angular step between rays, degrees (>0). */
  step: number;
  /** +1 when azimuth increases with the ray index, -1 when it decreases. */
  dir: 1 | -1;
  /** True when the last ray is followed by the first one (a full 360 deg sweep). */
  closed: boolean;
}

export function rayLayout(azimuth: Float32Array, nAz: number): RayLayout {
  if (nAz < 2) return { step: 1, dir: 1, closed: false };
  const mags = new Float64Array(nAz - 1);
  let signSum = 0;
  for (let i = 0; i < nAz - 1; i++) {
    const d = wrap180(azimuth[i + 1] - azimuth[i]);
    mags[i] = Math.abs(d);
    signSum += Math.sign(d);
  }
  const sorted = Float64Array.from(mags).sort();
  let step = sorted[Math.floor((sorted.length - 1) / 2)];
  if (!(step > 0)) step = sorted.find((m) => m > 0) ?? 1;
  const dir: 1 | -1 = signSum >= 0 ? 1 : -1;
  const back = wrap180(azimuth[0] - azimuth[nAz - 1]) * dir;
  return { step, dir, closed: back > 0 && back <= BRIDGE_FACTOR * step };
}

/**
 * Angular step (degrees, always >= 0) from ray `ai` to the next ray, or -1 when no cell should be
 * drawn there (end of an open sector, or a hole too big to fill).
 */
function stepToNext(azimuth: Float32Array, nAz: number, ai: number, lay: RayLayout): number {
  let next = ai + 1;
  if (next >= nAz) {
    if (!lay.closed) return -1;
    next = 0;
  }
  const d = Math.abs(wrap180(azimuth[next] - azimuth[ai]));
  const limit = Math.max(BRIDGE_FACTOR * lay.step, Math.min(MAX_FILL_DEG, MAX_FILL_STEPS * lay.step));
  return d <= limit ? d : -1;
}

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
  const lay = rayLayout(azimuth, nAz);

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
    // The wedge is centred on the ray's azimuth and reaches half-way to each neighbour. Next to a
    // hole, or at the end of a sector scan, it is only half a normal ray wide.
    const toNext = stepToNext(azimuth, nAz, ai, lay);
    const prevIdx = ai > 0 ? ai - 1 : nAz - 1;
    const fromPrev = ai > 0 || lay.closed ? stepToNext(azimuth, nAz, prevIdx, lay) : -1;
    const halfNext = (toNext >= 0 ? toNext : lay.step) / 2;
    const halfPrev = (fromPrev >= 0 ? fromPrev : lay.step) / 2;
    const az0 = (azimuth[ai] - lay.dir * halfPrev) * DEG2RAD;
    const az1 = (azimuth[ai] + lay.dir * halfNext) * DEG2RAD;
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

  const lay = rayLayout(azimuth, nAz);
  let cells = 0;
  for (let ai = 0; ai < nAz; ai++) {
    if (stepToNext(azimuth, nAz, ai, lay) < 0) continue;
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
    const toNext = stepToNext(azimuth, nAz, ai, lay);
    if (toNext < 0) continue; // end of a sector scan, or a hole in the rays: draw nothing
    const b0 = ai * nRange;
    const b1 = ((ai + 1) % nAz) * nRange;
    const az0 = azimuth[ai] * DEG2RAD;
    const az1 = (azimuth[ai] + lay.dir * toNext) * DEG2RAD;
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
