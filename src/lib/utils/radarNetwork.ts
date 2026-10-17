/**
 * Pure helpers for the "Radar network" (multi-site NEXRAD) workflow: geodesic
 * distance / bearing between sites, neighbour selection, and picking the Level II
 * volume nearest to a requested time.
 *
 * No imports and no side effects so it can be unit-tested with plain node.
 */

export interface NetworkStation {
  id: string;
  latitude?: number;
  longitude?: number;
  elevation_m?: number;
}

export interface Neighbour {
  id: string;
  latitude: number;
  longitude: number;
  distanceKm: number;
  /** Initial bearing from the centre to the neighbour, degrees clockwise from north [0, 360). */
  bearingDeg: number;
  /** 16-point compass label for bearingDeg (e.g. "NNW"). */
  compass: string;
}

export interface L2FileLike {
  name: string;
  path: string;
}

const EARTH_RADIUS_KM = 6371.0088;
const DEG = Math.PI / 180;

/** Great-circle (haversine) distance in km on a spherical Earth. */
export function haversineKm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const dLat = (lat2 - lat1) * DEG;
  const dLon = (lon2 - lon1) * DEG;
  const a =
    Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * DEG) * Math.cos(lat2 * DEG) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** Initial great-circle bearing from point 1 to point 2, degrees clockwise from north [0, 360). */
export function bearingDeg(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const p1 = lat1 * DEG;
  const p2 = lat2 * DEG;
  const dLon = (lon2 - lon1) * DEG;
  const y = Math.sin(dLon) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dLon);
  if (Math.abs(x) < 1e-12 && Math.abs(y) < 1e-12) return 0;
  return (Math.atan2(y, x) / DEG + 360) % 360;
}

const COMPASS = [
  'N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
  'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW',
];

/** 16-point compass label for a bearing in degrees (any real number). */
export function compassPoint(deg: number): string {
  const d = ((deg % 360) + 360) % 360;
  return COMPASS[Math.round(d / 22.5) % 16];
}

function hasCoords(s: NetworkStation): s is NetworkStation & { latitude: number; longitude: number } {
  return Number.isFinite(s.latitude) && Number.isFinite(s.longitude);
}

/**
 * Stations within `radiusKm` of the centre station, nearest first (the centre itself is
 * excluded). At most `limit` are returned. Returns [] when the centre has no coordinates.
 */
export function findNeighbours(
  centreId: string,
  stations: NetworkStation[],
  radiusKm: number,
  limit: number,
): Neighbour[] {
  const centre = stations.find((s) => s.id === centreId);
  if (!centre || !hasCoords(centre) || limit <= 0) return [];
  const out: Neighbour[] = [];
  for (const s of stations) {
    if (s.id === centreId || !hasCoords(s)) continue;
    const distanceKm = haversineKm(centre.latitude, centre.longitude, s.latitude, s.longitude);
    if (distanceKm > radiusKm) continue;
    const brg = bearingDeg(centre.latitude, centre.longitude, s.latitude, s.longitude);
    out.push({
      id: s.id,
      latitude: s.latitude,
      longitude: s.longitude,
      distanceKm,
      bearingDeg: brg,
      compass: compassPoint(brg),
    });
  }
  out.sort((a, b) => a.distanceKm - b.distanceKm || (a.id < b.id ? -1 : 1));
  return out.slice(0, limit);
}

// ── Time handling ────────────────────────────────────────────────────────────

const SCAN_NAME_RE = /^[A-Z0-9]{4}(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/;

/** UTC epoch milliseconds encoded in a Level II file name / key, or null. */
export function parseScanTime(name: string): number | null {
  const base = name.slice(name.lastIndexOf('/') + 1);
  const m = SCAN_NAME_RE.exec(base);
  if (!m) return null;
  const [y, mo, d, h, mi, s] = m.slice(1).map(Number);
  const t = Date.UTC(y, mo - 1, d, h, mi, s);
  const back = new Date(t);
  // Reject overflowed dates such as month 13 or day 32.
  if (back.getUTCFullYear() !== y || back.getUTCMonth() !== mo - 1 || back.getUTCDate() !== d) return null;
  if (h > 23 || mi > 59 || s > 59) return null;
  return t;
}

/**
 * The file whose scan time is nearest `targetMs` (ties go to the earlier scan).
 * `_MDM` metadata files and unparsable names are ignored. Returns null when none qualify
 * or the best one is further than `maxGapMs` (when given).
 */
export function nearestScan<T extends { name: string }>(
  files: T[],
  targetMs: number,
  maxGapMs?: number,
): T | null {
  let best: T | null = null;
  let bestGap = Infinity;
  let bestT = Infinity;
  for (const f of files) {
    if (f.name.endsWith('_MDM')) continue;
    const t = parseScanTime(f.name);
    if (t === null) continue;
    const gap = Math.abs(t - targetMs);
    if (gap < bestGap || (gap === bestGap && t < bestT)) {
      best = f;
      bestGap = gap;
      bestT = t;
    }
  }
  if (best && maxGapMs !== undefined && bestGap > maxGapMs) return null;
  return best;
}

/** Combine a `YYYY-MM-DD` date and `HH:MM[:SS]` time, both UTC, into epoch ms (NaN if invalid). */
export function utcTargetMs(date: string, time: string): number {
  const dm = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date.trim());
  const tm = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(time.trim());
  if (!dm || !tm) return NaN;
  const [y, mo, d] = dm.slice(1).map(Number);
  const h = Number(tm[1]);
  const mi = Number(tm[2]);
  const s = Number(tm[3] ?? 0);
  if (h > 23 || mi > 59 || s > 59) return NaN;
  const t = Date.UTC(y, mo - 1, d, h, mi, s);
  const back = new Date(t);
  if (back.getUTCMonth() !== mo - 1 || back.getUTCDate() !== d) return NaN;
  return t;
}

/** `YYYY-MM-DD` (UTC) for an epoch-ms value. */
export function utcDateStr(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

/**
 * UTC days whose listings can contain the volume nearest to `targetMs`: the target's own
 * day, plus the neighbouring day when the target is within `marginMs` of midnight (the
 * nearest scan may live across the boundary).
 */
export function candidateDays(targetMs: number, marginMs = 30 * 60 * 1000): string[] {
  const days = [utcDateStr(targetMs)];
  const before = utcDateStr(targetMs - marginMs);
  const after = utcDateStr(targetMs + marginMs);
  if (!days.includes(before)) days.unshift(before);
  if (!days.includes(after)) days.push(after);
  return days;
}

/** Four-letter station id at the start of a Level II file name / path, or null. */
export function stationFromName(name: string): string | null {
  const base = name.slice(name.lastIndexOf('/') + 1);
  const m = /^([A-Z][A-Z0-9]{3})(?=\d{8}_)/.exec(base);
  return m ? m[1] : null;
}

/** "152 km NNW" style label. */
export function formatRelative(n: Pick<Neighbour, 'distanceKm' | 'compass'>): string {
  return `${Math.round(n.distanceKm)} km ${n.compass}`;
}
