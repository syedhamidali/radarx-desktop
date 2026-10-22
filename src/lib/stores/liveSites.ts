/**
 * Where the live radars are: name and position of every radar the live map can use, by live id.
 * NEXRAD radars come from the bundled list at once; the DWD radars come from the radar catalogue,
 * which is loaded the first time a DWD radar is needed (a local file, no network).
 *
 * Nothing here is computed or guessed: a radar not found stays unknown and the views show only its id.
 */
import { writable, get } from 'svelte/store';
import { BUILTIN_SITES } from './radarSites';
import { loadCatalogue } from '../utils/radarCatalogue';
import { liveIdOf, liveNetworkOf, type LiveNetwork } from '../utils/liveLogic';

export interface LiveSite {
  /** The live id: "KTLX" or "dwd:ess". */
  id: string;
  network: LiveNetwork;
  name: string;
  /** State (NEXRAD) or country (DWD). */
  region: string;
  lat: number;
  lon: number;
}

const initial = new Map<string, LiveSite>(
  BUILTIN_SITES.map((s) => [s.id.toUpperCase(), { id: s.id.toUpperCase(), network: 'nexrad' as const, name: s.name, region: s.state ?? '', lat: s.lat, lon: s.lon }]),
);

/** Every live radar known so far (grows when the catalogue has loaded). */
export const liveSites = writable<Map<string, LiveSite>>(initial);

let loading: Promise<void> | null = null;

/** Add the catalogue's DWD radars (once). Safe to call from any component. */
export function ensureLiveSites(): Promise<void> {
  if (get(liveSites).size > initial.size) return Promise.resolve();
  if (!loading) {
    loading = loadCatalogue()
      .then((cat) => {
        const next = new Map(get(liveSites));
        for (const s of cat.sites) {
          const id = liveIdOf(s);
          if (id && !next.has(id)) next.set(id, { id, network: liveNetworkOf(id), name: s.name, region: liveNetworkOf(id) === 'dwd' ? s.country : s.region, lat: s.lat, lon: s.lon });
        }
        liveSites.set(next);
      })
      .catch((e) => {
        console.warn('[live] the radar catalogue could not be loaded:', e);
        loading = null;
      });
  }
  return loading;
}

/** "Essen, Germany" / "Oklahoma City, OK"; just the id when the radar is not known. */
export function siteLabel(site: LiveSite | undefined, id: string): string {
  if (!site) return id;
  return `${site.name}${site.region ? `, ${site.region}` : ''}`;
}
