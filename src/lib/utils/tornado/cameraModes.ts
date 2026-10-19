/**
 * First-person camera modes of the tornado simulation (pure functions of the resolved seed and the time).
 *
 *   chaser  : 300-1000 m from the vortex with the funnel ahead, a standing observer, slow handheld sway
 *   debris  : 60-150 m, the wall passes, debris flies past the lens, shake from the local wind (bounded)
 *   inside  : in the clear core looking up (physically lethal, labelled "simulated view")
 *
 * Every mode returns a pose for a given shot time `tau` (seconds from the start of the shot) and the
 * data time `t`; they use smoothstep easing, deterministic noise (sums of sines) and a hard cap on the shake
 * angle so the clips are comfortable to watch (no flashing, no violent jumps).
 */

import { axisAt, intensityAt, type ResolvedSeed } from './seed.ts';
import { horizontalSpeed } from './vortexField.ts';
import { windField } from './debris.ts';
import type { TornadoCamera } from './TornadoScene.ts';
import type { Vec3 } from './glUtil.ts';

export type CameraMode = 'chaser' | 'debris' | 'inside';

const smooth = (x: number) => {
  const c = Math.max(0, Math.min(1, x));
  return c * c * (3 - 2 * c);
};
const lerp = (a: number, b: number, k: number) => a + (b - a) * k;

/** Smooth deterministic noise in [-1, 1]: a sum of incommensurate sines. */
export function sway(t: number, seed: number): number {
  return (
    0.5 * Math.sin(t * 1.31 + seed * 12.9898) +
    0.3 * Math.sin(t * 2.77 + seed * 78.233) +
    0.2 * Math.sin(t * 5.43 + seed * 37.719)
  );
}

export interface ModeOptions {
  /** Duration of the shot (s), for the easing. */
  duration: number;
  /** Ground height lookup (m) at a local position. */
  groundZ?: (x: number, y: number) => number;
  /** Which side of the track the observer is on: +1 left of the direction of travel, -1 right. Default -1 (right, south-east). */
  side?: 1 | -1;
}

export interface ModePose extends TornadoCamera {
  /** Local wind speed at the camera (m/s) and the shake angle used (deg), for the report. */
  windAtCamera: number;
  shakeDeg: number;
  /** Distance to the vortex axis (m). */
  distance: number;
}

function unit(r: ResolvedSeed): { u: [number, number]; n: [number, number] } {
  const s = r.speed || 1;
  const u: [number, number] = [r.translation[0] / s, r.translation[1] / s];
  return { u, n: [-u[1], u[0]] }; // n = left of the direction of travel
}

/** Camera pose of a mode at shot time tau (s) for data time t. */
export function modePose(mode: CameraMode, r: ResolvedSeed, tau: number, t: number, o: ModeOptions): ModePose {
  const { u, n } = unit(r);
  const side = o.side ?? -1;
  const gz = o.groundZ ?? (() => 0);
  const A = axisAt(r, t);
  const A0 = axisAt(r, t - tau); // the axis at the start of the shot
  const dur = Math.max(1, o.duration);
  const f = tau / dur;

  let pos: Vec3;
  let target: Vec3;
  let fov = 50;
  let roll = 0;
  let shakeAmp = 0;

  if (mode === 'chaser') {
    // the observer stands ahead of and beside the track; the vortex approaches, the observer backs off slowly
    const ahead = lerp(780, 520, smooth(f)); // along the track from the start position of the vortex
    const lateral = side * 420;
    const base: [number, number] = [A0.x + u[0] * ahead + n[0] * lateral, A0.y + u[1] * ahead + n[1] * lateral];
    const retreat = 6 * tau; // m/s backing away along the track
    const cx = base[0] + u[0] * retreat, cy = base[1] + u[1] * retreat;
    const z = gz(cx, cy) + 2.0;
    pos = [cx, cy, z];
    // look at the funnel (tracks the axis with a little lead and lag so the framing is not rigid)
    const look: Vec3 = [A.x, A.y, gz(A.x, A.y) + 55 + 25 * Math.sin(tau * 0.2)];
    target = look;
    fov = 38;
    shakeAmp = 0.0016; // handheld sway, rad (~0.1 deg)
  } else if (mode === 'debris') {
    // fixed observer near the track; the vortex passes at ~70 m; the camera follows it with the head
    const miss = 75;
    const tPass = o.duration * 0.55;
    // place the observer relative to the axis position at the closest approach
    const Ap = axisAt(r, t - tau + tPass);
    const cx = Ap.x + n[0] * side * miss, cy = Ap.y + n[1] * side * miss;
    pos = [cx, cy, gz(cx, cy) + 1.8];
    // the head turns toward the axis, with a lag
    const lag = 0.9;
    const Al = axisAt(r, t - lag);
    target = [Al.x, Al.y, gz(Al.x, Al.y) + 16 + 22 * smooth(f)];
    fov = 62;
    shakeAmp = 0; // computed below from the wind
  } else {
    // inside: carried with the vortex, in the clear core, looking up
    const drift = 9 * Math.sin(tau * 0.31);
    const off = 0.28 * r.rm;
    const cx = A.x + off * Math.cos(tau * 0.7) + drift * 0.2, cy = A.y + off * Math.sin(tau * 0.7);
    const z = gz(cx, cy) + 3.0 + 82 * smooth((f - 0.04) / 0.9);
    pos = [cx, cy, z];
    // pitch rises from the horizon to nearly straight up, then pans
    const pitch = lerp(0.10, 0.62, smooth((f - 0.05) / 0.5)) + 0.5 * smooth((f - 0.7) / 0.3);
    const yaw = 0.7 + 0.5 * f - 0.25 * Math.sin(tau * 0.5);
    const d: Vec3 = [Math.cos(pitch) * Math.cos(yaw), Math.cos(pitch) * Math.sin(yaw), Math.sin(pitch)];
    target = [pos[0] + d[0] * 100, pos[1] + d[1] * 100, pos[2] + d[2] * 100];
    fov = 82;
    shakeAmp = 0.004;
  }

  // wind at the camera (ground frame), for the shake of the debris mode
  const w = windField(r, pos[0], pos[1], Math.max(2, pos[2]), t);
  const wind = Math.hypot(w[0], w[1]);
  const inten = intensityAt(r, t);
  const distance = Math.hypot(pos[0] - A.x, pos[1] - A.y);
  if (mode === 'debris') {
    // bounded: up to 1.4 degrees of yaw/pitch at 60 m/s, smooth (2-6 Hz band is avoided: slow sines only)
    const k = Math.min(1, (wind / 55) ** 2) * inten;
    shakeAmp = (1.4 * Math.PI / 180) * k;
  }
  const shakeDeg = (shakeAmp * 180) / Math.PI;
  // apply the shake as angular offsets of the view direction
  const fwd: Vec3 = [target[0] - pos[0], target[1] - pos[1], target[2] - pos[2]];
  const L = Math.hypot(fwd[0], fwd[1], fwd[2]);
  const yawN = shakeAmp * sway(tau * 2.1, 1), pitN = shakeAmp * sway(tau * 1.7, 2);
  const az = Math.atan2(fwd[1], fwd[0]) + yawN;
  const el = Math.asin(fwd[2] / L) + pitN;
  target = [pos[0] + L * Math.cos(el) * Math.cos(az), pos[1] + L * Math.cos(el) * Math.sin(az), pos[2] + L * Math.sin(el)];
  roll = shakeAmp * 0.6 * sway(tau * 1.3, 3);
  // positional jitter of the debris mode (a few cm to ~8 cm), consistent with a hand held camera in a gust
  if (mode === 'debris') {
    const j = 0.05 * Math.min(1, wind / 55);
    pos = [pos[0] + j * sway(tau * 3.1, 4), pos[1] + j * sway(tau * 2.9, 5), pos[2] + j * 0.6 * sway(tau * 3.3, 6)];
  }
  void horizontalSpeed;
  return { pos, target, fovDeg: fov, roll, windAtCamera: wind, shakeDeg, distance };
}

/** Velocity of the camera pose (finite difference), for the rain streak blur. */
export function poseVelocity(mode: CameraMode, r: ResolvedSeed, tau: number, t: number, o: ModeOptions): Vec3 {
  const h = 0.05;
  const a = modePose(mode, r, tau - h, t - h, o).pos;
  const b = modePose(mode, r, tau + h, t + h, o).pos;
  return [(b[0] - a[0]) / (2 * h), (b[1] - a[1]) / (2 * h), (b[2] - a[2]) / (2 * h)];
}
