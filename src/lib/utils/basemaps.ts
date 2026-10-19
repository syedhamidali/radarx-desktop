/**
 * Keyless slippy-map basemap providers for the PPI map background.
 *
 * `settings.mapStyle` selects one: either a provider id ("osm", tile theme follows the UI theme)
 * or a provider id plus a forced tile theme ("osm:dark"). The legacy default values "dark" /
 * "light" (from before providers existed) mean OpenStreetMap with the theme following the UI. The Settings dialog can build its picker from BASEMAPS and BASEMAP_THEMES and store
 * `encodeMapStyle(id, theme)`.
 *
 * The tile theme is applied as a CSS filter on the tile canvas only (never on the radar or
 * mosaic layers), so every provider works in both light and dark UIs from a single tile set.
 */

export interface BasemapProvider {
  id: string;
  label: string;
  /** URL template; {z} {x} {y}, and {s} for one of `subdomains`. Empty = no tiles (grey background). */
  url: string;
  subdomains?: string[];
  minZoom: number;
  maxZoom: number;
  /** Attribution text that must stay visible while the tiles are shown. */
  attribution: string;
  attributionUrl?: string;
}

export const BASEMAPS: readonly BasemapProvider[] = [
  {
    id: 'osm',
    label: 'OpenStreetMap',
    url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    minZoom: 1,
    maxZoom: 19,
    attribution: '© OpenStreetMap contributors',
    attributionUrl: 'https://www.openstreetmap.org/copyright',
  },
  {
    id: 'topo',
    label: 'OpenTopoMap',
    url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    subdomains: ['a', 'b', 'c'],
    minZoom: 1,
    maxZoom: 17,
    attribution: '© OpenStreetMap contributors, SRTM | OpenTopoMap (CC-BY-SA)',
    attributionUrl: 'https://opentopomap.org/about',
  },
  {
    id: 'none',
    label: 'None (plain background)',
    url: '',
    minZoom: 1,
    maxZoom: 18,
    attribution: '',
  },
];

export type BasemapTheme = 'auto' | 'dark' | 'light';
export const BASEMAP_THEMES: readonly BasemapTheme[] = ['auto', 'dark', 'light'];
export const DEFAULT_BASEMAP_ID = 'osm';

/** CSS filters that restyle standard (light) OSM-style tiles; 'light' leaves them untouched. */
export const BASEMAP_THEME_FILTER: Record<'dark' | 'light', string> = {
  dark: 'invert(1) hue-rotate(196deg) brightness(0.74) contrast(0.95) saturate(0.62)',
  light: 'none',
};

export function encodeMapStyle(id: string, theme: BasemapTheme = 'auto'): string {
  return theme === 'auto' ? id : `${id}:${theme}`;
}

export interface ResolvedBasemap {
  provider: BasemapProvider;
  theme: 'dark' | 'light';
  filter: string;
}

/**
 * Resolve `settings.mapStyle` + the UI theme mode into a provider and an effective tile theme.
 * Unknown values fall back to OpenStreetMap, a tile theme that follows the UI theme.
 */
export function resolveBasemap(mapStyle: string | undefined, uiMode: 'dark' | 'light' | 'auto'): ResolvedBasemap {
  const [idRaw, themeRaw] = (mapStyle ?? '').split(':');
  const id = idRaw === 'dark' || idRaw === 'light' ? DEFAULT_BASEMAP_ID : idRaw;
  const theme: string | undefined = themeRaw;
  const provider = BASEMAPS.find((b) => b.id === id) ?? BASEMAPS[0];
  let eff: 'dark' | 'light';
  if (theme === 'dark' || theme === 'light') eff = theme;
  else if (uiMode === 'light') eff = 'light';
  else if (uiMode === 'dark') eff = 'dark';
  else {
    eff =
      typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: light)').matches
        ? 'light'
        : 'dark';
  }
  return { provider, theme: eff, filter: BASEMAP_THEME_FILTER[eff] };
}

export function basemapTileUrl(p: BasemapProvider, z: number, x: number, y: number): string {
  if (!p.url) return '';
  const s = p.subdomains?.length ? p.subdomains[(x + y) % p.subdomains.length] : '';
  return p.url.replace('{s}', s).replace('{z}', String(z)).replace('{x}', String(x)).replace('{y}', String(y));
}
