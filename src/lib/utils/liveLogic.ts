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
const ID_RE = /^[A-Z0-9]{4}$/;

/** Upper-cased, validated, de-duplicated ids in the given order; at most MAX_LIVE_RADARS. */
export function normalizeRadarIds(ids: unknown): string[] {
  if (!Array.isArray(ids)) return [];
  const out: string[] = [];
  for (const raw of ids) {
    if (typeof raw !== 'string') continue;
    const id = raw.trim().toUpperCase();
    if (!ID_RE.test(id) || out.includes(id)) continue;
    out.push(id);
    if (out.length >= MAX_LIVE_RADARS) break;
  }
  return out;
}

/** Add the id when absent, remove it when present; a full list is left unchanged. */
export function toggleRadarId(ids: readonly string[], id: string): string[] {
  const key = id.trim().toUpperCase();
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
  path: string;
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
    if (e.status === 'ok' && e.path && e.name) {
      const f: LiveFileRequest = {
        station: e.station,
        path: e.path,
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

/** The first dialog of a live start: the listings (metadata only). */
export function describeLiveListing(stations: readonly string[], now: Date): ConsentText {
  const today = utcDay(now);
  const n = stations.length;
  return {
    title: `Start the live map (${n} radar${n === 1 ? '' : 's'})`,
    lines: [
      `A listing of today's folder ${today}/<SITE>/ (and yesterday's if today has none) for ${stations.join(', ')}: file names, sizes and times. No radar data yet.`,
      'Then the newest volume of each radar is offered for download with its exact size, in a second dialog.',
      'While the live map runs, the same listing is repeated at every update.',
    ],
    host: AWS_HOST,
    size: `About ${(0.1 * n).toFixed(1)} MB at most per update (one entry of a few hundred bytes per volume, roughly 290 volumes a day per radar).`,
  };
}

/** The second dialog: the volumes to download now, with their exact total size. */
export function describeLiveDownload(plan: DownloadPlan, intervalMin: number): ConsentText {
  const n = plan.toDownload.length;
  const lines = plan.toDownload.map((f) => `${f.station}: ${f.name} (${formatBytes(f.size)})`);
  if (plan.failed.length) lines.push(`Not available now: ${plan.failed.map((f) => `${f.station} (${f.message})`).join('; ')}.`);
  lines.push(
    `"Allow while the app is open" keeps updating every ${clampIntervalMin(intervalMin)} min: each update downloads only a radar's new volume (about this size each time a radar has one). "Allow once" updates one time, then pauses.`,
  );
  return {
    title: `Download ${n} Level II volume${n === 1 ? '' : 's'} for the live map`,
    lines,
    host: AWS_HOST,
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
