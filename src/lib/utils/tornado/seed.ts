/**
 * Seed data of the tornado simulation: what the observations say, resolved into the physical
 * parameters the simulation needs. Pure functions (no DOM, no GL); unit-tested in seed.test.mjs.
 *
 * OBSERVED (in the seed): the survey (rating, swath width, begin / end points and times), the radar
 * rotation (Vrot of the couplet at mesocyclone scale, its sense), the sounding or surface T and Td nearest
 * in time and space (-> LCL), the lightning flash positions and times, the radar reflectivity around the
 * tornado. MODELLED (resolved here): the near-core wind profile, the core radius (from the swath width and
 * the damage-onset speed), the funnel shape and the debris. A radar grid cannot resolve a 366 m tornado.
 *
 * Frame: local metres east / north of `origin`; time `t` in seconds on the data clock of the bundle (the
 * window start is t = 0). The caller maps clip time to data time (real time, 1 s = 1 s, is the physically
 * right rate for the swirl; the movie time-lapse is allowed, the swirl then looks faster).
 */

import {
  efPeakWind, coreRadiusFromSwath, makeVortex, makeMesoVortex, type Vortex,
} from './vortexField.ts';
import { makeCondition, type FunnelCondition, type Thermo } from './funnel.ts';

export interface TrackPoint {
  /** Seconds on the data clock. */
  t: number;
  lat: number;
  lon: number;
}

export interface FlashSeed {
  /** Seconds on the data clock. */
  t: number;
  lat: number;
  lon: number;
  /** Height above sea level (m) of the VHF source centroid. */
  altM: number;
  nSources?: number;
}

export interface EchoSeed {
  /** Reflectivity (dBZ) of the lowest level on a regular local grid (m east / north of the origin). */
  x0: number;
  y0: number;
  dx: number;
  nx: number;
  ny: number;
  /** Row-major (y rows), NaN or < -30 = no echo. */
  dbz: number[];
  /** Echo top (m) over the tornado, if known. */
  topM?: number;
  /** Time on the data clock of the volume. */
  t?: number;
}

export interface SeedData {
  /** Local frame origin (default: the first track point). */
  origin?: { lat: number; lon: number };
  /** Peak wind near the core (m/s); default: midpoint of the EF-scale range of `ef`. */
  vt?: number;
  /** Core radius (m); default: derived from `width` so that the swath edge sits at the damage-onset speed (29 m/s). */
  coreRadius?: number;
  /** Swath width from the survey (m). */
  width: number;
  /** EF rating, 'EF0' .. 'EF5'. */
  ef: string;
  /** Track of the vortex axis; two points are enough (the survey's begin and end), more are interpolated linearly. */
  track: TrackPoint[];
  /** Survey time span (s): the tornado exists only inside it. Default: the first and last track point. */
  span?: [number, number];
  /** Sense of rotation from the radar couplet. */
  sense?: 'cyclonic' | 'anticyclonic';
  /** Radar rotation of the PARENT circulation (couplet of a few km): Vrot (m/s), couplet radius (m, default 900), the radar and when. */
  meso?: { vrot: number; radiusM?: number; radar?: string; t?: number };
  /**
   * Near-surface temperature, dew point (deg C) and pressure (hPa) of the station nearest to the tornado in
   * space and time, with a citation shown on screen: the air that the funnel's pressure drop condenses.
   */
  thermo?: Thermo & { source?: string };
  /**
   * Thermodynamics of the warm updraft air that sets the ambient cloud base (a station or sounding in the
   * inflow, possibly farther away). Default: `thermo`. With rain-cooled surface air (T - Td < 0.5 K) the LCL of
   * `thermo` is tens of metres and would put the cloud base on the ground; the caller says which it used.
   */
  inflowThermo?: Thermo & { source?: string };
  /** LCL above the ground (m), used when no thermodynamics are given. */
  lcl?: number;
  lightningFlashes?: FlashSeed[];
  echo?: EchoSeed;
  /** The echo of every observed volume of the survey's life on one grid, and the echo pattern's motion (m/s east, north): the rain follows the echo. */
  echoSeries?: EchoSeed[];
  echoMotion?: [number, number];
  /** How long the tornado takes to reach full intensity after the survey's begin time and to die away before its end time (s): MODELLED (the survey gives only the times). Default 25 and 40. */
  ramp?: { form: number; decay: number };
  /** Credits and the like, shown in the labels. */
  survey?: { id?: string; source?: string; narrative?: string };
}

export interface ResolvedSeed {
  origin: { lat: number; lon: number };
  vortex: Vortex;
  meso: Vortex | null;
  condition: FunnelCondition;
  /** Condition of the ambient cloud base: the warm inflow air (default: the same as `condition`). */
  cloudCondition: FunnelCondition;
  /** The near-surface air at the nearest station (often rain-cooled, nearly saturated): the humidity of the haze. */
  nearCondition: FunnelCondition;
  /** Time span of the tornado (s) and the lifecycle ramps (s). */
  span: [number, number];
  ramp: { form: number; decay: number };
  /** Track in local metres. */
  track: { t: number; x: number; y: number }[];
  /** Translation velocity (m/s), and its heading (degrees clockwise from north). */
  translation: [number, number];
  headingDeg: number;
  speed: number;
  width: number;
  ef: string;
  vt: number;
  rm: number;
  coreSource: 'given' | 'survey-swath';
  flashes: (FlashSeed & { x: number; y: number; z: number })[];
  echo: EchoSeed | null;
  echoSeries: EchoSeed[];
  echoMotion: [number, number];
  credits: string[];
  summary: SeedSummary;
}

export interface SeedSummary {
  observed: string[];
  modelled: string[];
}

const M_PER_DEG_LAT = 110574;
const M_PER_DEG_LON_EQ = 111320;

/** Local metres of a point relative to the origin. */
export function toLocal(origin: { lat: number; lon: number }, lat: number, lon: number): [number, number] {
  return [
    (lon - origin.lon) * M_PER_DEG_LON_EQ * Math.cos((origin.lat * Math.PI) / 180),
    (lat - origin.lat) * M_PER_DEG_LAT,
  ];
}

export function fromLocal(origin: { lat: number; lon: number }, x: number, y: number): [number, number] {
  return [
    origin.lat + y / M_PER_DEG_LAT,
    origin.lon + x / (M_PER_DEG_LON_EQ * Math.cos((origin.lat * Math.PI) / 180)),
  ];
}

export function resolveSeed(seed: SeedData): ResolvedSeed {
  if (!seed.track || seed.track.length < 2) throw new Error('tornado seed: the track needs at least two points');
  const origin = seed.origin ?? { lat: seed.track[0].lat, lon: seed.track[0].lon };
  const track = seed.track
    .slice()
    .sort((a, b) => a.t - b.t)
    .map((p) => {
      const [x, y] = toLocal(origin, p.lat, p.lon);
      return { t: p.t, x, y };
    });
  const a = track[0];
  const b = track[track.length - 1];
  const dt = Math.max(1e-6, b.t - a.t);
  const translation: [number, number] = [(b.x - a.x) / dt, (b.y - a.y) / dt];
  const speed = Math.hypot(translation[0], translation[1]);
  const headingDeg = ((Math.atan2(translation[0], translation[1]) * 180) / Math.PI + 360) % 360;
  const vt = seed.vt ?? efPeakWind(seed.ef);
  let rm = seed.coreRadius ?? 0;
  let coreSource: ResolvedSeed['coreSource'] = 'given';
  if (!(rm > 0)) {
    rm = coreRadiusFromSwath(vt, seed.width, 29);
    coreSource = 'survey-swath';
  }
  const sense = seed.sense === 'anticyclonic' ? -1 : 1;
  const vortex = makeVortex({ vt, rm, sense });
  const meso = seed.meso ? makeMesoVortex(seed.meso.vrot, seed.meso.radiusM ?? 900, sense) : null;
  const th: Thermo = seed.thermo
    ? { t0: seed.thermo.t0, td0: seed.thermo.td0, p0: seed.thermo.p0 }
    : { t0: 20 + (seed.lcl ?? 600) / 125, td0: 20, p0: 990 }; // T - Td = LCL / 125 K (Espy), only as a fallback
  const nearCondition = makeCondition(th);
  // the air that the funnel's pressure drop condenses is the warm inflow when the caller gives one: the nearest
  // station is usually in rain-cooled, saturated air, where ANY pressure drop makes cloud and the funnel would be a fog bank
  const condition = seed.inflowThermo ? makeCondition({ t0: seed.inflowThermo.t0, td0: seed.inflowThermo.td0, p0: seed.inflowThermo.p0 }) : nearCondition;
  const cloudCondition = condition;
  const flashes = (seed.lightningFlashes ?? []).map((f) => {
    const [x, y] = toLocal(origin, f.lat, f.lon);
    return { ...f, x, y, z: f.altM };
  });
  const span = seed.span ?? [a.t, b.t];
  const credits: string[] = [];
  if (seed.survey?.source) credits.push(seed.survey.source);
  if (seed.thermo?.source) credits.push(seed.thermo.source);
  if (seed.inflowThermo?.source) credits.push(seed.inflowThermo.source);
  const observed = [
    `survey: ${seed.ef}, swath ${Math.round(seed.width)} m, path and time (${a.t.toFixed(0)}-${b.t.toFixed(0)} s)`,
    seed.meso ? `radar: parent circulation Vrot ${seed.meso.vrot.toFixed(0)} m/s, couplet radius ${(seed.meso.radiusM ?? 900).toFixed(0)} m${seed.meso.radar ? ` (${seed.meso.radar})` : ''}, ${sense > 0 ? 'cyclonic' : 'anticyclonic'}; period ~${(2 * Math.PI * (seed.meso.radiusM ?? 900) / seed.meso.vrot / 60).toFixed(0)} min, too slow to see` : 'radar couplet: none supplied',
    seed.thermo ? `nearest station T ${seed.thermo.t0} / Td ${seed.thermo.td0} C (LCL ${nearCondition.lcl.toFixed(0)} m, rain-cooled)${seed.thermo.source ? `: ${seed.thermo.source}` : ''}` : `LCL ${condition.lcl.toFixed(0)} m`,
    ...(seed.inflowThermo ? [`warm inflow T ${seed.inflowThermo.t0} / Td ${seed.inflowThermo.td0} C: LCL (cloud base, funnel condensation) ${condition.lcl.toFixed(0)} m${seed.inflowThermo.source ? `: ${seed.inflowThermo.source}` : ''}`] : []),
    `${flashes.length} lightning flashes (position and time)`,
  ];
  const modelled = [
    `near-core wind: Lamb-Oseen vortex, peak ${vt.toFixed(0)} m/s at r = ${rm.toFixed(0)} m (${coreSource === 'survey-swath' ? 'core from the swath width' : 'core given'})`,
    'funnel shape (from the cyclostrophic pressure deficit), debris, noise detail, rain curtains',
  ];
  return {
    origin, vortex, meso, condition, cloudCondition, nearCondition, span, ramp: { form: seed.ramp?.form ?? 25, decay: seed.ramp?.decay ?? 40 },
    track, translation, headingDeg, speed, width: seed.width, ef: seed.ef, vt, rm, coreSource,
    flashes, echo: seed.echo ?? null, echoSeries: seed.echoSeries ?? [], echoMotion: seed.echoMotion ?? [0, 0], credits, summary: { observed, modelled },
  };
}

/** Position of the vortex axis (local metres) at time t, or null outside the track span. */
export function axisAt(r: ResolvedSeed, t: number): { x: number; y: number } {
  const tr = r.track;
  if (t <= tr[0].t) {
    // extrapolate along the translation (used only for the debris warm-up before the first point)
    return { x: tr[0].x + r.translation[0] * (t - tr[0].t), y: tr[0].y + r.translation[1] * (t - tr[0].t) };
  }
  for (let i = 0; i < tr.length - 1; i++) {
    if (t <= tr[i + 1].t) {
      const f = (t - tr[i].t) / Math.max(1e-9, tr[i + 1].t - tr[i].t);
      return { x: tr[i].x + (tr[i + 1].x - tr[i].x) * f, y: tr[i].y + (tr[i + 1].y - tr[i].y) * f };
    }
  }
  const l = tr[tr.length - 1];
  return { x: l.x + r.translation[0] * (t - l.t), y: l.y + r.translation[1] * (t - l.t) };
}

/** Intensity factor 0..1 of the tornado at time t: 0 outside the surveyed span (never shown without evidence), ramps at the ends. */
export function intensityAt(r: ResolvedSeed, t: number): number {
  const [t0, t1] = r.span;
  if (t < t0 || t > t1) return 0;
  const up = Math.min(1, (t - t0) / r.ramp.form);
  const dn = Math.min(1, (t1 - t) / r.ramp.decay);
  const s = (x: number) => x * x * (3 - 2 * x);
  return s(Math.max(0, up)) * s(Math.max(0, dn));
}

/** Rotation period (s) of the core at its radius of maximum wind: 2 pi rm / Vt. */
export function rotationPeriod(r: ResolvedSeed): number {
  return (2 * Math.PI * r.rm) / r.vt;
}
