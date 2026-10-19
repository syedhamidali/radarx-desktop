/**
 * Science captions of the PERiLS movie. An event comes from the data (science_events.json of the
 * bundle, or derived from it by tools/perils_movie/render/science.py): a time, a kind, numbers, a
 * provenance class. The one-sentence plain-language explanation of the physics is written here from
 * standard references (Markowski and Richardson 2010; Kumjian 2013; Trapp 2013; Weisman 1992; Ryzhkov et al. 2005;
 * Bunkers et al. 2000); the NUMBERS in a caption are always the event's own, never made up.
 * Pure; unit-tested in perilsCaptions.test.mjs.
 */
import type { ProvKind } from './perilsData.ts';

export interface ScienceEvent {
  /** Seconds after the first step. */
  t_s: number;
  kind: string;
  title?: string | null;
  /** The data tool's own sentence with its numbers ("Outflow boundary at ...: temperature fell 2.0 K ..."). */
  caption?: string | null;
  /** Plain numbers with units, e.g. ["+2.1 hPa in 90 s", "-4.3 K"]. */
  numbers?: string[];
  text?: string | null;
  class?: string | null;
  /** The event is a candidate (evidence only), said in its title. */
  candidate?: boolean;
  where?: string | null;
  /** Position in km of the scene, where the event has one. */
  x?: number | null;
  y?: number | null;
  lat?: number | null;
  lon?: number | null;
  /** Seconds the caption stays (default 7 s of CLIP time). */
  hold_s?: number;
}

export interface Physics {
  title: string;
  text: string;
  prov: ProvKind;
}

/** Plain-language physics per event kind (one sentence each). */
export const PHYSICS: Record<string, Physics> = {
  gust_front: {
    title: 'Gust front',
    text: 'The leading edge of the storm\'s cold pool: air cooled by rain pushes out along the ground, so pressure jumps, wind shifts and temperature drops as it passes.',
    prov: 'observed',
  },
  pressure_jump: {
    title: 'Pressure jump',
    text: 'Cold, dense air arriving under the storm raises surface pressure within minutes: the mesohigh of the cold pool.',
    prov: 'observed',
  },
  cold_pool: {
    title: 'Cold pool',
    text: 'Rain-cooled air spreads out under the storm; its edge lifts the warm inflow and keeps the line going.',
    prov: 'observed',
  },
  rear_inflow_jet: {
    title: 'Rear-inflow jet',
    text: 'A rear-to-front current that descends into a line of storms and can bring damaging winds to the surface.',
    prov: 'retrieved',
  },
  zdr_column: {
    title: 'ZDR column',
    text: 'A column of high differential reflectivity above the melting level: the updraft is lofting liquid drops high enough to freeze into hail.',
    prov: 'retrieved',
  },
  debris_signature: {
    title: 'Debris signature',
    text: 'Low correlation coefficient with high reflectivity: lofted debris of different shapes and sizes, so a tornado is on the ground doing damage.',
    prov: 'retrieved',
  },
  tornado: {
    title: 'Tornado',
    text: 'A narrow, violently rotating column of air in contact with the ground; its position and strength rating come from the damage survey.',
    prov: 'observed',
  },
  rotation: {
    title: 'Low-level rotation',
    text: 'Strong inbound and outbound Doppler velocities side by side mark a vortex; the larger the difference, the stronger the rotation.',
    prov: 'retrieved',
  },
  hail: {
    title: 'Hail core',
    text: 'Hail looks different from rain to a polarimetric radar: very high reflectivity with low differential reflectivity and large, tumbling stones.',
    prov: 'retrieved',
  },
  first_flash: {
    title: 'Lightning mapped',
    text: 'The lightning mapping array locates the radio pulses of each flash in three dimensions; each point is a measured source.',
    prov: 'observed',
  },
  flash_rate: {
    title: 'Flash rate',
    text: 'Flash rate follows the strength of the updraft and the amount of ice: a jump can signal a strengthening storm.',
    prov: 'observed',
  },
  gust_front_speed: {
    title: 'Outflow speed',
    text: 'The speed at which the cold pool\'s edge spreads: if it outruns the storms they lose their warm inflow, if it keeps pace the line stays organised.',
    prov: 'observed',
  },
  line_motion: {
    title: 'Line motion',
    text: 'A squall line travels as a pattern while the cells inside it come and go; the pattern motion is that of the echo, not of the wind.',
    prov: 'retrieved',
  },
  rain_dsd: {
    title: 'Raindrop sizes',
    text: 'A disdrometer counts drops by size; Dm, the mean diameter weighted by mass, rises where fewer, larger drops carry the rain.',
    prov: 'observed',
  },
  vorticity: {
    title: 'Vertical vorticity',
    text: 'Spin about a vertical axis computed from the retrieved winds; at 1 km resolution it shows mesovortices and the rotation of the line, not a tornado.',
    prov: 'retrieved',
  },
  updraft: {
    title: 'Updraft',
    text: 'Warm moist air rising through the storm, retrieved from several radars; the strongest updrafts carry water high enough to freeze and grow hail.',
    prov: 'retrieved',
  },
  convergence: {
    title: 'Low-level convergence',
    text: 'Air flowing together near the ground is forced upward; along the gust front this lifts the warm inflow and can start new cells.',
    prov: 'retrieved',
  },
  mesovortex: {
    title: 'Mesovortex',
    text: 'A small circulation embedded in the line; mesovortices along squall lines often produce the line\'s brief tornadoes and damaging winds.',
    prov: 'retrieved',
  },
  size_sorting: {
    title: 'Size sorting',
    text: 'In sheared air big drops fall faster than small ones and drift apart from them, so big drops pile up on one side of the storm.',
    prov: 'retrieved',
  },
};

/**
 * Caption of an event: the data's own title and sentence (with its numbers), the physics sentence of its kind and the
 * provenance chip. A candidate says so in its title.
 */
export function captionOf(ev: ScienceEvent): { title: string; numbers: string; text: string; prov: ProvKind } {
  const ph = PHYSICS[ev.kind];
  const known = ['observed', 'model', 'analysis', 'retrieved', 'interpolated', 'stylised'];
  const cls = ev.class && known.includes(ev.class) ? (ev.class as ProvKind) : undefined;
  const title = ev.title ?? ph?.title ?? ev.kind.replace(/_/g, ' ');
  return {
    title: ev.candidate ? `${title} (candidate)` : title,
    numbers: ev.caption ?? (ev.numbers ?? []).join(' · '),
    text: ev.text ?? ph?.text ?? '',
    prov: cls ?? ph?.prov ?? 'observed',
  };
}

/** Importance of a kind when captions compete for the 120 seconds (higher is kept first). */
export const CAPTION_PRIORITY: Record<string, number> = {
  tornado: 10, rotation: 9, hail: 8, debris_signature: 8, gust_front: 7, rear_inflow_jet: 7, zdr_column: 6, updraft: 6, first_flash: 6,
  vorticity: 5, flash_rate: 5, line_motion: 5, rain_dsd: 4, gust_front_speed: 4, convergence: 4, size_sorting: 4,
};
/** How many captions of one kind the film shows (default 1). */
const MAX_PER_KIND: Record<string, number> = { tornado: 2, gust_front: 2, rain_dsd: 1 };

export interface ScheduledCaption {
  ev: ScienceEvent;
  /** Clip time the caption appears, and how long it stays (s). */
  t0: number;
  hold: number;
}

/**
 * Put the data events on the clip: each at its own clip time (`toClip` of its data time) where it fits, shifted a
 * little (later by up to `maxLate` s, earlier by up to 3 s) when another caption is in the way, left out when
 * there is no room, so that at most one caption is up at a time and none runs into the closing card
 * (`endSpan` s). More important kinds are placed first; of a kind only a few are shown.
 */
export function scheduleCaptions(events: ScienceEvent[], toClip: (td: number) => number, duration: number, hold = 6.5, endSpan = 9, maxLate = 10): ScheduledCaption[] {
  const order = events
    .map((ev, i) => ({ ev, i, p: CAPTION_PRIORITY[ev.kind] ?? 3 }))
    .sort((a, b) => b.p - a.p || a.ev.t_s - b.ev.t_s || a.i - b.i);
  const chosen: ScheduledCaption[] = [];
  const count = new Map<string, number>();
  const limit = duration - endSpan - 0.5;
  for (const { ev } of order) {
    if ((count.get(ev.kind) ?? 0) >= (MAX_PER_KIND[ev.kind] ?? 1)) continue;
    const h = ev.hold_s ?? hold;
    const c = Math.max(0.5, toClip(ev.t_s));
    let placed: number | null = null;
    for (const shift of [0, 0.5, 1, 1.5, 2, 3, 4, 5, 6, 7, 8, 9, 10, -1, -2, -3]) {
      if (shift > maxLate) continue;
      const t0 = c + shift;
      if (t0 < 0.5 || t0 + h > limit) continue;
      if (chosen.every((q) => t0 + h <= q.t0 + 1e-9 || q.t0 + q.hold <= t0 + 1e-9)) {
        placed = t0;
        break;
      }
    }
    if (placed === null) continue;
    chosen.push({ ev, t0: placed, hold: h });
    count.set(ev.kind, (count.get(ev.kind) ?? 0) + 1);
  }
  return chosen.sort((a, b) => a.t0 - b.t0);
}

/** The scheduled caption on screen at clip time `tc`, or null. */
export function activeCaption(sched: ScheduledCaption[], tc: number): ScheduledCaption | null {
  for (const q of sched) if (tc >= q.t0 && tc < q.t0 + q.hold) return q;
  return null;
}

/**
 * The event to caption at clip time `tc` when events are not scheduled: the latest event that started within the last
 * `hold` clip seconds (its own hold_s, default 7), or null. `toClip` maps an event's data time to clip time.
 */
export function activeEvent(events: ScienceEvent[], toClip: (td: number) => number, tc: number): ScienceEvent | null {
  let best: ScienceEvent | null = null;
  let bt = -Infinity;
  for (const ev of events) {
    const c = toClip(ev.t_s);
    const hold = ev.hold_s ?? 7;
    if (tc >= c && tc < c + hold && c > bt) {
      best = ev;
      bt = c;
    }
  }
  return best;
}

/** Opacity of a caption: fades in over 0.4 s and out over the last 0.8 s of its hold. */
export function captionAlpha(tc: number, c0: number, hold: number): number {
  const a = Math.min(1, (tc - c0) / 0.4);
  const b = Math.min(1, (c0 + hold - tc) / 0.8);
  return Math.max(0, Math.min(a, b));
}

/** Break `text` into lines of at most `maxChars`, at spaces. */
export function wrapText(text: string, maxChars: number): string[] {
  const words = text.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let cur = '';
  for (const w of words) {
    if (cur && cur.length + 1 + w.length > maxChars) {
      lines.push(cur);
      cur = w;
    } else cur = cur ? `${cur} ${w}` : w;
  }
  if (cur) lines.push(cur);
  return lines;
}
