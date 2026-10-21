/**
 * The worldwide radar-site catalogue (src/lib/data/radarCatalogue.json): one API for the radar
 * network map, the Cloud panel and the default-radar picker.
 *
 *   const cat = await loadCatalogue();          // once; the JSON is a separate chunk
 *   cat.search('essen')                         // by id, name, country, network (ranked)
 *   cat.filter({ access: ['open-loadable'], networks: ['DWD'] })
 *   cat.byId('deess')
 *   cat.nearest(51.4, 6.9, 3)                   // nearest sites with distance in km
 *
 * Pure logic (no Svelte, no DOM) so it runs under plain node for the unit tests; `loadCatalogue`
 * is the only async piece. Nothing here touches the network: the catalogue ships in the app.
 *
 * Science rule: every entry carries the source it was transcribed from, that source's licence and
 * the retrieval date; an entry's position is never computed or guessed here.
 */

export type DataAccess = 'open-loadable' | 'open-link' | 'key-required' | 'restricted' | 'unknown';

export const ACCESS_ORDER: readonly DataAccess[] = ['open-loadable', 'open-link', 'key-required', 'restricted', 'unknown'];

/** Label, one-line meaning and marker shape of each access class (shape and text, not colour alone). */
export const ACCESS_INFO: Record<DataAccess, { label: string; meaning: string; shape: 'circle' | 'square' | 'triangle' | 'cross' | 'ring'; glyph: string }> = {
  'open-loadable': { label: 'Loadable', meaning: 'The app opens volumes from an open, anonymous source.', shape: 'circle', glyph: '●' },
  'open-link': { label: 'Open data (link)', meaning: 'Open data exist, but the app does not load them yet; the card links to them.', shape: 'square', glyph: '■' },
  'key-required': { label: 'Key required', meaning: 'Free key or registration needed; not loaded by the app.', shape: 'triangle', glyph: '▲' },
  restricted: { label: 'Restricted', meaning: 'No open volume data known.', shape: 'cross', glyph: '✕' },
  unknown: { label: 'Unknown', meaning: 'Position known; no open data source verified.', shape: 'ring', glyph: '○' },
};

export interface CatalogueSource {
  name: string;
  url: string;
  licence: string;
  attribution: string;
}

export interface CatalogueSite {
  id: string;
  name: string;
  country: string;
  region: string;
  network: string;
  lat: number;
  lon: number;
  elev_m: number | null;
  band: string;
  polarisation: string;
  status: string;
  /** Key into `sources`. */
  source: string;
  retrieved: string;
  wmo?: string;
  wigos?: string;
  data_access: DataAccess;
  provider?: string;
  provider_site?: string;
  url?: string;
  note?: string;
}

export interface CatalogueDoc {
  retrieved: string;
  sources: Record<string, CatalogueSource>;
  counts?: Record<string, unknown>;
  sites: CatalogueSite[];
}

export interface SiteFilter {
  access?: readonly DataAccess[];
  networks?: readonly string[];
  countries?: readonly string[];
  /** Keep only sites whose status starts with "operational". */
  operationalOnly?: boolean;
}

export interface Nearest {
  site: CatalogueSite;
  km: number;
}

const R_KM = 6371.0088;
const RAD = Math.PI / 180;

export function haversineKm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const dLat = (lat2 - lat1) * RAD;
  const dLon = (lon2 - lon1) * RAD;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * RAD) * Math.cos(lat2 * RAD) * Math.sin(dLon / 2) ** 2;
  return 2 * R_KM * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** The network group of a site ("OSCAR/Japan" -> "OSCAR"). */
export function networkGroup(site: Pick<CatalogueSite, 'network'>): string {
  return site.network.split('/')[0];
}

function norm(s: string): string {
  return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
}

export class RadarCatalogue {
  readonly sites: readonly CatalogueSite[];
  readonly sources: Readonly<Record<string, CatalogueSource>>;
  readonly retrieved: string;
  private readonly idMap: Map<string, CatalogueSite>;
  /** 2x2 degree cells -> sites, for nearest() on a thousand sites without a full scan. */
  private readonly cells: Map<string, CatalogueSite[]>;
  private readonly cellDeg = 2;

  constructor(doc: CatalogueDoc) {
    this.sites = validSites(doc);
    this.sources = doc.sources ?? {};
    this.retrieved = String(doc.retrieved ?? '');
    this.idMap = new Map(this.sites.map((s) => [s.id.toLowerCase(), s]));
    this.cells = new Map();
    for (const s of this.sites) {
      const k = this.cellKey(s.lat, s.lon);
      const arr = this.cells.get(k);
      if (arr) arr.push(s);
      else this.cells.set(k, [s]);
    }
  }

  private cellKey(lat: number, lon: number): string {
    return `${Math.floor(lat / this.cellDeg)}:${Math.floor((((lon + 180) % 360) + 360) % 360 / this.cellDeg)}`;
  }

  byId(id: string): CatalogueSite | null {
    return this.idMap.get(String(id ?? '').toLowerCase()) ?? null;
  }

  /** Source record of a site (name, url, licence, attribution). */
  sourceOf(site: CatalogueSite): CatalogueSource {
    return this.sources[site.source] ?? { name: site.source, url: '', licence: 'not recorded', attribution: '' };
  }

  /** Distinct network groups, most sites first. */
  networks(): Array<{ id: string; count: number }> {
    const counts = new Map<string, number>();
    for (const s of this.sites) counts.set(networkGroup(s), (counts.get(networkGroup(s)) ?? 0) + 1);
    return [...counts].map(([id, count]) => ({ id, count })).sort((a, b) => b.count - a.count || a.id.localeCompare(b.id));
  }

  /** Distinct countries, alphabetical. */
  countries(): string[] {
    return [...new Set(this.sites.map((s) => s.country))].sort((a, b) => a.localeCompare(b));
  }

  countByAccess(sites: readonly CatalogueSite[] = this.sites): Record<DataAccess, number> {
    const out = { 'open-loadable': 0, 'open-link': 0, 'key-required': 0, restricted: 0, unknown: 0 };
    for (const s of sites) out[s.data_access]++;
    return out;
  }

  filter(f: SiteFilter, from: readonly CatalogueSite[] = this.sites): CatalogueSite[] {
    const access = f.access && f.access.length ? new Set(f.access) : null;
    const nets = f.networks && f.networks.length ? new Set(f.networks) : null;
    const countries = f.countries && f.countries.length ? new Set(f.countries) : null;
    return from.filter(
      (s) =>
        (!access || access.has(s.data_access)) &&
        (!nets || nets.has(networkGroup(s))) &&
        (!countries || countries.has(s.country)) &&
        (!f.operationalOnly || s.status.startsWith('operational')),
    );
  }

  /**
   * Search by id, name, country, network, WMO or WIGOS id. Every word of the query must match;
   * exact id matches come first, then id prefixes, then names, then the rest; at most `limit`.
   */
  search(query: string, limit = 50, from: readonly CatalogueSite[] = this.sites): CatalogueSite[] {
    const words = norm(query).split(/\s+/).filter(Boolean);
    if (words.length === 0) return from.slice(0, limit);
    const scored: Array<[number, CatalogueSite]> = [];
    for (const s of from) {
      const id = norm(s.id);
      const name = norm(s.name);
      const hay = `${id} ${name} ${norm(s.country)} ${norm(s.network)} ${s.wmo ?? ''} ${s.wigos ?? ''} ${norm(s.region)}`;
      if (!words.every((w) => hay.includes(w))) continue;
      let score = 3;
      if (words.length === 1 && id === words[0]) score = 0;
      else if (words.some((w) => id.startsWith(w))) score = 1;
      else if (words.some((w) => name.startsWith(w))) score = 2;
      scored.push([score, s]);
    }
    scored.sort((a, b) => a[0] - b[0] || a[1].name.localeCompare(b[1].name));
    return scored.slice(0, limit).map((x) => x[1]);
  }

  /** The `n` nearest sites to a point (great-circle distance), nearest first; `maxKm` optional. */
  nearest(lat: number, lon: number, n = 1, maxKm = Infinity): Nearest[] {
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return [];
    // Grow the cell ring until enough candidates are in, then rank exactly.
    const found: Nearest[] = [];
    const seen = new Set<CatalogueSite>();
    const cy = Math.floor(lat / this.cellDeg);
    const cx = Math.floor((((lon + 180) % 360) + 360) % 360 / this.cellDeg);
    const nx = Math.ceil(360 / this.cellDeg);
    for (let ring = 0; ring <= 8; ring++) {
      for (let dy = -ring; dy <= ring; dy++) {
        for (let dx = -ring; dx <= ring; dx++) {
          if (Math.max(Math.abs(dx), Math.abs(dy)) !== ring) continue;
          const key = `${cy + dy}:${(((cx + dx) % nx) + nx) % nx}`;
          for (const s of this.cells.get(key) ?? []) {
            if (seen.has(s)) continue;
            seen.add(s);
            const km = haversineKm(lat, lon, s.lat, s.lon);
            if (km <= maxKm) found.push({ site: s, km });
          }
        }
      }
      // Sites closer than the inner edge of the searched area are all in: `ring` cells of
      // (cellDeg * 111 km) in latitude is a safe bound; longitude cells only get narrower.
      found.sort((a, b) => a.km - b.km);
      if (found.length >= n && found[n - 1].km < ring * this.cellDeg * 111) break;
    }
    if (found.length < n) {
      // Sparse areas (oceans): fall back to the full scan once.
      const all = this.sites.map((s) => ({ site: s, km: haversineKm(lat, lon, s.lat, s.lon) })).filter((x) => x.km <= maxKm);
      all.sort((a, b) => a.km - b.km);
      return all.slice(0, n);
    }
    return found.slice(0, n);
  }
}

function validSites(doc: CatalogueDoc): CatalogueSite[] {
  const out: CatalogueSite[] = [];
  const seen = new Set<string>();
  for (const r of doc.sites ?? []) {
    if (!r || typeof r.id !== 'string' || !r.id || seen.has(r.id.toLowerCase())) continue;
    if (!Number.isFinite(r.lat) || !Number.isFinite(r.lon) || r.lat < -90 || r.lat > 90 || r.lon < -180 || r.lon > 180) continue;
    if (!(r.data_access in ACCESS_INFO)) continue;
    seen.add(r.id.toLowerCase());
    out.push({
      ...r,
      name: String(r.name ?? r.id),
      country: String(r.country ?? ''),
      region: String(r.region ?? ''),
      network: String(r.network ?? ''),
      band: String(r.band ?? ''),
      polarisation: String(r.polarisation ?? ''),
      status: String(r.status ?? 'unknown'),
      elev_m: Number.isFinite(r.elev_m) ? (r.elev_m as number) : null,
    });
  }
  return out;
}

// ── Map helpers (pure) ─────────────────────────────────────────────────────────────────────

/** Web Mercator: lon/lat -> world pixel at zoom z (256 px tiles). */
export function lonLatToWorld(lon: number, lat: number, z: number): [number, number] {
  const n = 256 * 2 ** z;
  const x = ((lon + 180) / 360) * n;
  const s = Math.sin(Math.max(-85.0511, Math.min(85.0511, lat)) * RAD);
  const y = (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * n;
  return [x, y];
}

export function worldToLonLat(x: number, y: number, z: number): [number, number] {
  const n = 256 * 2 ** z;
  const lon = (x / n) * 360 - 180;
  const lat = (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / n))) * 180) / Math.PI;
  return [lon, lat];
}

export interface Cluster {
  x: number;
  y: number;
  sites: CatalogueSite[];
  /** Access class to draw: the "best" one present (loadable beats link beats key ...). */
  access: DataAccess;
}

/**
 * Grid clustering in screen space: sites whose projected positions fall in the same `cellPx`
 * cell become one cluster drawn at their mean position. Deterministic and O(n), so a thousand
 * sites cluster in well under a millisecond per frame. Sites outside the view are dropped.
 */
export function clusterSites(
  sites: readonly CatalogueSite[],
  project: (lat: number, lon: number) => [number, number],
  width: number,
  height: number,
  cellPx: number,
): Cluster[] {
  const cells = new Map<string, Cluster>();
  const margin = 12;
  for (const s of sites) {
    const [x, y] = project(s.lat, s.lon);
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    if (x < -margin || y < -margin || x > width + margin || y > height + margin) continue;
    if (cellPx <= 1) {
      cells.set(s.id, { x, y, sites: [s], access: s.data_access });
      continue;
    }
    const key = `${Math.floor(x / cellPx)}:${Math.floor(y / cellPx)}`;
    const c = cells.get(key);
    if (!c) cells.set(key, { x, y, sites: [s], access: s.data_access });
    else {
      const k = c.sites.length;
      c.x = (c.x * k + x) / (k + 1);
      c.y = (c.y * k + y) / (k + 1);
      c.sites.push(s);
      if (ACCESS_ORDER.indexOf(s.data_access) < ACCESS_ORDER.indexOf(c.access)) c.access = s.data_access;
    }
  }
  return [...cells.values()];
}

/** The cluster (or single marker) under a screen point, within `radiusPx`. */
export function hitCluster(clusters: readonly Cluster[], x: number, y: number, radiusPx: number): Cluster | null {
  let best: Cluster | null = null;
  let bestD = radiusPx * radiusPx;
  for (const c of clusters) {
    const d = (c.x - x) ** 2 + (c.y - y) ** 2;
    if (d <= bestD) {
      bestD = d;
      best = c;
    }
  }
  return best;
}

// ── Loading ──────────────────────────────────────────────────────────────────────────────

let loading: Promise<RadarCatalogue> | null = null;

/** The bundled catalogue (fetched as its own chunk the first time, then shared). */
export function loadCatalogue(): Promise<RadarCatalogue> {
  if (!loading) {
    loading = import('../data/radarCatalogue.json').then((m) => new RadarCatalogue((m.default ?? m) as unknown as CatalogueDoc));
  }
  return loading;
}

/**
 * Hook for the default-radar picker (another branch builds it): asking to make a site the
 * default dispatches this event with the site; nothing in the settings store is touched here.
 */
export const DEFAULT_RADAR_EVENT = 'xradar:set-default-radar';

export function requestDefaultRadar(site: CatalogueSite): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent(DEFAULT_RADAR_EVENT, { detail: { site } }));
}
