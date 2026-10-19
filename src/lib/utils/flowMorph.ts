/**
 * Flow for the time interpolation of the storm clip: observed winds in, storm-relative flow out,
 * and a CPU reference of the interpolation maths (the GPU pass is morphPass.ts).
 *
 * Why storm-relative. A retrieved wind field is ground-relative. The echo of a storm does not move
 * with the local wind everywhere (it is carried by the storm motion, which the tracker measures, and
 * the echo is a pattern that propagates and evolves). What the winds add, and what a track cannot
 * give, is the turning and shearing of the echo: the rotation of a mesocyclone or of a tropical
 * cyclone's eyewall. So the velocity used to carry the echo is the tracked translation plus the
 * observed wind minus its mean over the echo at the same height (the mean of a rotating flow is its
 * translation). Where no wind was retrieved the anomaly is zero and the echo is only translated.
 */

export interface WindStep {
  nx: number;
  ny: number;
  nz: number;
  /** Position of cell (0, 0, 0) and cell size, km, in the wind grid's own frame. */
  x0: number;
  y0: number;
  z0: number;
  dx: number;
  dy: number;
  dz: number;
  /** (nz, ny, nx) m/s, NaN = no wind. */
  u: Float32Array;
  v: Float32Array;
  originLat: number;
  originLon: number;
  nRadars?: number;
  validFraction?: number;
}

/** A wind step from the bundle: JSON header and the float32 bytes (u block then v block). */
export function decodeWindStep(header: any, bytes: Uint8Array): WindStep | null {
  const nx = header.nx | 0, ny = header.ny | 0, nz = header.nz | 0;
  const n = nx * ny * nz;
  if (!(n > 0) || bytes.byteLength !== n * 8) return null;
  const buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  const f = new Float32Array(buf);
  return {
    nx, ny, nz, x0: header.x0_km, y0: header.y0_km, z0: header.z0_km, dx: header.dx_km, dy: header.dy_km, dz: header.dz_km,
    u: f.subarray(0, n), v: f.subarray(n, 2 * n),
    originLat: header.origin?.lat ?? 0, originLon: header.origin?.lon ?? 0,
    nRadars: header.n_radars, validFraction: header.valid_fraction,
  };
}

export interface MorphField {
  nx: number;
  ny: number;
  nz: number;
  origin: [number, number, number];
  cell: [number, number, number];
  /** 4 floats per cell: storm-relative u, v (m/s), 0, confidence (1 where a wind exists). */
  data: Float32Array;
  /** Mean wind (m/s) removed at every level (the translation of the echo as the winds see it). */
  mean: [number, number][];
  validCells: number;
}

/**
 * Storm-relative flow of a wind step. `isEcho(x, y, z)` says whether the echo is strong enough there
 * (coordinates km in the wind frame) for the cell to count in the level mean; with fewer than 20 such
 * cells in a level the mean over all valid cells of the level is used.
 */
export function stormRelativeField(w: WindStep, isEcho: (x: number, y: number, z: number) => boolean): MorphField {
  const { nx, ny, nz } = w;
  const data = new Float32Array(nx * ny * nz * 4);
  const mean: [number, number][] = [];
  let valid = 0;
  for (let k = 0; k < nz; k++) {
    let su = 0, sv = 0, n = 0, au = 0, av = 0, an = 0;
    const z = w.z0 + k * w.dz;
    for (let j = 0; j < ny; j++) {
      for (let i = 0; i < nx; i++) {
        const idx = (k * ny + j) * nx + i;
        const u = w.u[idx], v = w.v[idx];
        if (!Number.isFinite(u) || !Number.isFinite(v)) continue;
        au += u; av += v; an++;
        if (isEcho(w.x0 + i * w.dx, w.y0 + j * w.dy, z)) { su += u; sv += v; n++; }
      }
    }
    mean.push(n >= 20 ? [su / n, sv / n] : an ? [au / an, av / an] : [0, 0]);
  }
  for (let k = 0; k < nz; k++) {
    const [mu, mv] = mean[k];
    for (let j = 0; j < ny; j++) {
      for (let i = 0; i < nx; i++) {
        const idx = (k * ny + j) * nx + i;
        const u = w.u[idx], v = w.v[idx];
        const o = idx * 4;
        if (Number.isFinite(u) && Number.isFinite(v)) {
          data[o] = u - mu;
          data[o + 1] = v - mv;
          data[o + 3] = 1;
          valid++;
        }
      }
    }
  }
  return { nx, ny, nz, origin: [w.x0, w.y0, w.z0], cell: [w.dx, w.dy, w.dz], data, mean, validCells: valid };
}

// ─── CPU reference of the interpolation (mirrors morphPass.ts) ──────────────

export type Vec2 = [number, number];
/** Velocity (km/s) at a position (km). */
export type VelocityField = (x: number, y: number, z: number) => Vec2;

/** Position at the time of A: back along the flow for `t` seconds in `steps` midpoint steps. */
export function traceBack(p: [number, number, number], vel: VelocityField, t: number, steps = 4): [number, number] {
  let x = p[0], y = p[1];
  const h = t / steps;
  for (let i = 0; i < steps; i++) {
    const v1 = vel(x, y, p[2]);
    const v2 = vel(x - 0.5 * v1[0] * h, y - 0.5 * v1[1] * h, p[2]);
    x -= v2[0] * h;
    y -= v2[1] * h;
  }
  return [x, y];
}

/** Position at the time of B: forward along the flow for `t` seconds. */
export function traceForward(p: [number, number, number], vel: VelocityField, t: number, steps = 4): [number, number] {
  let x = p[0], y = p[1];
  const h = t / steps;
  for (let i = 0; i < steps; i++) {
    const v1 = vel(x, y, p[2]);
    const v2 = vel(x + 0.5 * v1[0] * h, y + 0.5 * v1[1] * h, p[2]);
    x += v2[0] * h;
    y += v2[1] * h;
  }
  return [x, y];
}

/** A field value (0..1) and its coverage (0..1) at a position; null = no data. */
export type Sampler = (x: number, y: number, z: number) => { val: number; cov: number } | null;

/**
 * Interpolated value and coverage of one cell at position `s` (0 = A, 1 = B) between volumes that are
 * `dt` seconds apart: A read where the cell came from, B where it is going, coverage-weighted blend.
 */
export function morphCell(a: Sampler, b: Sampler, p: [number, number, number], s: number, dt: number, vel: VelocityField, steps = 4): { val: number; cov: number } | null {
  const pa = traceBack(p, vel, s * dt, steps);
  const pb = traceForward(p, vel, (1 - s) * dt, steps);
  const sa = a(pa[0], pa[1], p[2]);
  const sb = b(pb[0], pb[1], p[2]);
  const wa = sa ? (1 - s) * sa.cov : 0;
  const wb = sb ? s * sb.cov : 0;
  const ss = wa + wb;
  if (ss <= 0.002) return null;
  return { val: (wa * (sa?.val ?? 0) + wb * (sb?.val ?? 0)) / ss, cov: ss };
}

/** Solid-body rotation about (cx, cy) with angular rate `omega` (rad/s, counter-clockwise), in km/s. */
export function solidBodyRotation(cx: number, cy: number, omega: number): VelocityField {
  return (x, y) => [-omega * (y - cy), omega * (x - cx)];
}
