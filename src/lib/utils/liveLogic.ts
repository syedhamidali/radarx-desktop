/**
 * Pure logic of the live map (no imports, no DOM): the default-radar list, the update timer with
 * its backoff, the ring buffer of recent composite frames, the consent wording and the status
 * line. Everything here runs under plain node for the unit tests.
 *
 * Science rule: the status line only states what the engine answered (the scan time parsed from
 * the volume name, the listing's size, the engine's clock). An age is shown only when a scan
 * time is known; a radar that did not answer is listed as such, never guessed.
 */

import { formatAge, formatBytes, AWS_HOST, type ConsentText } from './radarSites.ts';

// ── Default radars ──────────────────────────────────────────────────────────────────────────

/** Most radars the live map merges at once (the engine enforces the same bound). */
export const MAX_LIVE_RADARS = 8;

/**
 * A live radar is named by one id that is unambiguous across networks: a NEXRAD radar is its bare
 * four-letter id ("KTLX", the form older saved settings already use, so nothing is migrated) and a
 * DWD radar is "dwd:ess". The networks that are live are exactly these two (docs: Live map).
 */
export type LiveNetwork = 'nexrad' | 'dwd';
const NEXRAD_ID = /^[A-Z0-9]{4}$/;
const DWD_ID = /^dwd:[a-z]{3}$/;

/** The canonical form of an id ("ktlx" -> "KTLX", "DWD:ESS" -> "dwd:ess"), or null when it is none. */
export function canonicalRadarId(raw: unknown): string | null {
  if (typeof raw !== 'string') return null;
  const t = raw.trim();
  if (t.includes(':')) {
    const [net, site] = [t.slice(0, t.indexOf(':')).toLowerCase(), t.slice(t.indexOf(':') + 1)];
    if (net === 'nexrad') return NEXRAD_ID.test(site.toUpperCase()) ? site.toUpperCase() : null;
    const id = `${net}:${site.toLowerCase()}`;
    return DWD_ID.test(id) ? id : null;
  }
  return NEXRAD_ID.test(t.toUpperCase()) ? t.toUpperCase() : null;
}

export function liveNetworkOf(id: string): LiveNetwork {
  return id.startsWith('dwd:') ? 'dwd' : 'nexrad';
}

/** The site code inside an id ("dwd:ess" -> "ess", "KTLX" -> "KTLX"). */
export function liveSiteCode(id: string): string {
  return id.includes(':') ? id.slice(id.indexOf(':') + 1) : id;
}

/**
 * The live id of a radar-catalogue site, or null when the live map cannot use it (only NEXRAD and
 * DWD volumes are fetched live; other networks are listed on the map but not claimed to work).
 */
export function liveIdOf(site: { provider?: string; provider_site?: string; data_access?: string; id?: string }): string | null {
  if (site.data_access !== 'open-loadable') return null;
  if (site.provider === 'nexrad') return canonicalRadarId(site.provider_site ?? site.id);
  if (site.provider === 'dwd' && site.provider_site) return canonicalRadarId(`dwd:${site.provider_site}`);
  return null;
}

/** Why a catalogue site cannot be a live radar (for the tooltip of a disabled button); '' when it can. */
export function liveUnavailableReason(site: { provider?: string; data_access?: string; network?: string }): string {
  if (liveIdOf(site as any)) return '';
  if (site.provider === 'fmi') return 'Finland: the open FMI bucket is a fixed two-day sample from 2023, not a live feed.';
  const net = (site.network ?? '').split('/')[0] || 'this network';
  if (site.data_access === 'open-link') return `The live map cannot fetch ${net} volumes yet: open data exist, but the app has no loader for them.`;
  if (site.data_access === 'key-required') return `${net} volumes need a key or registration; the live map does not use them.`;
  return `No open volume feed is known for ${net} radars, so the live map cannot show this radar. Live networks: NEXRAD (USA) and DWD (Germany).`;
}

/** Valid, canonical, de-duplicated ids in the given order; at most MAX_LIVE_RADARS. */
export function normalizeRadarIds(ids: unknown): string[] {
  if (!Array.isArray(ids)) return [];
  const out: string[] = [];
  for (const raw of ids) {
    const id = canonicalRadarId(raw);
    if (!id || out.includes(id)) continue;
    out.push(id);
    if (out.length >= MAX_LIVE_RADARS) break;
  }
  return out;
}

/** Add the id when absent, remove it when present; a full list is left unchanged. */
export function toggleRadarId(ids: readonly string[], id: string): string[] {
  const key = canonicalRadarId(id) ?? id.trim().toUpperCase();
  if (ids.includes(key)) return ids.filter((x) => x !== key);
  if (ids.length >= MAX_LIVE_RADARS) return [...ids];
  return normalizeRadarIds([...ids, key]);
}

/** Move the id one step up (-1) or down (+1) in the order; out-of-range moves do nothing. */
export function moveRadarId(ids: readonly string[], id: string, dir: -1 | 1): string[] {
  const i = ids.indexOf(id);
  const j = i + dir;
  if (i < 0 || j < 0 || j >= ids.length) return [...ids];
  const out = [...ids];
  [out[i], out[j]] = [out[j], out[i]];
  return out;
}

// ── Timer and backoff ───────────────────────────────────────────────────────────────────────

export const MIN_INTERVAL_MIN = 2;
export const MAX_INTERVAL_MIN = 15;
export const DEFAULT_INTERVAL_MIN = 5;
/** Longest wait between two tries after repeated failures. */
export const MAX_BACKOFF_MS = 60 * 60_000;

export function clampIntervalMin(min: unknown): number {
  const n = typeof min === 'number' && Number.isFinite(min) ? Math.round(min) : DEFAULT_INTERVAL_MIN;
  return Math.min(MAX_INTERVAL_MIN, Math.max(MIN_INTERVAL_MIN, n));
}

/**
 * Milliseconds until the next update: the interval after a success, the interval doubled per
 * consecutive failure after the first (5, 10, 20, 40 min...) and never more than MAX_BACKOFF_MS,
 * so a dead network is not hammered and a radar that comes back is noticed within the hour.
 */
export function nextDelayMs(intervalMin: number, failures: number): number {
  const base = clampIntervalMin(intervalMin) * 60_000;
  const f = Math.max(0, Math.floor(failures));
  if (f <= 1) return base;
  return Math.min(MAX_BACKOFF_MS, base * 2 ** (f - 1));
}

/** "4:32" for a countdown, "now" when due, "" when nothing is scheduled. */
export function countdownText(dueAt: number | null, nowMs: number): string {
  if (dueAt === null || !Number.isFinite(dueAt)) return '';
  const s = Math.round((dueAt - nowMs) / 1000);
  if (s <= 0) return 'now';
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

// ── Check results and what to download ──────────────────────────────────────────────────────

/** One entry of a `live_check_result` (an engine `latest_scan_info` plus `held`). */
export interface LiveCheckEntry {
  station: string;
  provider?: LiveNetwork;
  /** The volume key of a DWD volume. */
  key?: string;
  status: 'ok' | 'not_found' | 'offline' | 'error';
  name?: string;
  path?: string;
  size?: number;
  scan_time?: string;
  age_s?: number;
  message?: string;
  held?: boolean;
}

export interface LiveFileRequest {
  station: string;
  provider?: LiveNetwork;
  /** The s3:// URL of a NEXRAD volume. */
  path?: string;
  /** The volume key of a DWD volume. */
  key?: string;
  name: string;
  size: number;
  scan_time?: string;
}

export interface DownloadPlan {
  /** Every radar whose newest volume is known, held or not (what the frame will be built from). */
  files: LiveFileRequest[];
  /** The subset that has to be downloaded now, and their exact total size from the listing. */
  toDownload: LiveFileRequest[];
  bytes: number;
  /** Radars that gave no usable volume, with the engine's reason. */
  failed: { station: string; message: string }[];
}

/** Split a check result into the files of the frame, the downloads it needs, and the failures. */
export function planDownloads(entries: readonly LiveCheckEntry[]): DownloadPlan {
  const files: LiveFileRequest[] = [];
  const toDownload: LiveFileRequest[] = [];
  const failed: DownloadPlan['failed'] = [];
  for (const e of entries) {
    if (e.status === 'ok' && (e.path || e.key) && e.name) {
      const f: LiveFileRequest = {
        station: e.station,
        ...(e.provider ? { provider: e.provider } : {}),
        ...(e.path ? { path: e.path } : {}),
        ...(e.key ? { key: e.key } : {}),
        name: e.name,
        size: typeof e.size === 'number' && Number.isFinite(e.size) ? e.size : 0,
        ...(e.scan_time ? { scan_time: e.scan_time } : {}),
      };
      files.push(f);
      if (!e.held) toDownload.push(f);
    } else {
      failed.push({ station: e.station, message: e.message ?? (e.status === 'not_found' ? 'no recent volume' : e.status) });
    }
  }
  return { files, toDownload, bytes: toDownload.reduce((n, f) => n + f.size, 0), failed };
}

/** Same volumes as the frame already shown: nothing new to build. */
export function sameVolumes(a: readonly { station: string; name: string }[], b: readonly { station: string; name: string }[]): boolean {
  if (a.length !== b.length) return false;
  const key = (x: { station: string; name: string }) => `${x.station}:${x.name}`;
  const sa = new Set(a.map(key));
  return b.every((x) => sa.has(key(x)));
}

// ── Consent wording ─────────────────────────────────────────────────────────────────────────

function utcDay(d: Date): string {
  return `${d.getUTCFullYear()}/${String(d.getUTCMonth() + 1).padStart(2, '0')}/${String(d.getUTCDate()).padStart(2, '0')}`;
}

/** What the engine says about a network's server (from `list_network_providers`; no network call). */
export interface LiveProviderInfo {
  name?: string;
  host: string;
  listing_text: string;
  listing_bytes: number;
  attribution: string;
  licence: string;
}

/** Used only when the engine could not be asked; the same words as the engine's DWD provider. */
const DWD_FALLBACK: LiveProviderInfo = {
  name: 'DWD Open Data',
  host: 'opendata.dwd.de (anonymous, read-only)',
  listing_text: 'Two folder indexes (reflectivity and radial velocity sweeps of the last two days): file names, times and sizes only. No radar data.',
  listing_bytes: 2_200_000,
  attribution: 'Source: Deutscher Wetterdienst (DWD), Open Data radar volume sweeps',
  licence: 'CC BY 4.0 with the source acknowledged (DWD legal notice)',
};

function split(stations: readonly string[]): { nexrad: string[]; dwd: string[] } {
  return { nexrad: stations.filter((s) => liveNetworkOf(s) === 'nexrad'), dwd: stations.filter((s) => liveNetworkOf(s) === 'dwd') };
}

/** The first dialog of a live start: the listings (metadata only), per network with its own server and size. */
export function describeLiveListing(stations: readonly string[], now: Date, info: { dwd?: LiveProviderInfo } = {}): ConsentText {
  const today = utcDay(now);
  const n = stations.length;
  const { nexrad, dwd } = split(stations);
  const dwdInfo = info.dwd ?? DWD_FALLBACK;
  const lines: string[] = [];
  if (nexrad.length) {
    const lead = dwd.length ? 'NEXRAD: a listing' : 'A listing';
    lines.push(`${lead} of today's folder ${today}/<SITE>/ (and yesterday's if today has none) for ${nexrad.join(', ')}: file names, sizes and times. No radar data yet.`);
  }
  if (dwd.length) {
    lines.push(`DWD (${dwd.map(liveSiteCode).join(', ')}): ${dwdInfo.listing_text} About ${formatBytes(dwdInfo.listing_bytes)} per radar.`);
  }
  lines.push(
    'Then the newest volume of each radar is offered for download with its exact size, in a second dialog.',
    'While the live map runs, the same listing is repeated at every update.',
  );
  const hosts = [...(nexrad.length ? [AWS_HOST] : []), ...(dwd.length ? [dwdInfo.host] : [])];
  const bytes = nexrad.length * 100_000 + dwd.length * dwdInfo.listing_bytes;
  const parts = [nexrad.length ? `${nexrad.length} NEXRAD at about 0.1 MB` : '', `${dwd.length} DWD at about ${formatBytes(dwdInfo.listing_bytes)}`].filter(Boolean);
  const size = dwd.length
    ? `About ${formatBytes(bytes)} per update in all (${parts.join(', ')}).`
    : `About ${(0.1 * n).toFixed(1)} MB at most per update (one entry of a few hundred bytes per volume, roughly 290 volumes a day per radar).`;
  return {
    title: `Start the live map (${n} radar${n === 1 ? '' : 's'})`,
    lines,
    host: hosts.join(' and '),
    size,
  };
}

/** The second dialog: the volumes to download now, with their exact total size and the credit of the data. */
export function describeLiveDownload(plan: DownloadPlan, intervalMin: number, info: { dwd?: LiveProviderInfo } = {}): ConsentText {
  const n = plan.toDownload.length;
  const { nexrad, dwd } = split(plan.toDownload.map((f) => f.station));
  const dwdInfo = info.dwd ?? DWD_FALLBACK;
  const lines = plan.toDownload.map((f) => `${f.station}: ${f.name} (${formatBytes(f.size)})`);
  if (plan.failed.length) lines.push(`Not available now: ${plan.failed.map((f) => `${f.station} (${f.message})`).join('; ')}.`);
  if (dwd.length) {
    lines.push(`DWD data: ${dwdInfo.attribution}. Licence: ${dwdInfo.licence}. A DWD volume is assembled from 20 small files (ten sweeps, reflectivity and radial velocity); the size above is their sum.`);
  }
  lines.push(
    `"Allow while the app is open" keeps updating every ${clampIntervalMin(intervalMin)} min: each update downloads only a radar's new volume (about this size each time a radar has one). "Allow once" updates one time, then pauses.`,
  );
  const hosts = [...(nexrad.length ? [AWS_HOST] : []), ...(dwd.length ? [dwdInfo.host] : [])];
  return {
    title: dwd.length ? `Download ${n} volume${n === 1 ? '' : 's'} for the live map` : `Download ${n} Level II volume${n === 1 ? '' : 's'} for the live map`,
    lines,
    host: hosts.join(' and ') || AWS_HOST,
    size: `${formatBytes(plan.bytes)} now (the exact sizes from the listing).`,
  };
}

// ── Frames (the recent loop) ────────────────────────────────────────────────────────────────

/**
 * One composite frame kept in memory. `time` is the mosaic's analysis time (the newest volume
 * start among its radars, UTC); `radars` names the exact volumes it was made from.
 */
export interface FrameMeta {
  id: string;
  time: string | null;
  receivedAt: number;
  radars: { station: string; name: string; scan_time?: string | null }[];
}

export const DEFAULT_MAX_FRAMES = 12;

/**
 * Append a frame to the ring buffer: a frame made of the same volumes as the newest one is not
 * added (the composite did not change), and the oldest frames beyond `max` are dropped.
 * Returns the new list and the frames that fell out (so their object URLs can be revoked).
 */
export function pushFrame<T extends FrameMeta>(frames: readonly T[], frame: T, max = DEFAULT_MAX_FRAMES): { frames: T[]; dropped: T[] } {
  const last = frames[frames.length - 1];
  if (last && sameVolumes(last.radars, frame.radars)) return { frames: [...frames], dropped: [frame] };
  const next = [...frames, frame];
  const keep = Math.max(1, Math.floor(max));
  const dropped = next.length > keep ? next.splice(0, next.length - keep) : [];
  return { frames: next, dropped };
}

/** The index to show after one loop step (wraps around); null = the newest frame. */
export function stepFrame(index: number | null, count: number, dir: 1 | -1): number | null {
  if (count <= 0) return null;
  const cur = index === null ? count - 1 : index;
  const next = (cur + dir + count) % count;
  return next === count - 1 ? null : next;
}

// ── Status line ─────────────────────────────────────────────────────────────────────────────

export interface RadarStatusEntry {
  station: string;
  status: 'pending' | 'ok' | 'not_found' | 'offline' | 'error';
  scan_time?: string | null;
  /** Age at the time the engine answered (s) and when that was (epoch ms). */
  age_s?: number | null;
  receivedAt?: number;
  message?: string;
}

/** "12:01 UTC, 4 min old" / "no recent volume" / "failed: ..." for one radar, from facts only. */
export function radarStatusText(r: RadarStatusEntry, nowMs: number): string {
  if (r.status === 'pending') return 'checking';
  if (r.status === 'ok') {
    const hhmm = r.scan_time ? r.scan_time.slice(11, 16) + ' UTC' : 'time unknown';
    const age =
      typeof r.age_s === 'number' && Number.isFinite(r.age_s)
        ? formatAge(r.age_s + Math.max(0, nowMs - (r.receivedAt ?? nowMs)) / 1000) + ' old'
        : 'age unknown';
    return `${hhmm}, ${age}`;
  }
  if (r.status === 'not_found') return 'no recent volume';
  if (r.status === 'offline') return 'offline';
  return `failed${r.message ? ': ' + r.message : ''}`;
}

export function hhmmss(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return '';
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())} UTC`;
}
