/**
 * Pure logic of how the live map is drawn and what its buttons do (no imports of the app, no DOM):
 * the display floor, the legend with it, the sentence on the map, the footprint of the radars and
 * the wording after opening the held volumes. Runs under plain node for the unit tests.
 *
 * Science rule: the floor is a display choice. The grid values, the exports and the hover read-out
 * are not changed by it, and the number is always shown next to the map.
 */

import { formatBytes } from './radarSites.ts';

// ── Display floor ───────────────────────────────────────────────────────────────────────────

/** The NWS convention: reflectivity below about 5 dBZ is not drawn. */
export const DEFAULT_FLOOR_DBZ = 5;
export const MIN_FLOOR_DBZ = -20;
export const MAX_FLOOR_DBZ = 40;
/** What the quick selector offers; the lowest draws every echo of the colour scale. */
export const FLOOR_CHOICES: readonly number[] = [-20, 0, 5, 10, 15, 20, 25];

export function clampFloorDbz(v: unknown): number {
  const n = typeof v === 'number' && Number.isFinite(v) ? Math.round(v) : DEFAULT_FLOOR_DBZ;
  return Math.min(MAX_FLOOR_DBZ, Math.max(MIN_FLOOR_DBZ, n));
}

/** The value sent to the engine: a floor at (or below) the start of the colour scale draws everything. */
export function engineFloor(floorDbz: number, vmin: number = MIN_FLOOR_DBZ): number | null {
  return floorDbz <= Math.max(vmin, MIN_FLOOR_DBZ) ? null : floorDbz;
}

/** "Hide echo below 5 dBZ" / "All echo drawn". */
export function floorText(floor: number | null): string {
  return floor === null || floor <= MIN_FLOOR_DBZ ? 'All echo drawn' : `Hide echo below ${floor} dBZ`;
}

function hexToRgb(hex: string): [number, number, number] | null {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHex(c: readonly number[]): string {
  return '#' + c.map((v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, '0')).join('');
}

/**
 * The legend as the map shows it with a display floor: the colour scale starts at the floor (a
 * colour interpolated between the neighbouring stops), the stops below it are dropped. With no
 * floor above the scale's start the legend is unchanged.
 */
export function legendAtFloor(
  legend: readonly [number, string][],
  vmin: number,
  floor: number | null,
): { vmin: number; legend: [number, string][] } {
  const stops = [...legend].sort((a, b) => a[0] - b[0]);
  if (floor === null || floor <= vmin || stops.length === 0) return { vmin, legend: stops };
  const above = stops.filter(([v]) => v > floor);
  const below = stops.filter(([v]) => v <= floor);
  const lo = below[below.length - 1];
  const hi = above[0];
  let start: [number, string];
  if (lo && hi) {
    const a = hexToRgb(lo[1]);
    const b = hexToRgb(hi[1]);
    const t = (floor - lo[0]) / (hi[0] - lo[0]);
    start = [floor, a && b ? rgbToHex(a.map((v, i) => v + (b[i] - v) * t)) : lo[1]];
  } else {
    start = [floor, (lo ?? hi)[1]];
  }
  return { vmin: floor, legend: [start, ...above] };
}

// ── The sentence on the map ─────────────────────────────────────────────────────────────────

/** "1 km", "1.5 km", "2 km": the grid of the composite as the label states it. */
export function gridLabel(resolutionM: number): string {
  if (!Number.isFinite(resolutionM) || resolutionM <= 0) return 'unknown grid';
  const km = resolutionM / 1000;
  return `${Number.isInteger(km) ? km : km.toFixed(1).replace(/\.0$/, '')} km`;
}

export interface LiveLabelInput {
  variable: string;
  radars: number;
  resolutionM: number;
  /** The floor the picture was drawn with (null = every echo). */
  floor: number | null;
  /** True when the image was smoothed for display (drawn pixels finer than the grid). */
  smoothed: boolean;
  isLatest: boolean;
}

/** What the picture is, its grid, what is not drawn. */
export function compositeLabel(i: LiveLabelInput): string {
  const parts = [
    `Composite (column maximum of ${i.variable}) merged from ${i.radars} radar volume${i.radars === 1 ? '' : 's'}, ${gridLabel(i.resolutionM)} grid${i.smoothed ? ', drawn smoothed' : ''}.`,
    i.floor === null ? 'All echo drawn.' : `Echo below ${i.floor} dBZ is not drawn.`,
  ];
  if (!i.isLatest) parts.push('Recent loop: not the newest frame.');
  parts.push('Not an official product.');
  return parts.join(' ');
}

// ── Where the map looks ─────────────────────────────────────────────────────────────────────

export interface Bounds {
  west: number;
  south: number;
  east: number;
  north: number;
}

/**
 * The lon/lat box around the range discs of some radar sites (what the composite will cover),
 * used to position the map before the first frame exists. A degree of longitude is shortened by
 * the cosine of the most poleward site, so the box is a little generous, never too small.
 */
export function footprintBounds(sites: readonly { lat: number; lon: number }[], rangeKm: number): Bounds | null {
  if (sites.length === 0) return null;
  const lats = sites.map((s) => s.lat);
  const lons = sites.map((s) => s.lon);
  const dLat = rangeKm / 111.2;
  const cosLat = Math.max(0.05, Math.cos((Math.max(...lats.map(Math.abs)) * Math.PI) / 180));
  const dLon = rangeKm / (111.2 * cosLat);
  return {
    west: Math.max(-180, Math.min(...lons) - dLon),
    east: Math.min(180, Math.max(...lons) + dLon),
    south: Math.max(-85, Math.min(...lats) - dLat),
    north: Math.min(85, Math.max(...lats) + dLat),
  };
}

// ── Opening the held volumes ────────────────────────────────────────────────────────────────

/** One volume of a `live_volumes_opened` reply. */
export interface OpenedVolume {
  station: string;
  name: string;
  size: number;
  file_id: string;
  already_open?: boolean;
}

/** The radars that have a volume the engine holds right now, in the order of the radar list. */
export function openableStations(radars: readonly { station: string; status?: string }[], held: readonly string[]): string[] {
  const have = new Set(held);
  return radars.filter((r) => (r.status === undefined || r.status === 'ok') && have.has(r.station)).map((r) => r.station);
}

/** The sentence after opening: what was opened and the disk the files use until they are closed. */
export function openedText(files: readonly OpenedVolume[], pinnedBytes: number, failed: readonly string[] = []): string {
  const fresh = files.filter((f) => !f.already_open);
  const n = files.length;
  const names = files.map((f) => f.station).join(', ');
  const head =
    fresh.length === 0
      ? `${n === 1 ? 'This volume is' : 'These volumes are'} already open (${names}).`
      : `Opened ${n} volume${n === 1 ? '' : 's'} (${names}) as normal files; the next live update does not replace them.`;
  const disk = fresh.length === 0 ? '' : `They use ${formatBytes(pinnedBytes)} on disk until you close them.`;
  const fail = failed.length ? ` Could not open: ${failed.join('; ')}.` : '';
  return `${head} ${disk}${fail}`.replace(/\s+/g, ' ').trim();
}

/** Why Multi-Doppler cannot start from the live map right now (null = it can). */
export function multiDopplerBlocked(openable: readonly string[]): string | null {
  if (openable.length < 2) return 'Multi-Doppler needs the volumes of at least two radars. Add a radar whose range overlaps, or wait for the missing volume.';
  return null;
}

/** The Layers panel line of a live layer: the analysis time and the radars it was merged from. */
export function liveLayerLine(analysisTime: string | null | undefined, stations: readonly string[]): string {
  const t = analysisTime ? analysisTime.replace('T', ' ').slice(0, 16) + ' UTC' : 'time unknown';
  return `Live composite: analysis time ${t}, radars ${stations.length ? stations.join(', ') : 'none'}`;
}
