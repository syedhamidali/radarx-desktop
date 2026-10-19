/**
 * Choreography of a rendered 3D clip: camera keyframes, the storm track and the mapping from clip
 * time to data time. Pure maths (no DOM / GL), unit-tested; the clip renderer (stormClip.ts) and the
 * tool tools/storm_clip/render_clip.py use it, and it is the data model for a future in-app
 * "Export animation" (a clip = camera keyframes + a time mapping + a list of observed volumes).
 *
 * Camera keyframe: the eye orbits a target that FOLLOWS the storm. `theta` / `phi` / `dist` are the
 * orbit angles (degrees, theta 180 = looking north from the south) and distance (km); `dx dy dz` shift
 * the target from the storm centre (km east / north / up, `dz` = height ASL). Between keyframes every
 * channel follows a cubic Hermite curve with monotone tangents (continuous velocity, zero at the
 * ends), so there are no jerks and the motion eases in at the start and out at the end.
 */

export interface CamKey {
  t: number;
  theta: number;
  phi: number;
  dist: number;
  dx: number;
  dy: number;
  dz: number;
}

export type CamChannel = 'theta' | 'phi' | 'dist' | 'dx' | 'dy' | 'dz';
const CHANNELS: CamChannel[] = ['theta', 'phi', 'dist', 'dx', 'dy', 'dz'];

/**
 * Cubic Hermite through (t_i, v_i) with monotone (Fritsch-Butland / PCHIP) tangents, zero at both
 * ends: continuous velocity, no overshoot between keyframes; clamped outside.
 */
export function hermite(ts: number[], vs: number[], t: number): number {
  const n = ts.length;
  if (n === 0) return 0;
  if (n === 1 || t <= ts[0]) return vs[0];
  if (t >= ts[n - 1]) return vs[n - 1];
  let i = 0;
  while (i < n - 2 && t > ts[i + 1]) i++;
  const tan = (k: number) => {
    if (k <= 0 || k >= n - 1) return 0;
    const h0 = ts[k] - ts[k - 1];
    const h1 = ts[k + 1] - ts[k];
    const d0 = (vs[k] - vs[k - 1]) / h0;
    const d1 = (vs[k + 1] - vs[k]) / h1;
    if (d0 * d1 <= 0) return 0;
    const w1 = 2 * h1 + h0;
    const w2 = h1 + 2 * h0;
    return (w1 + w2) / (w1 / d0 + w2 / d1);
  };
  const h = ts[i + 1] - ts[i];
  const u = (t - ts[i]) / h;
  const u2 = u * u;
  const u3 = u2 * u;
  return (
    (2 * u3 - 3 * u2 + 1) * vs[i] +
    (u3 - 2 * u2 + u) * h * tan(i) +
    (-2 * u3 + 3 * u2) * vs[i + 1] +
    (u3 - u2) * h * tan(i + 1)
  );
}

export function cameraAt(keys: CamKey[], t: number): CamKey {
  const ts = keys.map((k) => k.t);
  const out: any = { t };
  for (const c of CHANNELS) out[c] = hermite(ts, keys.map((k) => k[c]), t);
  return out as CamKey;
}

/** Storm centre (km) at data time `t` (s) by Hermite interpolation of the tracked positions. */
export function stormAt(track: { t: number; x: number; y: number }[], t: number): [number, number] {
  const ts = track.map((p) => p.t);
  // Catmull-Rom tangents at the ends would be zero (a stop): use the end velocities instead so the
  // centre keeps moving at the first and last observation.
  const ext = (vs: number[]) => {
    const n = vs.length;
    if (n < 2) return [vs[0] ?? 0, vs[0] ?? 0];
    const v0 = (vs[1] - vs[0]) / (ts[1] - ts[0]);
    const v1 = (vs[n - 1] - vs[n - 2]) / (ts[n - 1] - ts[n - 2]);
    return [v0, v1];
  };
  const pos = (vs: number[]) => {
    const n = vs.length;
    if (n === 1) return vs[0];
    const [v0, v1] = ext(vs);
    // linear extrapolation outside, Hermite with end velocities inside
    if (t <= ts[0]) return vs[0] + v0 * (t - ts[0]);
    if (t >= ts[n - 1]) return vs[n - 1] + v1 * (t - ts[n - 1]);
    let i = 0;
    while (i < n - 2 && t > ts[i + 1]) i++;
    const tan = (k: number) => (k === 0 ? v0 : k === n - 1 ? v1 : (vs[k + 1] - vs[k - 1]) / (ts[k + 1] - ts[k - 1]));
    const h = ts[i + 1] - ts[i];
    const u = (t - ts[i]) / h;
    const u2 = u * u;
    const u3 = u2 * u;
    return (2 * u3 - 3 * u2 + 1) * vs[i] + (u3 - 2 * u2 + u) * h * tan(i) + (-2 * u3 + 3 * u2) * vs[i + 1] + (u3 - u2) * h * tan(i + 1);
  };
  return [pos(track.map((p) => p.x)), pos(track.map((p) => p.y))];
}

/**
 * Data time (s) shown at clip time `t` (s): linear from the first to the last observation after
 * holding the first observation for `hold` s and the last for `hold` s (the clip starts and ends on
 * real volumes).
 */
export function dataTimeAt(t: number, duration: number, t0: number, t1: number, hold = 1.5): number {
  const a = Math.max(0, Math.min(1, (t - hold) / Math.max(1e-9, duration - 2 * hold)));
  const e = a * a * (3 - 2 * a) * 0.35 + a * 0.65; // a little ease, mostly constant speed
  return t0 + (t1 - t0) * e;
}

/** The clip time (s) at which data time `td` (s) is shown (inverse of `dataTimeAt`). */
export function clipTimeOf(td: number, duration: number, t0: number, t1: number, hold = 1.5): number {
  const target = Math.max(0, Math.min(1, (td - t0) / Math.max(1e-9, t1 - t0)));
  let lo = 0, hi = 1;
  for (let i = 0; i < 40; i++) {
    const m = (lo + hi) / 2;
    const e = m * m * (3 - 2 * m) * 0.35 + m * 0.65;
    if (e < target) lo = m; else hi = m;
  }
  return hold + ((lo + hi) / 2) * Math.max(1e-9, duration - 2 * hold);
}

/** Interval of the observed times `ts` that contains `t` and the position `s` in [0, 1] inside it. */
export function locate(ts: number[], t: number): { i: number; s: number } {
  if (ts.length < 2) return { i: 0, s: 0 };
  if (t <= ts[0]) return { i: 0, s: 0 };
  if (t >= ts[ts.length - 1]) return { i: ts.length - 2, s: 1 };
  let i = 0;
  while (i < ts.length - 2 && t > ts[i + 1]) i++;
  return { i, s: (t - ts[i]) / (ts[i + 1] - ts[i]) };
}

/**
 * Time interpolation of the cloud between volume A (time t_i) and volume B (t_i+1): A is carried
 * forward along the storm motion `m` (km per s) by s * dt and B back by (1 - s) * dt, then they
 * are cross-faded with weight s. The renderer samples A at pos - shiftA and B at pos - shiftB.
 */
export function morphShifts(m: [number, number], dt: number, s: number): { shiftA: [number, number, number]; shiftB: [number, number, number] } {
  return {
    shiftA: [m[0] * s * dt, m[1] * s * dt, 0],
    shiftB: [-m[0] * (1 - s) * dt, -m[1] * (1 - s) * dt, 0],
  };
}

/** Storm motion (km per s) of interval i from the tracked positions. */
export function intervalMotion(track: { t: number; x: number; y: number }[], i: number): [number, number] {
  const a = track[i];
  const b = track[i + 1];
  const dt = b.t - a.t || 1;
  return [(b.x - a.x) / dt, (b.y - a.y) / dt];
}

/** "02:04:37 UTC" from seconds since `t0Iso`. */
export function utcLabel(t0Iso: string, seconds: number, withSeconds = true): string {
  const d = new Date(new Date(t0Iso).getTime() + seconds * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}${withSeconds ? ':' + p(d.getUTCSeconds()) : ''} UTC`;
}

/**
 * The default storm choreography for a clip of `duration` seconds (about 36 s): a wide oblique
 * establishing shot, a slow orbit, a low-angle side view looking along the storm's motion (towards
 * the inflow side), a top-down view, and back to the oblique shot. `heading` is the storm's heading
 * (degrees clockwise from north): the low-angle view is taken from the storm's right-rear
 * quadrant looking along the direction of motion.
 */
export function stormChoreography(duration = 36, heading = 55, altKm = 4): CamKey[] {
  const f = duration / 36;
  const behind = (heading + 180) % 360; // camera azimuth when sitting behind the storm
  const low = nearTo(behind - 35, 505 + 60); // behind and a little to the right: along the inflow
  const top = nearTo(180, low); // top-down with north up
  const end = nearTo(205, top);
  const k = (t: number, theta: number, phi: number, dist: number, dz: number): CamKey => ({ t: t * f, theta, phi, dist, dx: 0, dy: 0, dz });
  return [
    k(0, 205, 30, 215, altKm),
    k(5, 215, 27, 165, altKm),
    k(12, 365, 22, 118, altKm),
    k(19, 505, 18, 100, altKm),
    k(24, low, 6, 128, altKm * 1.6),
    k(29, low - 8, 12, 118, altKm * 1.2),
    k(32.5, top, 86, 152, 0),
    k(36, end, 30, 215, altKm),
  ];
}

/** `v` plus a multiple of 360 degrees, as close as possible to `ref` (keeps a turn the short way round). */
export function nearTo(v: number, ref: number): number {
  return v + 360 * Math.round((ref - v) / 360);
}

/**
 * Where the shown box and the camera target sit. The box is FIXED (centred on the middle of the
 * storm track, half width `cropHalf` = the base half width plus half the track extent, so the storm
 * stays inside it for the whole clip) and the camera target follows only the fraction `lag` of the
 * storm's displacement from that middle, so the storm visibly travels across the map and past its
 * landmarks while staying in frame.
 */
export function trackFrame(track: { x: number; y: number }[], baseHalf: number): { anchor: [number, number]; cropHalf: number; extent: number } {
  const xs = track.map((p) => p.x);
  const ys = track.map((p) => p.y);
  const dx = Math.max(...xs) - Math.min(...xs);
  const dy = Math.max(...ys) - Math.min(...ys);
  const extent = Math.max(dx, dy);
  return { anchor: [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2], cropHalf: baseHalf + extent / 2, extent };
}

export function lagTarget(anchor: [number, number], storm: [number, number], lag: number): [number, number] {
  return [anchor[0] + lag * (storm[0] - anchor[0]), anchor[1] + lag * (storm[1] - anchor[1])];
}

/**
 * Camera for a tropical cyclone or any large, slowly turning system (a scene a few hundred km wide):
 * a wide oblique view, a slow orbit that comes lower over the eyewall, and a high look straight down
 * on the eye, where the rotation reads best. `viewKm` is the size of the scene (the distances scale
 * with it).
 */
export function cycloneChoreography(duration = 48, viewKm = 330, altKm = 3): CamKey[] {
  const f = duration / 36;
  const k = (t: number, theta: number, phi: number, dist: number, dz: number): CamKey => ({ t: t * f, theta, phi, dist, dx: 0, dy: 0, dz });
  return [
    k(0, 200, 30, viewKm * 1.15, altKm),
    k(8, 235, 24, viewKm * 0.95, altKm),
    k(17, 305, 20, viewKm * 0.72, altKm * 1.5),
    k(25, 365, 34, viewKm * 0.85, altKm),
    k(31, 400, 84, viewKm * 1.2, 0),
    k(36, 380, 60, viewKm * 1.3, altKm),
  ];
}

// ─── Flight camera (cyclone tour) ───────────────────────────────────────────

/**
 * A key of the flight camera. The eye is placed relative to the moving storm centre (the eye of a
 * cyclone): `r` km away at azimuth `az` (degrees clockwise from north), `alt` km above sea level, and
 * looks at the point `lr` km from the centre at azimuth `laz`, height `lalt` km.
 */
export interface FlightKey {
  t: number;
  r: number;
  az: number;
  alt: number;
  lr: number;
  laz: number;
  lalt: number;
}

const FLIGHT_CHANNELS: (keyof Omit<FlightKey, 't'>)[] = ['r', 'az', 'alt', 'lr', 'laz', 'lalt'];

export interface FlightPose {
  eye: [number, number, number];
  yawDeg: number;
  pitchDeg: number;
  /** The interpolated key (for the HUD). */
  key: FlightKey;
}

/** Pose of the flight camera at clip time `t` around the storm centre `c` (km). Pitch is limited to +/- 84 degrees. */
export function flightPose(keys: FlightKey[], t: number, c: [number, number]): FlightPose {
  const ts = keys.map((k) => k.t);
  const out: any = { t };
  for (const ch of FLIGHT_CHANNELS) out[ch] = hermite(ts, keys.map((k) => k[ch]), t);
  const k = out as FlightKey;
  const ar = (k.az * Math.PI) / 180, lr = (k.laz * Math.PI) / 180;
  const eye: [number, number, number] = [c[0] + k.r * Math.sin(ar), c[1] + k.r * Math.cos(ar), k.alt];
  const look: [number, number, number] = [c[0] + k.lr * Math.sin(lr), c[1] + k.lr * Math.cos(lr), k.lalt];
  const dx = look[0] - eye[0], dy = look[1] - eye[1], dz = look[2] - eye[2];
  const yaw = dx === 0 && dy === 0 ? ((k.az + 180) % 360 + 360) % 360 : ((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360;
  const pitch = Math.max(-84, Math.min(84, (Math.atan2(dz, Math.hypot(dx, dy)) * 180) / Math.PI));
  return { eye, yawDeg: yaw, pitchDeg: pitch, key: k };
}

/**
 * The cyclone tour (about 48 s): a wide establishing view, a descent to a flight along a rainband
 * toward the eyewall, through the wall into the eye (looking back at the wall around it), up out of the
 * eye, and a high look straight down. `eyeR` = radius of the eye (km) from the data, `scale` = size of
 * the system (km). Azimuth keys are unwrapped (a key at 400 after 330 is a further 70 degrees).
 */
export function cycloneFlight(duration = 48, eyeR = 15, scale = 330): FlightKey[] {
  const f = duration / 48;
  const k = (t: number, r: number, az: number, alt: number, lr: number, laz: number, lalt: number): FlightKey => ({ t: t * f, r, az, alt, lr, laz, lalt });
  const wall = Math.max(eyeR * 1.5, 14);
  return [
    k(0, scale * 0.85, 205, 60, 0, 0, 2),
    k(7, scale * 0.6, 235, 22, 0, 0, 3),
    k(15, scale * 0.38, 275, 12, 0, 0, 3),
    k(22, scale * 0.2, 325, 7, eyeR * 0.3, 590, 3),
    k(28, wall * 1.1, 372, 4, eyeR * 0.8, 590, 3.5),
    k(33, eyeR * 0.55, 410, 3, eyeR * 0.9, 590, 4),
    k(38, eyeR * 0.4, 440, 3.5, eyeR * 0.9, 600, 7),
    k(43, scale * 0.25, 480, 40, 0, 0, 2),
    k(48, 1, 500, 260, 0, 0, 0),
  ].map((q, i, a) => (i === a.length - 1 ? { ...q, t: duration } : q));
}

/**
 * Subtle camera shake in heavy rain: deterministic, smooth (sums of sines), eased by `level` (0..1).
 * Returns offsets: yaw and pitch in degrees, position in km.
 */
export function cameraShake(t: number, level: number): { yaw: number; pitch: number; dx: number; dy: number; dz: number } {
  const a = Math.max(0, Math.min(1, level));
  const e = a * a * (3 - 2 * a);
  const n = (f: number, p: number) => Math.sin(2 * Math.PI * f * t + p) * 0.6 + Math.sin(2 * Math.PI * f * 2.3 * t + p * 1.7) * 0.4;
  return {
    yaw: 0.45 * e * n(1.3, 0.3),
    pitch: 0.35 * e * n(1.7, 1.9),
    dx: 0.012 * e * n(2.1, 2.5),
    dy: 0.012 * e * n(1.9, 0.8),
    dz: 0.01 * e * n(2.7, 4.1),
  };
}
