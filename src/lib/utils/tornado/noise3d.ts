/**
 * Tileable 3D noise volume for the tornado renderer (deterministic, CPU generated once).
 *
 * RGBA8, size^3 texels, all channels periodic in all axes so the texture can use REPEAT wrapping:
 *   R  gradient noise fbm (4 octaves, base period 4)           soft billows
 *   G  inverted Worley F1 (period 6)                           cauliflower cells
 *   B  inverted Worley F1 (period 12) mixed with F2-F1         fine cellular detail
 *   A  gradient noise (period 8), single octave                warp / curl source
 */

function hash3(x: number, y: number, z: number, seed: number): number {
  let h = (Math.imul(x | 0, 374761393) ^ Math.imul(y | 0, 668265263) ^ Math.imul(z | 0, 2147483647) ^ Math.imul(seed | 0, 1274126177)) >>> 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177) >>> 0;
  h = Math.imul(h ^ (h >>> 16), 2246822519) >>> 0;
  h ^= h >>> 13;
  return (h >>> 0) / 4294967296;
}

function gradDot(ix: number, iy: number, iz: number, period: number, seed: number, dx: number, dy: number, dz: number): number {
  const px = ((ix % period) + period) % period;
  const py = ((iy % period) + period) % period;
  const pz = ((iz % period) + period) % period;
  // random unit-ish gradient from the hash (three independent hashes)
  const gx = hash3(px, py, pz, seed) * 2 - 1;
  const gy = hash3(px, py, pz, seed + 101) * 2 - 1;
  const gz = hash3(px, py, pz, seed + 202) * 2 - 1;
  const l = Math.hypot(gx, gy, gz) || 1;
  return (gx * dx + gy * dy + gz * dz) / l;
}

const fade = (t: number) => t * t * t * (t * (t * 6 - 15) + 10);

/** Periodic gradient noise at lattice coordinates (x, y, z) with the given period; roughly -0.9..0.9. */
export function gradientNoise(x: number, y: number, z: number, period: number, seed: number): number {
  const x0 = Math.floor(x), y0 = Math.floor(y), z0 = Math.floor(z);
  const fx = x - x0, fy = y - y0, fz = z - z0;
  const u = fade(fx), v = fade(fy), w = fade(fz);
  const g = (a: number, b: number, c: number) => gradDot(x0 + a, y0 + b, z0 + c, period, seed, fx - a, fy - b, fz - c);
  const x00 = g(0, 0, 0) + u * (g(1, 0, 0) - g(0, 0, 0));
  const x10 = g(0, 1, 0) + u * (g(1, 1, 0) - g(0, 1, 0));
  const x01 = g(0, 0, 1) + u * (g(1, 0, 1) - g(0, 0, 1));
  const x11 = g(0, 1, 1) + u * (g(1, 1, 1) - g(0, 1, 1));
  const y0v = x00 + v * (x10 - x00);
  const y1v = x01 + v * (x11 - x01);
  return y0v + w * (y1v - y0v);
}

/** Periodic Worley noise: returns [F1, F2] distances (in cell units) at lattice coordinates, period in cells. */
export function worley(x: number, y: number, z: number, period: number, seed: number): [number, number] {
  const x0 = Math.floor(x), y0 = Math.floor(y), z0 = Math.floor(z);
  let f1 = 9, f2 = 9;
  for (let k = -1; k <= 1; k++) {
    for (let j = -1; j <= 1; j++) {
      for (let i = -1; i <= 1; i++) {
        const cx = x0 + i, cy = y0 + j, cz = z0 + k;
        const px = ((cx % period) + period) % period;
        const py = ((cy % period) + period) % period;
        const pz = ((cz % period) + period) % period;
        const ox = cx + hash3(px, py, pz, seed);
        const oy = cy + hash3(px, py, pz, seed + 7);
        const oz = cz + hash3(px, py, pz, seed + 13);
        const d = Math.hypot(ox - x, oy - y, oz - z);
        if (d < f1) { f2 = f1; f1 = d; } else if (d < f2) f2 = d;
      }
    }
  }
  return [f1, f2];
}

/** Build the noise volume (size^3 RGBA8). */
export function buildNoiseVolume(size = 64, seed = 4242): Uint8Array {
  const out = new Uint8Array(size * size * size * 4);
  let o = 0;
  const clamp = (v: number) => Math.max(0, Math.min(255, Math.round(v * 255)));
  for (let z = 0; z < size; z++) {
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const u = x / size, v = y / size, w = z / size;
        // R: fbm of gradient noise
        let amp = 0.5, sum = 0, norm = 0;
        for (let oct = 0; oct < 4; oct++) {
          const per = 4 << oct;
          sum += amp * gradientNoise(u * per, v * per, w * per, per, seed + oct * 31);
          norm += amp;
          amp *= 0.5;
        }
        const r = 0.5 + (sum / norm) * 0.95;
        // G, B: Worley
        const [a1] = worley(u * 6, v * 6, w * 6, 6, seed + 5);
        const [b1, b2] = worley(u * 12, v * 12, w * 12, 12, seed + 9);
        const g = 1 - Math.min(1, a1 * 1.1);
        const b = 0.65 * (1 - Math.min(1, b1 * 1.2)) + 0.35 * Math.min(1, (b2 - b1) * 1.4);
        const a = 0.5 + 0.95 * gradientNoise(u * 8, v * 8, w * 8, 8, seed + 77);
        out[o++] = clamp(r);
        out[o++] = clamp(g);
        out[o++] = clamp(b);
        out[o++] = clamp(a);
      }
    }
  }
  return out;
}
