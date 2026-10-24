/**
 * Web Audio player of the lightning sound (the part that cannot be unit tested in node; the plan, the
 * mixer maths and the synthesis are in liveSound.ts and are).
 *
 * Graph: every flash is a buffer source (thunder: shaped noise made by `synthThunder`; tick: a click)
 * -> a panner at the position of the flash (HRTF, binaural-style; the distance falls in the buffer level, the
 * panner only does the direction) -> the layer bus (thunder, ticks: a gain each, mute = 0) -> the master gain
 * -> a limiter (a compressor at ratio 20:1 with its threshold at the ceiling) -> the speakers.
 * The listener follows the pose given by `getPose` (the radar, or the 3D camera), updated every frame.
 * Nothing starts until `play` is called from a click (the browser's rule), and `stop` silences everything.
 */
import { DEFAULT_MIXER, layerGain, synthThunder, synthTick, type ListenerPose, type Mixer, type SoundEvent } from './liveSound.ts';
import { dbToGain } from './soundscape.ts';

/** At most this many flashes are voiced in one replay (the loudest by thunder level); the rest are counted and said. */
export const MAX_VOICES = 150;
const SYNTH_RATE = 16000; // thunder is low-passed below 4 kHz; the browser resamples

export interface PlayResult {
  voiced: number;
  skipped: number;
  /** Seconds from now until the last sound has ended. */
  durationS: number;
  firstThunderS: number | null;
}

export class LightningSound {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private limiter: DynamicsCompressorNode | null = null;
  private bus: Record<'thunder' | 'ticks', GainNode> | null = null;
  private sources: AudioScheduledSourceNode[] = [];
  private raf = 0;
  private endTimer: ReturnType<typeof setTimeout> | null = null;
  private cache = new Map<string, AudioBuffer>();
  private getPose: () => ListenerPose = () => ({ x: 0, y: 0, yawDeg: 0 });
  playing = false;

  /** True when this browser can play it at all. */
  static supported(): boolean {
    return typeof window !== 'undefined' && (typeof AudioContext !== 'undefined' || typeof (window as any).webkitAudioContext !== 'undefined');
  }

  private ensure(): AudioContext {
    if (this.ctx) return this.ctx;
    const Ctor: typeof AudioContext = (window as any).AudioContext ?? (window as any).webkitAudioContext;
    const ctx = new Ctor();
    const limiter = ctx.createDynamicsCompressor();
    limiter.knee.value = 0;
    limiter.ratio.value = 20;
    limiter.attack.value = 0.002;
    limiter.release.value = 0.1;
    const master = ctx.createGain();
    const thunder = ctx.createGain();
    const ticks = ctx.createGain();
    thunder.connect(master);
    ticks.connect(master);
    master.connect(limiter);
    limiter.connect(ctx.destination);
    this.ctx = ctx;
    this.master = master;
    this.limiter = limiter;
    this.bus = { thunder, ticks };
    return ctx;
  }

  /** Apply the mixer now (works while playing). */
  setMixer(m: Mixer): void {
    if (!this.ctx || !this.master || !this.limiter || !this.bus) return;
    const t = this.ctx.currentTime;
    this.master.gain.setTargetAtTime(dbToGain(m.masterDb), t, 0.03);
    this.limiter.threshold.setTargetAtTime(Math.min(0, m.ceilingDb), t, 0.03);
    this.bus.thunder.gain.setTargetAtTime(layerGain(m, 'thunder'), t, 0.03);
    this.bus.ticks.gain.setTargetAtTime(layerGain(m, 'ticks'), t, 0.03);
  }

  private buffer(kind: 'thunder' | 'tick', key: string, make: () => Float32Array): AudioBuffer {
    const k = `${kind}:${key}`;
    const hit = this.cache.get(k);
    if (hit) return hit;
    const pcm = make();
    const buf = this.ctx!.createBuffer(1, pcm.length, SYNTH_RATE);
    buf.copyToChannel(pcm as Float32Array<ArrayBuffer>, 0);
    if (this.cache.size > 400) this.cache.clear();
    this.cache.set(k, buf);
    return buf;
  }

  private syncListener(): void {
    if (!this.ctx) return;
    const L = this.ctx.listener;
    const p = this.getPose();
    const t = this.ctx.currentTime;
    const yaw = (p.yawDeg * Math.PI) / 180;
    // audio axes: x east, y up, z = -north (metres); the default forward, -z, is north
    L.positionX.setValueAtTime(p.x * 1000, t);
    L.positionY.setValueAtTime(0, t);
    L.positionZ.setValueAtTime(-p.y * 1000, t);
    L.forwardX.setValueAtTime(Math.sin(yaw), t);
    L.forwardY.setValueAtTime(0, t);
    L.forwardZ.setValueAtTime(-Math.cos(yaw), t);
    L.upX.setValueAtTime(0, t);
    L.upY.setValueAtTime(1, t);
    L.upZ.setValueAtTime(0, t);
  }

  /** Play the events (their times counted from now). Call from a click. */
  async play(events: SoundEvent[], opt: { getPose: () => ListenerPose; mixer: Mixer; seed: number; strengths?: (flash: number) => number; heightKm?: (flash: number) => number }): Promise<PlayResult> {
    this.stop();
    const ctx = this.ensure();
    if (ctx.state === 'suspended') await ctx.resume();
    this.getPose = opt.getPose;
    this.setMixer(opt.mixer ?? DEFAULT_MIXER);
    this.syncListener();
    const chosen = events.length > MAX_VOICES ? events.slice().sort((a, b) => b.gain - a.gain).slice(0, MAX_VOICES) : events;
    const start = ctx.currentTime + 0.15;
    let end = 0;
    let first: number | null = null;
    for (const e of chosen) {
      const panner = ctx.createPanner();
      panner.panningModel = 'HRTF';
      panner.distanceModel = 'linear';
      panner.rolloffFactor = 0; // the level carries the distance
      panner.positionX.value = e.x * 1000;
      panner.positionY.value = (opt.heightKm?.(e.flash) ?? 0) * 1000;
      panner.positionZ.value = -e.y * 1000;
      const strength = Math.max(0, Math.min(1, opt.strengths?.(e.flash) ?? 0.5));
      // thunders are shared between flashes of about the same distance (steps of 15 %) and strength (steps of 0.2): 150 voices need a few dozen buffers
      const dq = Math.round(Math.log(e.distKm + 0.5) / Math.log(1.15));
      const sq = Math.round(strength * 5);
      const th = this.buffer('thunder', `${dq}|${sq}`, () =>
        synthThunder({ seed: opt.seed + dq, distKm: Math.max(0, Math.exp(dq * Math.log(1.15)) - 0.5), energy: sq / 5, sampleRate: SYNTH_RATE }),
      );
      const src = ctx.createBufferSource();
      src.buffer = th;
      src.connect(panner);
      panner.connect(this.bus!.thunder);
      src.start(start + e.thunderAt);
      this.sources.push(src);
      end = Math.max(end, e.thunderAt + th.duration);
      if (first === null || e.thunderAt < first) first = e.thunderAt;

      const tick = this.buffer('tick', `${Math.round(strength * 10)}`, () => synthTick({ seed: opt.seed, strength, sampleRate: SYNTH_RATE }));
      const ts = ctx.createBufferSource();
      ts.buffer = tick;
      const tp = ctx.createPanner();
      tp.panningModel = 'HRTF';
      tp.distanceModel = 'linear';
      tp.rolloffFactor = 0;
      tp.positionX.value = e.x * 1000;
      tp.positionY.value = (opt.heightKm?.(e.flash) ?? 0) * 1000;
      tp.positionZ.value = -e.y * 1000;
      ts.connect(tp);
      tp.connect(this.bus!.ticks);
      ts.start(start + e.tickAt);
      this.sources.push(ts);
      end = Math.max(end, e.tickAt + tick.duration);
    }
    this.playing = true;
    const loop = () => {
      if (!this.playing) return;
      this.syncListener();
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
    this.endTimer = setTimeout(() => this.stop(), (end + 0.5) * 1000);
    return { voiced: chosen.length, skipped: events.length - chosen.length, durationS: end, firstThunderS: first };
  }

  stop(): void {
    this.playing = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
    if (this.endTimer) clearTimeout(this.endTimer);
    this.endTimer = null;
    for (const s of this.sources) {
      try {
        s.stop();
        s.disconnect();
      } catch {
        /* already ended */
      }
    }
    this.sources = [];
  }

  dispose(): void {
    this.stop();
    void this.ctx?.close();
    this.ctx = null;
    this.cache.clear();
  }
}
