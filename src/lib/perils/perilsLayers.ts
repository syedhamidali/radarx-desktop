/**
 * Which layers of the movie are shown, and how strongly, in each shot: "layers fade by relevance".
 * The table gives an opacity per layer and shot; between shots the values crossfade with the same
 * smoothstep as the camera (choreography.py, 5 s centred on the start of the next shot), so a layer
 * never pops. Pure; unit-tested in perilsLayers.test.mjs.
 */

export type LayerId =
  | 'radars' | 'stations' | 'lma' | 'hail' | 'rotation' | 'tornadoTrack' | 'funnel' | 'zdr' | 'convStrat' | 'balloon'
  | 'mrms' | 'isotherms' | 'flow' | 'wsurface' | 'captions' | 'skewt' | 'envPanel' | 'stripCharts' | 'timeHeight' | 'lmaPanel' | 'stationCard'
  | 'modelCloud' | 'surfaceModels' | 'dsd' | 'rainStreaks';

const SHOTS = ['establishing', 'flyby', 'tornado', 'window', 'rise', 'topdown'] as const;
type ShotName = (typeof SHOTS)[number];

/** Opacity [0..1] per layer for the shots in order: establishing, flyby, tornado, window, rise, topdown. */
export const LAYER_TABLE: Record<LayerId, number[]> = {
  radars: [1, 0.55, 0.0, 0.35, 0.6, 1],
  stations: [0.7, 0.9, 0.6, 1, 0.7, 1],
  lma: [1, 1, 1, 1, 1, 1],
  hail: [0.35, 1, 1, 0.7, 0.5, 0.8],
  rotation: [0.5, 0.9, 1, 0.4, 0.5, 0.8],
  tornadoTrack: [1, 1, 1, 0.5, 0.8, 1],
  funnel: [0, 0.6, 1, 0.5, 0.5, 0],
  zdr: [0.3, 0.8, 0.7, 0.3, 0.3, 0.5],
  convStrat: [0.5, 0, 0, 0, 0.2, 1],
  balloon: [0.9, 0.5, 0.2, 0.9, 0.3, 0.5],
  mrms: [1, 0.8, 0.5, 0.8, 1, 1],
  isotherms: [0.6, 0.6, 0.5, 0.0, 0.5, 0],
  flow: [0, 0.3, 1, 0.5, 0.5, 0],
  wsurface: [0, 0, 0, 0, 0, 0],
  captions: [1, 1, 1, 1, 1, 1],
  skewt: [0.95, 0, 0, 0, 0.9, 0],
  envPanel: [1, 0, 0, 0, 0.8, 0.6],
  stripCharts: [0, 0.4, 0, 1, 0.3, 0],
  timeHeight: [0, 0, 0, 1, 0.3, 0],
  lmaPanel: [1, 1, 0.9, 0.9, 1, 1],
  stationCard: [0.3, 0.8, 0.5, 1, 0.5, 0.3],
  modelCloud: [0.5, 0, 0, 0, 0.3, 0.5],
  surfaceModels: [0.7, 0.3, 0.2, 0.3, 0.5, 0.8],
  dsd: [0, 0, 0, 1, 0.3, 0],
  rainStreaks: [0, 0, 0, 1, 0, 0],
};

export function smoothstep(x: number): number {
  const c = Math.max(0, Math.min(1, x));
  return c * c * (3 - 2 * c);
}

export interface ShotSpan {
  name: string;
  t0: number;
  t1: number;
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
    const w = smoothstep((t - (shots[j].t0 - overlap / 2)) / overlap);
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
