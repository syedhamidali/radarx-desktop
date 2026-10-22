/**
 * Pure logic of the nowcast panel (no Svelte, no DOM, no network: unit tested with node).
 *
 * A nowcast is a FORECAST (docs/SCIENCE.md): an extrapolation of the last radar frames, experimental
 * guidance and never an observation. Everything here keeps three things together wherever a frame
 * of it is shown or saved: the class chip text (FCST), the origin and valid time with the lead,
 * and the engine's one-sentence skill statement with the guidance sentence. If the engine did not
 * send the wording, the fallbacks below say so; they never leave the label out.
 */

import { formatUtc, isoMs, parseProvenance, type Provenance } from './provenance.ts';

// ── Limits of the request ───────────────────────────────────────────────────────────────────

/** The longest lead the panel offers. Verified skill ends well before it (see LIMITS). */
export const NOWCAST_MAX_LEAD_MIN = 120;
export const NOWCAST_STEPS = [5, 10, 15] as const;
export const NOWCAST_MAX_MEMBERS = 8;
/** The engine keeps this many recent composites (the live map's frames) in memory. */
export const ENGINE_KEEPS_COMPOSITES = 4;
/** Frames the motion is estimated from (the last three give two pair estimates). */
export const MOTION_FRAMES = 3;
export const GUIDANCE_FALLBACK = 'Experimental guidance, not an official forecast.';
export const SKILL_FALLBACK = 'The engine did not send a skill statement for this run: treat it as unverified.';

export type NowcastSourceKind = 'files' | 'live' | 'grid';

export interface NowcastSettings {
  source: NowcastSourceKind;
  leadMin: number;
  stepMin: number;
  members: number;
  /** Grid cell of the gridding of open volumes, metres (1000 is the cell of the verification). */
  resolutionM: 1000 | 2000;
  /** Path of a gridded frames file (the grid source; desktop only). */
  gridPath: string;
}

export const DEFAULT_NOWCAST_SETTINGS: NowcastSettings = {
  source: 'files',
  leadMin: 60,
  stepMin: 5,
  members: 4,
  resolutionM: 1000,
  gridPath: '',
};

const num = (v: unknown, fallback: number): number => (typeof v === 'number' && Number.isFinite(v) ? v : fallback);

/** The multiples of `stepMin` up to the maximum lead (what the lead selector offers). */
export function leadChoices(stepMin: number): number[] {
  const step = snapStep(stepMin);
  const out: number[] = [];
  for (let l = step; l <= NOWCAST_MAX_LEAD_MIN + 1e-9; l += step) out.push(l);
  return out;
}

export function snapStep(stepMin: unknown): number {
  const s = num(stepMin, 5);
  return NOWCAST_STEPS.reduce((best, c) => (Math.abs(c - s) < Math.abs(best - s) ? c : best), NOWCAST_STEPS[0]) as number;
}

/** The nearest multiple of the step within [step, 120] (the engine wants a whole number of steps). */
export function snapLead(leadMin: unknown, stepMin: unknown): number {
  const step = snapStep(stepMin);
  const l = Math.min(NOWCAST_MAX_LEAD_MIN, Math.max(step, num(leadMin, 60)));
  return Math.min(Math.floor(NOWCAST_MAX_LEAD_MIN / step) * step, Math.max(step, Math.round(l / step) * step));
}

export function clampMembers(v: unknown): number {
  return Math.min(NOWCAST_MAX_MEMBERS, Math.max(0, Math.round(num(v, 0))));
}

/** Any stored or hand-edited settings object, made valid (unknown keys dropped). */
export function sanitizeSettings(raw: unknown): NowcastSettings {
  const o = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const step = snapStep(o.stepMin ?? DEFAULT_NOWCAST_SETTINGS.stepMin);
  return {
    source: o.source === 'live' || o.source === 'grid' || o.source === 'files' ? o.source : DEFAULT_NOWCAST_SETTINGS.source,
    stepMin: step,
    leadMin: snapLead(o.leadMin ?? DEFAULT_NOWCAST_SETTINGS.leadMin, step),
    members: clampMembers(o.members ?? DEFAULT_NOWCAST_SETTINGS.members),
    resolutionM: o.resolutionM === 2000 ? 2000 : 1000,
    gridPath: typeof o.gridPath === 'string' ? o.gridPath.slice(0, 1024) : '',
  };
}

/** Changing the step keeps the lead a whole number of steps. */
export function withStep(s: NowcastSettings, stepMin: number): NowcastSettings {
  const step = snapStep(stepMin);
  return { ...s, stepMin: step, leadMin: snapLead(s.leadMin, step) };
}

export function leadCount(s: Pick<NowcastSettings, 'leadMin' | 'stepMin'>): number {
  return Math.round(snapLead(s.leadMin, s.stepMin) / snapStep(s.stepMin));
}

// ── Inputs: open files, the live loop, a gridded file ───────────────────────────────────────

export interface FileRowLike {
  id: string;
  name: string;
  timeMs: number | null;
  selected: boolean;
}

export interface NowcastInput {
  /** Which message field carries the input, and the values. */
  kind: NowcastSourceKind;
  ids: string[];
  path: string;
  /** Why the run cannot start (null = it can). */
  problem: string | null;
  /** A caution that does not stop the run. */
  caution: string | null;
  /** Human summary, e.g. "3 volumes, 02:00 to 02:10 UTC". */
  summary: string;
}

const hhmm = (ms: number): string => new Date(ms).toISOString().slice(11, 16);

function spanText(times: number[]): string {
  if (times.length === 0) return '';
  const a = Math.min(...times);
  const b = Math.max(...times);
  return a === b ? `${hhmm(a)} UTC` : `${hhmm(a)} to ${hhmm(b)} UTC`;
}

/** The selected open volumes in time order. One frame (no motion) is allowed but said. */
export function inputFromFiles(rows: readonly FileRowLike[]): NowcastInput {
  const picked = rows.filter((r) => r.selected);
  const base = { kind: 'files' as const, path: '' };
  if (picked.length === 0) {
    return { ...base, ids: [], problem: 'Open radar volumes (several in time) and select them', caution: null, summary: '' };
  }
  const noTime = picked.filter((r) => r.timeMs === null);
  if (noTime.length > 0) {
    return {
      ...base,
      ids: [],
      problem: `No scan time for ${noTime.map((r) => r.name).slice(0, 3).join(', ')}: the nowcast needs the time of every volume`,
      caution: null,
      summary: '',
    };
  }
  const sorted = [...picked].sort((a, b) => (a.timeMs as number) - (b.timeMs as number));
  const times = sorted.map((r) => r.timeMs as number);
  const distinct = new Set(times.map((t) => Math.round(t / 150_000))).size;
  let caution: string | null = null;
  if (distinct < 2) {
    caution = 'One scan time only: there is no motion to follow, so the result is the last frame held still (persistence).';
  } else {
    const gaps = times.slice(1).map((t, i) => t - times[i]);
    const worst = Math.max(...gaps);
    if (worst > 30 * 60_000) caution = `A gap of ${Math.round(worst / 60_000)} min between volumes: the motion is estimated from the last ${MOTION_FRAMES} frames only.`;
  }
  return {
    ...base,
    ids: sorted.map((r) => r.id),
    problem: null,
    caution,
    summary: `${picked.length} volume${picked.length === 1 ? '' : 's'}, ${spanText(times)}`,
  };
}

export interface LiveFrameLike {
  id: string;
  time: string | null;
  mosaicId: string;
}

/** The newest frames of the live loop the engine still holds (it keeps ENGINE_KEEPS_COMPOSITES). */
export function inputFromLive(frames: readonly LiveFrameLike[], n: number = MOTION_FRAMES): NowcastInput {
  const base = { kind: 'live' as const, path: '' };
  const usable = frames.filter((f) => !!f.mosaicId);
  if (usable.length === 0) {
    return { ...base, ids: [], problem: 'The live map has no frames yet: start it (it asks before downloading) and wait for a few updates', caution: null, summary: '' };
  }
  const take = Math.max(1, Math.min(ENGINE_KEEPS_COMPOSITES, Math.round(n)));
  const newest = usable.slice(-take);
  const times = newest.map((f) => isoMs(f.time)).filter((t): t is number => t !== null);
  let caution: string | null = null;
  if (newest.length < 2) {
    caution = 'One live frame only: there is no motion to follow, so the result is the last frame held still (persistence). Wait for the next update.';
  } else if (times.length === newest.length) {
    const gaps = times.slice(1).map((t, i) => t - times[i]);
    if (Math.min(...gaps) <= 0) caution = 'Two live frames have the same time: they count as one.';
    else if (Math.max(...gaps) > 30 * 60_000) caution = `A gap of ${Math.round(Math.max(...gaps) / 60_000)} min between live frames.`;
  }
  return {
    ...base,
    ids: newest.map((f) => f.mosaicId),
    problem: null,
    caution,
    summary: `the last ${newest.length} live frame${newest.length === 1 ? '' : 's'}${times.length ? `, ${spanText(times)}` : ''}`,
  };
}

export function inputFromGrid(path: string): NowcastInput {
  const p = path.trim();
  return {
    kind: 'grid',
    ids: [],
    path: p,
    problem: p ? null : 'Give the gridded frames file (NetCDF written by the engine)',
    caution: null,
    summary: p ? p.split(/[\\/]/).pop() || p : '',
  };
}

// ── The request ─────────────────────────────────────────────────────────────────────────────

export function buildRunMessage(s: NowcastSettings, input: NowcastInput, requestId: string): Record<string, unknown> {
  const clean = sanitizeSettings(s);
  const options: Record<string, unknown> = {
    lead_min: clean.leadMin,
    step_min: clean.stepMin,
    n_members: clean.members,
    n_frames: MOTION_FRAMES,
    // the probability layers come on demand (nowcast_layers): only the nowcast itself with the run
    layers: 'deterministic',
  };
  if (input.kind === 'files') options.resolution_m = clean.resolutionM;
  const msg: Record<string, unknown> = { type: 'nowcast_run', request_id: requestId, options };
  if (input.kind === 'files') msg.file_ids = input.ids;
  else if (input.kind === 'live') msg.mosaic_ids = input.ids;
  else msg.frames_path = input.path;
  return msg;
}

// ── Products ────────────────────────────────────────────────────────────────────────────────

/** `det` (the nowcast), `p:<t>` (probability of the input quantity >= t), `r:<t>` (rain rate, mm/h). */
export type ProductKey = string;
export const DETERMINISTIC: ProductKey = 'det';

export function productKey(kind: 'deterministic' | 'probability', threshold?: number | null, rate = false): ProductKey {
  if (kind === 'deterministic') return DETERMINISTIC;
  return `${rate ? 'r' : 'p'}:${Number(threshold)}`;
}

export interface ProductRequest {
  kind: 'deterministic' | 'probability';
  threshold: number | null;
  rate: boolean;
}

export function parseProduct(key: ProductKey): ProductRequest | null {
  if (key === DETERMINISTIC) return { kind: 'deterministic', threshold: null, rate: false };
  const m = /^([pr]):(-?\d+(?:\.\d+)?)$/.exec(key);
  if (!m) return null;
  return { kind: 'probability', threshold: Number(m[2]), rate: m[1] === 'r' };
}

export interface ProductChoice {
  key: ProductKey;
  label: string;
}

/** What the "Show" selector offers for a result: the nowcast, then the probabilities. */
export function productChoices(r: Pick<NowcastResult, 'field' | 'units' | 'thresholds' | 'rateThresholds'>): ProductChoice[] {
  const out: ProductChoice[] = [{ key: DETERMINISTIC, label: `Nowcast (${r.field === 'reflectivity' ? 'reflectivity' : 'rain rate'}, ${r.units})` }];
  for (const t of r.thresholds) out.push({ key: productKey('probability', t), label: `Probability of at least ${t} ${r.units}` });
  if (r.field === 'reflectivity') for (const t of r.rateThresholds) out.push({ key: productKey('probability', t, true), label: `Probability of at least ${t} mm/h` });
  return out;
}

// ── The result ──────────────────────────────────────────────────────────────────────────────

export interface NowcastLayerImage {
  lead: number;
  kind: 'deterministic' | 'probability';
  threshold: number | null;
  rate: boolean;
  validTime: string | null;
  png: string;
  vmin: number;
  vmax: number;
  units: string;
  legend: [number, string][];
  label: string;
}

export interface NowcastResult {
  nowcastId: string;
  originTime: string;
  leads: number[];
  validTimes: string[];
  field: string;
  units: string;
  members: number;
  thresholds: number[];
  rateThresholds: number[];
  /** 1-sigma position uncertainty per lead, km (null where the engine sent none). */
  sigmaKm: (number | null)[];
  /** Fraction of the grid forecastable per lead (the rest is "not forecastable", not "dry"). */
  forecastFraction: (number | null)[];
  skill: string;
  guidance: string;
  notes: string[];
  provenance: Provenance | null;
  corners: [number, number][];
  motion: { u: number | null; v: number | null; method: string };
  sources: string[];
  nInputFrames: number;
}

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const text = (v: unknown, max = 2000): string => (typeof v === 'string' ? v.trim().slice(0, max) : '');

/** A defensive parse of `nowcast_result`; null when it is not a usable result. */
export function parseResult(msg: any): NowcastResult | null {
  if (!msg || typeof msg !== 'object') return null;
  const id = text(msg.nowcast_id, 64);
  const leads = Array.isArray(msg.leads_min) ? msg.leads_min.filter(finite) : [];
  const origin = text(msg.origin_time, 64);
  if (!id || leads.length === 0 || !origin) return null;
  const valid = Array.isArray(msg.valid_times) ? msg.valid_times.map((v: unknown) => text(v, 64)) : [];
  const statsLeads: any[] = Array.isArray(msg.stats?.leads) ? msg.stats.leads : [];
  const at = (k: number) => statsLeads.find((s) => finite(s?.lead_min) && Math.abs(s.lead_min - leads[k]) < 1e-6);
  const prov = parseProvenance(msg.provenance);
  const record = msg.record && typeof msg.record === 'object' ? msg.record : {};
  const corners = Array.isArray(msg.corners) && msg.corners.length === 4 ? (msg.corners as [number, number][]) : [];
  return {
    nowcastId: id,
    originTime: origin,
    leads,
    validTimes: leads.map((_: number, k: number) => valid[k] || ''),
    field: text(msg.field, 40) || 'reflectivity',
    units: text(msg.units, 20) || 'dBZ',
    members: finite(msg.n_members) ? Math.max(0, Math.round(msg.n_members)) : 0,
    thresholds: Array.isArray(msg.thresholds) ? msg.thresholds.filter(finite) : [],
    rateThresholds: Array.isArray(msg.rate_thresholds) ? msg.rate_thresholds.filter(finite) : [],
    sigmaKm: leads.map((_: number, k: number) => (finite(at(k)?.position_sigma_km) ? (at(k).position_sigma_km as number) : null)),
    forecastFraction: leads.map((_: number, k: number) => (finite(at(k)?.forecast_fraction) ? (at(k).forecast_fraction as number) : null)),
    skill: text(record.skill) || SKILL_FALLBACK,
    guidance: text(record.guidance, 300) || GUIDANCE_FALLBACK,
    notes: Array.isArray(msg.notes) ? msg.notes.map((n: unknown) => text(n, 600)).filter(Boolean).slice(0, 20) : [],
    provenance: prov,
    corners,
    motion: {
      u: finite(msg.motion?.u_mean_ms) ? msg.motion.u_mean_ms : null,
      v: finite(msg.motion?.v_mean_ms) ? msg.motion.v_mean_ms : null,
      method: text(msg.motion?.method, 80),
    },
    sources: Array.isArray(msg.sources) ? msg.sources.map((s: unknown) => text(s, 200)).filter(Boolean).slice(0, 8) : [],
    nInputFrames: finite(msg.n_input_frames) ? msg.n_input_frames : 0,
  };
}

/** A defensive parse of one layer of `nowcast_result.layers` / `nowcast_layers_result.layers`. */
export function parseLayer(raw: any): NowcastLayerImage | null {
  if (!raw || typeof raw !== 'object' || typeof raw.png_b64 !== 'string' || !raw.png_b64) return null;
  if (!finite(raw.lead_min)) return null;
  const kind = raw.kind === 'probability' ? 'probability' : raw.kind === 'deterministic' ? 'deterministic' : null;
  if (!kind) return null;
  return {
    lead: raw.lead_min,
    kind,
    threshold: finite(raw.threshold) ? raw.threshold : null,
    rate: raw.rate === true,
    validTime: text(raw.valid_time, 64) || null,
    png: raw.png_b64,
    vmin: finite(raw.vmin) ? raw.vmin : 0,
    vmax: finite(raw.vmax) ? raw.vmax : 1,
    units: text(raw.units, 20),
    legend: Array.isArray(raw.legend) ? raw.legend.filter((p: any) => Array.isArray(p) && finite(p[0]) && typeof p[1] === 'string') : [],
    label: text(raw.label, 200),
  };
}

export function layerKey(l: Pick<NowcastLayerImage, 'kind' | 'threshold' | 'rate'>): ProductKey {
  return productKey(l.kind, l.threshold, l.rate);
}

// ── The time slider ─────────────────────────────────────────────────────────────────────────

/** Next lead index for a key press; clamps to the ends. */
export function stepIndex(i: number, n: number, dir: number | 'first' | 'last'): number {
  if (n <= 0) return 0;
  if (dir === 'first') return 0;
  if (dir === 'last') return n - 1;
  return Math.min(n - 1, Math.max(0, i + dir));
}

/** The index of the lead nearest to `lead` (0 for an empty list). */
export function leadIndexNear(leads: readonly number[], lead: number): number {
  let best = 0;
  leads.forEach((l, k) => {
    if (Math.abs(l - lead) < Math.abs(leads[best] - lead)) best = k;
  });
  return best;
}

export const formatLead = (min: number): string => `+${Number.isInteger(min) ? min : min.toFixed(1)} min`;

export const formatSigma = (km: number | null | undefined): string =>
  km === null || km === undefined || !Number.isFinite(km) ? 'not given' : `${km < 10 ? km.toFixed(1) : Math.round(km)} km`;

/** Short, always-present label of one frame: what to print next to the picture. */
export interface FrameLabel {
  chip: string;
  /** "FORECAST +30 min" */
  headline: string;
  origin: string;
  valid: string;
  lead: string;
  sigma: string;
  product: string;
  guidance: string;
  skill: string;
  /** The members behind the probabilities ("4 members" / "single run"). */
  ensemble: string;
}

export function frameLabel(r: NowcastResult, leadIndex: number, product: ProductKey = DETERMINISTIC): FrameLabel {
  const k = Math.min(Math.max(0, leadIndex), r.leads.length - 1);
  const lead = formatLead(r.leads[k]);
  const p = parseProduct(product);
  const productText =
    !p || p.kind === 'deterministic'
      ? `Nowcast of ${r.field === 'reflectivity' ? 'reflectivity' : 'rain rate'} (${r.units})`
      : `Probability of at least ${p.threshold} ${p.rate ? 'mm/h' : r.units}`;
  return {
    chip: 'FCST',
    headline: `FORECAST ${lead}`,
    origin: `Origin ${formatUtc(r.originTime)}`,
    valid: r.validTimes[k] ? `Valid ${formatUtc(r.validTimes[k])}` : 'Valid time unknown',
    lead,
    sigma: `Position uncertainty ${formatSigma(r.sigmaKm[k])} (1 sigma)`,
    product: productText,
    guidance: r.guidance,
    skill: r.skill,
    ensemble: r.members > 1 ? `${r.members} members` : 'single run (no ensemble)',
  };
}

/** The lines printed on a saved picture (PNG): the same words as on screen, never fewer. */
export function captionLines(l: FrameLabel): string[] {
  return [`${l.chip}  ${l.headline}   ${l.origin}   ${l.valid}`, `${l.product}; ${l.sigma}; ${l.ensemble}`, l.guidance, l.skill];
}

/** Wrap text to lines of at most `maxChars` (words are not split). Used for the picture caption. */
export function wrapText(s: string, maxChars: number): string[] {
  const out: string[] = [];
  let line = '';
  for (const w of s.split(/\s+/).filter(Boolean)) {
    if (line && (line + ' ' + w).length > maxChars) {
      out.push(line);
      line = w;
    } else line = line ? line + ' ' + w : w;
  }
  if (line) out.push(line);
  return out;
}

export function exportBaseName(r: Pick<NowcastResult, 'originTime' | 'leads'>, leadIndex: number, product: ProductKey): string {
  const stamp = r.originTime.replace(/[^0-9T]/g, '').slice(0, 13) || 'origin';
  const lead = r.leads[Math.min(Math.max(0, leadIndex), r.leads.length - 1)] ?? 0;
  const p = parseProduct(product);
  const what = !p || p.kind === 'deterministic' ? 'nowcast' : `prob_${p.rate ? 'rate' : 'dbz'}${String(p.threshold).replace('.', 'p')}`;
  return `fcst_${what}_${stamp}_plus${String(Math.round(lead)).padStart(3, '0')}min`;
}

// ── Position-uncertainty chart ──────────────────────────────────────────────────────────────

export interface SigmaChart {
  width: number;
  height: number;
  left: number;
  top: number;
  right: number;
  bottom: number;
  max: number;
  points: { x: number; y: number; lead: number; km: number }[];
  /** x of the vertical marker for the shown lead (null when its value is unknown). */
  markX: number | null;
  ticksY: { y: number; label: string }[];
}

/** Geometry of the sigma-versus-lead line (an SVG of width x height). */
export function sigmaChart(leads: readonly number[], sigma: readonly (number | null)[], shown: number, width = 240, height = 84): SigmaChart {
  const left = 30;
  const right = 8;
  const top = 6;
  const bottom = 18;
  const vals = sigma.filter(finite);
  const hi = vals.length ? Math.max(...vals) : 1;
  const step = hi <= 4 ? 1 : hi <= 10 ? 2 : hi <= 20 ? 5 : 10;
  const max = Math.max(step, Math.ceil(hi / step) * step);
  const lmin = leads.length ? Math.min(...leads) : 0;
  const lmax = leads.length ? Math.max(...leads) : 1;
  const span = lmax - lmin || 1;
  const xOf = (l: number) => left + ((l - lmin) / span) * (width - left - right);
  const yOf = (v: number) => top + (1 - v / max) * (height - top - bottom);
  const points = leads.flatMap((l, k) => (finite(sigma[k]) ? [{ x: xOf(l), y: yOf(sigma[k] as number), lead: l, km: sigma[k] as number }] : []));
  const ticksY: { y: number; label: string }[] = [];
  for (let v = 0; v <= max + 1e-9; v += step) ticksY.push({ y: yOf(v), label: String(v) });
  const k = Math.min(Math.max(0, shown), leads.length - 1);
  return { width, height, left, top, right, bottom, max, points, markX: leads.length && finite(sigma[k]) ? xOf(leads[k]) : null, ticksY };
}

// ── The verified limits (docs/NOWCAST.md), short ────────────────────────────────────────────

/** What the verification on eight public NEXRAD cases says, in the words the panel shows. */
export const VERIFIED_LIMITS: readonly string[] = [
  'It only moves the echo pattern: no storm grows, decays or forms, and nothing is added upstream of the observed area.',
  'It beat holding the last frame still for the first 20 to 45 minutes in moving convection (squall lines, supercells), and it did not beat it anywhere beyond an hour.',
  'Stationary or rotating rain (lake-effect bands, hurricane bands) gained nothing over the last frame, and a near-empty frame is not extrapolated.',
  'Strong cores keep their position but not their strength: skill at 45 dBZ falls from 0.60 to 0.09 (CSI) in an hour.',
  'The probabilities carry the uncertainty of the motion only, so they are too sharp at both ends (forecast 2 percent, observed 19 percent).',
  'Grey or empty means not observed or not forecastable, never no rain.',
];

export const NOWCAST_NOT_OBSERVATION = 'A forecast is never an observation: it is drawn from past radar frames, not measured.';
