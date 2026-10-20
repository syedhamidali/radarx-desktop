/**
 * Which layers of the movie are shown, and how strongly, in each shot: "layers fade by relevance".
 * The table gives an opacity per layer and shot; between shots the values crossfade with the same
 * smoothstep as the camera (choreography.py, 5 s centred on the start of the next shot), so a layer
 * never pops. Pure; unit-tested in perilsLayers.test.mjs.
 */

export type LayerId =
  | 'radars' | 'stations' | 'lma' | 'hail' | 'rotation' | 'tornadoTrack' | 'funnel' | 'zdr' | 'convStrat' | 'balloon'
  | 'mrms' | 'isotherms' | 'flow' | 'wsurface' | 'captions' | 'skewt' | 'envPanel' | 'stripCharts' | 'timeHeight' | 'lmaPanel' | 'stationCard'
  | 'modelCloud' | 'surfaceModels' | 'dsd' | 'rainStreaks' | 'immersion' | 'tornadoCard' | 'minimap' | 'sim'
  | 'focusInset' | 'swath' | 'rotSeries';

const SHOTS = [
  'establishing', 'flyby', 'tornado', 'window', 'rise', 'topdown',
  'focus_wide', 'focus_chaser', 'focus_close', 'focus_low', 'focus_swath', 'focus_top',
] as const;
type ShotName = (typeof SHOTS)[number];

/**
 * Opacity [0..1] per layer for the shots in order: establishing, flyby, tornado (the v2 film's single tornado shot), window, rise,
 * topdown, then the six angles of the tornado focus: wide, chaser, close, low, swath, top.
 */
export const LAYER_TABLE: Record<LayerId, number[]> = {
  radars: [1, 0.55, 0.0, 0.35, 0.6, 1, 0.3, 0, 0, 0, 0, 0.4],
  stations: [0.7, 0.9, 0.0, 1, 0.7, 1, 0.3, 0, 0, 0, 0, 0.5],
  lma: [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
  hail: [0.35, 1, 0.0, 0.7, 0.5, 0.8, 0, 0, 0, 0, 0, 0],
  rotation: [0.5, 0.9, 0.0, 0.4, 0.5, 0.8, 0, 0, 0, 0, 0, 0],
  tornadoTrack: [1, 1, 0.0, 0.5, 0.8, 1, 1, 0.4, 0, 0, 0, 1],
  funnel: [0, 0.6, 1, 0.5, 0.5, 0, 0.5, 1, 1, 1, 1, 0],
  zdr: [0.3, 0.8, 0.0, 0.3, 0.3, 0.5, 0, 0, 0, 0, 0, 0],
  convStrat: [0.5, 0, 0, 0, 0.2, 1, 0, 0, 0, 0, 0, 0],
  balloon: [1, 1, 0.0, 1, 0.8, 0.8, 0, 0, 0, 0, 0, 0],
  mrms: [1, 0.8, 0.5, 0.8, 1, 1, 0.8, 0.4, 0.4, 0.4, 0.4, 1],
  isotherms: [0.6, 0.6, 0.0, 0.0, 0.5, 0, 0, 0, 0, 0, 0, 0],
  flow: [0, 0.3, 0.0, 0.5, 0.5, 0, 0, 0, 0, 0, 0, 0],
  wsurface: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  captions: [1, 1, 0.0, 1, 1, 1, 0, 0, 0, 0, 0, 0],
  skewt: [0.95, 0, 0, 0, 0.9, 0, 0, 0, 0, 0, 0, 0],
  envPanel: [0, 0, 0, 0, 0, 0.6, 0, 0, 0, 0, 0, 0],
  stripCharts: [0, 0.4, 0, 1, 0.3, 0, 0, 0, 0, 0, 0, 0],
  timeHeight: [0, 0, 0, 1, 0.3, 0, 0, 0, 0, 0, 0, 0],
  lmaPanel: [1, 1, 0.0, 0.9, 1, 1, 0.5, 0, 0, 0, 0, 0.5],
  stationCard: [0, 0.8, 0.0, 1, 0, 0.3, 0, 0, 0, 0, 0, 0],
  modelCloud: [0.5, 0, 0, 0, 0.3, 0.5, 0, 0, 0, 0, 0, 0],
  surfaceModels: [0.7, 0.3, 0.0, 0.3, 0.5, 0.8, 0.3, 0, 0, 0, 0, 0.4],
  dsd: [0, 0, 0, 1, 0.3, 0, 0, 0, 0, 0, 0, 0],
  rainStreaks: [0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0],
  /** Not a drawn layer: the immersive grade of the tornado shots (labels, arrows and cards out; one lower third). */
  immersion: [0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1, 0],
  /** The tornado card of the wide shots (the immersive shots have their own lower third). */
  tornadoCard: [0, 1, 0, 0.7, 0, 0.7, 1, 0, 0, 0, 0, 1],
  /** The plan-view inset ("where the camera is"): in every shot but the tornado focus, which has its own. */
  minimap: [1, 1, 0.6, 1, 1, 1, 0, 0, 0, 0, 0, 0],
  /** The simulated tornado (further multiplied by the camera's nearness to the tornado and by the data clock being near real time). */
  sim: [0, 0, 1, 0, 0, 0, 1, 1, 1, 1, 1, 1],
  /** The tornado focus inset: column-maximum echo and storm-relative velocity around the tornado, the camera and its view cone. */
  focusInset: [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1],
  /** The damage swath on the ground (the surveyed width) up to the tornado's position. */
  swath: [0, 0, 0, 0.5, 0.8, 1, 1, 0, 0, 0, 0.3, 0.8],
  /** The small time series of the radar rotation next to the surveyed life of the tornado. */
  rotSeries: [0, 0, 0, 0, 0, 0, 1, 0.85, 0.7, 0.7, 0.7, 1],
};

export function smoothstep(x: number): number {
  const c = Math.max(0, Math.min(1, x));
  return c * c * (3 - 2 * c);
}

export interface ShotSpan {
  name: string;
  t0: number;
  t1: number;
  /** Seconds of the glide into this shot (the camera's); the default overlap of `crossfade` when absent. */
  overlap?: number | null;
}

/**
 * Value of a per-shot list at clip time `t`: shot j fades in over `overlap` seconds centred on its
 * start, on top of the shots before it. A shot name outside the list takes the establishing value.
 */
export function crossfade(shots: ShotSpan[], values: number[], t: number, overlap = 5): number {
  const val = (name: string) => {
    const i = SHOTS.indexOf(name as ShotName);
    return values[i < 0 ? 0 : i] ?? 0;
  };
  if (!shots.length) return val('establishing');
  let v = val(shots[0].name);
  for (let j = 1; j < shots.length; j++) {
    const ov = shots[j].overlap ?? overlap;
    const w = smoothstep((t - (shots[j].t0 - ov / 2)) / ov);
    v = (1 - w) * v + w * val(shots[j].name);
  }
  return v;
}

export class LayerVis {
  private overrides = new Map<LayerId, number>();
  private shots: ShotSpan[];

  constructor(shots: ShotSpan[]) {
    this.shots = shots;
  }

  /** Force a layer on (1) or off (0) for the whole film (a switch), or clear with undefined. */
  set(id: LayerId, v: number | undefined): void {
    if (v === undefined) this.overrides.delete(id);
    else this.overrides.set(id, v);
  }

  at(id: LayerId, t: number): number {
    const o = this.overrides.get(id);
    if (o !== undefined) return o;
    return crossfade(this.shots, LAYER_TABLE[id], t);
  }
}
