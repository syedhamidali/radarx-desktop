/**
 * Radar-site markers on the 3D ground. Loaded lazily by the 3D viewer the first time the layer is
 * on (it brings the catalogue with it). Pure bookkeeping: the viewer draws the markers in its
 * overlay (`drawOverlay` -> `SceneMarker`), and asks this module for them.
 *
 * Sites are placed with the same azimuthal-equidistant projection as the echoes and the ground
 * map, so a marker sits where the map tile under it says the site is. Only sites inside the
 * displayed range are returned; the radar of the open file is skipped (it has its own mast).
 */
import { catalogue, siteChecks } from '../stores/radarSites';
import { latLonToDataSpace } from './geoProjection';
import { scanState, liveAge, type RadarSite, type ScanState } from './radarSites';
import type { SceneMarker } from './scene3d';
import type { SiteCheck } from '../stores/radarSites';

let sites: RadarSite[] = [];
let checks: Record<string, SiteCheck> = {};
let version = 0;
let memo: { key: string; out: SceneMarker[] } | null = null;
let colors: { theme: string; unknown: string; recent: string; stale: string } | null = null;

function palette() {
  const root = document.documentElement;
  const theme = root.getAttribute('data-theme') ?? '';
  if (colors && colors.theme === theme) return colors;
  const cs = getComputedStyle(root);
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  colors = {
    theme,
    unknown: v('--c-text-dim', '#a6b9cd'),
    recent: v('--c-nominal', '#3ee3a4'),
    stale: v('--c-caution', '#ffb92e'),
  };
  return colors;
}

/** Follow the catalogue and the engine's answers; `onChange` fires when the markers may differ. */
export function startSceneSites(onChange: () => void): () => void {
  const u1 = catalogue.subscribe((c) => { sites = c; version++; onChange(); });
  const u2 = siteChecks.subscribe((c) => { checks = c; version++; onChange(); });
  return () => { u1(); u2(); };
}

function stateOf(id: string, nowMs: number): ScanState {
  const c = checks[id.toUpperCase()];
  return c?.state === 'ok' ? scanState(liveAge(c.info, c.receivedAt, nowMs)) : 'unknown';
}

/** Markers within `rangeKm` of the radar at (lat, lon). Cached until the inputs (or 10 s) change. */
export function sceneMarkers(lat: number, lon: number, rangeKm: number): SceneMarker[] {
  const nowMs = Date.now();
  const pal = palette();
  const key = `${version}|${lat}|${lon}|${rangeKm}|${pal.theme}|${Math.floor(nowMs / 10_000)}`;
  if (memo && memo.key === key) return memo.out;
  const out: SceneMarker[] = [];
  for (const s of sites) {
    const [xm, ym] = latLonToDataSpace(s.lat, s.lon, lat, lon);
    const x = xm / 1000;
    const y = ym / 1000;
    const d = Math.hypot(x, y);
    if (!Number.isFinite(d) || d < 1 || d > rangeKm) continue;
    const st = stateOf(s.id, nowMs);
    out.push({
      x,
      y,
      label: s.id,
      color: st === 'recent' ? pal.recent : st === 'stale' ? pal.stale : pal.unknown,
      fill: st === 'recent' ? 'full' : st === 'stale' ? 'half' : 'none',
    });
  }
  memo = { key, out };
  return out;
}
