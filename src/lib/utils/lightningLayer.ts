/**
 * The observed lightning layer (GLM and LMA): the pure part. No Svelte, no DOM, no Web Audio, so it is
 * unit tested with node (lightningLayer.test.mjs).
 *
 * The engine (python/engine/lightning_layer.py) reads the files and answers `lightning_result` with
 * columns as base64 float32/int32. Times are seconds after `t_ref` (UTC). A *point* is an LMA source or
 * a GLM group (measured position and time); a *flash* groups points. The layer draws the points of the
 * flashes inside a time window that is tied to the volume time, connects the points of one flash in time
 * order, and fades them with age. Nothing is drawn where the instrument has no data: the coverage
 * intervals of the files come with the answer and the series show a gap, not a zero, where they are missing.
 *
 * Provenance class OBSERVED. GLM has no height (points are on the ground); positions of GLM carry a
 * parallax uncertainty the panel states.
 */
import { latLonToDataSpace } from './geoProjection.ts';

export type LightningKind = 'glm' | 'lma';

export interface LightningData {
  kind: LightningKind;
  instrument: string;
  /** Epoch ms of the time every time array counts from. */
  tRefMs: number;
  files: string[];
  n: number;
  /** Per point: seconds after tRef (ascending), degrees, km ASL (NaN for GLM), raw strength, brightness 0..1, stations (-1 unknown), flash index. */
  t: Float32Array;
  lat: Float32Array;
  lon: Float32Array;
  altKm: Float32Array;
  strength: Float32Array;
  brightness: Float32Array;
  stations: Int32Array;
  flash: Int32Array;
  nf: number;
  f: {
    t0: Float32Array;
    t1: Float32Array;
    lat: Float32Array;
    lon: Float32Array;
    altKm: Float32Array;
    nPoints: Int32Array;
    strength: Float32Array;
    areaKm2: Float32Array;
  };
  /** Intervals (seconds after tRef) where the instrument data exist. */
  coverage: [number, number][];
  strengthInfo: { unit: string; what: string; brightnessScale: string };
  meta: Record<string, any>;
  notes: string[];
  provenance: unknown;
}

// ── Decoding ────────────────────────────────────────────────────────────────

function bytesOf(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Little-endian float32 array of a base64 string. */
export function f32(b64: string | undefined | null): Float32Array {
  if (!b64) return new Float32Array(0);
  const u = bytesOf(b64);
  const dv = new DataView(u.buffer, u.byteOffset, u.byteLength);
  const out = new Float32Array(u.byteLength >> 2);
  for (let i = 0; i < out.length; i++) out[i] = dv.getFloat32(i * 4, true);
  return out;
}

/** Little-endian int32 array of a base64 string. */
export function i32(b64: string | undefined | null): Int32Array {
  if (!b64) return new Int32Array(0);
  const u = bytesOf(b64);
  const dv = new DataView(u.buffer, u.byteOffset, u.byteLength);
  const out = new Int32Array(u.byteLength >> 2);
  for (let i = 0; i < out.length; i++) out[i] = dv.getInt32(i * 4, true);
  return out;
}

/** The data of a `lightning_result` message. Throws a readable error for an answer that is not one. */
export function decodeLightning(msg: any): LightningData {
  if (!msg || (msg.kind !== 'glm' && msg.kind !== 'lma') || !msg.points || !msg.flashes) {
    throw new Error('The engine sent lightning data in a form this version does not know.');
  }
  const tRefMs = Date.parse(msg.t_ref);
  if (!Number.isFinite(tRefMs)) throw new Error('The lightning data carry no valid reference time.');
  const p = msg.points;
  const f = msg.flashes;
  const d: LightningData = {
    kind: msg.kind,
    instrument: String(msg.instrument ?? ''),
    tRefMs,
    files: Array.isArray(msg.files) ? msg.files.map(String) : [],
    n: Number(p.n) || 0,
    t: f32(p.t),
    lat: f32(p.lat),
    lon: f32(p.lon),
    altKm: f32(p.alt_km),
    strength: f32(p.strength),
    brightness: f32(p.brightness),
    stations: i32(p.stations),
    flash: i32(p.flash),
    nf: Number(f.n) || 0,
    f: {
      t0: f32(f.t0),
      t1: f32(f.t1),
      lat: f32(f.lat),
      lon: f32(f.lon),
      altKm: f32(f.alt_km),
      nPoints: i32(f.n_points),
      strength: f32(f.strength),
      areaKm2: f32(f.area_km2),
    },
    coverage: Array.isArray(msg.coverage) ? msg.coverage.map((c: number[]) => [Number(c[0]), Number(c[1])] as [number, number]) : [],
    strengthInfo: {
      unit: String(msg.strength?.unit ?? ''),
      what: String(msg.strength?.what ?? ''),
      brightnessScale: String(msg.strength?.brightness_scale ?? ''),
    },
    meta: msg.meta ?? {},
    notes: Array.isArray(msg.notes) ? msg.notes.map(String) : [],
    provenance: msg.provenance,
  };
  if (d.t.length !== d.n || d.flash.length !== d.n || d.f.t0.length !== d.nf) {
    throw new Error('The lightning data are inconsistent (column lengths differ).');
  }
  return d;
}

// ── The time window ─────────────────────────────────────────────────────────

export interface WindowSpec {
  /** Window start, minutes from the volume time (negative = before). */
  startMin: number;
  /** Window length, minutes. */
  lengthMin: number;
  /** Time constant of the fade, minutes (brightness is exp(-age / decay), age counted from the window end). */
  decayMin: number;
}

export const DEFAULT_WINDOW: WindowSpec = { startMin: 0, lengthMin: 5, decayMin: 1.5 };

/** The window as seconds after the data reference time, for a volume time (epoch ms). */
export function windowSeconds(d: Pick<LightningData, 'tRefMs'>, volumeMs: number, w: WindowSpec): { start: number; end: number } {
  const start = (volumeMs - d.tRefMs) / 1000 + w.startMin * 60;
  return { start, end: start + w.lengthMin * 60 };
}

/** First index in the ascending array with value >= x. */
export function lowerBound(a: ArrayLike<number>, x: number): number {
  let lo = 0;
  let hi = a.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (a[mid] < x) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** Brightness factor of something `age` seconds old (1 = new): exp(-age / decay). Negative ages (the future) are 0. */
export function fade(ageS: number, decayS: number): number {
  if (!(ageS >= 0) || !(decayS > 0)) return 0;
  return Math.exp(-ageS / decayS);
}

/** Smallest fade that is still drawn. */
export const MIN_FADE = 0.03;

export interface Visible {
  /** Indices of the points in the window (ascending time). */
  idx: number[];
  /** Brightness 0..1 of each: the point's strength-brightness times the fade by age. */
  b: number[];
  /** Age in seconds from the window end. */
  age: number[];
  startS: number;
  endS: number;
}

/** The points whose time is inside [start, end] seconds, with their faded brightness. Points are sorted by time. */
export function visiblePoints(d: LightningData, startS: number, endS: number, decayMin: number): Visible {
  const idx: number[] = [];
  const b: number[] = [];
  const age: number[] = [];
  const decayS = Math.max(1, decayMin * 60);
  for (let i = lowerBound(d.t, startS); i < d.n && d.t[i] <= endS; i++) {
    const a = endS - d.t[i];
    const fd = fade(a, decayS);
    if (fd < MIN_FADE) continue;
    idx.push(i);
    b.push(Math.min(1, (0.25 + 0.75 * d.brightness[i]) * fd));
    age.push(a);
  }
  return { idx, b, age, startS, endS };
}

/** The visible points grouped by flash, each flash's points in time order (the "channel"). */
export function channelsOf(d: LightningData, idx: number[]): Map<number, number[]> {
  const out = new Map<number, number[]>();
  for (const i of idx) {
    const fl = d.flash[i];
    const list = out.get(fl);
    if (list) list.push(i);
    else out.set(fl, [i]);
  }
  return out;
}

/** The fraction (0..1) of [startS, endS] covered by the data intervals. */
export function coveredFraction(coverage: [number, number][], startS: number, endS: number): number {
  const span = endS - startS;
  if (!(span > 0)) return 0;
  let covered = 0;
  for (const [a, b] of coverage) covered += Math.max(0, Math.min(b, endS) - Math.max(a, startS));
  return Math.min(1, covered / span);
}

/** What the layer says about a window: nothing is drawn from a gap and the panel says so. */
export function windowCoverageText(d: LightningData, startS: number, endS: number): string {
  const frac = coveredFraction(d.coverage, startS, endS);
  if (d.coverage.length === 0) return 'The data carry no coverage times.';
  if (frac >= 0.999) return '';
  if (frac <= 0.001) return 'This window is outside the time the loaded files cover: no data, so nothing is drawn (this is not "no lightning").';
  return `The loaded files cover ${Math.round(frac * 100)} % of this window; the rest has no data (not "no lightning").`;
}

// ── Series ──────────────────────────────────────────────────────────────────

export interface Series {
  /** Bin edges, seconds after tRef (length n+1). */
  edges: number[];
  count: number[];
  /** Per minute; NaN where the data do not cover the whole bin. */
  rate: number[];
}

/**
 * Counts of event times per bin and the rate per minute. A bin the data do not cover completely is a gap
 * (NaN), not zero. The last bin may be shorter than `binS`; its rate uses its true length. Same maths as
 * `rate_series` in the engine (tested against the same hand counts).
 */
export function rateSeries(times: ArrayLike<number>, startS: number, endS: number, binS = 60, coverage?: [number, number][]): Series {
  if (!(endS > startS) || !(binS > 0)) throw new Error('need end > start and a positive bin length');
  const n = Math.ceil((endS - startS) / binS - 1e-9);
  const edges: number[] = [];
  for (let k = 0; k <= n; k++) edges.push(startS + binS * k);
  edges[n] = Math.min(edges[n], endS);
  const count = new Array<number>(n).fill(0);
  for (let i = 0; i < times.length; i++) {
    const t = times[i];
    if (t < startS || t > endS) continue;
    let k = Math.floor((t - startS) / binS);
    if (k >= n) k = n - 1; // an event exactly at the end belongs to the last bin
    count[k]++;
  }
  const rate = count.map((c, k) => c / ((edges[k + 1] - edges[k]) / 60));
  if (coverage) {
    for (let k = 0; k < n; k++) {
      const lo = edges[k];
      const hi = edges[k + 1];
      if (!coverage.some(([a, b]) => a <= lo + 1e-9 && b >= hi - 1e-9)) {
        count[k] = NaN;
        rate[k] = NaN;
      }
    }
  }
  return { edges, count, rate };
}

/** The time range worth plotting: the files' coverage joined with the first and last event. */
export function dataSpan(d: LightningData): [number, number] | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const [a, b] of d.coverage) {
    lo = Math.min(lo, a);
    hi = Math.max(hi, b);
  }
  if (d.n > 0) {
    lo = Math.min(lo, d.t[0]);
    hi = Math.max(hi, d.t[d.n - 1]);
  }
  return Number.isFinite(lo) && hi > lo ? [lo, hi] : null;
}

/** A bin length that gives 20 to 60 bins over a span: 10 s, 30 s, 1, 2, 5, 10, 30 min. */
export function niceBin(spanS: number): number {
  for (const b of [10, 30, 60, 120, 300, 600, 1800]) if (spanS / b <= 60) return b;
  return 3600;
}

/** Flash-rate series (flash start times) over the span of the data. */
export function flashRate(d: LightningData, binS?: number): Series | null {
  const span = dataSpan(d);
  if (!span) return null;
  const bin = binS ?? niceBin(span[1] - span[0]);
  return rateSeries(d.f.t0, span[0], span[1], bin, d.coverage);
}

/** Point (source or group) rate series over the span of the data. */
export function pointRate(d: LightningData, binS?: number): Series | null {
  const span = dataSpan(d);
  if (!span) return null;
  const bin = binS ?? niceBin(span[1] - span[0]);
  return rateSeries(d.t, span[0], span[1], bin, d.coverage);
}

// ── Positions ───────────────────────────────────────────────────────────────

export interface Placed {
  /** Metres east / north of the radar (azimuthal equidistant, the projection of the PPI and the map). */
  x: Float64Array;
  y: Float64Array;
}

/** Positions of all points relative to a radar. */
export function placeAll(d: LightningData, radarLat: number, radarLon: number): Placed {
  const x = new Float64Array(d.n);
  const y = new Float64Array(d.n);
  for (let i = 0; i < d.n; i++) {
    const [px, py] = latLonToDataSpace(d.lat[i], d.lon[i], radarLat, radarLon);
    x[i] = px;
    y[i] = py;
  }
  return { x, y };
}

export interface ScenePoint {
  /** km east / north of the radar, km above the scene ground (GLM: 0). */
  x: number;
  y: number;
  z: number;
  b: number;
  flash: number;
}

/**
 * Points of a window as scene positions for the 3D view. LMA heights are metres above mean sea level in
 * the file; the scene's height is above the radar's ground, so the radar altitude (km ASL, from the
 * file) is subtracted (terrain between is not modelled). GLM has no height: its points are drawn on the
 * ground and the panel says so. Points beyond `rangeKm` of the radar are left out.
 */
export function scenePoints(d: LightningData, vis: Visible, placed: Placed, radarAltKm: number, rangeKm: number): ScenePoint[] {
  const out: ScenePoint[] = [];
  for (let k = 0; k < vis.idx.length; k++) {
    const i = vis.idx[k];
    const x = placed.x[i] / 1000;
    const y = placed.y[i] / 1000;
    if (Math.hypot(x, y) > rangeKm) continue;
    const alt = d.altKm[i];
    const z = Number.isFinite(alt) ? Math.max(0, alt - radarAltKm) : 0;
    out.push({ x, y, z, b: vis.b[k], flash: d.flash[i] });
  }
  return out;
}

// ── LMA statistics (height distribution, lightning jumps) ───────────────────

export interface LightningStats {
  height: { zKm: number[]; zStepM: number; sources: number[]; initiations: number[]; reference: string };
  rate: { binS: number; edgesS: number[]; perMin: (number | null)[] };
  jump: { available: boolean; reason: string; startS: number[]; level: number[]; sigma?: number; minRatePerMin?: number; gapsSetToZero?: number };
  notes: string[];
  references: string[];
}

/** The answer of `lightning_stats` (computed by radarx in the engine). */
export function decodeStats(msg: any): LightningStats {
  if (!msg?.height || !Array.isArray(msg.height.z_km) || !msg.jump) throw new Error('The engine sent lightning statistics in a form this version does not know.');
  const h = msg.height;
  return {
    height: { zKm: h.z_km.map(Number), zStepM: Number(h.z_step_m), sources: h.sources.map(Number), initiations: h.initiations.map(Number), reference: String(h.reference ?? '') },
    rate: { binS: Number(msg.rate?.bin_s ?? 60), edgesS: (msg.rate?.edges_s ?? []).map(Number), perMin: msg.rate?.per_min ?? [] },
    jump: {
      available: !!msg.jump.available,
      reason: String(msg.jump.reason ?? ''),
      startS: (msg.jump.start_s ?? []).map(Number),
      level: (msg.jump.level ?? []).map(Number),
      sigma: msg.jump.sigma,
      minRatePerMin: msg.jump.min_rate_per_min,
      gapsSetToZero: msg.jump.gaps_set_to_zero,
    },
    notes: Array.isArray(msg.notes) ? msg.notes.map(String) : [],
    references: Array.isArray(msg.references) ? msg.references.map(String) : [],
  };
}

/**
 * The height (km) below which a share `p` (0..1) of the counts lies, by linear interpolation inside the bin
 * that crosses it. `zKm` are bin centres, ascending, evenly spaced by `stepKm`. NaN when there are no counts.
 */
export function heightPercentile(zKm: number[], counts: number[], p: number, stepKm: number): number {
  const total = counts.reduce((a, c) => a + c, 0);
  if (!(total > 0)) return NaN;
  const target = p * total;
  let acc = 0;
  for (let i = 0; i < counts.length; i++) {
    if (acc + counts[i] >= target && counts[i] > 0) {
      const frac = (target - acc) / counts[i];
      return zKm[i] - stepKm / 2 + frac * stepKm;
    }
    acc += counts[i];
  }
  return zKm[zKm.length - 1] + stepKm / 2;
}

/** Centre of the bin with the most counts (km); NaN without counts. */
export function modeHeight(zKm: number[], counts: number[]): number {
  let best = -1;
  let at = NaN;
  for (let i = 0; i < counts.length; i++) {
    if (counts[i] > best) {
      best = counts[i];
      at = zKm[i];
    }
  }
  return best > 0 ? at : NaN;
}

// ── The request ─────────────────────────────────────────────────────────────

/** [south, west, north, east] degrees of a square `rangeKm` around a point (longitudes wrapped into -180..180). */
export function boxAround(lat: number, lon: number, rangeKm: number): [number, number, number, number] {
  const dLat = rangeKm / 111.2;
  const south = Math.max(-89.9, lat - dLat);
  const north = Math.min(89.9, lat + dLat);
  const cos = Math.max(0.05, Math.cos((Math.max(Math.abs(south), Math.abs(north)) * Math.PI) / 180));
  const dLon = Math.min(180, rangeKm / (111.2 * cos));
  const wrap = (x: number) => ((((x + 180) % 360) + 360) % 360) - 180;
  return [south, dLon >= 180 ? -180 : wrap(lon - dLon), north, dLon >= 180 ? 180 : wrap(lon + dLon)];
}

/** Everything the engine needs to read the files; the parameters come from the settings (all shown in the panel). */
export interface OpenParams {
  parallaxOn: boolean;
  parallaxKm: number;
  qc: { maxChi2: number; minStations: number };
  link: { distanceM: number; timeS: number; minPoints: number; maxDurationS: number | null };
}

export const DEFAULT_OPEN: OpenParams = {
  parallaxOn: false,
  parallaxKm: 10,
  // Fuchs et al. (2016): chi-square at most 1 and at least six stations; 3 km, 150 ms, at most 3 s (Colorado LMA values)
  qc: { maxChi2: 1, minStations: 6 },
  link: { distanceM: 3000, timeS: 0.15, minPoints: 1, maxDurationS: 3 },
};

/** The `lightning_open` message for the files, the radar (when one is open) and the parameters. */
export function buildOpenRequest(requestId: string, paths: string[], radar: { lat: number; lon: number; rangeKm: number } | null, p: OpenParams): Record<string, unknown> {
  const msg: Record<string, unknown> = { type: 'lightning_open', request_id: requestId, paths };
  if (radar) msg.box = boxAround(radar.lat, radar.lon, radar.rangeKm);
  if (p.parallaxOn) msg.parallax_to_km = p.parallaxKm;
  msg.max_chi2 = p.qc.maxChi2;
  msg.min_stations = p.qc.minStations;
  msg.link = {
    distance_m: p.link.distanceM,
    time_s: p.link.timeS,
    min_points: p.link.minPoints,
    max_duration_s: p.link.maxDurationS,
  };
  return msg;
}

// ── Numbers for the panel ───────────────────────────────────────────────────

export interface WindowStats {
  flashes: number;
  points: number;
  /** Flashes per minute in the window (NaN when the data do not cover all of it). */
  ratePerMin: number;
  meanPointsPerFlash: number;
}

/** Flashes (by start time) and points in a window, and the rate per minute. */
export function windowStats(d: LightningData, startS: number, endS: number): WindowStats {
  let flashes = 0;
  for (let i = lowerBound(d.f.t0, startS); i < d.nf; i++) {
    if (d.f.t0[i] > endS) break;
    flashes++;
  }
  // flash start times are sorted by the engine (GLM: first event; LMA: first source), so the loop above is a range scan
  let points = 0;
  for (let i = lowerBound(d.t, startS); i < d.n && d.t[i] <= endS; i++) points++;
  const covered = coveredFraction(d.coverage, startS, endS) >= 0.999;
  const minutes = (endS - startS) / 60;
  return {
    flashes,
    points,
    ratePerMin: covered && minutes > 0 ? flashes / minutes : NaN,
    meanPointsPerFlash: flashes ? points / flashes : 0,
  };
}

/** "3 flashes", "1 flash". */
export function flashesText(n: number): string {
  return `${n} ${n === 1 ? 'flash' : 'flashes'}`;
}

export function kindLabel(k: LightningKind): string {
  return k === 'glm' ? 'GOES GLM flashes (observed)' : 'LMA sources and flashes (observed)';
}

/**
 * The words about where the instrument sees: stations and field of view when known. Detection
 * efficiency is not computed or drawn; the text says so.
 */
export function coverageNote(d: LightningData): string {
  if (d.kind === 'glm') {
    const fov = d.meta?.field_of_view;
    const fovText = fov ? ` The field of view reaches ${fov.lat[0].toFixed(0)} to ${fov.lat[1].toFixed(0)} degrees latitude and ${fov.lon[0].toFixed(0)} to ${fov.lon[1].toFixed(0)} degrees longitude.` : '';
    return `GLM pixels are about 8 km at nadir and grow toward the edge of the field of view. The file does not give a detection efficiency; it is lower toward the edge and for weak flashes, so a flash that is not drawn may still have happened.${fovText}`;
  }
  const n = Array.isArray(d.meta?.stations) ? d.meta.stations.length : null;
  return `${n ? `The array has ${n} stations in the file. ` : ''}Sources are located best inside and near the network; detection efficiency and location accuracy fall with range and with fewer stations on a source. This program does not compute a detection efficiency, so the edge of the useful area is not drawn.`;
}
