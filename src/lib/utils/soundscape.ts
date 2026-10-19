/**
 * The sound of the storm clip: wind, rain and thunder, synthesised from the data with Web Audio.
 * It is NOT a recording and the interface says so ({@link AUDIO_LABEL}).
 *
 * The same code builds the audio graph for a live AudioContext (playback in a viewer) and for an
 * OfflineAudioContext (the clip: rendered faster than real time, then muxed into the MP4 by the
 * render script). Everything is procedural: filtered noise, no samples.
 *
 *  - Wind: noise whose loudness and brightness follow the wind speed at the listener (the retrieved
 *    wind at the observer when there is one, otherwise a labelled proxy), with gusts.
 *  - Rain: bright noise whose level follows the reflectivity at the listener.
 *  - Thunder: every flash starts thunder that reaches the listener after distance / 343 m/s (divided
 *    by `compress` in a time-lapse clip, see {@link thunderDelay}). Each part of the channel arrives
 *    at its own time, which gives the rolling: the envelope is built from the real channel geometry
 *    (distance of every segment to the listener). Loudness falls with distance, the low-pass cutoff
 *    falls with distance (far thunder is a low rumble), a close strike adds a sharp crack. The
 *    rumble is split in three panned parts (start, middle and end of the channel) through HRTF
 *    PannerNodes; the listener (position and heading) follows the camera, so panning and distance
 *    change as the camera moves.
 *
 * The pure helpers at the top have unit tests (soundscape.test.mjs).
 */
import { SPEED_OF_SOUND_MS, thunderDelay, mulberry32, type Flash } from './lightning.ts';

export const AUDIO_LABEL = 'Synthesised from the data, not a recording';

// ─── Pure helpers ───────────────────────────────────────────────────────────

/** Loudness of the wind bed for a wind speed (m/s): rises steadily with the speed. */
export function windGain(speedMs: number): number {
  const v = Math.max(0, speedMs);
  return 0.22 * Math.min(1.3, Math.pow(v / 25, 1.5));
}

/** Low-pass cutoff (Hz) of the wind bed: a stronger wind is brighter. */
export function windCutoffHz(speedMs: number): number {
  return 160 + 38 * Math.max(0, Math.min(60, speedMs));
}

/** Loudness of rain for a reflectivity-derived level 0..1. */
export function rainGain(level: number): number {
  return 0.3 * Math.pow(Math.max(0, Math.min(1, level)), 1.4);
}

/** Loudness of thunder from a flash `distKm` away (1 = a very close, strong flash). */
export function thunderGain(distKm: number, energy = 1): number {
  return Math.min(1, (0.35 + 0.65 * energy) * (1.6 / (1.6 + Math.max(0, distKm))) * 2.2);
}

/** Low-pass cutoff (Hz) of thunder: close strikes are bright, distant thunder is a low rumble. */
export function thunderCutoffHz(distKm: number): number {
  return Math.max(70, Math.min(4000, 3200 / (1 + Math.max(0, distKm) / 3.5)));
}

/** Time constant (s) of the fading tail of thunder: longer for distant storms (more rolling). */
export function thunderTail(distKm: number): number {
  return 0.6 + 0.05 * Math.max(0, distKm);
}

export interface ThunderPart {
  /** Envelope samples at `rate` Hz, starting at `t0` (clip time, s). */
  env: Float32Array;
  t0: number;
  /** Where this part comes from (km, scene frame, true heights). */
  pos: [number, number, number];
  /** Share of the thunder's loudness. */
  weight: number;
}

export interface Thunder {
  flashId: number;
  /** Clip time the first sound reaches the listener, and the distance of the nearest part of the channel (km). */
  arrival: number;
  distKm: number;
  gain: number;
  cutoffHz: number;
  parts: ThunderPart[];
  rate: number;
  /** Position of the nearest channel point (for the crack). */
  nearest: [number, number, number];
}

/**
 * Thunder of a flash for a listener at `listener` (km): arrival times of every channel segment
 * (distance / speed of sound / compress), an envelope per third of the channel in arrival order.
 */
export function thunderOf(f: Flash, listener: [number, number, number], compress = 1, rate = 200, parts = 3): Thunder {
  const n = f.segments.length / 6;
  const segs: { tau: number; a: number; mid: [number, number, number]; r: number }[] = [];
  let dMin = Infinity;
  let nearest: [number, number, number] = f.origin;
  for (let i = 0; i < n; i++) {
    const s = f.segments;
    const mid: [number, number, number] = [(s[i * 6] + s[i * 6 + 3]) / 2, (s[i * 6 + 1] + s[i * 6 + 4]) / 2, (s[i * 6 + 2] + s[i * 6 + 5]) / 2];
    const len = Math.hypot(s[i * 6 + 3] - s[i * 6], s[i * 6 + 4] - s[i * 6 + 1], s[i * 6 + 5] - s[i * 6 + 2]);
    const r = Math.hypot(mid[0] - listener[0], mid[1] - listener[1], mid[2] - listener[2]);
    const lw = f.level[i] === 0 ? 1 : f.level[i] === 1 ? 0.5 : 0.3;
    segs.push({ tau: f.t + thunderDelay(r, compress), a: (len * lw) / (r + 0.6), mid, r });
    if (r < dMin) { dMin = r; nearest = mid; }
  }
  segs.sort((p, q) => p.tau - q.tau);
  const tail = thunderTail(dMin) / Math.sqrt(Math.max(1, compress));
  const alpha = Math.exp(-1 / (rate * tail));
  const out: ThunderPart[] = [];
  const first = segs.length ? segs[0].tau : f.t;
  const last = segs.length ? segs[segs.length - 1].tau : f.t;
  const nSamp = Math.ceil((last - first + 4 * tail + 0.3) * rate);
  const per = Math.max(1, Math.ceil(segs.length / parts));
  let norm = 0;
  for (let g = 0; g < parts; g++) {
    const grp = segs.slice(g * per, (g + 1) * per);
    if (!grp.length) continue;
    const e = new Float32Array(nSamp);
    let cx = 0, cy = 0, cz = 0, ws = 0;
    for (const sg of grp) {
      const sigma = 0.04 + 0.01 * Math.sqrt(sg.r);
      const c = Math.round((sg.tau - first) * rate);
      const half = Math.ceil(sigma * rate * 2);
      for (let k = -half; k <= half; k++) {
        const idx = c + k;
        if (idx < 0 || idx >= nSamp) continue;
        e[idx] += sg.a * Math.exp(-0.5 * (k / (sigma * rate)) ** 2);
      }
      cx += sg.mid[0] * sg.a; cy += sg.mid[1] * sg.a; cz += sg.mid[2] * sg.a; ws += sg.a;
    }
    // the tail: each arrival keeps ringing and fades (a one-pole filter of the arrivals)
    let y = 0;
    for (let k = 0; k < nSamp; k++) {
      y = e[k] + y * alpha;
      e[k] = y;
    }
    for (let k = 0; k < nSamp; k++) norm = Math.max(norm, e[k]);
    out.push({ env: e, t0: first, pos: [cx / ws, cy / ws, cz / ws], weight: ws });
  }
  // the loudest part of the whole thunder is 1: the loudness goes in `gain`
  if (norm > 0) for (const p of out) for (let k = 0; k < p.env.length; k++) p.env[k] /= norm;
  return {
    flashId: f.id, arrival: first, distKm: dMin, gain: thunderGain(dMin, f.energy), cutoffHz: thunderCutoffHz(dMin),
    parts: out, rate, nearest,
  };
}

/** Smooth gust factor (around 1) for the wind: band-limited noise with the turbulence intensity `ti`. */
export function gustSeries(seed: number, n: number, dt: number, ti = 0.22): Float32Array {
  const rng = mulberry32(seed);
  const out = new Float32Array(n);
  // sum of a few slow sinusoids with random phases (periods 2 to 14 s)
  const comps: { w: number; ph: number; a: number }[] = [];
  for (let i = 0; i < 6; i++) comps.push({ w: (2 * Math.PI) / (2 + 12 * rng()), ph: rng() * Math.PI * 2, a: 0.5 + rng() });
  const tot = comps.reduce((s, c) => s + c.a * c.a, 0);
  for (let i = 0; i < n; i++) {
    let v = 0;
    for (const c of comps) v += c.a * Math.sin(c.w * i * dt + c.ph);
    out[i] = Math.max(0.2, 1 + (ti * v) / Math.sqrt(tot / 2));
  }
  return out;
}

/** Linear interpolation of a series sampled every `dt` seconds. */
export function sampleSeries(a: ArrayLike<number>, dt: number, t: number): number {
  if (!a.length) return 0;
  const x = Math.max(0, t / dt);
  const i = Math.min(a.length - 1, Math.floor(x));
  const j = Math.min(a.length - 1, i + 1);
  const f = Math.min(1, x - i);
  return a[i] * (1 - f) + a[j] * f;
}

// ─── The audio graph ────────────────────────────────────────────────────────

export interface ListenerTrack {
  /** Seconds between samples. */
  dt: number;
  /** km (scene frame, true heights) and degrees. */
  x: ArrayLike<number>;
  y: ArrayLike<number>;
  z: ArrayLike<number>;
  yawDeg: ArrayLike<number>;
  pitchDeg: ArrayLike<number>;
}

export interface SoundInput {
  duration: number;
  listener: ListenerTrack;
  /** Wind speed (m/s) and rain level (0..1) at the listener, sampled every `dtSeries` s. */
  windMs: ArrayLike<number>;
  rain: ArrayLike<number>;
  dtSeries: number;
  flashes: Flash[];
  /** Time-lapse compression of the light-to-sound delay (1 = real physics). */
  compress: number;
  seed?: number;
  /** Overall level of each part, 0..1 (1 = default). */
  levels?: { wind?: number; rain?: number; thunder?: number };
}

function noiseBuffer(ctx: BaseAudioContext, seconds: number, seed: number, pinkish = true): AudioBuffer {
  const sr = ctx.sampleRate;
  const buf = ctx.createBuffer(2, Math.floor(seconds * sr), sr);
  for (let ch = 0; ch < 2; ch++) {
    const rng = mulberry32(seed * 31 + ch * 977 + 5);
    const d = buf.getChannelData(ch);
    let b0 = 0, b1 = 0, b2 = 0;
    for (let i = 0; i < d.length; i++) {
      const w = rng() * 2 - 1;
      if (pinkish) {
        // a cheap pink approximation (three one-pole sections)
        b0 = 0.997 * b0 + w * 0.0555;
        b1 = 0.985 * b1 + w * 0.0750;
        b2 = 0.950 * b2 + w * 0.1538;
        d[i] = (b0 + b1 + b2 + w * 0.5) * 0.55;
      } else d[i] = w;
    }
  }
  return buf;
}

/** Schedule the whole soundscape on `ctx` (live or offline). Nothing here depends on real time. */
export function buildSoundscape(ctx: BaseAudioContext, input: SoundInput): { thunders: Thunder[] } {
  const lv = { wind: 1, rain: 1, thunder: 1, ...(input.levels ?? {}) };
  const seed = input.seed ?? 1;
  const out = ctx.createDynamicsCompressor();
  out.threshold.value = -9;
  out.knee.value = 8;
  out.ratio.value = 10;
  out.attack.value = 0.004;
  out.release.value = 0.25;
  const master = ctx.createGain();
  master.gain.value = 0.85;
  out.connect(master);
  master.connect(ctx.destination);

  // listener: position and heading follow the camera. Audio axes: x east, y up, z = -north (so the
  // default forward, -z, is north); metres.
  const L = ctx.listener;
  const tr = input.listener;
  const nL = tr.x.length;
  for (let i = 0; i < nL; i++) {
    const t = i * tr.dt;
    const yaw = (tr.yawDeg[i] * Math.PI) / 180, pit = (tr.pitchDeg[i] * Math.PI) / 180;
    L.positionX.setValueAtTime(tr.x[i] * 1000, t);
    L.positionY.setValueAtTime(tr.z[i] * 1000, t);
    L.positionZ.setValueAtTime(-tr.y[i] * 1000, t);
    L.forwardX.setValueAtTime(Math.sin(yaw) * Math.cos(pit), t);
    L.forwardY.setValueAtTime(Math.sin(pit), t);
    L.forwardZ.setValueAtTime(-Math.cos(yaw) * Math.cos(pit), t);
    L.upX.setValueAtTime(0, t);
    L.upY.setValueAtTime(1, t);
    L.upZ.setValueAtTime(0, t);
  }

  const nSer = Math.min(input.windMs.length, Math.floor(input.duration / input.dtSeries) + 1);
  const gust = gustSeries(seed, nSer, input.dtSeries);

  // wind: a low rumble + a brighter band; both follow the speed, with gusts
  const noise = noiseBuffer(ctx, 8, seed);
  const wind = ctx.createBufferSource();
  wind.buffer = noise;
  wind.loop = true;
  const wLow = ctx.createBiquadFilter();
  wLow.type = 'lowpass';
  wLow.Q.value = 0.6;
  const wGain = ctx.createGain();
  wGain.gain.value = 0;
  wind.connect(wLow);
  wLow.connect(wGain);
  wGain.connect(out);
  const wind2 = ctx.createBufferSource();
  wind2.buffer = noiseBuffer(ctx, 7, seed + 11);
  wind2.loop = true;
  const wBand = ctx.createBiquadFilter();
  wBand.type = 'bandpass';
  wBand.Q.value = 0.9;
  const wGain2 = ctx.createGain();
  wGain2.gain.value = 0;
  wind2.connect(wBand);
  wBand.connect(wGain2);
  wGain2.connect(out);
  for (let i = 0; i < nSer; i++) {
    const t = i * input.dtSeries;
    const sp = input.windMs[i] * gust[i];
    wLow.frequency.setValueAtTime(windCutoffHz(sp), t);
    wGain.gain.setValueAtTime(windGain(sp) * lv.wind * 0.7, t);
    wBand.frequency.setValueAtTime(350 + 18 * Math.min(60, sp), t);
    wGain2.gain.setValueAtTime(windGain(sp) * windGain(sp) * 1.6 * lv.wind, t);
  }
  wind.start(0);
  wind2.start(0);

  // rain
  const rain = ctx.createBufferSource();
  rain.buffer = noiseBuffer(ctx, 5, seed + 23, false);
  rain.loop = true;
  const rHp = ctx.createBiquadFilter();
  rHp.type = 'highpass';
  rHp.frequency.value = 1800;
  const rLp = ctx.createBiquadFilter();
  rLp.type = 'lowpass';
  rLp.frequency.value = 9000;
  const rGain = ctx.createGain();
  rGain.gain.value = 0;
  rain.connect(rHp);
  rHp.connect(rLp);
  rLp.connect(rGain);
  rGain.connect(out);
  const nRain = Math.min(input.rain.length, nSer);
  for (let i = 0; i < nRain; i++) rGain.gain.setValueAtTime(rainGain(input.rain[i]) * lv.rain, i * input.dtSeries);
  rain.start(0);

  // thunder
  const thunders: Thunder[] = [];
  const thunderNoise = noiseBuffer(ctx, 12, seed + 101);
  const clickNoise = noiseBuffer(ctx, 1, seed + 211, false);
  for (const f of input.flashes) {
    const li = Math.min(nL - 1, Math.max(0, Math.round(f.t / tr.dt)));
    const listener: [number, number, number] = [tr.x[li], tr.y[li], tr.z[li]];
    const th = thunderOf(f, listener, input.compress);
    thunders.push(th);
    if (th.arrival >= input.duration) continue;
    const rng = mulberry32(seed + f.id * 13 + 3);
    for (const part of th.parts) {
      const src = ctx.createBufferSource();
      src.buffer = thunderNoise;
      src.loop = true;
      const start = Math.max(0, part.t0);
      const off = rng() * 8;
      const lp = ctx.createBiquadFilter();
      lp.type = 'lowpass';
      lp.frequency.value = th.cutoffHz;
      lp.Q.value = 0.7;
      const shelf = ctx.createBiquadFilter();
      shelf.type = 'lowshelf';
      shelf.frequency.value = 140;
      shelf.gain.value = th.distKm > 4 ? 7 : 3;
      const g = ctx.createGain();
      g.gain.value = 0;
      // the envelope at its own rate, as a value curve
      const dur = part.env.length / th.rate;
      const peak = th.gain * lv.thunder * (0.45 + 0.55 * part.weight / Math.max(1e-9, th.parts.reduce((m, p) => Math.max(m, p.weight), 0)));
      const curve = new Float32Array(part.env.length);
      for (let k = 0; k < curve.length; k++) curve[k] = part.env[k] * peak;
      if (part.t0 >= 0) g.gain.setValueCurveAtTime(curve, start, dur);
      const pan = ctx.createPanner();
      pan.panningModel = 'HRTF';
      pan.distanceModel = 'linear';
      pan.rolloffFactor = 0; // our own gain carries the distance
      pan.positionX.value = part.pos[0] * 1000;
      pan.positionY.value = part.pos[2] * 1000;
      pan.positionZ.value = -part.pos[1] * 1000;
      src.connect(lp);
      lp.connect(shelf);
      shelf.connect(g);
      g.connect(pan);
      pan.connect(out);
      src.start(start, off, dur + 0.1);
    }
    // a close strike: a sharp crack at the first arrival
    if (th.distKm < 4) {
      const ck = ctx.createBufferSource();
      ck.buffer = clickNoise;
      const hp = ctx.createBiquadFilter();
      hp.type = 'highpass';
      hp.frequency.value = 900;
      const cg = ctx.createGain();
      const lvl = 0.9 * (1 - th.distKm / 4) * (0.5 + 0.5 * f.energy) * lv.thunder;
      cg.gain.setValueAtTime(0, Math.max(0, th.arrival - 0.001));
      cg.gain.linearRampToValueAtTime(lvl, th.arrival + 0.002);
      cg.gain.exponentialRampToValueAtTime(0.001, th.arrival + 0.12);
      const pan = ctx.createPanner();
      pan.panningModel = 'HRTF';
      pan.distanceModel = 'linear';
      pan.rolloffFactor = 0;
      pan.positionX.value = th.nearest[0] * 1000;
      pan.positionY.value = th.nearest[2] * 1000;
      pan.positionZ.value = -th.nearest[1] * 1000;
      ck.connect(hp);
      hp.connect(cg);
      cg.connect(pan);
      pan.connect(out);
      ck.start(Math.max(0, th.arrival - 0.001), rng() * 0.5, 0.2);
    }
  }
  return { thunders };
}

/** Render the soundscape offline: stereo Float32 channels at `sampleRate`. */
export async function renderSoundscape(input: SoundInput, sampleRate = 48000): Promise<{ left: Float32Array; right: Float32Array; thunders: Thunder[]; peak: number; sampleRate: number }> {
  const ctx = new OfflineAudioContext(2, Math.ceil(input.duration * sampleRate), sampleRate);
  const { thunders } = buildSoundscape(ctx, input);
  const buf = await ctx.startRendering();
  const left = buf.getChannelData(0).slice();
  const right = buf.getChannelData(1).slice();
  let peak = 0;
  for (let i = 0; i < left.length; i++) peak = Math.max(peak, Math.abs(left[i]), Math.abs(right[i]));
  // keep a little headroom: scale a hot render down (never up)
  if (peak > 0.95) {
    const k = 0.95 / peak;
    for (let i = 0; i < left.length; i++) { left[i] *= k; right[i] *= k; }
    peak = 0.95;
  }
  return { left, right, thunders, peak, sampleRate };
}

export { SPEED_OF_SOUND_MS };
