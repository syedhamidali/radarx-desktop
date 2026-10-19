/**
 * Lightning light for the tornado scene: the brightness envelope of a flash as a function of time (pure, tested in
 * flashes.test.mjs). The positions and times of the flashes are observed (LMA); the burst shape is the one of
 * `lightning.ts` (2 to 4 strokes 0.05 to 0.17 s apart, each a fast rise and an exponential decay of the light the
 * cloud scatters) and the brightness is a model. Kept separate from lightning.ts so that this module shares no
 * chunk with the storm clip.
 */

export interface FlashEnv {
  /** Time of the first stroke (data clock, s). */
  t: number;
  strokes: { dt: number; amp: number }[];
  /** 0..1 relative strength. */
  energy: number;
}

/** Decay time of the light the cloud scatters (s), as in lightning.ts. */
export const CLOUD_TAU = 0.09;
/** Seconds after the last stroke when a flash can be forgotten. */
export const FLASH_TAIL = 0.6;

function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** The envelope of flash number `id` at time `t`: same input, same flash. */
export function makeFlashEnv(id: number, t: number, energy?: number): FlashEnv {
  const r = rng(id * 104729 + 17);
  const n = 2 + Math.floor(r() * 3);
  const strokes: FlashEnv['strokes'] = [];
  let dt = 0;
  for (let i = 0; i < n; i++) {
    strokes.push({ dt, amp: 0.25 + 0.4 * r() });
    dt += 0.05 + 0.12 * r();
  }
  return { t, strokes, energy: energy ?? 0.5 + 0.5 * r() };
}

/** Light (0..1.2) the cloud and the ground receive at time t. */
export function flashLight(f: FlashEnv, t: number): number {
  let li = 0;
  for (const s of f.strokes) {
    const x = t - (f.t + s.dt);
    if (x >= 0) li += s.amp * Math.exp(-x / CLOUD_TAU);
  }
  return Math.min(1.2, li * (0.6 + 0.6 * f.energy));
}

export function flashEnd(f: FlashEnv): number {
  return f.t + (f.strokes[f.strokes.length - 1]?.dt ?? 0) + FLASH_TAIL;
}
