/**
 * What the synthesised sound of a storm clip reads from the data, as pure functions (no DOM, no Web
 * Audio); unit-tested in soundSampling.test.mjs.
 *
 *  - Rain: the reflectivity at the LOWEST gridded level that has a value over the listener, searched
 *    from the ground up to {@link RAIN_SEARCH_KM} above it. A merged grid far from the radars has no
 *    value near the ground (the lowest beams pass overhead), so reading one fixed low height would
 *    leave the rain silent although it is raining above. Reflectivity is not a rain rate: the level
 *    is only a linear map of dBZ, 20 dBZ none, 55 dBZ full.
 *  - Thunder: the light-to-sound delay is real time; a time-lapse film shows data time through an
 *    eased clock, so the arrival is found by going through the clock: the data time of the flash plus
 *    the real delay, mapped back to film time.
 */
import { dbzOfCode } from './clipSteps.ts';
import { SPEED_OF_SOUND_MS } from './lightning.ts';

/** Reflectivity (dBZ) with no rain sound, and with the full rain sound. */
export const RAIN_DBZ_NONE = 20;
export const RAIN_DBZ_FULL = 55;
/** The rain sampler looks for the lowest echo up to this height above the ground (km). */
export const RAIN_SEARCH_KM = 2.5;

/** The part of a gridded volume the sampler needs (a `CartVolume` is one). */
export interface EchoGrid {
  nx: number;
  ny: number;
  nz: number;
  x0: number;
  y0: number;
  dx: number;
  dy: number;
  /** Level heights (km), ascending. */
  z: ArrayLike<number>;
  /** (nz, ny, nx), x fastest: 0 = no value, 1..255 a code of vmin..vmax. */
  data: ArrayLike<number>;
  vmin: number;
  vmax: number;
}

/**
 * The reflectivity at the lowest level with a value in the column over (x, y) (nearest column),
 * among the levels from `zFromKm` to `zToKm` (both included, a half level of tolerance below).
 * Null when that column has no value at those levels or (x, y) is outside the grid.
 */
export function lowestEcho(v: EchoGrid | null | undefined, x: number, y: number, zFromKm: number, zToKm: number): { dbz: number; zKm: number } | null {
  if (!v) return null;
  const i = Math.round((x - v.x0) / v.dx), j = Math.round((y - v.y0) / v.dy);
  if (i < 0 || j < 0 || i >= v.nx || j >= v.ny) return null;
  // levels nearer than half a level spacing to zFrom count (the grid rarely has the exact height)
  const half = v.nz > 1 ? Math.abs(v.z[1] - v.z[0]) / 2 : 0.25;
  for (let k = 0; k < v.nz; k++) {
    const zk = v.z[k];
    if (zk < zFromKm - half) continue;
    if (zk > zToKm) break;
    const c = v.data[(k * v.ny + j) * v.nx + i];
    if (c !== 0) return { dbz: dbzOfCode(v, c), zKm: zk };
  }
  return null;
}

/** Rain level 0..1 of a reflectivity: linear in dBZ, 20 dBZ none, 55 dBZ full (not a rain rate). */
export function rainLevelOfDbz(dbz: number): number {
  if (!Number.isFinite(dbz)) return 0;
  return Math.max(0, Math.min(1, (dbz - RAIN_DBZ_NONE) / (RAIN_DBZ_FULL - RAIN_DBZ_NONE)));
}

/**
 * Rain level 0..1 at a listener on the ground at `groundKm`, from the lowest echo of the column up to
 * `RAIN_SEARCH_KM` above the ground; `zKm`: the height used (null when there was no echo).
 */
export function rainAtListener(v: EchoGrid | null | undefined, x: number, y: number, groundKm: number, searchKm = RAIN_SEARCH_KM): { level: number; zKm: number | null } {
  const e = lowestEcho(v, x, y, groundKm, groundKm + searchKm);
  return e ? { level: rainLevelOfDbz(e.dbz), zKm: e.zKm } : { level: 0, zKm: null };
}

/** The film clock: data time (s) shown at film time `t`, and its inverse. */
export interface FilmClock {
  dataAt(tFilm: number): number;
  filmAt(tData: number): number;
  /** Last data time of the film and its film time; later data times run on at `rateAfter` data s per film s. */
  dataEnd?: number;
  rateAfter?: number;
}

/** Real light-to-sound delay (s) over `distKm`. */
export function realDelayS(distKm: number): number {
  return (distKm * 1000) / SPEED_OF_SOUND_MS;
}

/**
 * Film time at which the thunder of a flash seen at film time `tFlash` arrives from `distKm` away:
 * the flash's data time plus the real delay, through the clock back to film time. Past the last data
 * time of the film the clock runs on at `rateAfter`.
 */
export function thunderArrivalFilm(tFlash: number, distKm: number, clock: FilmClock): number {
  const td = clock.dataAt(tFlash) + realDelayS(distKm);
  if (clock.dataEnd !== undefined && td > clock.dataEnd) {
    return clock.filmAt(clock.dataEnd) + (td - clock.dataEnd) / Math.max(1e-6, clock.rateAfter ?? 1);
  }
  return Math.max(tFlash, clock.filmAt(td));
}

/** "1 flash", "33 flashes". */
export function flashCount(n: number): string {
  return `${n} ${n === 1 ? 'flash' : 'flashes'}`;
}
