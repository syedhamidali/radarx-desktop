/**
 * Pure logic of the "Radar sites" layer: the site catalogue (bundled NEXRAD list + a user-supplied
 * CSV/JSON), scan-age state, screen placement with label declutter, and the network-consent rules.
 *
 * No imports and no side effects, so it runs under plain node for the unit tests.
 *
 * Science rule of this layer: a marker has a "recent" or "stale" state only when the engine has
 * answered for that very site in this session (an S3 listing). Without an answer the state is
 * "unknown" (neutral). Nothing is guessed from the clock, the time of day or the network type.
 */

export interface RadarSite {
  id: string;
  name: string;
  lat: number;
  lon: number;
  elev_m: number | null;
  /** US state / region code when the catalogue has one. */
  state?: string;
  /** Network label of a user-supplied catalogue ("NEXRAD" for the bundled list). */
  network?: string;
  /** True for sites that came from the user's own file. */
  custom?: boolean;
}

export const NEXRAD_NETWORK = 'NEXRAD';

// ── Catalogue parsing (bundled JSON and user files) ─────────────────────────────────────────

export const MAX_CUSTOM_SITES = 5000;
const ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.\-]{0,15}$/;

export interface SiteRejection {
  /** 1-based row (CSV, header excluded) or array index + 1 (JSON). */
  row: number;
  reason: string;
}

export interface ParsedSites {
  sites: RadarSite[];
  rejected: SiteRejection[];
  format: 'csv' | 'json' | 'unknown';
  /** Set when the whole file is unusable. */
  error?: string;
}

function num(v: unknown): number {
  if (typeof v === 'number') return v;
  if (typeof v !== 'string') return NaN;
  const t = v.trim();
  return t === '' ? NaN : Number(t);
}

/** Validate one raw record into a site, or return the reason it was refused. */
export function toSite(raw: Record<string, unknown>, network?: string): RadarSite | string {
  const pick = (...keys: string[]) => {
    for (const k of keys) {
      for (const rk of Object.keys(raw)) {
        if (rk.trim().toLowerCase() === k) return raw[rk];
      }
    }
    return undefined;
  };
  const id = String(pick('id', 'site', 'code', 'icao', 'station', 'identifier') ?? '').trim();
  if (!ID_RE.test(id)) return id ? `bad id "${id.slice(0, 20)}"` : 'missing id';
  const lat = num(pick('lat', 'latitude'));
  const lon = num(pick('lon', 'lng', 'long', 'longitude'));
  if (!Number.isFinite(lat) || lat < -90 || lat > 90) return `latitude out of range (${id})`;
  if (!Number.isFinite(lon) || lon < -180 || lon > 360) return `longitude out of range (${id})`;
  const elevRaw = num(pick('elev_m', 'elevation_m', 'elevation', 'elev', 'height_m', 'altitude'));
  const nameRaw = String(pick('name', 'label', 'city', 'location') ?? '').trim();
  const netRaw = String(pick('network', 'type', 'country') ?? '').trim();
  const stateRaw = String(pick('state', 'region') ?? '').trim();
  return {
    id,
    name: (nameRaw || id).slice(0, 60),
    lat,
    lon: lon > 180 ? lon - 360 : lon,
    elev_m: Number.isFinite(elevRaw) ? elevRaw : null,
    ...(stateRaw ? { state: stateRaw.slice(0, 12) } : {}),
    network: (netRaw || network || 'custom').slice(0, 24),
    custom: true,
  };
}

/** Split one CSV line (comma, semicolon or tab; double quotes with "" escapes). */
export function splitCsvLine(line: string, delim: string): string[] {
  const out: string[] = [];
  let cur = '';
  let q = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (q) {
      if (ch === '"') {
        if (line[i + 1] === '"') { cur += '"'; i++; } else q = false;
      } else cur += ch;
    } else if (ch === '"') q = true;
    else if (ch === delim) { out.push(cur); cur = ''; }
    else cur += ch;
  }
  out.push(cur);
  return out.map((s) => s.trim());
}

/**
 * Parse a user-supplied site list. JSON: an array of objects or `{ "sites": [...] }`; CSV: a header
 * row (id, name, lat, lon, optional elev_m / network / state) then one site per line. Rows that do
 * not validate are reported, not silently dropped; the first record of an id wins.
 */
export function parseSiteList(text: string, fileName = ''): ParsedSites {
  const body = text.replace(/^﻿/, '').trim();
  const empty = (error: string, format: ParsedSites['format'] = 'unknown'): ParsedSites => ({
    sites: [], rejected: [], format, error,
  });
  if (!body) return empty('The file is empty.');
  const looksJson = /\.json$/i.test(fileName) || body[0] === '[' || body[0] === '{';
  const records: Array<Record<string, unknown>> = [];
  let network: string | undefined;
  let format: ParsedSites['format'];

  if (looksJson) {
    format = 'json';
    let doc: unknown;
    try { doc = JSON.parse(body); } catch { return empty('Not valid JSON.', 'json'); }
    let arr: unknown = doc;
    if (doc && typeof doc === 'object' && !Array.isArray(doc)) {
      const d = doc as Record<string, unknown>;
      if (typeof d.network === 'string') network = d.network;
      arr = d.sites ?? d.stations ?? d.radars;
    }
    if (!Array.isArray(arr)) return empty('Expected a list of sites ([...] or {"sites": [...]}).', 'json');
    for (const r of arr) records.push(r && typeof r === 'object' && !Array.isArray(r) ? (r as Record<string, unknown>) : {});
  } else {
    format = 'csv';
    const lines = body.split(/\r?\n/).filter((l) => l.trim() !== '' && !l.trim().startsWith('#'));
    if (lines.length < 2) return empty('A CSV needs a header row and at least one site.', 'csv');
    const delim = [',', ';', '\t'].reduce((best, d) =>
      lines[0].split(d).length > lines[0].split(best).length ? d : best, ',');
    const head = splitCsvLine(lines[0], delim);
    for (let i = 1; i < lines.length; i++) {
      const cells = splitCsvLine(lines[i], delim);
      const rec: Record<string, unknown> = {};
      head.forEach((h, j) => { rec[h] = cells[j] ?? ''; });
      records.push(rec);
    }
  }

  const sites: RadarSite[] = [];
  const rejected: SiteRejection[] = [];
  const seen = new Set<string>();
  records.forEach((raw, i) => {
    if (sites.length >= MAX_CUSTOM_SITES) {
      if (rejected.length < 50) rejected.push({ row: i + 1, reason: `more than ${MAX_CUSTOM_SITES} sites: the rest is ignored` });
      return;
    }
    const s = toSite(raw, network);
    if (typeof s === 'string') { if (rejected.length < 50) rejected.push({ row: i + 1, reason: s }); return; }
    const key = s.id.toUpperCase();
    if (seen.has(key)) { if (rejected.length < 50) rejected.push({ row: i + 1, reason: `duplicate id ${s.id}` }); return; }
    seen.add(key);
    sites.push(s);
  });
  if (sites.length === 0) return { sites, rejected, format, error: 'No usable site in the file.' };
  return { sites, rejected, format };
}

/** Bundled NEXRAD document -> sites. Records that do not validate are dropped (the file is ours). */
export function nexradSitesFrom(doc: { sites?: Array<Record<string, any>> }): RadarSite[] {
  const out: RadarSite[] = [];
  for (const r of doc.sites ?? []) {
    if (!r || typeof r.id !== 'string' || !Number.isFinite(r.lat) || !Number.isFinite(r.lon)) continue;
    out.push({
      id: r.id,
      name: String(r.name ?? r.id),
      lat: r.lat,
      lon: r.lon,
      elev_m: Number.isFinite(r.elev_m) ? r.elev_m : null,
      ...(r.state ? { state: String(r.state) } : {}),
      network: NEXRAD_NETWORK,
    });
  }
  return out;
}

/** Built-in sites (optional) plus the user's; a user site replaces a built-in one with the same id. */
export function mergeCatalogue(builtin: RadarSite[], custom: RadarSite[], includeBuiltin: boolean): RadarSite[] {
  const byId = new Map<string, RadarSite>();
  if (includeBuiltin) for (const s of builtin) byId.set(s.id.toUpperCase(), s);
  for (const s of custom) byId.set(s.id.toUpperCase(), s);
  return [...byId.values()].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

/** Only sites of the bundled NEXRAD list can be fetched (the engine reads AWS NEXRAD Level II). */
export function isFetchable(site: RadarSite, builtinIds: ReadonlySet<string>): boolean {
  return builtinIds.has(site.id.toUpperCase());
}

// ── Scan age and state ──────────────────────────────────────────────────────────────────────

export type ScanState = 'unknown' | 'recent' | 'stale';
/** A volume younger than this is "recent". */
export const RECENT_S = 600;

/** `unknown` unless an age is known (a finite number >= 0). */
export function scanState(ageS: number | null | undefined): ScanState {
  if (typeof ageS !== 'number' || !Number.isFinite(ageS) || ageS < 0) return 'unknown';
  return ageS < RECENT_S ? 'recent' : 'stale';
}

/** "42 s", "7 min", "3 h 05 min", "2 d 4 h". */
export function formatAge(ageS: number): string {
  if (!Number.isFinite(ageS) || ageS < 0) return 'unknown';
  const s = Math.round(ageS);
  if (s < 90) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 120) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${String(m % 60).padStart(2, '0')} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

export function formatBytes(n: number | null | undefined): string {
  if (typeof n !== 'number' || !Number.isFinite(n) || n < 0) return 'unknown size';
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}

/** Engine reply of `get_nexrad_latest_info` (the fields this layer reads). */
export interface LatestInfo {
  status: 'ok' | 'not_found' | 'offline' | 'error';
  station: string;
  name?: string;
  path?: string;
  size?: number;
  scan_time?: string;
  age_s?: number;
  last_modified?: string;
  /** The engine's UTC clock when it answered. */
  now?: string;
  message?: string;
}

/** Age (s) of the scan right now: the engine's age at reply time plus the time since the reply. */
export function liveAge(info: LatestInfo | undefined, receivedAtMs: number, nowMs: number): number | null {
  if (!info || info.status !== 'ok' || typeof info.age_s !== 'number' || !Number.isFinite(info.age_s)) return null;
  return info.age_s + Math.max(0, nowMs - receivedAtMs) / 1000;
}

// ── Placement and declutter ─────────────────────────────────────────────────────────────────

export interface Placed {
  site: RadarSite;
  x: number;
  y: number;
  /** Draw the id label next to the dot (hidden when it would collide). */
  label: boolean;
}

export interface PlaceOptions {
  width: number;
  height: number;
  /** Sites to keep labelled whatever happens (selected, open file). */
  pinned?: ReadonlySet<string>;
  /** Higher = placed (and labelled) first. */
  priority?: (s: RadarSite) => number;
  /** Most labels at a time. */
  maxLabels?: number;
  /** Approximate glyph width of the label font in px. */
  glyphPx?: number;
}

const DOT_R = 7;
const LABEL_H = 14;

/**
 * Project the sites, drop the ones outside the view, and decide which labels fit. Greedy by
 * priority: a label is hidden when its box would overlap an earlier label or any marker dot, so
 * zooming out thins the labels instead of printing 150 overlapping ids.
 */
export function placeSites(
  sites: readonly RadarSite[],
  project: (lat: number, lon: number) => [number, number],
  o: PlaceOptions,
): Placed[] {
  const margin = DOT_R + 2;
  const glyph = o.glyphPx ?? 6.5;
  const maxLabels = o.maxLabels ?? 60;
  const prio = o.priority ?? (() => 0);
  const inView: Placed[] = [];
  for (const site of sites) {
    const [x, y] = project(site.lat, site.lon);
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    if (x < -margin || y < -margin || x > o.width + margin || y > o.height + margin) continue;
    inView.push({ site, x, y, label: false });
  }
  const order = [...inView].sort((a, b) => prio(b.site) - prio(a.site) || (a.site.id < b.site.id ? -1 : 1));
  const boxes: Array<readonly [number, number, number, number]> = [];
  // Dots of the sites handled before this one: a label never covers a more important marker.
  const doneDots: Array<readonly [number, number, number, number]> = [];
  let n = 0;
  const hit = (a: readonly number[], b: readonly number[]) => a[0] < b[2] && a[2] > b[0] && a[1] < b[3] && a[3] > b[1];
  for (const p of order) {
    const pinned = o.pinned?.has(p.site.id) ?? false;
    const w = p.site.id.length * glyph + 8;
    const box = [p.x + DOT_R, p.y - LABEL_H / 2, p.x + DOT_R + w, p.y + LABEL_H / 2] as const;
    const free = n < maxLabels && !boxes.some((b) => hit(b, box)) && !doneDots.some((d) => hit(d, box));
    if (pinned || free) {
      p.label = true;
      boxes.push(box);
      n++;
    }
    doneDots.push([p.x - DOT_R, p.y - DOT_R, p.x + DOT_R, p.y + DOT_R]);
  }
  return inView;
}

/** The site nearest to a point, within `maxKm` (haversine), or null. */
export function nearestSite(
  sites: readonly RadarSite[], lat: number, lon: number, maxKm: number,
): { site: RadarSite; km: number } | null {
  const R = 6371.0088;
  const rad = Math.PI / 180;
  let best: { site: RadarSite; km: number } | null = null;
  for (const s of sites) {
    const dLat = (s.lat - lat) * rad;
    const dLon = (s.lon - lon) * rad;
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat * rad) * Math.cos(s.lat * rad) * Math.sin(dLon / 2) ** 2;
    const km = 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
    if (km <= maxKm && (!best || km < best.km)) best = { site: s, km };
  }
  return best;
}

// ── Consent ─────────────────────────────────────────────────────────────────────────────────

/** Nothing leaves the machine before the user agrees; the choice lives for this session only. */
export interface ConsentState {
  /** The user ticked "allow for this session": further requests of these two kinds go ahead. */
  session: boolean;
}

export type ConsentStep = 'list' | 'download';
export type ConsentChoice = 'once' | 'session' | 'cancel';

export const NO_CONSENT: ConsentState = { session: false };

export function needsPrompt(state: ConsentState, _step: ConsentStep): boolean {
  return !state.session;
}

/** Apply the user's answer: whether to proceed and the new state. "cancel" never changes the state. */
export function resolveChoice(state: ConsentState, choice: ConsentChoice): { proceed: boolean; state: ConsentState } {
  if (choice === 'cancel') return { proceed: false, state };
  if (choice === 'session') return { proceed: true, state: { session: true } };
  return { proceed: true, state };
}

export interface ConsentText {
  title: string;
  /** What will be fetched, one line each. */
  lines: string[];
  /** Where the request goes. */
  host: string;
  /** Size statement shown to the user. */
  size: string;
}

export const AWS_HOST = 's3.amazonaws.com (public bucket unidata-nexrad-level2, anonymous, read-only)';

function utcDay(d: Date): string {
  return `${d.getUTCFullYear()}/${String(d.getUTCMonth() + 1).padStart(2, '0')}/${String(d.getUTCDate()).padStart(2, '0')}`;
}

/** The exact wording of the consent dialog (pure, so tests can check what the user is told). */
export function describeRequest(
  step: ConsentStep,
  station: string,
  now: Date,
  file?: { name?: string; size?: number | null },
): ConsentText {
  if (step === 'list') {
    const today = utcDay(now);
    const yday = utcDay(new Date(now.getTime() - 86_400_000));
    return {
      title: `Check the latest scan of ${station}`,
      lines: [
        `A listing of the folders ${today}/${station}/ (and ${yday}/${station}/ if today has none).`,
        'File names, sizes and times only. No radar data is downloaded.',
      ],
      host: AWS_HOST,
      size: 'About 0.1 MB at most (one entry of a few hundred bytes per volume, roughly 290 volumes a day).',
    };
  }
  return {
    title: `Download the latest volume of ${station}`,
    lines: [
      `One Level II file: ${file?.name ?? 'the latest volume'}.`,
      'It is saved in a temporary folder and opened like any other file.',
    ],
    host: AWS_HOST,
    size: `${formatBytes(file?.size)} (from the S3 listing).`,
  };
}
