/**
 * Tree damage along the track (pure, deterministic; tested in treeDamage.test.mjs).
 *
 * The vortex axis moves on the straight survey line at constant speed, so for every tree the time and
 * distance of the closest approach are analytic. The peak wind it feels is the vortex swirl at that distance
 * (near the ground); a tree whose peak exceeds its failure speed (hardwoods snap or uproot near 33 m/s, pines
 * near 30 m/s, in the range of the EF0/EF1 tree damage indicators, at crown height) goes down as the vortex passes, in the direction of the wind
 * at that moment. Trees farther out only lean with the instantaneous wind. This is MODELLED damage, a
 * consequence of the modelled wind field, not the surveyed damage at that place.
 */

import { windAt, tangential } from './vortexField.ts';
import { axisAt, intensityAt, type ResolvedSeed } from './seed.ts';

export interface TreeState {
  /** Tilt angle (rad) of the whole tree about its base: 0 upright, ~1.45 lying. */
  tilt: number;
  /** Horizontal direction of the fall (unit). */
  dir: [number, number];
  /** 0..1: how far the tree is toward its fallen state. */
  fall: number;
  /** Peak wind (m/s) at the tree. */
  peak: number;
}

export const FAIL_SPEED = { hardwood: 33, pine: 30 };

const smooth = (x: number) => {
  const c = Math.max(0, Math.min(1, x));
  return c * c * (3 - 2 * c);
};

export function treeState(r: ResolvedSeed, x: number, y: number, pine: boolean, t: number, out: TreeState = { tilt: 0, dir: [1, 0], fall: 0, peak: 0 }): TreeState {
  const a = r.track[0];
  const b = r.track[r.track.length - 1];
  const dx = b.x - a.x, dy = b.y - a.y;
  const L2 = dx * dx + dy * dy;
  const s = Math.max(0, Math.min(1, ((x - a.x) * dx + (y - a.y) * dy) / L2));
  const px = a.x + s * dx, py = a.y + s * dy;
  const d = Math.hypot(x - px, y - py);
  const tc = a.t + s * (b.t - a.t);
  const peak = tangential(r.vortex, d, 15);
  out.peak = peak;
  const thr = pine ? FAIL_SPEED.pine : FAIL_SPEED.hardwood;
  // instantaneous lean from the wind now (applied by the shader as well); here only the fall state
  const ax = axisAt(r, t);
  const rel = [x - ax.x, y - ax.y];
  const inten = intensityAt(r, t);
  const w = windAt(r.vortex, rel[0], rel[1], 15);
  const sp = Math.hypot(w[0], w[1]) * inten;
  if (peak > thr) {
    const k = smooth((t - (tc - 1.2)) / 2.4);
    out.fall = k;
    out.tilt = 1.45 * k;
    // fall direction: the wind at the closest approach plus the translation
    const axC = axisAt(r, tc);
    const wc = windAt(r.vortex, x - axC.x, y - axC.y, 15);
    let fx = wc[0] + r.translation[0] * 0.6, fy = wc[1] + r.translation[1] * 0.6;
    const l = Math.hypot(fx, fy) || 1;
    fx /= l; fy /= l;
    out.dir[0] = fx; out.dir[1] = fy;
  } else {
    out.fall = 0;
    out.tilt = 0; // the shader leans it with the wind
    out.dir[0] = 0; out.dir[1] = 0;
  }
  void sp;
  return out;
}
