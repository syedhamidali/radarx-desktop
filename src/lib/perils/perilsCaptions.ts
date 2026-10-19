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
  title?: string;
  /** Plain numbers with units, e.g. ["+2.1 hPa in 90 s", "-4.3 K"]. */
  numbers?: string[];
  text?: string;
  class?: string;
  where?: string;
  lat?: number;
  lon?: number;
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

/** Caption of an event: the data's own title and numbers, the physics sentence, the provenance chip. */
export function captionOf(ev: ScienceEvent): { title: string; numbers: string; text: string; prov: ProvKind } {
  const ph = PHYSICS[ev.kind];
  const cls = ev.class && ['observed', 'model', 'analysis', 'retrieved', 'interpolated', 'stylised'].includes(ev.class) ? (ev.class as ProvKind) : undefined;
  return {
    title: ev.title ?? ph?.title ?? ev.kind.replace(/_/g, ' '),
    numbers: (ev.numbers ?? []).join(' · '),
    text: ev.text ?? ph?.text ?? '',
    prov: cls ?? ph?.prov ?? 'observed',
  };
}

/**
 * The event to caption at clip time `tc`: the latest event that started within the last `hold` clip
 * seconds (its own hold_s, default 7), or null. `toClip` maps an event's data time to clip time.
 * Events whose clip times overlap show the most recent one.
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
