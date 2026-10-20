/**
 * The data of the tornado focus (docs/PERILS_MOVIE.md, "Tornado focus"), pure and unit-tested in perilsFocusData.test.mjs:
 * the series of low-level rotation next to the SURVEYED tornado position (azimuthal shear and Vrot of the merged radar
 * volumes, where the maximum is, how far from the survey position), its value at a data time, the geometry words, and the
 * captions of the five angles. Every number a caption shows is computed here from the data, none is typed in.
 *
 * What the numbers are: the radars measure the PARENT circulation on a 500 m grid; the tornado (366 m wide) is unresolved.
 * The rotation maximum is not at the surveyed position (it lies 1 to 3 km away, see verification_geometry.md): the survey
 * position is the truth for the tornado, the radar rotation is shown separately with its offset stated.
 */
import { compass16 } from './perilsData.ts';
import { bracket } from './perilsDirector.ts';

export interface FocusPoint {
  /** Data time (s after the first step) of the step. */
  t: number;
  /** Maximum azimuthal shear (s-1) within `radiusKm` of the surveyed position, and the largest Vrot (m/s). */
  as: number | null;
  as_within_1km?: number | null;
  vrot: number | null;
  /** Where the shear maximum is (km) and how far / which way it lies from the surveyed position. */
  x: number | null;
  y: number | null;
  offset_km: number | null;
  bearing_deg: number | null;
  radar: string | null;
  ref_x: number;
  ref_y: number;
  phase: 'before' | 'on' | 'after';
  cmax_at_dbz?: number | null;
  core40_km?: number | null;
  core40_bearing_deg?: number | null;
  core50_km?: number | null;
  core50_bearing_deg?: number | null;
  /** Mean storm-relative wind at 0.5 km within 5 km of the survey position (RETRIEVED, multi-Doppler): speed and the bearing it blows from. */
  sr_wind?: { speed_ms: number; from_deg: number; valid_fraction: number } | null;
}

export interface FocusData {
  vsr: ({ bin: string; nx: number; ny: number } | null)[];
  shear: ({ bin: string; nx: number; ny: number } | null)[];
  vsrLim: number;
  shearLim: number;
  motion_ms: [number, number];
  radiusKm: number;
  series: FocusPoint[];
  note?: string;
}

/** The rotation threshold (s-1) of the data tool's vortex candidates (docs/PERILS_BUNDLE.md). */
export const SHEAR_CANDIDATE = 0.012;

export interface FocusNow {
  as: number | null;
  vrot: number | null;
  /** Rotation maximum (km), its distance and bearing from the surveyed position `ref`, and the radar that saw it. */
  cx: number | null;
  cy: number | null;
  offsetKm: number | null;
  bearingDeg: number | null;
  radar: string | null;
  core50Km: number | null;
  core50Bearing: number | null;
  cmaxAt: number | null;
  srWindMs: number | null;
  srWindFrom: number | null;
}

/** Linear value between two steps; where one of them has none, the other only for the half of the interval nearer to it (nothing is invented across a gap). */
const lerpN = (a: number | null | undefined, b: number | null | undefined, f: number): number | null => {
  const ok = (v: number | null | undefined): v is number => v != null && Number.isFinite(v);
  if (!ok(a)) return ok(b) && f >= 0.5 ? b : null;
  if (!ok(b)) return f < 0.5 ? a : null;
  return a + (b - a) * f;
};

/** Bearing (degrees clockwise from north) of the vector (dx east, dy north). */
export function bearingOf(dx: number, dy: number): number {
  return (((Math.atan2(dx, dy) * 180) / Math.PI) % 360 + 360) % 360;
}

/** The series at data time `td`: shear, Vrot and the rotation maximum interpolated between the two steps; the offset from `ref` (the surveyed position now). */
export function focusNow(f: FocusData | undefined | null, td: number, ref: { x: number; y: number } | null): FocusNow | null {
  const s = f?.series;
  if (!s || !s.length) return null;
  const { i, f: w } = bracket(s.map((p) => p.t), td);
  const a = s[i];
  const b = s[Math.min(i + 1, s.length - 1)];
  const near = w < 0.5 ? a : b;
  const cx = lerpN(a.x, b.x, w);
  const cy = lerpN(a.y, b.y, w);
  let offsetKm: number | null = null;
  let bearingDeg: number | null = null;
  if (cx != null && cy != null && ref) {
    offsetKm = Math.hypot(cx - ref.x, cy - ref.y);
    bearingDeg = bearingOf(cx - ref.x, cy - ref.y);
  }
  return {
    as: lerpN(a.as, b.as, w),
    vrot: lerpN(a.vrot, b.vrot, w),
    cx, cy, offsetKm, bearingDeg,
    radar: near.radar,
    core50Km: near.core50_km ?? null,
    core50Bearing: near.core50_bearing_deg ?? null,
    cmaxAt: near.cmax_at_dbz ?? null,
    srWindMs: near.sr_wind?.speed_ms ?? null,
    srWindFrom: near.sr_wind?.from_deg ?? null,
  };
}

/** Range and the circular mean bearing of the offsets of the rotation maxima from the surveyed position over the survey's life. */
export function offsetStats(f: FocusData | undefined | null): { minKm: number; maxKm: number; medianKm: number; meanBearing: number; n: number } | null {
  const pts = (f?.series ?? []).filter((p) => p.phase === 'on' && p.offset_km != null && p.bearing_deg != null);
  if (!pts.length) return null;
  const d = pts.map((p) => p.offset_km as number).sort((x, y) => x - y);
  const sx = pts.reduce((s, p) => s + Math.sin(((p.bearing_deg as number) * Math.PI) / 180), 0);
  const cy = pts.reduce((s, p) => s + Math.cos(((p.bearing_deg as number) * Math.PI) / 180), 0);
  return { minKm: d[0], maxKm: d[d.length - 1], medianKm: d[Math.floor(d.length / 2)], meanBearing: bearingOf(sx, cy), n: pts.length };
}

/** "1.9 km NW of the surveyed position" (or '' without a position). */
export function offsetText(now: FocusNow | null): string {
  if (!now || now.offsetKm == null || now.bearingDeg == null) return '';
  return `${now.offsetKm.toFixed(1)} km ${compass16(now.bearingDeg)} of the surveyed position`;
}

export interface FocusInfo {
  rating: string;
  widthM: number | null;
  /** Clock strings (UTC) of the survey's begin and end. */
  touchdown: string;
  end: string;
  /** Length of the surveyed track and the part travelled so far (km). */
  trackKm: number;
  doneKm: number;
  /** Speed of the tornado along the survey (m/s) and the bearing it moves toward. */
  speedMs: number;
  headingDeg: number;
  /** Distance of the camera from the tornado (m) in the beat. */
  distanceM: number;
  /** Rotation period of the simulated core (s), when the simulation is in use. */
  periodCoreS: number | null;
  /** The station nearest to the tornado: name, distance (km), bearing from the tornado, its air and the temperature change over six minutes. */
  station: { name: string; km: number; bearing: number; tempC: number | null; dewC: number | null; windMs: number | null; windFrom: number | null; dTempC: number | null } | null;
}

export interface FocusCaption {
  title: string;
  lines: string[];
}

const f1 = (v: number | null | undefined, d = 1): string => (v == null || !Number.isFinite(v) ? '-' : v.toFixed(d));

/** The words of the lower third for one angle of the focus (`beat` = the shot name); [] lines when the beat has nothing to say. */
export function focusCaption(beat: string, now: FocusNow | null, info: FocusInfo, stats: ReturnType<typeof offsetStats>): FocusCaption {
  const width = info.widthM ? `${Math.round(info.widthM)} m` : 'narrow';
  const rotation = now && now.as != null
    ? `Radar rotation near the tornado: shear ${f1(now.as, 3)} s⁻¹${now.vrot != null ? `, Vrot ${f1(now.vrot, 0)} m/s` : ''}, strongest ${offsetText(now) || 'near it'}${now.radar ? ` (${now.radar})` : ''}; the survey position is the one used.`
    : 'No radar rotation maximum near the tornado at this time.';
  const inflow = now && now.srWindMs != null && now.srWindFrom != null
    ? `Air flows in from the ${compass16(now.srWindFrom)} at ${f1(now.srWindMs, 0)} m/s relative to the storm (multi-Doppler retrieval, 0.5 km).`
    : '';
  switch (beat) {
    case 'focus_wide': {
      const near = now != null && now.core50Km != null && now.core50Bearing != null && now.core50Km < 2.5;
      const core = now && now.core50Km != null && now.core50Bearing != null
        ? near
          ? `the tornado sits at the edge of a strong echo: 50 dBZ or more ${f1(now.core50Km, 1)} km to the ${compass16(now.core50Bearing)}`
          : `the nearest echo of 50 dBZ or more is ${f1(now.core50Km, 0)} km to the ${compass16(now.core50Bearing)}`
        : 'no echo of 50 dBZ or more nearby';
      return {
        title: `${info.rating} tornado under a squall-line vortex`,
        lines: [
          `Survey (NWS): on the ground ${info.touchdown}-${info.end} UTC, damage path ${width} wide and ${f1(info.trackKm, 1)} km long; between the surveyed ends the path is a straight line (INTERPOLATED).`,
          `Radar: ${core}, at the bend of a narrow band of 45-55 dBZ in a broad area of 40-50 dBZ. A squall-line (QLCS) segment with an embedded circulation, as the data tool classifies the event: not an isolated supercell.`,
          ...(inflow ? [inflow] : []),
        ],
      };
    }
    case 'focus_chaser':
      return {
        title: `${info.rating} tornado touches down ${info.touchdown} UTC (survey)`,
        lines: [
          'SIMULATION seeded by observations: position, width and rating from the NWS survey, air from the stations, lightning from the mapping array. The funnel, the swirl and the debris are modelled.',
          rotation,
        ],
      };
    case 'focus_close':
      return {
        title: `Passing ${Math.round(info.distanceM)} m away (simulated view)`,
        lines: [
          `The tornado moves ${f1(info.speedMs, 0)} m/s toward the ${compass16(info.headingDeg)} (survey: ${f1(info.trackKm, 1)} km in 12 minutes). Real time.`,
          `The core turns once in ${info.periodCoreS != null ? f1(info.periodCoreS, 1) : '-'} s (modelled from the ${info.rating} wind range). The radars see only the parent circulation: the ${width} tornado is unresolved on their 500 m grid.`,
        ],
      };
    case 'focus_low': {
      const st = info.station;
      const air = st
        ? `Nearest station, ${st.name} (${f1(st.km, 0)} km ${compass16(st.bearing)}): ${f1(st.tempC, 1)} °C, dew point ${f1(st.dewC, 1)} °C, wind from the ${st.windFrom != null ? compass16(st.windFrom) : '-'} at ${f1(st.windMs, 0)} m/s; ${st.dTempC != null ? `${st.dTempC >= 0 ? '+' : ''}${f1(st.dTempC, 1)} K in six minutes: ${Math.abs(st.dTempC) < 0.5 ? 'rain-cooled air already, no further front passes here' : 'a change of the air mass'}.` : 'no temperature change available.'}`
        : '';
      return { title: 'Looking up from the inflow side', lines: [inflow || 'Inflow from the south.', ...(air ? [air] : [])] };
    }
    case 'focus_swath':
      return {
        title: 'Looking back along the damage path',
        lines: [
          `Path so far: ${f1(info.doneKm, 1)} of ${f1(info.trackKm, 1)} km, ${width} wide (surveyed ends, straight line between: INTERPOLATED). Real time.`,
          "The trees that lie down along it are MODELLED: a tree falls where the simulated wind passes 30-33 m/s. That is the simulation's damage, not the surveyed damage.",
        ],
      };
    case 'focus_top':
      return {
        title: 'The path so far',
        lines: [
          `Damage swath ${width} wide: ${f1(info.doneKm, 1)} of ${f1(info.trackKm, 1)} km so far, on the surveyed line (straight between the surveyed ends: INTERPOLATED). The tornado is on the ground until ${info.end} UTC.`,
          stats
            ? `Radar rotation maxima lie ${f1(stats.minKm, 1)}-${f1(stats.maxKm, 1)} km (median ${f1(stats.medianKm, 1)}) ${compass16(stats.meanBearing)} of the surveyed position in every volume: the survey position is used for the tornado.`
            : rotation,
        ],
      };
    default:
      return { title: '', lines: [] };
  }
}
