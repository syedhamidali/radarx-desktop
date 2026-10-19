/**
 * Procedural rural landscape (fallback ground when no satellite imagery is in the tile cache).
 *
 * It is NOT the real land cover of the place: a stylised, deterministic Mississippi / Alabama farmland
 * of pasture, hay, ploughed fields, woodland, tree lines along field edges, section-line roads, yards and
 * farm buildings (the damage survey of the case mentions chicken houses). It only gives the eye the scale cues
 * a flat plane lacks. With imagery present the colour comes from the satellite image and only the
 * trees are (re)derived from it (see `forestFromImagery`).
 *
 * Output raster (RGBA8, size^2, row 0 = south edge, covering [cx-half, cx+half]^2 in local metres):
 *   R  land class: 0 pasture, 1 hay, 2 ploughed, 3 green crop, 4 woodland, 5 pond / wet, 6 yard
 *   G  per-field variation 0..255 (hue / row angle)
 *   B  road mask 0..255 (soft edge); >= 128 inside the road
 *   A  tree density 0..255 (woodland and tree lines)
 */

import { gradientNoise } from './noise3d.ts';

export const LAND = { PASTURE: 0, HAY: 1, PLOUGHED: 2, CROP: 3, WOOD: 4, WET: 5, YARD: 6 } as const;

function h2(x: number, y: number, seed: number): number {
  let h = (Math.imul(x | 0, 374761393) ^ Math.imul(y | 0, 668265263) ^ Math.imul(seed | 0, 1274126177)) >>> 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177) >>> 0;
  h = Math.imul(h ^ (h >>> 16), 2246822519) >>> 0;
  h ^= h >>> 13;
  return (h >>> 0) / 4294967296;
}

export interface LandscapeOptions {
  cx: number;
  cy: number;
  /** Half width of the raster (m). */
  half: number;
  size?: number;
  seed?: number;
  /** Where trees and buildings are generated (metres, local frame) and how far around it. */
  focus?: [number, number];
  treeRadius?: number;
  /** Cell spacing of the field pattern (m). */
  fieldSize?: number;
}

export interface Landscape {
  cx: number;
  cy: number;
  half: number;
  size: number;
  data: Uint8Array;
  /** x, y, height, crown radius, variation, species (0 hardwood, 1 pine) per tree. */
  trees: Float32Array;
  /** x, y, width, depth, wall height, rotation (rad), kind (0 house, 1 barn, 2 chicken house, 3 shed), tone. */
  buildings: Float32Array;
  /** Roads as line segments x0, y0, x1, y1, width (for power poles). */
  poles: Float32Array;
  seed: number;
}

export function buildLandscape(o: LandscapeOptions): Landscape {
  const size = o.size ?? 1024;
  const seed = o.seed ?? 20220330;
  const S = o.fieldSize ?? 360;
  const half = o.half;
  const data = new Uint8Array(size * size * 4);
  const px = (2 * half) / size;
  const x0 = o.cx - half, y0 = o.cy - half;
  const sec = 1609; // section-line road spacing (one mile)

  // woodland fraction, low frequency
  const wood = (x: number, y: number) => 0.5 + 0.5 * gradientNoise(x / 2600 + 100.3, y / 2600 + 50.7, 0.5, 4096, seed + 3);
  const roadWarp = (x: number, y: number) => 60 * gradientNoise(x / 900 + 7.1, y / 900 + 3.3, 1.5, 4096, seed + 5);

  for (let j = 0; j < size; j++) {
    const y = y0 + (j + 0.5) * px;
    for (let i = 0; i < size; i++) {
      const x = x0 + (i + 0.5) * px;
      // warped Voronoi cell
      const wx = x + 90 * gradientNoise(x / 700 + 11, y / 700 + 17, 2.5, 4096, seed + 11);
      const wy = y + 90 * gradientNoise(x / 700 + 23, y / 700 + 29, 3.5, 4096, seed + 13);
      const gx = Math.floor(wx / S), gy = Math.floor(wy / S);
      let d1 = 1e9, d2 = 1e9, idx = 0, idy = 0;
      for (let k = -1; k <= 1; k++) {
        for (let l = -1; l <= 1; l++) {
          const cx = gx + l, cy = gy + k;
          const sx = (cx + 0.15 + 0.7 * h2(cx, cy, seed + 1)) * S;
          const sy = (cy + 0.15 + 0.7 * h2(cx, cy, seed + 2)) * S;
          const d = Math.hypot(wx - sx, wy - sy);
          if (d < d1) { d2 = d1; d1 = d; idx = cx; idy = cy; } else if (d < d2) d2 = d;
        }
      }
      const border = (d2 - d1) * 0.5;
      const hv = h2(idx, idy, seed + 3);
      const hv2 = h2(idx, idy, seed + 4);
      const wd = wood(x, y);
      let cls: number;
      if (hv < 0.12 + 0.62 * Math.max(0, wd - 0.28)) cls = LAND.WOOD;
      else if (hv2 < 0.5) cls = LAND.PASTURE;
      else if (hv2 < 0.68) cls = LAND.HAY;
      else if (hv2 < 0.82) cls = LAND.PLOUGHED;
      else if (hv2 < 0.97) cls = LAND.CROP;
      else cls = LAND.WET;
      // tree lines along some field borders
      let tree = 0;
      if (cls === LAND.WOOD) tree = 215 + 40 * h2(idx, idy, seed + 6);
      else if (border < 5 && h2(Math.min(idx, idy), Math.max(idx, idy) + 13, seed + 8) < 0.45) tree = 230;
      else if (border < 9 && h2(Math.min(idx, idy), Math.max(idx, idy) + 13, seed + 8) < 0.45) tree = 120;
      // roads on warped section lines
      const rx = x + roadWarp(x, y), ry = y + roadWarp(y, x);
      const ix = Math.floor((rx + sec * 0.37) / sec), iy = Math.floor((ry + sec * 0.61) / sec);
      const hasX = h2(ix, 91, seed + 20) < 0.7; // a road along y on this x line
      const hasY = h2(iy, 93, seed + 21) < 0.7;
      const wxR = h2(ix, 7, seed + 22) < 0.2 ? 7 : 4.5; // paved highway wider
      const wyR = h2(iy, 8, seed + 23) < 0.2 ? 7 : 4.5;
      const dLineX = (((rx + sec * 0.37) % sec) + sec) % sec;
      const dX = Math.min(dLineX, sec - dLineX);
      const dLineY = (((ry + sec * 0.61) % sec) + sec) % sec;
      const dY = Math.min(dLineY, sec - dLineY);
      let m = 0;
      if (hasX) m = Math.max(m, Math.min(1, Math.max(0, (wxR - dX) / 1.4 + 0.5)));
      if (hasY) m = Math.max(m, Math.min(1, Math.max(0, (wyR - dY) / 1.4 + 0.5)));
      if (m > 0.02) { tree = Math.min(tree, 40); if (m > 0.5 && cls === LAND.WOOD) cls = LAND.PASTURE; }
      const o4 = (j * size + i) * 4;
      data[o4] = cls;
      data[o4 + 1] = Math.floor(255 * h2(idx, idy, seed + 9));
      data[o4 + 2] = Math.round(255 * m);
      data[o4 + 3] = Math.round(tree * (1 - m));
    }
  }

  const L: Landscape = { cx: o.cx, cy: o.cy, half, size, data, trees: new Float32Array(0), buildings: new Float32Array(0), poles: new Float32Array(0), seed };
  const focus = o.focus ?? [o.cx, o.cy];
  placeFeatures(L, focus[0], focus[1], o.treeRadius ?? 1100);
  return L;
}

/** Raster lookup (nearest), returns [class, var, road, tree] or null outside. */
export function sampleLand(L: Landscape, x: number, y: number): [number, number, number, number] | null {
  const u = (x - (L.cx - L.half)) / (2 * L.half);
  const v = (y - (L.cy - L.half)) / (2 * L.half);
  if (u < 0 || u >= 1 || v < 0 || v >= 1) return null;
  const i = Math.floor(u * L.size), j = Math.floor(v * L.size);
  const o = (j * L.size + i) * 4;
  return [L.data[o], L.data[o + 1], L.data[o + 2], L.data[o + 3]];
}

/** Place trees, farm buildings and poles around a focus point from the raster. */
export function placeFeatures(L: Landscape, fx: number, fy: number, radius: number, density = 1): void {
  const seed = L.seed;
  const trees: number[] = [];
  const step = 11;
  const gx0 = Math.floor((fx - radius) / step), gx1 = Math.ceil((fx + radius) / step);
  const gy0 = Math.floor((fy - radius) / step), gy1 = Math.ceil((fy + radius) / step);
  for (let gy = gy0; gy <= gy1; gy++) {
    for (let gx = gx0; gx <= gx1; gx++) {
      const x = (gx + h2(gx, gy, seed + 31)) * step;
      const y = (gy + h2(gx, gy, seed + 32)) * step;
      if (Math.hypot(x - fx, y - fy) > radius) continue;
      const s = sampleLand(L, x, y);
      if (!s) continue;
      const [cls, vr, , t] = s;
      if (t < 60) continue;
      const p = (t / 255) * density;
      if (h2(gx, gy, seed + 33) > p * 0.92) continue;
      const pine = cls === LAND.WOOD ? (vr < 90 ? 1 : 0) : 0;
      const hgt = pine ? 15 + 12 * h2(gx, gy, seed + 34) : 11 + 11 * h2(gx, gy, seed + 34);
      const crown = pine ? 2.4 + 1.4 * h2(gx, gy, seed + 35) : 4.2 + 3.2 * h2(gx, gy, seed + 35);
      trees.push(x, y, hgt, crown, h2(gx, gy, seed + 36), pine);
    }
  }
  L.trees = new Float32Array(trees);

  // farm buildings: a few compounds near the focus, at pasture / yard cells near roads
  const b: number[] = [];
  const poles: number[] = [];
  const nC = 9;
  for (let c = 0; c < nC; c++) {
    const ang = h2(c, 1, seed + 40) * Math.PI * 2;
    const rad = Math.sqrt(h2(c, 2, seed + 40)) * radius * 0.95;
    let x = fx + Math.cos(ang) * rad, y = fy + Math.sin(ang) * rad;
    // snap near a road if one is within 120 m
    let best: [number, number] | null = null;
    for (let k = 0; k < 40 && !best; k++) {
      const tx = x + (h2(c, k, seed + 41) - 0.5) * 240, ty = y + (h2(c, k, seed + 42) - 0.5) * 240;
      const s = sampleLand(L, tx, ty);
      if (s && s[2] < 5 && s[0] !== LAND.WOOD && s[0] !== LAND.WET) {
        // a road within 30 m?
        for (const [ox, oy] of [[30, 0], [-30, 0], [0, 30], [0, -30]]) {
          const q = sampleLand(L, tx + ox, ty + oy);
          if (q && q[2] > 128) { best = [tx, ty]; break; }
        }
      }
    }
    if (!best) continue;
    [x, y] = best;
    const rot = (Math.floor(h2(c, 3, seed + 43) * 4) * Math.PI) / 2 + (h2(c, 4, seed + 43) - 0.5) * 0.2;
    const kind = h2(c, 5, seed + 44);
    // the house
    b.push(x, y, 11 + 4 * h2(c, 6, seed + 45), 8 + 3 * h2(c, 7, seed + 45), 3.2, rot, 0, h2(c, 8, seed + 45));
    // barn / sheds
    b.push(x + Math.cos(rot) * 38, y + Math.sin(rot) * 38, 12, 20, 5.5, rot, 1, h2(c, 9, seed + 45));
    b.push(x - Math.sin(rot) * 30, y + Math.cos(rot) * 30, 6, 7, 2.8, rot + 0.3, 3, h2(c, 10, seed + 45));
    if (kind < 0.55) {
      // chicken houses: long low sheds in a row, metal roofs
      const nh = 3 + Math.floor(h2(c, 11, seed + 46) * 3);
      for (let k = 0; k < nh; k++) {
        const ox = -Math.sin(rot) * (60 + k * 26), oy = Math.cos(rot) * (60 + k * 26);
        b.push(x + ox + Math.cos(rot) * 20, y + oy + Math.sin(rot) * 20, 14, 120, 3.8, rot + Math.PI / 2, 2, h2(c, 12 + k, seed + 46));
      }
    }
  }
  L.buildings = new Float32Array(b);

  // poles every 55 m along roads near the focus (line scan on the raster)
  for (let gy = gy0; gy <= gy1; gy += 5) {
    for (let gx = gx0; gx <= gx1; gx += 5) {
      const x = gx * step, y = gy * step;
      if (Math.hypot(x - fx, y - fy) > radius) continue;
      const s = sampleLand(L, x, y);
      if (s && s[2] > 160 && h2(gx, gy, seed + 50) < 0.5) poles.push(x + 5, y + 5, 9 + 2 * h2(gx, gy, seed + 51));
    }
  }
  L.poles = new Float32Array(poles);
}

/** Tree density map from satellite imagery (RGBA8): dark, green-dominant pixels are woodland. Replaces channel A. */
export function forestFromImagery(L: Landscape, rgba: Uint8ClampedArray | Uint8Array, imgSize: number, imgHalf: number, imgCx: number, imgCy: number): void {
  for (let j = 0; j < L.size; j++) {
    for (let i = 0; i < L.size; i++) {
      const x = L.cx - L.half + ((i + 0.5) / L.size) * 2 * L.half;
      const y = L.cy - L.half + ((j + 0.5) / L.size) * 2 * L.half;
      const u = (x - (imgCx - imgHalf)) / (2 * imgHalf), v = (y - (imgCy - imgHalf)) / (2 * imgHalf);
      const o4 = (j * L.size + i) * 4;
      if (u < 0 || u >= 1 || v < 0 || v >= 1) continue;
      const ii = Math.floor(u * imgSize), jj = Math.floor((1 - v) * imgSize); // imagery rows run north to south
      const q = (jj * imgSize + ii) * 4;
      const r = rgba[q], g = rgba[q + 1], b = rgba[q + 2];
      const lum = (r + g + b) / 3;
      const greenish = g > r * 1.02 && g > b * 1.05;
      const dark = lum < 85;
      L.data[o4 + 3] = greenish && dark ? 220 : greenish && lum < 105 ? 90 : 0;
      L.data[o4] = greenish && dark ? LAND.WOOD : L.data[o4];
    }
  }
}

/** Remove trees, buildings and poles within `r` metres of a point (the observer's standing place). */
export function clearAround(L: Landscape, x: number, y: number, r: number): void {
  const keep = (arr: Float32Array, stride: number): Float32Array => {
    const out: number[] = [];
    for (let i = 0; i < arr.length; i += stride) {
      if (Math.hypot(arr[i] - x, arr[i + 1] - y) < r) continue;
      for (let k = 0; k < stride; k++) out.push(arr[i + k]);
    }
    return new Float32Array(out);
  };
  L.trees = keep(L.trees, 6);
  L.buildings = keep(L.buildings, 8);
  L.poles = keep(L.poles, 3);
}
