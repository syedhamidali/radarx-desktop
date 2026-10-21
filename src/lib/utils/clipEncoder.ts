/**
 * The film is encoded inside the browser that draws it.
 *
 * `rxd animate` (and the Movie Studio) draw every frame of a storm clip in a headless Chrome
 * (StormClipView, `window.__xrClip`). Instead of taking a PNG screenshot of each frame and encoding
 * them afterwards with a video library in the engine, the page encodes each frame as soon as it is
 * drawn with the browser's own WebCodecs encoders and muxes the result in memory:
 *
 *  - video: H.264 (`avc1.*`, High profile, level from the size and rate) in an MP4, or, where the
 *    browser has no H.264 encoder, VP9 in a WebM. The engine falls back to PNG frames when neither
 *    exists. The codecs are the browser's (Chrome ships them under its vendor's licences); nothing
 *    is downloaded or linked into the engine.
 *  - sound: AAC-LC (`mp4a.40.2`, 48 kHz stereo) in the MP4, Opus in the WebM, from the PCM that
 *    `renderAudio` synthesised in the same page, cut or padded to exactly the length of the video.
 *  - the container is written by mp4-muxer / webm-muxer (MIT, vendored in src/lib/vendor).
 *
 * Timing: frame i has the timestamp i / fps (no real-time clock anywhere); a key frame every
 * `keyframeS` seconds (2 s). Quality: the `--crf` of the command is turned into a target bit rate
 * (`bitrateFor`), because WebCodecs has no constant-quality mode that every browser honours.
 *
 * The finished file sits in the page's memory once (the muxer's buffer); the engine pulls it in
 * chunks (`read`) and writes it to disk. The exported pure functions have unit tests.
 */

import { Muxer as Mp4Muxer, ArrayBufferTarget as Mp4Target } from '../vendor/mp4-muxer/mp4-muxer.mjs';
import { Muxer as WebmMuxer, ArrayBufferTarget as WebmTarget } from '../vendor/webm-muxer/webm-muxer.mjs';

/** What the browser can encode (`ClipEncoder.support()`); every flag false when WebCodecs is absent. */
export interface EncoderSupport {
  webcodecs: boolean;
  /** H.264 High profile (`avc1.64xxxx`) at 1080p30. */
  avc: boolean;
  vp9: boolean;
  /** AAC-LC 48 kHz stereo. */
  aac: boolean;
  opus: boolean;
  /** Chrome's answer about a hardware H.264 encoder (`prefer-hardware` accepted), informational. */
  avcHardware: boolean;
  /** Why a codec is missing, when the browser said (one line per codec that failed). */
  notes: string[];
}

export type Container = 'mp4' | 'webm';

/** The codecs a film is written with, chosen from what the browser supports (`pickCodecs`). */
export interface CodecChoice {
  container: Container;
  /** File extension the engine should use. */
  ext: 'mp4' | 'webm';
  video: 'avc' | 'vp9';
  /** null: the browser can encode the picture but not the sound; the film is silent (reported). */
  audio: 'aac' | 'opus' | null;
}

/** H.264 first (MP4), then VP9 (WebM); null when the browser cannot encode video at all. */
export function pickCodecs(s: EncoderSupport): CodecChoice | null {
  if (!s.webcodecs) return null;
  if (s.avc) return { container: 'mp4', ext: 'mp4', video: 'avc', audio: s.aac ? 'aac' : s.opus ? 'opus' : null };
  if (s.vp9) return { container: 'webm', ext: 'webm', video: 'vp9', audio: s.opus ? 'opus' : null };
  return null;
}

/**
 * Target bit rate (bit/s) for a `--crf` style quality: bits per pixel per frame of 0.12 at CRF 23,
 * doubling every 6 CRF steps down (the x264 convention), so CRF 18 is about 0.21 bpp: 1080p30 ≈ 13 Mbit/s,
 * 720p30 ≈ 5.9 Mbit/s. Clamped to 250 kbit/s .. 80 Mbit/s.
 */
export function bitrateFor(width: number, height: number, fps: number, crf: number): number {
  const bpp = 0.12 * Math.pow(2, (23 - crf) / 6);
  const raw = width * height * fps * bpp;
  return Math.round(Math.min(80e6, Math.max(250e3, raw)));
}

/** H.264 levels: (level_idc, max macroblocks per second, max frame size in macroblocks), table A-1. */
const AVC_LEVELS: [number, number, number][] = [
  [30, 40500, 1620], [31, 108000, 3600], [32, 216000, 5120], [40, 245760, 8192], [41, 245760, 8192],
  [42, 522240, 8704], [50, 589824, 22080], [51, 983040, 36864], [52, 2073600, 36864], [60, 4177920, 139264],
  [61, 8355840, 139264], [62, 16711680, 139264],
];

/** The lowest H.264 level that holds this size and rate, as `level_idc` (e.g. 40 for 1080p30). */
export function avcLevelIdc(width: number, height: number, fps: number): number {
  const mbs = Math.ceil(width / 16) * Math.ceil(height / 16);
  const mbps = mbs * fps;
  for (const [idc, maxMbps, maxFs] of AVC_LEVELS) if (mbps <= maxMbps && mbs <= maxFs) return idc;
  return 62;
}

/** `avc1.6400xx`: High profile, constraint flags 0, the level from `avcLevelIdc` in hex. */
export function avcCodecString(width: number, height: number, fps: number): string {
  return `avc1.6400${avcLevelIdc(width, height, fps).toString(16).padStart(2, '0')}`;
}

/** VP9 profile 0, level from the size (1080p30: 4.0 = "40"), 8 bit: `vp09.00.40.08`. */
export function vp9CodecString(width: number, height: number, fps: number): string {
  const px = width * height * fps;
  const level = px <= 829440 * 30 ? '21' : px <= 2073600 * 30 ? '40' : px <= 2073600 * 60 ? '41' : px <= 8912896 * 30 ? '50' : px <= 8912896 * 60 ? '51' : '60';
  return `vp09.00.${level}.08`;
}

/** Timestamp (µs) of frame `i` at `fps`: exact multiples, rounded once (never accumulated). */
export function frameTimestampUs(i: number, fps: number): number {
  return Math.round((i * 1e6) / fps);
}

/** Frames between key frames (every `seconds`, at least 1). */
export function keyFrameInterval(fps: number, seconds = 2): number {
  return Math.max(1, Math.round(fps * seconds));
}

/**
 * The interleaved 16-bit stereo PCM cut or padded with silence to exactly the length of `nFrames`
 * frames at `fps` (the same rule as the engine's PyAV path): `round(nFrames / fps * sampleRate)` sample frames.
 */
export function fitPcm(pcm: Int16Array, sampleRate: number, nFrames: number, fps: number): Int16Array {
  const want = Math.round((nFrames / fps) * sampleRate) * 2;
  if (pcm.length === want) return pcm;
  if (pcm.length > want) return pcm.subarray(0, want);
  const out = new Int16Array(want);
  out.set(pcm);
  return out;
}

/** Interleaved 16-bit stereo -> planar float32 (all left samples, then all right), -1..1. */
export function pcmToPlanarF32(pcm: Int16Array): Float32Array<ArrayBuffer> {
  const n = pcm.length >> 1;
  const out = new Float32Array(new ArrayBuffer(n * 2 * 4));
  for (let i = 0; i < n; i++) {
    out[i] = pcm[i * 2] / 32768;
    out[n + i] = pcm[i * 2 + 1] / 32768;
  }
  return out;
}

/** Base64 of a byte range (what `page.evaluate` can carry; 32 KB pieces keep the call stack small). */
export function toBase64(bytes: Uint8Array): string {
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

export interface EncoderOptions {
  width: number;
  height: number;
  fps: number;
  /** x264-style quality (18 default of the command), mapped to a bit rate by `bitrateFor`. */
  crf: number;
  keyframeS?: number;
  /** Add a sound track (AAC in MP4, Opus in WebM) at this rate; the PCM comes later via `addAudio`. */
  sound?: { sampleRate: number } | null;
  choice: CodecChoice;
}

/** What `finish()` reports (also the record of the film). */
export interface EncoderResult {
  bytes: number;
  frames: number;
  keyFrames: number;
  container: Container;
  ext: 'mp4' | 'webm';
  videoCodec: string;
  audioCodec: string | null;
  audioFrames: number;
  bitrate: number;
  hardware: string | null;
}

const AUDIO_BITRATE = 160_000;

/** Encodes frames as they are drawn and muxes them; one instance per film. */
export class ClipEncoder {
  private readonly opts: EncoderOptions;
  private readonly videoCodec: string;
  private readonly audioCodec: string | null;
  private readonly bitrate: number;
  private readonly every: number;
  private readonly mp4: { muxer: Mp4Muxer<Mp4Target>; target: Mp4Target } | null = null;
  private readonly webm: { muxer: WebmMuxer<WebmTarget>; target: WebmTarget } | null = null;
  private venc: VideoEncoder | null = null;
  private aenc: AudioEncoder | null = null;
  private frames = 0;
  private keyFrames = 0;
  private audioFrames = 0;
  private hardware: string | null = null;
  private failure: Error | null = null;
  private bytes: Uint8Array | null = null;

  /** Ask the browser once what it can encode (real `isConfigSupported` calls, no encode). */
  static async support(): Promise<EncoderSupport> {
    const out: EncoderSupport = { webcodecs: false, avc: false, vp9: false, aac: false, opus: false, avcHardware: false, notes: [] };
    if (typeof VideoEncoder === 'undefined' || typeof AudioEncoder === 'undefined') {
      out.notes.push('WebCodecs is not available in this page (needs a secure context: http://127.0.0.1 or https)');
      return out;
    }
    out.webcodecs = true;
    const W = 1920, H = 1080, fps = 30;
    const tryVideo = async (key: 'avc' | 'vp9', cfg: VideoEncoderConfig) => {
      try {
        const r = await VideoEncoder.isConfigSupported(cfg);
        out[key] = !!r.supported;
        if (!r.supported) out.notes.push(`${cfg.codec}: not supported by this browser`);
      } catch (e) {
        out.notes.push(`${cfg.codec}: ${String(e)}`);
      }
    };
    await tryVideo('avc', { codec: avcCodecString(W, H, fps), width: W, height: H, bitrate: bitrateFor(W, H, fps, 18), framerate: fps, avc: { format: 'avc' } });
    await tryVideo('vp9', { codec: vp9CodecString(W, H, fps), width: W, height: H, bitrate: bitrateFor(W, H, fps, 18), framerate: fps });
    if (out.avc) {
      try {
        const r = await VideoEncoder.isConfigSupported({ codec: avcCodecString(W, H, fps), width: W, height: H, bitrate: 8e6, framerate: fps, hardwareAcceleration: 'prefer-hardware', avc: { format: 'avc' } });
        out.avcHardware = !!r.supported;
      } catch { /* informational only */ }
    }
    const tryAudio = async (key: 'aac' | 'opus', codec: string) => {
      try {
        const r = await AudioEncoder.isConfigSupported({ codec, sampleRate: 48000, numberOfChannels: 2, bitrate: AUDIO_BITRATE });
        out[key] = !!r.supported;
        if (!r.supported) out.notes.push(`${codec}: not supported by this browser`);
      } catch (e) {
        out.notes.push(`${codec}: ${String(e)}`);
      }
    };
    await tryAudio('aac', 'mp4a.40.2');
    await tryAudio('opus', 'opus');
    return out;
  }

  constructor(opts: EncoderOptions) {
    this.opts = opts;
    const { width, height, fps, choice } = opts;
    this.bitrate = bitrateFor(width, height, fps, opts.crf);
    this.every = keyFrameInterval(fps, opts.keyframeS ?? 2);
    this.videoCodec = choice.video === 'avc' ? avcCodecString(width, height, fps) : vp9CodecString(width, height, fps);
    const sound = opts.sound ? choice.audio : null;
    this.audioCodec = sound === 'aac' ? 'mp4a.40.2' : sound === 'opus' ? 'opus' : null;
    const sampleRate = opts.sound?.sampleRate ?? 48000;
    if (choice.container === 'mp4') {
      const target = new Mp4Target();
      const muxer = new Mp4Muxer({
        target,
        video: { codec: choice.video === 'avc' ? 'avc' : 'vp9', width, height, frameRate: fps },
        audio: sound ? { codec: sound, numberOfChannels: 2, sampleRate } : undefined,
        fastStart: 'in-memory',
        firstTimestampBehavior: 'offset',
      });
      this.mp4 = { muxer, target };
    } else {
      const target = new WebmTarget();
      const muxer = new WebmMuxer({
        target,
        video: { codec: 'V_VP9', width, height, frameRate: fps },
        audio: sound ? { codec: 'A_OPUS', numberOfChannels: 2, sampleRate } : undefined,
        type: 'webm',
        firstTimestampBehavior: 'offset',
      });
      this.webm = { muxer, target };
    }
    const onError = (e: DOMException) => { this.failure = this.failure ?? new Error(`encoder: ${e.message}`); };
    this.venc = new VideoEncoder({
      output: (chunk, meta) => {
        const hw = meta?.decoderConfig?.hardwareAcceleration;
        if (hw && hw !== 'no-preference' && !this.hardware) this.hardware = String(hw);
        if (chunk.type === 'key') this.keyFrames++;
        if (this.mp4) this.mp4.muxer.addVideoChunk(chunk, meta);
        else this.webm!.muxer.addVideoChunk(chunk, meta);
      },
      error: onError,
    });
    const vcfg: VideoEncoderConfig = {
      codec: this.videoCodec, width, height, bitrate: this.bitrate, framerate: fps, bitrateMode: 'variable', latencyMode: 'quality',
    };
    if (choice.video === 'avc') vcfg.avc = { format: 'avc' };
    this.venc.configure(vcfg);
    if (this.audioCodec) {
      this.aenc = new AudioEncoder({
        output: (chunk, meta) => {
          if (this.mp4) this.mp4.muxer.addAudioChunk(chunk, meta);
          else this.webm!.muxer.addAudioChunk(chunk, meta);
        },
        error: onError,
      });
      this.aenc.configure({ codec: this.audioCodec, sampleRate, numberOfChannels: 2, bitrate: AUDIO_BITRATE });
    }
  }

  /** The settings in force (for the engine's record). */
  describe(): { videoCodec: string; audioCodec: string | null; bitrate: number; keyFrameInterval: number; container: Container; ext: 'mp4' | 'webm' } {
    return { videoCodec: this.videoCodec, audioCodec: this.audioCodec, bitrate: this.bitrate, keyFrameInterval: this.every, container: this.opts.choice.container, ext: this.opts.choice.ext };
  }

  private check(): void {
    if (this.failure) throw this.failure;
  }

  /** Encode the picture of frame `i` (a canvas or bitmap of the film's size); frames arrive in order. */
  async addFrame(source: CanvasImageSource, i: number): Promise<number> {
    this.check();
    const enc = this.venc;
    if (!enc || enc.state !== 'configured') throw new Error('the video encoder is not open');
    if (i !== this.frames) throw new Error(`frames must arrive in order: expected ${this.frames}, got ${i}`);
    const fps = this.opts.fps;
    const frame = new VideoFrame(source, { timestamp: frameTimestampUs(i, fps), duration: frameTimestampUs(i + 1, fps) - frameTimestampUs(i, fps) });
    try {
      enc.encode(frame, { keyFrame: i % this.every === 0 });
    } finally {
      frame.close();
    }
    this.frames++;
    // keep the queue short: the page waits for the encoder rather than piling frames up in memory
    while (enc.encodeQueueSize > 8 && !this.failure) await new Promise((r) => setTimeout(r, 2));
    this.check();
    return this.frames;
  }

  /** The sound track: interleaved 16-bit stereo PCM, fitted to `nFrames` frames (the video length). */
  async addAudio(pcm: Int16Array, sampleRate: number, nFrames: number): Promise<number> {
    this.check();
    const enc = this.aenc;
    if (!enc) throw new Error('this film has no sound track');
    if (sampleRate !== (this.opts.sound?.sampleRate ?? 48000)) throw new Error(`the sound is ${sampleRate} Hz, the encoder expects ${this.opts.sound?.sampleRate}`);
    const fitted = fitPcm(pcm, sampleRate, nFrames, this.opts.fps);
    const total = fitted.length >> 1;
    const block = sampleRate; // one second per AudioData
    for (let off = 0; off < total; off += block) {
      const n = Math.min(block, total - off);
      const data = new AudioData({
        format: 'f32-planar', sampleRate, numberOfFrames: n, numberOfChannels: 2,
        timestamp: Math.round((off * 1e6) / sampleRate),
        data: pcmToPlanarF32(fitted.subarray(off * 2, (off + n) * 2)),
      });
      try {
        enc.encode(data);
      } finally {
        data.close();
      }
    }
    this.audioFrames = total;
    await enc.flush();
    this.check();
    return total;
  }

  /** Flush the encoders, write the container; afterwards `read` gives the bytes. */
  async finish(): Promise<EncoderResult> {
    this.check();
    if (!this.venc) throw new Error('the encoder is closed');
    if (this.frames === 0) throw new Error('no frames were encoded');
    await this.venc.flush();
    this.venc.close();
    this.venc = null;
    if (this.aenc) {
      await this.aenc.flush();
      this.aenc.close();
      this.aenc = null;
    }
    this.check();
    let buffer: ArrayBuffer;
    if (this.mp4) {
      this.mp4.muxer.finalize();
      buffer = this.mp4.target.buffer;
    } else {
      this.webm!.muxer.finalize();
      buffer = this.webm!.target.buffer;
    }
    this.bytes = new Uint8Array(buffer);
    const choice = this.opts.choice;
    return {
      bytes: this.bytes.length, frames: this.frames, keyFrames: this.keyFrames, container: choice.container, ext: choice.ext,
      videoCodec: this.videoCodec, audioCodec: this.audioCodec, audioFrames: this.audioFrames, bitrate: this.bitrate, hardware: this.hardware,
    };
  }

  /** `length` bytes from `offset` of the finished file, base64 (the engine pulls the file in chunks). */
  read(offset: number, length: number): string {
    if (!this.bytes) throw new Error('finish() first');
    return toBase64(this.bytes.subarray(offset, Math.min(this.bytes.length, offset + length)));
  }

  size(): number {
    return this.bytes?.length ?? 0;
  }

  dispose(): void {
    try { this.venc?.close(); } catch { /* already closed */ }
    try { this.aenc?.close(); } catch { /* already closed */ }
    this.venc = this.aenc = null;
    this.bytes = null;
  }
}
