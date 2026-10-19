/**
 * The data of the PERiLS movie that is not the radar volume: shapes of perils.json (written by
 * tools/perils_movie/render/convert_bundle.py, everything in km of the scene frame, times in seconds
 * after the first step) and the pure lookups the heads-up display needs. No DOM, no GL; unit-tested
 * in perilsData.test.mjs.
 */
import { bracket, lerpAt } from './perilsDirector.ts';

export type ProvKind = 'observed' | 'model' | 'analysis' | 'retrieved' | 'interpolated' | 'stylised';

export interface PerilsStep {
  t_s: number;
  time: string;
  radars: string[];
  offsets_s: Record<string, number>;
}

export interface Station {
  id: string;
  name: string;
  type: string;
  x: number;
  y: number;
  lat: number;
  lon: number;
  altKm: number;
  series: Record<string, (number | null)[]>;
  events: { t_s?: number; kind?: string; [k: string]: unknown }[];
}

export interface SoundingLevels {
  pressure_hpa: (number | null)[];
  height_msl_m: (number | null)[];
  temperature_c: (number | null)[];
  dewpoint_c: (number | null)[];
  u_ms: (number | null)[];
  v_ms: (number | null)[];
}

export interface SoundingRec {
  id: string;
  site: string;
  /** Launch time, seconds after the first step (negative = before the window). */
  launch: number;
  launchIso: string;
  x: number;
  y: number;
  drift: [number, number, number, number][];
  levels: SoundingLevels | null;
  diagnostics: Record<string, unknown>;
}

export interface TornadoRec {
  id?: string;
  rating?: string;
  width_m?: number;
  source?: string;
  path: { t: number; x: number; y: number }[];
  [k: string]: unknown;
}

export interface RotationTrack {
  id?: string;
  radar?: string;
  resolution_m?: number;
  points: { t: number; x: number; y: number; vrot_ms?: number; tds?: boolean; [k: string]: unknown }[];
  [k: string]: unknown;
}

export interface MrmsMeta {
  bin: string;
  x0: number;
  y0: number;
  dx: number;
  nx: number;
  ny: number;
}

export interface LmaFlash {
  id: number;
  t0: number;
  t1: number;
  n: number;
  /** Sources: [t (s after the first step), x km, y km, height km, power dBW]. */
  s: number[][];
}

export interface SourceCredit {
  name: string;
  class: string;
  instrument?: string;
  credit?: string;
  licence_note?: string;
  what?: string;
}

export interface PerilsData {
  version: number;
  time0: string;
  origin: { lat: number; lon: number };
  title: string;
  case: string;
  window: { start?: string; end?: string; duration_s?: number };
  grid: { x0: number; y0: number; dx: number; nx: number; ny: number; zTop: number };
  steps: PerilsStep[];
  motion: { dx_km: number; dy_km: number; dt_s: number; u_ms: number; v_ms: number; sharpness: number; fallback: boolean }[];
  track: { t: number; x: number; y: number }[];
  line: ({ cx: number; cy: number; axis_deg: number; perp_deg: number; half_len_km: number; front_km: number; back_km: number } | null)[];
  radars: { name: string; x: number; y: number; altKm: number; type: string; lat: number; lon: number }[];
  lma: { flashes: LmaFlash[]; nSources: number } | null;
  stations: Station[];
  soundings: SoundingRec[];
  environment: Record<string, unknown> | null;
  tornadoes: TornadoRec[];
  rotation: RotationTrack[];
  mrms: (MrmsMeta | null)[];
  hail: ({ n: number; p: number[][] } | null)[];
  tds: (number[][] | null)[];
  layers: ({ bin: string; names: string[]; ny: number; nx: number } | null)[];
  zdrColumns: (number[][] | null)[];
  sources: SourceCredit[];
  [k: string]: unknown;
}

// ─── series ─────────────────────────────────────────────────────────────────

/**
 * Linear value of a series at time `t`; null outside its time span, where either neighbour is
 * missing, or across a gap longer than `maxGap` seconds (a measured value is never invented in a gap).
 */
export function seriesAt(ts: number[] | undefined, vs: (number | null)[] | undefined, t: number, maxGap = 120): number | null {
  if (!ts || !vs || ts.length === 0 || ts.length !== vs.length) return null;
  if (t < ts[0] - 1e-9 || t > ts[ts.length - 1] + 1e-9) return null;
  const { i, f } = bracket(ts, t);
  const a = vs[i];
  const b = vs[Math.min(i + 1, vs.length - 1)];
  if (a === null || a === undefined || !Number.isFinite(a)) return f < 0.5 || b == null ? null : b;
  if (b === null || b === undefined || !Number.isFinite(b)) return f >= 0.5 ? null : a;
  if (ts[Math.min(i + 1, ts.length - 1)] - ts[i] > maxGap) return f < 0.5 ? a : b;
  return a + (b - a) * f;
}

export interface Readout {
  rain: number | null;
  dm: number | null;
  dbz: number | null;
  windMs: number | null;
  windDir: number | null;
  tempC: number | null;
  pressHpa: number | null;
}

export function stationReadout(st: Station, t: number): Readout {
  const s = st.series ?? {};
  const ts = s.t_s as number[] | undefined;
  const at = (k: string) => seriesAt(ts, s[k], t);
  return { rain: at('rain_rate_mmh'), dm: at('dm_mm'), dbz: at('dbz'), windMs: at('wind_speed_ms'), windDir: at('wind_dir_deg'), tempC: at('temperature_c'), pressHpa: at('pressure_hpa') };
}

/** The station's series covers time `t` (so a read-out is real, not extrapolated). */
export function stationHasData(st: Station, t: number): boolean {
  const ts = st.series?.t_s as number[] | undefined;
  return !!ts && ts.length > 0 && t >= ts[0] && t <= ts[ts.length - 1];
}

// ─── soundings ──────────────────────────────────────────────────────────────

/** The sounding whose launch is nearest in time to data time `t`, with the offset (launch - t, seconds). */
export function nearestSounding(snds: SoundingRec[], t: number): { snd: SoundingRec; offsetS: number } | null {
  let best: SoundingRec | null = null;
  let bd = Infinity;
  for (const s of snds) {
    const d = Math.abs(s.launch - t);
    if (d < bd) {
      bd = d;
      best = s;
    }
  }
  return best ? { snd: best, offsetS: best.launch - t } : null;
}

/** Balloon position (x, y, height km) at time `t` from the drift track, or null outside the flight. */
export function balloonAt(snd: SoundingRec, t: number): [number, number, number] | null {
  const d = snd.drift;
  if (!d || d.length < 2 || t < d[0][0] || t > d[d.length - 1][0]) return null;
  const ts = d.map((q) => q[0]);
  return [lerpAt(ts, d.map((q) => q[1]), t), lerpAt(ts, d.map((q) => q[2]), t), lerpAt(ts, d.map((q) => q[3]), t)];
}

// ─── tornado ────────────────────────────────────────────────────────────────

/** Position of tornado `tor` at time `t`: only inside its surveyed span, `interpolated` between survey points. */
export function tornadoAt(tor: TornadoRec, t: number): { x: number; y: number; interpolated: boolean } | null {
  const p = tor.path;
  if (!p || p.length < 2 || t < p[0].t || t > p[p.length - 1].t) return null;
  const ts = p.map((q) => q.t);
  const { i, f } = bracket(ts, t);
  const exact = f === 0 || f === 1;
  return { x: p[i].x + (p[i + 1].x - p[i].x) * f, y: p[i].y + (p[i + 1].y - p[i].y) * f, interpolated: !exact };
}

/** Rotation-track point nearest in time to `t` within `tolS` seconds, or null. */
export function rotationAt(tr: RotationTrack, t: number, tolS = 90): RotationTrack['points'][number] | null {
  let best: RotationTrack['points'][number] | null = null;
  let bd = tolS;
  for (const p of tr.points) {
    const d = Math.abs(p.t - t);
    if (d <= bd) {
      bd = d;
      best = p;
    }
  }
  return best;
}

// ─── observed steps and interpolation ───────────────────────────────────────

/** Weights of the two observed volumes around data time `t`: `{k, s}` with `s` the position in interval k. */
export function interpWeights(stepT: number[], t: number): { k: number; s: number; wA: number; wB: number } {
  if (stepT.length < 2) return { k: 0, s: 0, wA: 1, wB: 0 };
  const { i, f } = bracket(stepT, t);
  return { k: i, s: f, wA: 1 - f, wB: f };
}

/** Index of the observed step within `tolS` seconds of `t`, or -1 (the frame then shows an interpolated volume). */
export function observedStepAt(stepT: number[], t: number, tolS = 6): number {
  let best = -1;
  let bd = tolS;
  stepT.forEach((s, i) => {
    const d = Math.abs(s - t);
    if (d <= bd) {
      bd = d;
      best = i;
    }
  });
  return best;
}

/** Seconds -> "22:04:10" UTC (clock only) from the ISO time of the first step. */
export function clockUtc(t0Iso: string, seconds: number): string {
  const d = new Date(new Date(t0Iso).getTime() + seconds * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}

/** "+12 s" / "-1 min 30 s" style offset. */
export function fmtOffset(s: number): string {
  const sign = s < 0 ? '-' : '+';
  const a = Math.abs(Math.round(s));
  return a >= 120 ? `${sign}${Math.floor(a / 60)} min ${a % 60 ? `${a % 60} s` : ''}`.trim() : `${sign}${a} s`;
}

/** Cardinal label of a bearing in degrees. */
export function compass16(deg: number): string {
  const names = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  return names[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16];
}
