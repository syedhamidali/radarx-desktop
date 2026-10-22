/**
 * The bundled outlines of the radar globe: Natural Earth 110m land (its outlines are the coastlines) and
 * country borders on land, both public domain, in src/lib/data/globeGeo.json (written by
 * scripts/build_globe_geo.py; 50 KB). They are what the globe draws when the basemap is "None", and they are
 * the only geography it needs then: nothing is fetched.
 *
 * Pure decoding and a 2D-canvas painter for the plain land texture; no DOM unless a canvas is passed in.
 */
import { polylineVectors, graticuleLines } from './globeMath';

export interface GlobeGeoDoc {
  v: number;
  unit: number;
  land: number[][];
  borders: number[][];
}

export interface GlobeGeo {
  /** Land rings as [lon, lat, lon, lat, ...] (closed implicitly). */
  land: Float64Array[];
  /** Borders as [lon, lat, ...]. */
  borders: Float64Array[];
}

/** Undo the delta coding of scripts/build_globe_geo.py. */
export function decodeLine(flat: readonly number[], unit: number): Float64Array {
  const n = Math.floor(flat.length / 2);
  const out = new Float64Array(n * 2);
  let x = 0;
  let y = 0;
  for (let i = 0; i < n; i++) {
    x += flat[2 * i];
    y += flat[2 * i + 1];
    out[2 * i] = x * unit;
    out[2 * i + 1] = y * unit;
  }
  return out;
}

export function decodeGeo(doc: GlobeGeoDoc): GlobeGeo {
  return {
    land: (doc.land ?? []).map((l) => decodeLine(l, doc.unit)),
    borders: (doc.borders ?? []).map((l) => decodeLine(l, doc.unit)),
  };
}

let loading: Promise<GlobeGeo> | null = null;

/** The bundled outlines (a separate chunk, loaded the first time the globe needs them). */
export function loadGlobeGeo(): Promise<GlobeGeo> {
  if (!loading) loading = import('../data/globeGeo.json').then((m) => decodeGeo((m.default ?? m) as unknown as GlobeGeoDoc));
  return loading;
}

/** Everything the overlay strokes, as unit vectors ready for {@link traceVisible}. */
export interface GlobeLines {
  /** Coastlines: the outlines of the land rings, without the artificial edges (the pole line and the date line). */
  coast: Float32Array[];
  borders: Float32Array[];
  graticule: Float32Array[];
}

/**
 * Natural Earth's Antarctica ring runs along the date line and the south pole to close the polygon; those
 * edges are not coast. Cut each ring into runs without them.
 */
export function coastRuns(ring: Float64Array): Float64Array[] {
  const n = ring.length / 2;
  const artificial = (i: number, j: number) => {
    const y1 = ring[2 * i + 1], y2 = ring[2 * j + 1];
    const x1 = ring[2 * i], x2 = ring[2 * j];
    if (y1 <= -89.9 && y2 <= -89.9) return true;
    if (Math.abs(x1) >= 179.95 && Math.abs(x2) >= 179.95 && Math.sign(x1) === Math.sign(x2)) return true;
    return false;
  };
  const runs: Float64Array[] = [];
  let cur: number[] = [];
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n;
    if (cur.length === 0) cur.push(ring[2 * i], ring[2 * i + 1]);
    if (artificial(i, j)) {
      if (cur.length >= 4) runs.push(Float64Array.from(cur));
      cur = [];
    } else {
      cur.push(ring[2 * j], ring[2 * j + 1]);
    }
  }
  if (cur.length >= 4) runs.push(Float64Array.from(cur));
  return runs;
}

export function buildGlobeLines(geo: GlobeGeo): GlobeLines {
  return {
    coast: geo.land.flatMap((r) => coastRuns(r)).map((r) => polylineVectors(r)),
    borders: geo.borders.map((b) => polylineVectors(b)),
    graticule: graticuleLines(30, 3).map((g) => polylineVectors(g)),
  };
}

/** Colours of the plain (no basemap) land texture. */
export interface PlainColours {
  ocean: string;
  land: string;
}

/**
 * Paint the plain world into a 2D canvas as a plate carree image (longitude across, latitude down):
 * ocean, then the land rings filled (even-odd, so lakes and inland seas that Natural Earth cuts out
 * stay water). The strokes (coast, borders, graticule) are drawn crisply on top of the sphere instead.
 */
export function paintPlainLand(ctx: CanvasRenderingContext2D, w: number, h: number, geo: GlobeGeo | null, c: PlainColours): void {
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = c.ocean;
  ctx.fillRect(0, 0, w, h);
  if (!geo) return;
  ctx.fillStyle = c.land;
  ctx.beginPath();
  for (const ring of geo.land) {
    const n = ring.length / 2;
    for (let i = 0; i < n; i++) {
      const x = ((ring[2 * i] + 180) / 360) * w;
      const y = (0.5 - ring[2 * i + 1] / 180) * h;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.closePath();
  }
  ctx.fill('evenodd');
}
