/**
 * The sound of the lightning layer in the viewer: the pure part (no Web Audio, no DOM), unit tested in
 * liveSound.test.mjs. The Web Audio graph that plays it is in liveSoundEngine.ts.
 *
 * WHAT IS DATA AND WHAT IS MADE UP (said in the interface too: "Synthesised from the data, not a recording"):
 *  - from the data: the time of each flash, its distance from the listener, its direction (relative to
 *    where the listener faces), its strength (GLM radiant energy or LMA source count, scaled between the 5th
 *    and 95th percentile of the flashes read). The light-to-sound delay is physics: distance / 343 m/s,
 *    divided by the time-lapse factor when the window is replayed faster than real time.
 *  - made up: the timbre. Thunder here is shaped noise (a crack for a near flash, a low rumble for a far
 *    one, rolling longer with distance); it is not a model of real thunder and not a recording.
 *  - not offered: wind, rain, hail or tornado sounds. Nothing in lightning data says anything about them.
 *
 * Everything is deterministic: the same seed gives the same samples.
 */
import { mulberry32 } from './lightning.ts';
import { dbToGain, thunderCutoffHz, thunderGain, thunderTail } from './soundscape.ts';
import { lowerBound, type LightningData } from './lightningLayer.ts';

export const SOUND_LABEL = 'Synthesised from the data, not a recording';
export const SPEED_OF_SOUND_MS = 343;

// ── Geometry ────────────────────────────────────────────────────────────────

/** Light-to-sound delay (s) over `distKm`, divided by the time-lapse factor `compress` (1 = real time). */
export function thunderDelayS(distKm: number, compress = 1): number {
  return (Math.max(0, distKm) * 1000) / SPEED_OF_SOUND_MS / Math.max(1e-9, compress);
}

/** Where the listener is and which way it faces (km east / north of the radar; degrees clockwise from north). */
export interface ListenerPose {
  x: number;
  y: number;
  yawDeg: number;
}

/** Direction of a source relative to where the listener faces, degrees in (-180, 180], positive to the right. */
export function relativeAzimuthDeg(pose: ListenerPose, sx: number, sy: number): number {
  const bearing = (Math.atan2(sx - pose.x, sy - pose.y) * 180) / Math.PI; // clockwise from north
  let a = bearing - pose.yawDeg;
  a = ((((a + 180) % 360) + 360) % 360) - 180;
  return a === -180 ? 180 : a;
}

/** Equal-power stereo gains for an azimuth (0 = ahead, +90 = right): left^2 + right^2 = 1. A source behind folds to the side it is on. */
export function equalPowerPan(azimuthDeg: number): { left: number; right: number } {
  const p = Math.max(-1, Math.min(1, Math.sin((azimuthDeg * Math.PI) / 180)));
  const theta = ((p + 1) * Math.PI) / 4;
  return { left: Math.cos(theta), right: Math.sin(theta) };
}

// ── Mixer ───────────────────────────────────────────────────────────────────

export type LayerId = 'thunder' | 'ticks';

export interface Mixer {
  /** Master level, dB (0 = unchanged). */
  masterDb: number;
  /** Highest sample level the limiter lets through, dBFS (negative). */
  ceilingDb: number;
  layers: Record<LayerId, { levelDb: number; mute: boolean }>;
}

export const DEFAULT_MIXER: Mixer = {
  masterDb: -6,
  ceilingDb: -6,
  layers: { thunder: { levelDb: 0, mute: false }, ticks: { levelDb: -12, mute: true } },
};

/** The linear gain of a layer: 0 when muted, else its level in dB. */
export function layerGain(m: Mixer, id: LayerId): number {
  const l = m.layers[id];
  return l.mute ? 0 : dbToGain(l.levelDb);
}

/** Limiter: the gain that keeps a peak (linear, 0..) at or below the ceiling; never above 1 (it never amplifies). */
export function limiterGain(peak: number, ceilingDb: number): number {
  const ceiling = dbToGain(Math.min(0, ceilingDb));
  return peak > ceiling ? ceiling / peak : 1;
}

/** The layers and what drives each (the report of which instrument drove which layer). */
export function layerReport(d: LightningData): { id: LayerId; name: string; driver: string; kind: string }[] {
  const what = d.kind === 'glm' ? `${d.instrument}: flash time, position and radiant energy` : `${d.instrument}: flash time, position and number of sources`;
  return [
    { id: 'thunder', name: 'Thunder', driver: `${what}; arrival after distance / 343 m/s (time-lapse scaled)`, kind: 'timbre made up' },
    { id: 'ticks', name: 'Flash ticks', driver: `${what}; a short tick at the time of each flash (no delay)`, kind: 'sonification, made up' },
  ];
}

// ── Flash strength ──────────────────────────────────────────────────────────

/**
 * A strength 0..1 for every flash: GLM flash radiant energy, or the number of LMA sources, on a log scale
 * between the 5th and 95th percentile of the flashes read (a flash with no value gets 0.5).
 */
export function flashStrength01(d: LightningData): Float32Array {
  const raw = d.kind === 'glm' ? d.f.strength : d.f.nPoints;
  const v: number[] = [];
  for (let i = 0; i < d.nf; i++) if (raw[i] > 0 && Number.isFinite(raw[i])) v.push(Math.log10(raw[i]));
  const out = new Float32Array(d.nf).fill(0.5);
  if (v.length < 2) return out;
  const s = v.slice().sort((a, b) => a - b);
  const q = (p: number) => s[Math.min(s.length - 1, Math.max(0, Math.round(p * (s.length - 1))))];
  const lo = q(0.05);
  const hi = q(0.95);
  if (!(hi > lo)) return out;
  for (let i = 0; i < d.nf; i++) out[i] = raw[i] > 0 && Number.isFinite(raw[i]) ? Math.min(1, Math.max(0, (Math.log10(raw[i]) - lo) / (hi - lo))) : 0.5;
  return out;
}

// ── The replay plan ─────────────────────────────────────────────────────────

export interface SoundEvent {
  flash: number;
  /** Seconds from the start of the replay at which the flash happens (the tick). */
  tickAt: number;
  /** Seconds from the start at which its thunder arrives. */
  thunderAt: number;
  distKm: number;
  azimuthDeg: number;
  /** Thunder loudness (linear, before the mixer), low-pass cutoff (Hz) and tail (s). */
  gain: number;
  cutoffHz: number;
  tailS: number;
  /** Position of the flash, km east / north of the radar. */
  x: number;
  y: number;
}

export interface ReplayOptions {
  /** Data seconds per real second (30 = a 5-minute window lasts 10 s). */
  speed: number;
  /** Time-lapse factor of the thunder delay; default the same as `speed`. */
  delayCompress?: number;
  seed?: number;
}

/**
 * The flashes that start inside [startS, endS] (seconds after tRef) as sound events, in order of arrival.
 * `place(i)` gives the position (km east / north of the radar) of flash i; the distance is horizontal plus
 * the initiation height for LMA (km above the listener, which is on the ground).
 */
export function planReplay(
  d: LightningData,
  startS: number,
  endS: number,
  pose: ListenerPose,
  place: (flash: number) => [number, number],
  opt: ReplayOptions,
): SoundEvent[] {
  const strength = flashStrength01(d);
  const speed = Math.max(1e-6, opt.speed);
  const compress = Math.max(1e-6, opt.delayCompress ?? speed);
  const out: SoundEvent[] = [];
  for (let i = lowerBound(d.f.t0, startS); i < d.nf && d.f.t0[i] <= endS; i++) {
    const [x, y] = place(i);
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    const z = Number.isFinite(d.f.altKm[i]) ? d.f.altKm[i] : 0;
    const distKm = Math.hypot(x - pose.x, y - pose.y, z);
    const tickAt = (d.f.t0[i] - startS) / speed;
    out.push({
      flash: i,
      tickAt,
      thunderAt: tickAt + thunderDelayS(distKm, compress),
      distKm,
      azimuthDeg: relativeAzimuthDeg(pose, x, y),
      gain: thunderGain(distKm, strength[i]),
      cutoffHz: thunderCutoffHz(distKm),
      tailS: thunderTail(distKm),
      x,
      y,
    });
  }
  out.sort((a, b) => a.thunderAt - b.thunderAt);
  return out;
}

// ── Synthesis (deterministic PCM) ───────────────────────────────────────────

/** Level of a buffer: RMS in dBFS (not a perceptual loudness: no K-weighting) and the peak in dBFS. */
export function rmsDb(buf: ArrayLike<number>): number {
  let s = 0;
  for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i];
  return buf.length ? 10 * Math.log10(Math.max(1e-20, s / buf.length)) : -Infinity;
}
export function peakDb(buf: ArrayLike<number>): number {
  let p = 0;
  for (let i = 0; i < buf.length; i++) p = Math.max(p, Math.abs(buf[i]));
  return 20 * Math.log10(Math.max(1e-10, p));
}

/**
 * Thunder as mono samples. Shaped noise: a low-pass (cutoff falls with distance), an envelope that
 * rises in 40 ms and rolls out over a tail that grows with distance, a short crack for a flash within
 * 4 km. The amplitude is `thunderGain(dist, energy)` times the envelope; the peak is kept at or below
 * 0.95. Timbre is made up; the delay, direction and level are from the data.
 */
export function synthThunder(o: { seed: number; distKm: number; energy: number; sampleRate: number }): Float32Array {
  const sr = o.sampleRate;
  const tail = thunderTail(o.distKm);
  const seconds = Math.min(12, 0.3 + 4.5 * tail);
  const n = Math.max(1, Math.floor(seconds * sr));
  const rng = mulberry32(o.seed >>> 0);
  const out = new Float32Array(n);
  const cutoff = thunderCutoffHz(o.distKm);
  const alpha = 1 - Math.exp((-2 * Math.PI * cutoff) / sr); // one-pole low-pass, applied twice
  let y1 = 0;
  let y2 = 0;
  const gain = thunderGain(o.distKm, o.energy);
  const attack = 0.04;
  // a slow wobble of the rumble level (deterministic: from the same generator)
  const w1 = 0.6 + rng() * 1.4;
  const w2 = 1.7 + rng() * 2.1;
  const p1 = rng() * Math.PI * 2;
  const p2 = rng() * Math.PI * 2;
  let peak = 0;
  for (let i = 0; i < n; i++) {
    const t = i / sr;
    const env = (t < attack ? t / attack : Math.exp(-(t - attack) / tail)) * (0.8 + 0.2 * Math.sin(2 * Math.PI * w1 * t + p1) * Math.sin(2 * Math.PI * w2 * t + p2));
    const white = rng() * 2 - 1;
    y1 += alpha * (white - y1);
    y2 += alpha * (y1 - y2);
    let v = y2 * 3.2 * env;
    if (o.distKm < 4 && t < 0.12) v += (rng() * 2 - 1) * (1 - o.distKm / 4) * Math.exp(-t / 0.02) * 0.9; // the crack of a near strike
    v *= gain;
    out[i] = v;
    peak = Math.max(peak, Math.abs(v));
  }
  const cap = 0.95;
  if (peak > cap) for (let i = 0; i < n; i++) out[i] *= cap / peak;
  return out;
}

/** A short tick (a click with a fast fall), mono, for the "flash ticks" layer: a sonification of the time of a flash. */
export function synthTick(o: { seed: number; strength: number; sampleRate: number }): Float32Array {
  const sr = o.sampleRate;
  const n = Math.floor(0.05 * sr);
  const rng = mulberry32(o.seed >>> 0);
  const out = new Float32Array(n);
  const amp = 0.25 + 0.55 * Math.max(0, Math.min(1, o.strength));
  for (let i = 0; i < n; i++) out[i] = (rng() * 2 - 1) * amp * Math.exp(-i / (0.006 * sr));
  return out;
}

/** Scale `buf` so that its RMS is `targetDb` dBFS, then down so that its peak stays at or below `ceilingDb`. Returns the gain used (linear). */
export function normaliseTo(buf: Float32Array, targetDb: number, ceilingDb: number): number {
  const rms = rmsDb(buf);
  if (!Number.isFinite(rms) || rms < -120) return 1;
  let g = dbToGain(targetDb - rms);
  const peak = dbToGain(peakDb(buf)) * g;
  const ceil = dbToGain(Math.min(0, ceilingDb));
  if (peak > ceil) g *= ceil / peak;
  for (let i = 0; i < buf.length; i++) buf[i] *= g;
  return g;
}
