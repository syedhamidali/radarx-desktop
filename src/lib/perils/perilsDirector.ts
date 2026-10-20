/**
 * The PERiLS movie's clock and camera: reads timeline.json and camera_path.json (written by
 * tools/perils_movie/render/choreography.py, the single authority) and answers, for a clip time,
 * the data time, the speed-up, the camera pose and the shot. Pure (no DOM, no GL); unit-tested in
 * perilsDirector.test.mjs.
 *
 * Both files are dense (one entry per frame), so a lookup is a linear interpolation between two
 * neighbours; the data time is monotone by construction (Python builds it from a positive speed).
 */

export interface TimelineFile {
  data_start: string;
  data_end: string;
  data_window_s: number;
  clip_duration_s: number;
  fps: number;
  frames: number;
  speedup_mean: number;
  speedup_min: number;
  speedup_max: number;
  tornado_slowdown: { centre_clip_s: number; width_s: number; level: number } | null;
  mapping: { t_clip_s: number[]; t_data_s: number[] };
  speedup: number[];
  shots: { name: string; label: string; t0: number; t1: number }[];
}

export interface CamEntry {
  t_clip_s: number;
  lat: number;
  lon: number;
  alt_m: number;
  heading_deg: number;
  pitch_deg: number;
  x_km: number;
  y_km: number;
  z_km: number;
  vexag: number;
  /** Vertical field of view of the lens (degrees); absent = the renderer's default. */
  fov_deg?: number;
  shot: string;
}

export interface Pose {
  /** Geographic position and the lens of the frame (for the tornado simulation). */
  lat: number;
  lon: number;
  altM: number;
  x: number;
  y: number;
  zKm: number;
  yawDeg: number;
  pitchDeg: number;
  vexag: number;
  fov?: number;
  shot: string;
}

/** Index and fraction of `v` in the ascending array `a` (clamped to the ends). */
export function bracket(a: ArrayLike<number>, v: number): { i: number; f: number } {
  const n = a.length;
  if (n < 2 || v <= a[0]) return { i: 0, f: 0 };
  if (v >= a[n - 1]) return { i: n - 2, f: 1 };
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const m = (lo + hi) >> 1;
    if (a[m] <= v) lo = m;
    else hi = m;
  }
  const d = a[hi] - a[lo];
  return { i: lo, f: d > 0 ? (v - a[lo]) / d : 0 };
}

/** Linear interpolation of `ys` at `xs`-position `x` (clamped). */
export function lerpAt(xs: ArrayLike<number>, ys: ArrayLike<number>, x: number): number {
  const { i, f } = bracket(xs, x);
  return ys[i] + (ys[i + 1] - ys[i]) * f;
}

/** Shortest signed difference b - a of two bearings, degrees in (-180, 180]. */
export function angleDiff(a: number, b: number): number {
  return ((((b - a) % 360) + 540) % 360) - 180;
}

/** `a` moved toward `b` by fraction `f` the short way round; result in [0, 360). */
export function lerpAngle(a: number, b: number, f: number): number {
  return (((a + angleDiff(a, b) * f) % 360) + 360) % 360;
}

export class Director {
  readonly tl: TimelineFile;
  readonly cam: CamEntry[];
  private camT: Float64Array;

  constructor(tl: TimelineFile, cam: CamEntry[]) {
    this.tl = tl;
    this.cam = cam;
    this.camT = Float64Array.from(cam, (c) => c.t_clip_s);
  }

  get duration(): number {
    return this.tl.clip_duration_s;
  }

  get frameCount(): number {
    return this.tl.frames;
  }

  /** Seconds after the data start shown at clip time `t`. */
  dataTime(t: number): number {
    return lerpAt(this.tl.mapping.t_clip_s, this.tl.mapping.t_data_s, t);
  }

  /** Clip time at which data time `td` is shown (inverse; clamped to the clip). */
  clipTimeOfData(td: number): number {
    return lerpAt(this.tl.mapping.t_data_s, this.tl.mapping.t_clip_s, td);
  }

  /** Data seconds per clip second at clip time `t`. */
  speedup(t: number): number {
    return lerpAt(this.tl.mapping.t_clip_s, this.tl.speedup, t);
  }

  pose(t: number): Pose {
    const { i, f } = bracket(this.camT, t);
    const a = this.cam[i];
    const b = this.cam[Math.min(i + 1, this.cam.length - 1)];
    return {
      lat: a.lat + (b.lat - a.lat) * f,
      lon: a.lon + (b.lon - a.lon) * f,
      altM: a.alt_m + (b.alt_m - a.alt_m) * f,
      x: a.x_km + (b.x_km - a.x_km) * f,
      y: a.y_km + (b.y_km - a.y_km) * f,
      zKm: a.z_km + (b.z_km - a.z_km) * f,
      yawDeg: lerpAngle(a.heading_deg, b.heading_deg, f),
      pitchDeg: a.pitch_deg + (b.pitch_deg - a.pitch_deg) * f,
      vexag: a.vexag + (b.vexag - a.vexag) * f,
      fov: a.fov_deg !== undefined ? a.fov_deg + ((b.fov_deg ?? a.fov_deg) - a.fov_deg) * f : undefined,
      shot: f < 0.5 ? a.shot : b.shot,
    };
  }

  /** The shot in charge at clip time `t`. */
  shotAt(t: number): { name: string; label: string } {
    const shots = this.tl.shots;
    let s = shots[0];
    for (const q of shots) if (t >= q.t0) s = q;
    return { name: s.name, label: s.label };
  }
}

/** Id of the station nearest to the eye (3-D distance, km; stations stand on the flat ground). Ties: first. */
export function nearestStation<T extends { id: string; x: number; y: number; moving?: boolean }>(stations: T[], eye: [number, number, number]): T | null {
  let best: T | null = null;
  let bd = Infinity;
  for (const s of stations) {
    if (s.moving) continue; // a vehicle is not a place to stand
    const d = Math.hypot(s.x - eye[0], s.y - eye[1], eye[2]);
    if (d < bd) {
      bd = d;
      best = s;
    }
  }
  return best;
}
