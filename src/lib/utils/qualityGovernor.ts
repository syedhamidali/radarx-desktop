/**
 * Adaptive quality for the WebGL ray-marching 3D views.
 *
 * Ray marching is the one thing in this app that can make the whole computer unusable: a full
 * resolution frame on a software or integrated GPU can take seconds and starve the GPU process
 * (and with it the compositor, so the page cannot even be repainted or screenshotted).
 *
 * Three cooperating pieces keep that from happening:
 *
 *  - {@link QualityGovernor}: chooses a quality level (render scale, ray step length, max steps,
 *    sampling features, volume-texture budget) from measured frame cost. Drops fast, recovers
 *    slowly with hysteresis and back-off, detects weak renderers at start-up and honours a user
 *    preference (Auto / High / Medium / Low, persisted).
 *  - {@link ProgressiveRenderer}: draws every frame in horizontal bands into an off-screen
 *    framebuffer, one band per task, sized from the measured GPU cost per pixel so that no single
 *    GPU submission is expected to run longer than ~10 ms. Band completion is detected with
 *    non-blocking fences (never gl.finish / readPixels), which also gives a real GPU-time
 *    measurement for the governor. Between bands the main thread is released.
 *  - Time-slicing helpers ({@link yieldToMain}) for the CPU-side packing/uploading of volume data.
 */
import { writable, get, type Readable } from 'svelte/store';
import { perfStats } from '../stores/perfStats';

// ─── Levels ─────────────────────────────────────────────────────────────────

export const LEVEL_NAMES = ['minimal', 'low', 'medium', 'high', 'ultra'] as const;
export type QualityLevel = (typeof LEVEL_NAMES)[number];
export type QualityMode = 'auto' | 'high' | 'medium' | 'low';

/** Mode -> level index when the user forces a level. */
const FORCED_INDEX: Record<Exclude<QualityMode, 'auto'>, number> = { high: 3, medium: 2, low: 1 };

interface LevelSpec {
  name: QualityLevel;
  /** Fraction of the canvas resolution the volume is rendered at. */
  renderScale: number;
  /** Multiplier on the user's ray step length. */
  stepMul: number;
  /** Hard cap on ray-march iterations per pixel (step length grows to honour it). */
  maxSteps: number;
  /** 0 = nearest sample, 1 = azimuth interpolation, 2 = azimuth + elevation interpolation. */
  interp: 0 | 1 | 2;
}

const LEVELS: LevelSpec[] = [
  { name: 'minimal', renderScale: 0.35, stepMul: 5.0, maxSteps: 110, interp: 0 },
  { name: 'low', renderScale: 0.5, stepMul: 3.5, maxSteps: 200, interp: 0 },
  { name: 'medium', renderScale: 0.65, stepMul: 2.2, maxSteps: 320, interp: 1 },
  { name: 'high', renderScale: 0.85, stepMul: 1.4, maxSteps: 640, interp: 2 },
  { name: 'ultra', renderScale: 1.0, stepMul: 1.0, maxSteps: 2400, interp: 2 },
];

/** Volume texture budget (MB of R8) by quality ceiling index. */
const TEX_BUDGET_MB = [8, 16, 32, 48, 64];

/** Typical ray length / base step used to estimate relative cost of levels. */
const NOMINAL_STEPS = 1800;
/** While the camera is moving, render smaller and step further. */
const INTERACT_SCALE = 0.75;
const INTERACT_STEP = 1.6;
const MIN_SCALE = 0.3;

/** Frame-time targets (ms). */
const TARGET_INTERACTIVE_MS = 28;
const TARGET_IDLE_MS = 150;
/** A single GPU submission (or main-thread draw call) slower than this triggers the safety net. */
export const EMERGENCY_MS = 250;

export interface QualityParams {
  level: QualityLevel;
  index: number;
  interacting: boolean;
  renderScale: number;
  stepMul: number;
  maxSteps: number;
  interp: 0 | 1 | 2;
  texBudgetMB: number;
  /** Per-pixel work estimate (steps x sampling cost). */
  weight: number;
  /** renderScale^2 x weight: cost relative to other levels. */
  relCost: number;
}

function makeParams(index: number, interacting: boolean, texBudgetMB: number): QualityParams {
  const spec = LEVELS[Math.max(0, Math.min(LEVELS.length - 1, index))];
  const renderScale = interacting ? Math.max(MIN_SCALE, spec.renderScale * INTERACT_SCALE) : spec.renderScale;
  const stepMul = interacting ? spec.stepMul * INTERACT_STEP : spec.stepMul;
  const steps = Math.min(spec.maxSteps, NOMINAL_STEPS / stepMul);
  const weight = steps * (1 + 0.35 * spec.interp);
  return {
    level: spec.name,
    index,
    interacting,
    renderScale,
    stepMul,
    maxSteps: spec.maxSteps,
    interp: spec.interp,
    texBudgetMB,
    weight,
    relCost: renderScale * renderScale * weight,
  };
}

// ─── Hardware detection ─────────────────────────────────────────────────────

export interface GpuInfo {
  renderer: string;
  vendor: string;
  /** Software rasteriser (SwiftShader, llvmpipe, ...). */
  weak: boolean;
  /** Integrated / low-end hints: few cores, little memory, Intel iGPU. */
  modest: boolean;
}

const WEAK_RENDERER = /swiftshader|llvmpipe|softpipe|software|basic render|microsoft basic|mesa offscreen|\bcpu\b/i;

export function detectGpu(gl: WebGL2RenderingContext | null): GpuInfo {
  let renderer = '';
  let vendor = '';
  try {
    if (gl) {
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      renderer = String(gl.getParameter(ext ? ext.UNMASKED_RENDERER_WEBGL : gl.RENDERER) ?? '');
      vendor = String(gl.getParameter(ext ? ext.UNMASKED_VENDOR_WEBGL : gl.VENDOR) ?? '');
    }
  } catch {
    /* extension lookup can fail on a lost context */
  }
  const weak = WEAK_RENDERER.test(renderer) || WEAK_RENDERER.test(vendor);
  let modest = false;
  try {
    const nav = navigator as Navigator & { deviceMemory?: number };
    const cores = nav.hardwareConcurrency ?? 8;
    const mem = nav.deviceMemory ?? 8;
    modest = cores <= 2 || mem <= 2 || /intel/i.test(renderer + vendor) && !/\barc\b/i.test(renderer);
  } catch {
    /* ignore */
  }
  return { renderer, vendor, weak, modest };
}

// ─── Governor ───────────────────────────────────────────────────────────────

const STORAGE_KEY = 'xradar.3d.quality';

export function loadQualityMode(): QualityMode {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === 'auto' || v === 'high' || v === 'medium' || v === 'low') return v;
  } catch {
    /* storage blocked */
  }
  return 'auto';
}

function saveQualityMode(m: QualityMode): void {
  try {
    localStorage.setItem(STORAGE_KEY, m);
  } catch {
    /* storage blocked */
  }
}

export interface GovernorState {
  mode: QualityMode;
  level: QualityLevel;
  idleLevel: QualityLevel;
  ceiling: QualityLevel;
  renderScale: number;
  frameMsEma: number;
  weakRenderer: boolean;
  gpu: string;
  /** Safety net fired: show the "Reduced 3D quality" hint. */
  hint: boolean;
}

/** The governor whose frames are currently on screen: the only one that publishes to perfStats. */
let activeGovernor: QualityGovernor | null = null;
let activeSince = 0;

export class QualityGovernor {
  private readonly _state = writable<GovernorState>();
  readonly state: Readable<GovernorState> = { subscribe: this._state.subscribe };

  mode: QualityMode;
  weak = false;
  gpu = '';
  /** Highest canvas pixel ratio worth rendering at. */
  dprCap = 2;

  private index = 3;
  private hwCeil = 4;
  private penalty = 0;
  private cpr = 0; // ms per unit of relCost (EMA)
  private emaMs = 0;
  private calm = 0;
  private lastChangeAt = -1e9;
  private lastRiseAt = -1e9;
  private riseHoldMs = 1500;
  private lastEmergencyAt = -1e9;
  private overrideSafety = false;
  private hint = false;
  private lastParamsIdle: QualityParams | null = null;
  private lastPublishAt = 0;
  private extraVolumeMB = 0;

  constructor(readonly name = '3d') {
    this.mode = loadQualityMode();
    this.index = this.startIndex();
    this.emit(true);
    this.probeBattery();
  }

  // ── configuration ──────────────────────────────────────────────────────

  /** Inspect the GL context (renderer string, hardware hints) and pick the start level / ceiling. */
  configureGpu(gl: WebGL2RenderingContext | null): GpuInfo {
    const info = detectGpu(gl);
    this.weak = info.weak;
    this.gpu = info.renderer;
    this.hwCeil = info.weak ? 2 : 4;
    this.dprCap = info.weak ? 1 : info.modest ? 1.5 : 2;
    this.index = this.startIndex(info);
    if (info.weak) this.cpr = 0;
    this.emit(true);
    return info;
  }

  private startIndex(info?: GpuInfo): number {
    if (this.mode !== 'auto') return FORCED_INDEX[this.mode];
    if (info?.weak) return 1; // low
    if (info?.modest) return 2; // medium
    return 3; // high; climbs to ultra when frames are cheap
  }

  private probeBattery(): void {
    try {
      const nav = navigator as Navigator & { getBattery?: () => Promise<{ charging: boolean; level: number }> };
      nav.getBattery?.().then((b) => {
        if (!b.charging && b.level < 0.25) {
          this.hwCeil = Math.min(this.hwCeil, 3);
          this.index = Math.min(this.index, this.hwCeil);
          this.emit(true);
        }
      }).catch(() => {});
    } catch {
      /* not available */
    }
  }

  setMode(m: QualityMode): void {
    if (m === this.mode) return;
    this.mode = m;
    saveQualityMode(m);
    this.penalty = 0;
    this.hint = false;
    this.overrideSafety = false;
    this.calm = 0;
    this.lastChangeAt = performance.now();
    this.index = m === 'auto' ? Math.min(this.index, this.hwCeil) : FORCED_INDEX[m];
    this.emit(true);
  }

  /** User dismissed the hint and wants their chosen quality kept: stop reducing it. */
  keepMySetting(): void {
    this.overrideSafety = true;
    this.penalty = 0;
    this.hint = false;
    if (this.mode === 'auto') this.index = Math.min(this.hwCeil, this.index + 2);
    this.emit(true);
  }

  dismissHint(): void {
    this.hint = false;
    this.emit(true);
  }

  /** Bytes the owner reports it holds in the volume (for state/diagnostics only). */
  setVolumeMB(mb: number): void {
    this.extraVolumeMB = mb;
    if (!activeGovernor || activeGovernor === this) perfStats.update((s) => ({ ...s, volumeTexMB: mb }));
  }

  // ── queries ────────────────────────────────────────────────────────────

  private ceilingIndex(): number {
    return this.mode === 'auto' ? this.hwCeil : FORCED_INDEX[this.mode];
  }

  /** Volume texture budget follows the quality ceiling, not the dynamic level (no re-packing churn). */
  textureBudgetMB(): number {
    return TEX_BUDGET_MB[this.ceilingIndex()];
  }

  private baseIndex(): number {
    if (this.mode === 'auto') return Math.min(this.index, this.hwCeil);
    return Math.max(0, FORCED_INDEX[this.mode] - this.penalty);
  }

  /** Parameters for the next frame. */
  getParams(interacting: boolean): QualityParams {
    const base = this.baseIndex();
    const tex = this.textureBudgetMB();
    if (interacting || this.mode !== 'auto') {
      const p = makeParams(base, interacting, tex);
      if (!interacting) this.lastParamsIdle = p;
      return p;
    }
    // Idle auto: the settled frame is the best quality predicted to stay inside the idle budget.
    let pick = base;
    if (this.cpr > 0) {
      for (let k = this.hwCeil; k > base; k--) {
        if (this.cpr * makeParams(k, false, tex).relCost <= TARGET_IDLE_MS * 0.7) {
          pick = k;
          break;
        }
      }
    }
    const p = makeParams(pick, false, tex);
    this.lastParamsIdle = p;
    return p;
  }

  // ── feedback ───────────────────────────────────────────────────────────

  /** Owner went away: stop being the one that publishes to perfStats. */
  release(): void {
    if (activeGovernor === this) activeGovernor = null;
  }

  /** Report the GPU time of a completed frame rendered with `params`. */
  observe(params: QualityParams, ms: number): void {
    if (!(ms >= 0) || params.relCost <= 0) return;
    // The main radar view outranks the small volume panel (whose frames also wait behind the
    // main view's GPU work and would read as slow) unless it has been quiet for a while.
    const now = performance.now();
    if (!activeGovernor || activeGovernor === this || this.name === 'radar3d' || now - activeSince > 10000) {
      activeGovernor = this;
    }
    if (activeGovernor === this) activeSince = now;
    const sample = ms / params.relCost;
    this.cpr = this.cpr === 0 ? sample : this.cpr * 0.65 + sample * 0.35;
    this.emaMs = this.emaMs === 0 ? ms : this.emaMs * 0.7 + ms * 0.3;

    if (this.mode === 'auto') {
      const now = performance.now();
      const base = this.baseIndex();
      const target = params.interacting ? TARGET_INTERACTIVE_MS : TARGET_IDLE_MS;
      const tooSlow = params.interacting
        ? ms > target * 1.2
        : params.index === base && ms > target * 2.5;

      if (tooSlow && base > 0) {
        // Drop straight to the best level predicted to fit, at least one step down.
        let next = 0;
        for (let k = base - 1; k > 0; k--) {
          if (this.cpr * makeParams(k, params.interacting, 0).relCost <= target * 0.85) {
            next = k;
            break;
          }
        }
        // Dropping soon after a rise means that rise was wrong: wait longer before trying again.
        if (now - this.lastRiseAt < 8000) this.riseHoldMs = Math.min(this.riseHoldMs * 2, 32000);
        this.index = next;
        this.lastChangeAt = now;
        this.calm = 0;
      } else if (params.interacting && ms < target * 0.5) {
        this.calm++;
        if (
          this.calm >= 4 &&
          base < this.hwCeil &&
          now - this.lastChangeAt > this.riseHoldMs &&
          this.cpr * makeParams(base + 1, true, 0).relCost < target * 0.75
        ) {
          this.index = base + 1;
          this.lastChangeAt = now;
          this.lastRiseAt = now;
          this.calm = 0;
        }
      } else if (params.interacting) {
        this.calm = 0;
      }
    }
    this.emit(false);
  }

  /**
   * A single GPU submission or draw call exceeded {@link EMERGENCY_MS}: the estimate was badly
   * wrong (new GPU load, window moved to another display, ...). Drop two levels immediately.
   * Returns true if the quality was reduced.
   */
  emergency(ms: number, params?: QualityParams): boolean {
    if (this.overrideSafety) return false;
    const now = performance.now();
    if (now - this.lastEmergencyAt < 1500) return false; // bands of the old level still draining
    this.lastEmergencyAt = now;
    const before = this.baseIndex();
    if (this.mode === 'auto') {
      this.index = Math.max(0, before - 2);
      this.riseHoldMs = Math.max(this.riseHoldMs, 10000);
    } else {
      this.penalty = Math.min(this.penalty + 2, FORCED_INDEX[this.mode]);
    }
    if (params && params.relCost > 0) this.cpr = Math.max(this.cpr, ms / params.relCost);
    this.lastChangeAt = now;
    this.calm = 0;
    const reduced = this.baseIndex() < before;
    if (reduced) this.hint = true;
    this.emit(true);
    return reduced;
  }

  // ── publishing ─────────────────────────────────────────────────────────

  snapshot(): GovernorState {
    const base = this.baseIndex();
    return {
      mode: this.mode,
      level: LEVELS[base].name,
      idleLevel: this.lastParamsIdle?.level ?? LEVELS[base].name,
      ceiling: LEVELS[this.ceilingIndex()].name,
      renderScale: this.lastParamsIdle?.renderScale ?? LEVELS[base].renderScale,
      frameMsEma: this.emaMs,
      weakRenderer: this.weak,
      gpu: this.gpu,
      hint: this.hint,
    };
  }

  private emit(force: boolean): void {
    const now = performance.now();
    if (!force && now - this.lastPublishAt < 400) return;
    this.lastPublishAt = now;
    const s = this.snapshot();
    this._state.set(s);
    if (activeGovernor && activeGovernor !== this) return;
    perfStats.update((p) => ({
      ...p,
      quality: `${s.idleLevel}${s.mode === 'auto' ? ' (auto)' : ''}`,
      qualityMode: s.mode,
      frameMsEma: s.frameMsEma,
      renderScale: s.renderScale,
      volumeTexMB: this.extraVolumeMB || p.volumeTexMB,
      gpuRenderer: s.gpu || p.gpuRenderer,
    }));
  }

  current(): GovernorState {
    return get(this.state);
  }
}

// ─── Scheduling helpers ─────────────────────────────────────────────────────

let _chan: MessageChannel | null = null;
const _pending: Array<() => void> = [];

/**
 * Yield to the browser so input and painting can run, then continue. Uses scheduler.postTask when
 * the engine has it, a MessageChannel otherwise (no 4 ms setTimeout clamp), setTimeout last.
 */
export function yieldToMain(background = false): Promise<void> {
  const sched = (globalThis as unknown as {
    scheduler?: { postTask?: (cb: () => void, o?: { priority: string }) => Promise<void> };
  }).scheduler;
  if (sched?.postTask) {
    return sched.postTask(() => {}, { priority: background ? 'background' : 'user-visible' }).catch(() => {});
  }
  if (typeof MessageChannel !== 'undefined') {
    if (!_chan) {
      _chan = new MessageChannel();
      _chan.port1.onmessage = () => {
        const f = _pending.shift();
        f?.();
      };
    }
    return new Promise<void>((resolve) => {
      _pending.push(resolve);
      _chan!.port2.postMessage(0);
    });
  }
  return new Promise<void>((resolve) => setTimeout(resolve, 0));
}

// ─── Progressive (banded, fenced) renderer ──────────────────────────────────
//
// Why bands, and why every band is presented:
//
// On software and some integrated GPUs a full ray-marching frame is seconds of work. The browser's
// GPU process runs it as ONE task, during which nothing else can be drawn: page repaints, input
// feedback and screenshots all stall ("the computer froze"). Splitting the frame into bands only
// helps if each band is really submitted separately, and ANGLE (Vulkan/SwiftShader, and to a
// lesser extent others) defers submission of off-screen work until something is presented: a
// fence after off-screen draws never signals, and all bands then run as one giant batch at the
// end. So every band is followed by a blit of its rows onto the (preserved) canvas, which both
// forces the submission and shows the progress; the fence is then created after the blit.
//
// A fence's latency is `floor + gpuCost`: the floor (compositor round trip) is learned as the
// smallest latency seen and subtracted, so band sizes follow the real GPU cost. Bands start
// small and double until their cost becomes measurable, then are sized to a cost budget.

export interface ProgressiveJob {
  /**
   * Called once at the start of every frame: snapshot camera/uniform state for the frame.
   * `w`/`h` is the render resolution. Return false to skip the frame (nothing to draw yet).
   */
  begin(params: QualityParams, w: number, h: number): boolean;
  /**
   * Draw rows [y, y+rows) of the frame. The framebuffer, viewport and scissor are already set;
   * the job must clear and draw (and rebind its own program/textures: nothing else is assumed).
   */
  drawBand(y: number, rows: number, w: number, h: number): void;
}

export interface ProgressiveOptions {
  gl: WebGL2RenderingContext;
  /**
   * The canvas. Its context MUST have been created with `preserveDrawingBuffer: true` so partial
   * results stay on screen between bands.
   */
  canvas: HTMLCanvasElement;
  governor: QualityGovernor;
  job: ProgressiveJob;
  /** Called when drawing throws; the renderer then stops. */
  onError: (err: unknown) => void;
}

/** Bands cost no more than this fraction of the fence floor, clamped to [MIN, MAX] ms. */
const BAND_BUDGET_MIN_MS = 12;
const BAND_BUDGET_MAX_MS = 60;
/** Bands before which the fence floor is unknown (and shader JIT inflates latency). */
const WARMUP_BANDS = 2;

export class ProgressiveRenderer {
  private gl: WebGL2RenderingContext;
  private canvas: HTMLCanvasElement;
  private gov: QualityGovernor;
  private job: ProgressiveJob;
  private onError: (e: unknown) => void;

  private fbo: WebGLFramebuffer | null = null;
  private fboTex: WebGLTexture | null = null;
  private fboW = 0;
  private fboH = 0;
  private hasImage = false;
  /** Size of the image last rendered into the target (a sub-rectangle at reduced scale). */
  private imgW = 0;
  private imgH = 0;

  private dirty = false;
  private rafId = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private settleTimer: ReturnType<typeof setTimeout> | null = null;
  private interactUntil = 0;
  private lost = false;
  private disposed = false;
  private stopped = false;
  private starting = false;

  private frame: {
    params: QualityParams;
    w: number;
    h: number;
    y: number;
    costMs: number;
    rawMs: number;
    bands: number;
    startedAt: number;
  } | null = null;
  private inflight: { sync: WebGLSync; t0: number; rows: number; px: number; flagged: boolean } | null = null;

  /** Smallest fence latency seen (ms): the round-trip floor of this machine. Infinity = unknown. */
  private floorMs = Infinity;
  private bandsSeen = 0;
  /** Pixel-work (pixels x weight) believed to cost about one band budget. */
  private pxBudget = 0;

  /** Diagnostics. */
  stats = {
    frames: 0,
    bands: 0,
    lastFrameMs: 0,
    lastFrameWallMs: 0,
    lastBands: 0,
    maxBandCostMs: 0,
    lastBandRows: 0,
    floorMs: 0,
    lastFrameCostRaw: 0,
  };

  constructor(o: ProgressiveOptions) {
    this.gl = o.gl;
    this.canvas = o.canvas;
    this.gov = o.governor;
    this.job = o.job;
    this.onError = o.onError;
  }

  isInteracting(): boolean {
    return performance.now() < this.interactUntil;
  }

  /** Camera / slider activity: render coarser until input pauses, then one settled frame. */
  noteInteraction(): void {
    this.interactUntil = performance.now() + 220;
    if (this.settleTimer) clearTimeout(this.settleTimer);
    this.settleTimer = setTimeout(() => {
      this.settleTimer = null;
      this.request();
    }, 240);
    this.request();
  }

  /** Request a redraw (coalesced; nothing runs while idle). */
  request(): void {
    if (this.disposed || this.lost || this.stopped) return;
    this.dirty = true;
    if (this.starting || this.frame || this.inflight || this.timer || this.rafId) return;
    this.rafId = requestAnimationFrame(() => {
      this.rafId = 0;
      this.startFrame();
    });
  }

  setLost(lost: boolean): void {
    this.lost = lost;
    if (lost) {
      this.cancelWork();
      this.fbo = null;
      this.fboTex = null;
      this.fboW = this.fboH = 0;
      this.hasImage = false;
    } else {
      this.request();
    }
  }

  /** Resume after an error stopped the renderer. */
  resume(): void {
    this.stopped = false;
    this.request();
  }

  dispose(): void {
    this.disposed = true;
    this.cancelWork();
    if (this.settleTimer) clearTimeout(this.settleTimer);
    this.settleTimer = null;
    if (!this.lost) {
      try {
        if (this.fbo) this.gl.deleteFramebuffer(this.fbo);
        if (this.fboTex) this.gl.deleteTexture(this.fboTex);
      } catch {
        /* context gone */
      }
    }
    this.fbo = null;
    this.fboTex = null;
  }

  /** Show the last completed image again (e.g. right after a canvas resize cleared the canvas). */
  presentLast(): void {
    if (this.hasImage && !this.lost && !this.disposed) {
      try {
        this.blit(0, this.imgH, this.imgW, this.imgH);
      } catch {
        /* context gone */
      }
    }
  }

  private cancelWork(): void {
    if (this.rafId) cancelAnimationFrame(this.rafId);
    this.rafId = 0;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (this.inflight) {
      try {
        this.gl.deleteSync(this.inflight.sync);
      } catch {
        /* lost */
      }
    }
    this.inflight = null;
    this.frame = null;
  }

  private ensureFbo(w: number, h: number): void {
    const gl = this.gl;
    if (this.fbo && this.fboTex && w === this.fboW && h === this.fboH) return;
    if (this.fbo) gl.deleteFramebuffer(this.fbo);
    if (this.fboTex) gl.deleteTexture(this.fboTex);
    this.fboTex = gl.createTexture();
    gl.activeTexture(gl.TEXTURE7); // keep clear of the job's texture units
    gl.bindTexture(gl.TEXTURE_2D, this.fboTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.bindTexture(gl.TEXTURE_2D, null);
    gl.activeTexture(gl.TEXTURE0);
    this.fbo = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.fboTex, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    this.fboW = w;
    this.fboH = h;
    this.hasImage = false;
  }

  private startFrame(): void {
    if (this.disposed || this.lost || this.stopped || !this.dirty) return;
    if (this.canvas.width === 0 || this.canvas.height === 0) return;
    this.starting = true;
    try {
      const params = this.gov.getParams(this.isInteracting());
      const w = Math.max(32, Math.round(this.canvas.width * params.renderScale));
      const h = Math.max(32, Math.round(this.canvas.height * params.renderScale));
      this.dirty = false;
      // One render target at canvas size; lower render scales use a sub-rectangle of it, so
      // switching between interactive and settled quality never reallocates GPU memory.
      this.ensureFbo(this.canvas.width, this.canvas.height);
      if (!this.job.begin(params, w, h)) return;
      this.frame = { params, w, h, y: 0, costMs: 0, rawMs: 0, bands: 0, startedAt: performance.now() };
      this.submitBand();
    } catch (e) {
      this.fail(e);
    } finally {
      this.starting = false;
    }
  }

  private bandBudget(): number {
    const f = Number.isFinite(this.floorMs) ? this.floorMs : 0;
    return Math.min(BAND_BUDGET_MAX_MS, Math.max(BAND_BUDGET_MIN_MS, f * 0.3));
  }

  /** Smallest cost (ms) we can tell apart from the fence floor's noise. */
  private resolvable(): number {
    const f = Number.isFinite(this.floorMs) ? this.floorMs : 0;
    return Math.max(6, f * 0.2);
  }

  private chooseRows(): number {
    const f = this.frame!;
    const remaining = f.h - f.y;
    const perRow = f.w * f.params.weight;
    if (this.pxBudget <= 0) this.pxBudget = 8 * perRow; // probe: a few rows
    const rows = Math.floor(this.pxBudget / perRow);
    return Math.max(Math.min(4, remaining), Math.min(remaining, rows));
  }

  private submitBand(): void {
    const f = this.frame;
    if (!f || this.disposed || this.lost) return;
    const gl = this.gl;
    try {
      const rows = this.chooseRows();
      const t0 = performance.now();
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
      gl.viewport(0, 0, f.w, f.h);
      gl.enable(gl.SCISSOR_TEST);
      gl.scissor(0, f.y, f.w, rows);
      this.job.drawBand(f.y, rows, f.w, f.h);
      gl.disable(gl.SCISSOR_TEST);
      this.blit(f.y, rows, f.w, f.h); // forces the submission of this band and shows its rows
      const sync = gl.fenceSync(gl.SYNC_GPU_COMMANDS_COMPLETE, 0);
      gl.flush();
      const cpuMs = performance.now() - t0;
      if (!sync) throw new Error('fenceSync unavailable');
      this.inflight = { sync, t0, rows, px: rows * f.w * f.params.weight, flagged: false };
      if (cpuMs > EMERGENCY_MS && this.bandsSeen >= WARMUP_BANDS) {
        this.inflight.flagged = true;
        this.gov.emergency(cpuMs, f.params);
      }
      this.pollSoon(2);
    } catch (e) {
      this.fail(e);
    }
  }

  private pollSoon(ms: number): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.poll();
    }, ms);
  }

  private poll(): void {
    const inf = this.inflight;
    const f = this.frame;
    if (!inf || !f || this.disposed || this.lost) return;
    try {
      const gl = this.gl;
      const now = performance.now();
      const done = gl.getSyncParameter(inf.sync, gl.SYNC_STATUS) === gl.SIGNALED;
      if (!done) {
        const waited = now - inf.t0;
        const floor = Number.isFinite(this.floorMs) ? this.floorMs : 0;
        // Slower than the round-trip floor by more than the limit: the cost estimate was badly
        // wrong. (Not during warm-up: the first draws include shader JIT on software GL.)
        if (!inf.flagged && this.bandsSeen >= WARMUP_BANDS && waited - floor > EMERGENCY_MS) {
          inf.flagged = true;
          this.gov.emergency(waited - floor, f.params);
        }
        this.pollSoon(waited > 200 ? 16 : waited > 40 ? 6 : 3);
        return;
      }
      gl.deleteSync(inf.sync);
      this.inflight = null;
      const latency = now - inf.t0;
      this.bandsSeen++;
      f.bands++;
      f.y += inf.rows;
      this.stats.bands++;
      this.stats.lastBandRows = inf.rows;

      // Floor: smallest latency seen (creeping up slowly so a one-off fast reading cannot pin it).
      // The first bands include shader JIT, so they do not count towards cost at all.
      let cost: number;
      if (this.bandsSeen <= WARMUP_BANDS) {
        this.floorMs = Math.min(this.floorMs, latency);
        cost = 0;
      } else {
        this.floorMs = Math.min(this.floorMs * 1.01, latency);
        cost = Math.max(0, latency - this.floorMs);
        f.rawMs += cost;
      }
      this.stats.floorMs = this.floorMs;
      this.stats.maxBandCostMs = Math.max(this.stats.maxBandCostMs, cost);

      // Size the next band: gallop (x2) while the cost is indistinguishable from the floor,
      // then follow the measured cost per pixel-work towards the band budget.
      const thr = this.resolvable();
      const budget = this.bandBudget();
      if (this.bandsSeen > WARMUP_BANDS || cost > 0) {
        if (cost < thr) {
          this.pxBudget = Math.max(this.pxBudget, inf.px * 1.6);
          f.costMs += thr * 0.25;
        } else {
          const target = (inf.px * budget) / cost;
          this.pxBudget = cost > budget * 1.5 ? target : (this.pxBudget + target) / 2;
          f.costMs += cost;
        }
      } else {
        this.pxBudget = Math.max(this.pxBudget, inf.px * 1.6); // warm-up: grow, unmeasured
      }

      if (f.y >= f.h) {
        this.finishFrame();
        return;
      }
      // Multi-band frame: leave the GPU process some air so the compositor and the rest of the
      // machine keep running.
      const gap = Math.min(40, cost * 0.5);
      this.timer = setTimeout(() => {
        this.timer = null;
        this.submitBand();
      }, gap);
    } catch (e) {
      this.fail(e);
    }
  }

  private finishFrame(): void {
    const f = this.frame!;
    this.hasImage = true;
    this.imgW = f.w;
    this.imgH = f.h;
    this.frame = null;
    this.stats.frames++;
    this.stats.lastFrameMs = f.costMs;
    this.stats.lastFrameCostRaw = f.rawMs;
    this.stats.lastFrameWallMs = performance.now() - f.startedAt;
    this.stats.lastBands = f.bands;
    // Only report once the floor is known: warm-up frames carry shader JIT time.
    if (this.bandsSeen > WARMUP_BANDS) this.gov.observe(f.params, f.costMs);
    // Next frame (camera moved or quality changed meanwhile) starts on the next rAF.
    if (this.dirty) this.request();
  }

  /** Copy rows [y, y+rows) of the render target onto the canvas (scaled to the canvas size). */
  private blit(y: number, rows: number, w: number, h: number): void {
    const gl = this.gl;
    if (!this.fbo || !this.fboW || w <= 0 || h <= 0) return;
    const cw = this.canvas.width;
    const ch = this.canvas.height;
    const sy = ch / h;
    const dy0 = Math.max(0, Math.floor(y * sy));
    const dy1 = Math.min(ch, Math.ceil((y + rows) * sy));
    gl.bindFramebuffer(gl.READ_FRAMEBUFFER, this.fbo);
    gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, null);
    gl.disable(gl.SCISSOR_TEST);
    const same = w === cw && h === ch;
    gl.blitFramebuffer(
      0, y, w, y + rows,
      0, dy0, cw, dy1,
      gl.COLOR_BUFFER_BIT,
      same ? gl.NEAREST : gl.LINEAR,
    );
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }

  private fail(e: unknown): void {
    this.cancelWork();
    this.stopped = true;
    try {
      this.onError(e);
    } catch {
      /* owner callback must not throw into the scheduler */
    }
  }
}
