/**
 * The hook between the storm-clip renderer (utils/stormClip.ts) and a film that is more than one
 * storm: a `ClipExtras` object replaces the clock (the data time of a clip time), the camera (a pose
 * per clip time), the lightning light and the heads-up display, and may draw into the 3-D scene's
 * overlay. The PERiLS movie (perilsExtras.ts) is the one implementation. Types only: no code, so
 * the renderer pays nothing for it.
 */
import type { CartesianRenderer } from '../utils/cartesianRenderer';
import type { CartVolume } from '../utils/cartesianVolume';

/** A camera as the overlay sees it (the same shape as the renderer's frame). */
export interface OverlayCam {
  eye: [number, number, number];
  fwd: [number, number, number];
  right: [number, number, number];
  up: [number, number, number];
  tanX: number;
  tanY: number;
  w: number;
  h: number;
}

export interface ExtrasPose {
  x: number;
  y: number;
  /** Eye height above sea level, km (true, not exaggerated). */
  zKm: number;
  yawDeg: number;
  pitchDeg: number;
  /** Vertical exaggeration of the scene for this frame. */
  vexag: number;
  /** Vertical field of view, degrees (0 = the renderer's default). */
  fov?: number;
}

/** What the renderer tells the extras about the frame being drawn. */
export interface FrameInfo {
  /** Clip time, s. */
  t: number;
  /** Data time, s after the first step. */
  td: number;
  /** Interval of the two observed volumes the frame is interpolated from, and the position in it (0..1). */
  k: number;
  s: number;
  vexag: number;
  eye: [number, number, number];
  yawDeg: number;
  pitchDeg: number;
}

/** What the extras may use of the renderer. */
export interface ClipApi {
  renderer: CartesianRenderer;
  /** The first volume (grid, sites, vmin / vmax). */
  g0: CartVolume;
  /** Observed step times, s. */
  stepTimes: number[];
  baseUrl: string;
  width: number;
  height: number;
  terrainKmAt(x: number, y: number): number;
  /** Whether the realistic (stylised) storm look is on. */
  realistic: boolean;
}

export interface ClipExtras {
  /** Data time (s after the first step) shown at clip time `t`. */
  dataTime(t: number): number;
  /** Camera at clip time `t`. */
  pose(t: number): ExtrasPose;
  /** Load what the extras need (called once, from `prepare`, after the ground is set up). */
  prepare(api: ClipApi): Promise<void>;
  /** Called before each frame is drawn (set the ground, the flash light ...). */
  beforeFrame?(info: FrameInfo): void;
  /** Light of the lightning for the 3-D scene at this frame (scene km, z already exaggerated). */
  flashLight?(info: FrameInfo): { points: { x: number; y: number; z: number; i: number }[]; sky: number } | null;
  /** Annotations inside the 3-D scene (projected with `cam`): markers, tracks, lightning. */
  drawScene?(ctx: CanvasRenderingContext2D, cam: OverlayCam, info: FrameInfo): void;
  /** The whole heads-up display (replaces the single-storm banner). */
  drawHud?(ctx: CanvasRenderingContext2D, w: number, h: number, info: FrameInfo): void;
  /** Await anything asynchronous (DOM insets) before the frame's pixels are taken. */
  settle?(): Promise<void>;
  /** Report for the render script. */
  report?(): Record<string, unknown>;
}
