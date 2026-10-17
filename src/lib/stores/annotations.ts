import { writable, derived, get } from 'svelte/store';
import { radarData } from './radarData';
import { dataSpaceToLatLon } from '../utils/geoProjection';

// ─── Types ─────────────────────────────────────────────────────────────

export type MeasureToolMode = 'none' | 'distance' | 'area' | 'bearing';
export type AnnotationToolMode = 'none' | 'text' | 'arrow' | 'circle' | 'rectangle';

/** A point in radar data coordinates (metres from radar centre). */
export interface DataPoint {
  x: number;  // east-west metres
  y: number;  // north-south metres
}

/** Items are drawn in radar-relative metres, so each one remembers the radar site it was made on. */
interface SiteTagged {
  /** "lat,lon" of the radar the points are relative to (absent on items saved by older versions). */
  site?: string;
}

export interface DistanceMeasurement extends SiteTagged {
  id: string;
  type: 'distance';
  points: [DataPoint, DataPoint];
  distanceKm: number;
}

export interface AreaMeasurement extends SiteTagged {
  id: string;
  type: 'area';
  points: DataPoint[];
  areaKm2: number;
}

export interface BearingMeasurement extends SiteTagged {
  id: string;
  type: 'bearing';
  points: [DataPoint, DataPoint];
  bearingDeg: number;
  distanceKm: number;
}

export type Measurement = DistanceMeasurement | AreaMeasurement | BearingMeasurement;

export interface TextAnnotation extends SiteTagged {
  id: string;
  type: 'text';
  position: DataPoint;
  text: string;
}

export interface ArrowAnnotation extends SiteTagged {
  id: string;
  type: 'arrow';
  start: DataPoint;
  end: DataPoint;
}

export interface CircleAnnotation extends SiteTagged {
  id: string;
  type: 'circle';
  center: DataPoint;
  radiusM: number;
}

export interface RectAnnotation extends SiteTagged {
  id: string;
  type: 'rectangle';
  corner1: DataPoint;
  corner2: DataPoint;
}

export type Annotation = TextAnnotation | ArrowAnnotation | CircleAnnotation | RectAnnotation;

export interface AnnotationsState {
  measureMode: MeasureToolMode;
  annotationMode: AnnotationToolMode;
  measurements: Measurement[];
  annotations: Annotation[];
  /** Points collected so far for the current in-progress measurement. */
  pendingPoints: DataPoint[];
  /** Currently active measurement result text shown in toolbar. */
  activeResult: string;
}

// ─── Helpers ───────────────────────────────────────────────────────────

let _nextId = 1;
function uid(): string {
  return `m${Date.now()}_${_nextId++}`;
}

/** The radar the current data is relative to, as "lat,lon" (rounded to ~100 m), or null if unknown. */
function siteOf(attrs: Record<string, any> | undefined): { key: string; lat: number; lon: number } | null {
  const lat = parseFloat(attrs?.latitude ?? attrs?.origin_latitude);
  const lon = parseFloat(attrs?.longitude ?? attrs?.origin_longitude);
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || (lat === 0 && lon === 0)) return null;
  return { key: `${lat.toFixed(3)},${lon.toFixed(3)}`, lat, lon };
}

function currentSite() {
  return siteOf(get(radarData).attributes);
}

export function distanceKm(a: DataPoint, b: DataPoint): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  return Math.sqrt(dx * dx + dy * dy) / 1000;
}

/**
 * Bearing from a to b in degrees clockwise from true north. With the radar site known this is the
 * great-circle initial bearing (the plane azimuth differs from it by the meridian convergence,
 * ~2 deg at 200 km east/west of the radar); otherwise the azimuth in the radar plane.
 */
export function bearingDeg(a: DataPoint, b: DataPoint, site?: { lat: number; lon: number } | null): number {
  if (site) {
    const [lat1, lon1] = dataSpaceToLatLon(a.x, a.y, site.lat, site.lon);
    const [lat2, lon2] = dataSpaceToLatLon(b.x, b.y, site.lat, site.lon);
    const p1 = (lat1 * Math.PI) / 180;
    const p2 = (lat2 * Math.PI) / 180;
    const dl = ((lon2 - lon1) * Math.PI) / 180;
    const y = Math.sin(dl) * Math.cos(p2);
    const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
    return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
  }
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  return ((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360;
}

/** Shoelace formula for polygon area in km^2. */
export function polygonAreaKm2(pts: DataPoint[]): number {
  if (pts.length < 3) return 0;
  let area = 0;
  for (let i = 0; i < pts.length; i++) {
    const j = (i + 1) % pts.length;
    area += pts[i].x * pts[j].y;
    area -= pts[j].x * pts[i].y;
  }
  return Math.abs(area / 2) / 1e6; // m^2 -> km^2
}

// ─── Store ─────────────────────────────────────────────────────────────

const STORAGE_KEY = 'xradar-annotations';

function loadFromStorage(): Partial<AnnotationsState> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      return {
        measurements: parsed.measurements ?? [],
        annotations: parsed.annotations ?? [],
      };
    }
  } catch { /* ignore */ }
  return {};
}

function saveToStorage(state: AnnotationsState) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      measurements: state.measurements,
      annotations: state.annotations,
    }));
  } catch { /* ignore */ }
}

const stored = loadFromStorage();

const initialState: AnnotationsState = {
  measureMode: 'none',
  annotationMode: 'none',
  measurements: stored.measurements ?? [],
  annotations: stored.annotations ?? [],
  pendingPoints: [],
  activeResult: '',
};

export const annotationsStore = writable<AnnotationsState>(initialState);

/** Items made on the radar that is currently open (items from other sites are kept, not shown). */
const onCurrentSite = (item: SiteTagged, site: string | undefined) => !item.site || !site || item.site === site;
export const visibleMeasurements = derived([annotationsStore, radarData], ([$s, $r]) => {
  const key = siteOf($r.attributes)?.key;
  return $s.measurements.filter((m) => onCurrentSite(m, key));
});
export const visibleAnnotations = derived([annotationsStore, radarData], ([$s, $r]) => {
  const key = siteOf($r.attributes)?.key;
  return $s.annotations.filter((a) => onCurrentSite(a, key));
});

// Auto-persist when the persisted data changes (not on tool-mode / pending-point updates)
let lastPersistedMeasurements: unknown = initialState.measurements;
let lastPersistedAnnotations: unknown = initialState.annotations;
annotationsStore.subscribe((s) => {
  if (s.measurements === lastPersistedMeasurements && s.annotations === lastPersistedAnnotations) return;
  lastPersistedMeasurements = s.measurements;
  lastPersistedAnnotations = s.annotations;
  saveToStorage(s);
});

// ─── Actions ───────────────────────────────────────────────────────────

export function setMeasureMode(mode: MeasureToolMode) {
  annotationsStore.update((s) => ({
    ...s,
    measureMode: mode,
    annotationMode: 'none',
    pendingPoints: [],
    activeResult: '',
  }));
}

export function setAnnotationMode(mode: AnnotationToolMode) {
  annotationsStore.update((s) => ({
    ...s,
    annotationMode: mode,
    measureMode: 'none',
    pendingPoints: [],
    activeResult: '',
  }));
}

export function addPendingPoint(pt: DataPoint) {
  const s = get(annotationsStore);
  const siteInfo = currentSite();
  const site = siteInfo?.key;

  // A double-click delivers two click events at the same spot first: do not keep both as vertices.
  const last = s.pendingPoints[s.pendingPoints.length - 1];
  if (last && Math.hypot(pt.x - last.x, pt.y - last.y) < 1) return;

  if (s.measureMode === 'distance') {
    const pts = [...s.pendingPoints, pt];
    if (pts.length >= 2) {
      const d = distanceKm(pts[0], pts[1]);
      const m: DistanceMeasurement = {
        id: uid(), site, type: 'distance', points: [pts[0], pts[1]], distanceKm: d,
      };
      annotationsStore.update((st) => ({
        ...st,
        measurements: [...st.measurements, m],
        pendingPoints: [],
        activeResult: `${d.toFixed(2)} km`,
      }));
    } else {
      annotationsStore.update((st) => ({ ...st, pendingPoints: pts }));
    }
  } else if (s.measureMode === 'bearing') {
    const pts = [...s.pendingPoints, pt];
    if (pts.length >= 2) {
      const b = bearingDeg(pts[0], pts[1], siteInfo);
      const d = distanceKm(pts[0], pts[1]);
      const m: BearingMeasurement = {
        id: uid(), site, type: 'bearing', points: [pts[0], pts[1]], bearingDeg: b, distanceKm: d,
      };
      annotationsStore.update((st) => ({
        ...st,
        measurements: [...st.measurements, m],
        pendingPoints: [],
        activeResult: `${b.toFixed(1)}° / ${d.toFixed(2)} km`,
      }));
    } else {
      annotationsStore.update((st) => ({ ...st, pendingPoints: pts }));
    }
  } else if (s.measureMode === 'area') {
    annotationsStore.update((st) => ({
      ...st,
      pendingPoints: [...st.pendingPoints, pt],
    }));
  } else if (s.annotationMode === 'arrow') {
    const pts = [...s.pendingPoints, pt];
    if (pts.length >= 2) {
      const a: ArrowAnnotation = { id: uid(), site, type: 'arrow', start: pts[0], end: pts[1] };
      annotationsStore.update((st) => ({
        ...st,
        annotations: [...st.annotations, a],
        pendingPoints: [],
      }));
    } else {
      annotationsStore.update((st) => ({ ...st, pendingPoints: pts }));
    }
  } else if (s.annotationMode === 'circle') {
    const pts = [...s.pendingPoints, pt];
    if (pts.length >= 2) {
      const dx = pts[1].x - pts[0].x;
      const dy = pts[1].y - pts[0].y;
      const r = Math.sqrt(dx * dx + dy * dy);
      const a: CircleAnnotation = { id: uid(), site, type: 'circle', center: pts[0], radiusM: r };
      annotationsStore.update((st) => ({
        ...st,
        annotations: [...st.annotations, a],
        pendingPoints: [],
      }));
    } else {
      annotationsStore.update((st) => ({ ...st, pendingPoints: pts }));
    }
  } else if (s.annotationMode === 'rectangle') {
    const pts = [...s.pendingPoints, pt];
    if (pts.length >= 2) {
      const a: RectAnnotation = { id: uid(), site, type: 'rectangle', corner1: pts[0], corner2: pts[1] };
      annotationsStore.update((st) => ({
        ...st,
        annotations: [...st.annotations, a],
        pendingPoints: [],
      }));
    } else {
      annotationsStore.update((st) => ({ ...st, pendingPoints: pts }));
    }
  } else if (s.annotationMode === 'text') {
    const a: TextAnnotation = { id: uid(), site, type: 'text', position: pt, text: 'Annotation' };
    annotationsStore.update((st) => ({
      ...st,
      annotations: [...st.annotations, a],
    }));
  }
}

/** Finish an area polygon (close it). */
export function finishAreaMeasurement() {
  const s = get(annotationsStore);
  if (s.measureMode !== 'area' || s.pendingPoints.length < 3) return;
  const area = polygonAreaKm2(s.pendingPoints);
  const m: AreaMeasurement = {
    id: uid(), site: currentSite()?.key, type: 'area', points: [...s.pendingPoints], areaKm2: area,
  };
  annotationsStore.update((st) => ({
    ...st,
    measurements: [...st.measurements, m],
    pendingPoints: [],
    activeResult: `${area.toFixed(2)} km²`,
  }));
}

export function updateAnnotationText(id: string, text: string) {
  annotationsStore.update((s) => ({
    ...s,
    annotations: s.annotations.map((a) =>
      a.id === id && a.type === 'text' ? { ...a, text } : a
    ),
  }));
}

export function updateAnnotationPosition(id: string, position: DataPoint) {
  annotationsStore.update((s) => ({
    ...s,
    annotations: s.annotations.map((a) =>
      a.id === id && a.type === 'text' ? { ...a, position } : a
    ),
  }));
}

export function deleteMeasurement(id: string) {
  annotationsStore.update((s) => ({
    ...s,
    measurements: s.measurements.filter((m) => m.id !== id),
  }));
}

export function deleteAnnotation(id: string) {
  annotationsStore.update((s) => ({
    ...s,
    annotations: s.annotations.filter((a) => a.id !== id),
  }));
}

/** Clear the items shown for the open radar (items made on other radars are kept). */
export function clearAll() {
  const key = currentSite()?.key;
  annotationsStore.update((s) => ({
    ...s,
    measurements: s.measurements.filter((m) => !onCurrentSite(m, key)),
    annotations: s.annotations.filter((a) => !onCurrentSite(a, key)),
    pendingPoints: [],
    activeResult: '',
  }));
}

export function exportAnnotationsJSON(): string {
  const s = get(annotationsStore);
  const key = currentSite()?.key;
  return JSON.stringify(
    {
      site: key ?? null,
      measurements: s.measurements.filter((m) => onCurrentSite(m, key)),
      annotations: s.annotations.filter((a) => onCurrentSite(a, key)),
    },
    null,
    2,
  );
}
