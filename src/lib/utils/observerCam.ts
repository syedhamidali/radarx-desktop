/**
 * The "window" camera of the storm clip: an observer standing at a fixed place on the ground and
 * looking at the storm, like a camera on a window sill recording the sky. Pure helpers (no DOM,
 * no GL); unit-tested in observerCam.test.mjs.
 *
 * Scene coordinates: x east, y north (km). Bearings are degrees clockwise from north.
 */

export interface ObserverSettings {
  /** Position in the scene (km east / north of the bundle origin); default: beside the storm track. */
  x?: number;
  y?: number;
  /** Eye height above the ground in metres (2 = standing, 10 to 20 = a roof). */
  eyeM?: number;
  /** Distance from the track to the observer, km, and how far upwind of its middle point. */
  distKm?: number;
  upwindKm?: number;
  /** Which side of the track the observer stands on (looking at it): 'right' of the heading or 'left'. */
  side?: 'right' | 'left';
  /** How much the camera turns with the storm (0 = fixed, 1 = keeps the storm in the middle). */
  pan?: number;
  /** Look up by this many degrees (the storm top is high). */
  pitchDeg?: number;
}

export const OBSERVER_DEFAULTS: Required<Omit<ObserverSettings, 'x' | 'y'>> = {
  eyeM: 10,
  distKm: 10,
  upwindKm: 4,
  side: 'right',
  pan: 0.3,
  pitchDeg: 12,
};

/** Bearing (degrees clockwise from north, 0..360) of the vector (dx east, dy north). */
export function bearingDeg(dx: number, dy: number): number {
  return ((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360;
}

/** Signed difference b - a of two bearings, in (-180, 180]. */
export function bearingDiff(a: number, b: number): number {
  return ((((b - a) % 360) + 540) % 360) - 180;
}

/**
 * Default observer position: `distKm` to the side of the track's middle point (looking at the
 * track) and `upwindKm` against the storm motion, so the storm comes in from the far side, passes
 * and leaves. `headingDeg` is the direction the storm moves toward.
 */
export function placeObserver(anchor: [number, number], headingDeg: number, o: ObserverSettings = {}): [number, number] {
  if (o.x !== undefined && o.y !== undefined) return [o.x, o.y];
  const d = o.distKm ?? OBSERVER_DEFAULTS.distKm;
  const up = o.upwindKm ?? OBSERVER_DEFAULTS.upwindKm;
  const h = (headingDeg * Math.PI) / 180;
  const hx = Math.sin(h), hy = Math.cos(h);
  // the right-hand normal of the heading is (hy, -hx)
  const sgn = (o.side ?? OBSERVER_DEFAULTS.side) === 'right' ? 1 : -1;
  return [anchor[0] + sgn * d * hy - up * hx, anchor[1] - sgn * d * hx - up * hy];
}

export interface ObserverPose {
  yawDeg: number;
  pitchDeg: number;
}

/**
 * Where the camera looks: a fixed bearing toward the middle of the track, turned by the fraction
 * `pan` of how far the storm has moved away from it, so the storm drifts slowly across the frame.
 */
export function observerPose(obs: [number, number], storm: [number, number], anchor: [number, number], o: ObserverSettings = {}): ObserverPose {
  const pan = o.pan ?? OBSERVER_DEFAULTS.pan;
  const b0 = bearingDeg(anchor[0] - obs[0], anchor[1] - obs[1]);
  const b1 = bearingDeg(storm[0] - obs[0], storm[1] - obs[1]);
  return { yawDeg: (b0 + pan * bearingDiff(b0, b1) + 360) % 360, pitchDeg: o.pitchDeg ?? OBSERVER_DEFAULTS.pitchDeg };
}

/** Horizontal field of view (degrees) of a camera with the vertical half-angle tangent `tanY` and aspect w / h. */
export function horizontalFovDeg(tanY: number, aspect: number): number {
  return (2 * Math.atan(tanY * aspect) * 180) / Math.PI;
}

/** Elevation angle (degrees) of the top of a cloud of height `topKm` at ground distance `distKm`. */
export function elevationDeg(distKm: number, topKm: number, eyeKm = 0): number {
  return (Math.atan2(topKm - eyeKm, Math.max(distKm, 1e-6)) * 180) / Math.PI;
}

/** Compass label (N, NE, ... NW) of a bearing. */
export function compassLabel(bearing: number): string {
  const names = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  return names[Math.round(((bearing % 360) + 360) % 360 / 22.5) % 16];
}
