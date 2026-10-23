/**
 * Basin outlines on the map: pure helpers (no DOM) for the basin overlay.
 *
 * The engine sends the basins as a GeoJSON FeatureCollection (WGS 84, simplified for drawing, with
 * the rainfall statistics in the feature properties). Here they are projected to screen pixels with
 * a caller-supplied function, filled by the basin-mean value on the same colour scale as the map
 * layer, and labelled. The colour of a basin is the area-weighted basin mean from the engine, never
 * something computed from the picture.
 */

export type LonLat = [number, number];
export type Ring = LonLat[];
export type Project = (lat: number, lon: number) => [number, number];

export interface BasinGeometry {
  type: 'Polygon' | 'MultiPolygon';
  coordinates: number[][][] | number[][][][];
}

export interface BasinFeature {
  type: 'Feature';
  geometry: BasinGeometry;
  properties: Record<string, any> & { id: string; name?: string };
}

export interface BasinCollection {
  type: 'FeatureCollection';
  provenance_type?: string;
  label?: string;
  features: BasinFeature[];
}

/** Polygons of a geometry as arrays of rings (exterior first, then holes). */
export function polygonsOf(g: BasinGeometry): number[][][][] {
  return g.type === 'Polygon' ? [g.coordinates as number[][][]] : (g.coordinates as number[][][][]);
}

/** SVG path of a basin in screen pixels (fill with `fill-rule: evenodd` so holes stay open). */
export function basinPath(g: BasinGeometry, project: Project): string {
  const out: string[] = [];
  for (const poly of polygonsOf(g)) {
    for (const ring of poly) {
      if (ring.length < 3) continue;
      out.push(
        ring
          .map(([lon, lat], i) => {
            const [x, y] = project(lat, lon);
            return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
          })
          .join('') + 'Z',
      );
    }
  }
  return out.join('');
}

/** Centre of the biggest exterior ring (vertex mean of its bounding box), for a label; [lon, lat]. */
export function labelPoint(g: BasinGeometry): LonLat | null {
  let best: { area: number; c: LonLat } | null = null;
  for (const poly of polygonsOf(g)) {
    const ring = poly[0];
    if (!ring || ring.length < 3) continue;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const [x, y] of ring) {
      x0 = Math.min(x0, x); x1 = Math.max(x1, x);
      y0 = Math.min(y0, y); y1 = Math.max(y1, y);
    }
    const area = (x1 - x0) * (y1 - y0);
    if (!best || area > best.area) best = { area, c: [(x0 + x1) / 2, (y0 + y1) / 2] };
  }
  return best ? best.c : null;
}

function hexToRgb(hex: string): [number, number, number] | null {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const v = parseInt(m[1], 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}

/**
 * Colour of a value on the layer's legend (value, "#rrggbb" stops, linear in RGB); values outside the
 * stops take the end colours. Returns null when the value or the legend is unusable.
 */
export function colorOnLegend(value: number | null | undefined, legend: [number, string][] | undefined): string | null {
  if (value === null || value === undefined || !Number.isFinite(value) || !legend?.length) return null;
  const stops = [...legend].sort((a, b) => a[0] - b[0]);
  if (value <= stops[0][0]) return stops[0][1];
  const last = stops[stops.length - 1];
  if (value >= last[0]) return last[1];
  for (let i = 1; i < stops.length; i++) {
    if (value <= stops[i][0]) {
      const [v0, c0] = stops[i - 1];
      const [v1, c1] = stops[i];
      const a = hexToRgb(c0);
      const b = hexToRgb(c1);
      if (!a || !b) return c1;
      const t = v1 > v0 ? (value - v0) / (v1 - v0) : 0;
      const mix = a.map((x, k) => Math.round(x + (b[k] - x) * t));
      return `rgb(${mix[0]},${mix[1]},${mix[2]})`;
    }
  }
  return last[1];
}

/** The number a basin is coloured by: the gauge-adjusted mean when there is one, else the radar mean. */
export function basinMean(f: BasinFeature, adjusted: boolean): number | null {
  const p = f.properties;
  const v = adjusted && p.accumulation_adjusted_mean_mm != null ? p.accumulation_adjusted_mean_mm : p.accumulation_mean_mm;
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** A click that was a drag (the map pans on mouse down) does not select a basin. */
export function isClick(down: { x: number; y: number } | null, up: { x: number; y: number }, slop = 4): boolean {
  if (!down) return true;
  return Math.hypot(up.x - down.x, up.y - down.y) <= slop;
}

/** Tooltip text of a basin: id, area, mean and maximum depth with uncertainty and coverage, with units. */
export function basinTitle(f: BasinFeature, adjusted: boolean): string {
  const p = f.properties;
  const fmt = (v: unknown, d = 1) => (typeof v === 'number' && Number.isFinite(v) ? v.toFixed(d) : '–');
  const mean = basinMean(f, adjusted);
  const parts = [
    p.name && p.name !== p.id ? `${p.id} (${p.name})` : String(p.id),
    `${fmt(p.area_km2, 0)} km²`,
    `mean ${fmt(mean)} mm${adjusted ? ' (adjusted)' : ''}`,
    `±${fmt(p.accumulation_sigma_mm)} mm (1σ)`,
    `max cell ${fmt(p.accumulation_max_mm)} mm`,
    `${fmt(typeof p.coverage === 'number' ? 100 * p.coverage : null, 0)} % of the basin has an estimate`,
  ];
  return parts.join(' · ');
}
