/**
 * Movie Studio: make a storm movie (MP4) from the files on disk, interactively.
 *
 * The same engine functions as `rxd animate`, over the WebSocket:
 *
 *   -> {type:'animate_capabilities', request_id}             <- {type:'animate_capabilities', ...}
 *   -> {type:'animate_plan', request_id, inputs, options}    <- animate_progress* then
 *        {type:'animate_plan_result', plan} | {type:'animate_error', message}
 *   -> {type:'animate_still', request_id, inputs, options, still_at, width, height}
 *                                                            <- {type:'animate_still_result', png, still_at_s, plan}
 *   -> {type:'animate_render', request_id, inputs, options, output}
 *                                                            <- {type:'animate_render_result', video, provenance, ...}
 *   -> {type:'animate_cancel', request_id}                   <- {type:'animate_cancelled'}
 *   -> {type:'animate_video', request_id, path}              <- header + binary frames (the MP4)
 *   -> {type:'animate_copy', request_id, src, dst}           <- {type:'animate_copied', path, sidecar}
 *
 * Only the most recent request of each kind is honoured. Every long request is an operation in the
 * operations tray (cancel there or in the dialog).
 */

import { writable, derived, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { beginOp, type OpHandle } from '../utils/requestScheduler';
import { registerJobSource } from './jobSources';
import { addToast } from './toasts';

// ── Types ──────────────────────────────────────────────────────────────────

export type CameraPreset = 'orbit' | 'wide' | 'top-down' | 'window' | 'flight';
export type Light = 'daylight' | 'dusk' | 'overcast' | 'night';
export type Palette = 'white' | 'soft' | 'stormy' | 'cyclone';

export interface MovieOptions {
  // what to film
  start: string;
  end: string;
  reference: string;
  // the storm
  focus: 'storm' | 'scene';
  cropKm: number;
  track: 'cells' | 'linear' | 'eye';
  cellDbz: number;
  // camera and clip
  camera: CameraPreset;
  duration: number;
  fps: number;
  width: number;
  height: number;
  // the look
  stormStyle: 'scientific' | 'realistic';
  ground: 'map' | 'realistic';
  light: Light;
  palette: Palette;
  lookPreset: string;
  mapTheme: 'dark' | 'light';
  title: string;
  subtitle: string;
  // sound (synthesised; only when the engine has it)
  sound: boolean;
  soundThunderDb: number;
  drawFlashes: boolean;
  flashesFromGlm: string;
  lightningModel: boolean;
  // output
  outputName: string;
  frameFormat: 'png' | 'jpg';
}

export const DEFAULT_OPTIONS: MovieOptions = {
  start: '',
  end: '',
  reference: 'auto',
  focus: 'storm',
  cropKm: 60,
  track: 'cells',
  cellDbz: 45,
  camera: 'orbit',
  duration: 36,
  fps: 30,
  width: 1920,
  height: 1080,
  stormStyle: 'scientific',
  ground: 'map',
  light: 'daylight',
  palette: 'white',
  lookPreset: '',
  mapTheme: 'dark',
  title: '',
  subtitle: '',
  sound: false,
  soundThunderDb: 0,
  drawFlashes: false,
  flashesFromGlm: '',
  lightningModel: false,
  outputName: '',
  frameFormat: 'png',
};

export interface Capabilities {
  ready: boolean;
  plan_ready: boolean;
  problems: { what: string; hint: string }[];
  sound: boolean;
  chrome: { ok: boolean; path?: string; hint?: string };
  renderer: { ok: boolean; path?: string; hint?: string };
  av: boolean;
  playwright: boolean;
  radarx: boolean;
  default_output_dir: string;
  cache_dir: string;
  choices: { cameras: string[]; palettes: string[]; lights: string[]; tracks: string[] };
}

/** The plan (`rxd animate --no-render --json`) plus the text report. */
export interface MoviePlan {
  inputs: { radars: string[]; used_radars: string[]; reference: string; tolerance_s: number; steps: number; skipped: { what: string; reason: string }[] };
  steps: { index: number; time: string; t_s: number; offsets_s: Record<string, number>; files: Record<string, string> }[];
  storm: {
    tracker: string;
    displacement_km: number;
    heading_deg: number;
    mean_speed_ms: number;
    span_s: number;
    track: { index: number; time: string; lat: number; lon: number; max_dbz: number; area_km2: number; top_km: number; speed_ms_since_previous: number | null }[];
  };
  frames: { frames: number; observed_frames: number; interpolated_frames: number; interpolated_share: number; data_span_s: number; longest_gap_s: number; shortest_gap_s: number; fps: number; duration_s: number; timelapse: number; size: [number, number] };
  camera: { mode: string; source: string; target_lag: number; path: any };
  warnings: string[];
  report: string;
  command: string;
  seconds: number;
}

export interface RenderResult {
  video: string | null;
  provenance: string | null;
  frames_written: string | null;
  ffmpeg: string | null;
  video_bytes: number | null;
  sound: any;
  warnings: string[];
  seconds: number;
  plan: MoviePlan;
}

export interface Job {
  running: boolean;
  requestId: string | null;
  percent: number;
  message: string;
  error: string | null;
}

const idle = (): Job => ({ running: false, requestId: null, percent: 0, message: '', error: null });

// ── State ──────────────────────────────────────────────────────────────────

export const movieStudioOpen = writable(false);
/** Files and folders to film (absolute paths; a folder may hold one sub-folder per radar). */
export const movieInputs = writable<string[]>([]);
export const movieOptions = writable<MovieOptions>({ ...DEFAULT_OPTIONS });
export const capabilities = writable<Capabilities | null>(null);
export const capabilitiesError = writable<string | null>(null);

export const planJob = writable<Job>(idle());
export const plan = writable<MoviePlan | null>(null);

export const stillJob = writable<Job>(idle());
/** Data URL of the last preview still and the clip time it shows. */
export const still = writable<{ url: string; at: number } | null>(null);

export const renderJob = writable<Job>(idle());
export const result = writable<RenderResult | null>(null);
/** Object URL of the finished MP4 (revoked when replaced). */
export const videoUrl = writable<string | null>(null);

export const anyRunning = derived([planJob, stillJob, renderJob], ([$p, $s, $r]) => $p.running || $s.running || $r.running);
registerJobSource('movie', derived(renderJob, ($r) => ($r.running ? 1 : 0)));

let seq = 0;
const ops = new Map<string, OpHandle>();
let wired = false;

function newId(kind: string): string {
  return `movie-${kind}-${Date.now()}-${++seq}`;
}

function wire(): void {
  if (wired) return;
  wired = true;
  wsManager.onMessage('animate_capabilities', (m: any) => {
    capabilities.set(m as Capabilities);
    capabilitiesError.set(null);
  });
  wsManager.onMessage('animate_progress', (m: any) => {
    const id = m.request_id as string;
    const patch = (j: Job) => (j.requestId === id ? { ...j, percent: m.percent ?? j.percent, message: m.message ?? '' } : j);
    planJob.update(patch);
    stillJob.update(patch);
    renderJob.update(patch);
    ops.get(id)?.update(String(m.message ?? ''));
  });
  wsManager.onMessage('animate_plan_result', (m: any) => {
    if (!finish(planJob, m.request_id)) return;
    plan.set(m.plan as MoviePlan);
  });
  wsManager.onMessage('animate_still_result', (m: any) => {
    if (!finish(stillJob, m.request_id)) return;
    still.set({ url: `data:image/png;base64,${m.png}`, at: Number(m.still_at_s ?? 0) });
    if (m.plan) plan.set(m.plan as MoviePlan);
  });
  wsManager.onMessage('animate_render_result', (m: any) => {
    if (!finish(renderJob, m.request_id)) return;
    const r = m as RenderResult;
    result.set(r);
    if (r.plan) plan.set(r.plan);
    if (r.video) {
      addToast('success', `Movie written: ${r.video.split(/[\\/]/).pop()}`);
      fetchVideo(r.video);
    } else if (r.frames_written) {
      addToast('warning', `No MP4 could be written (PyAV is missing); the frames are in ${r.frames_written}`, 12000);
    }
  });
  wsManager.onMessage('animate_cancelled', (m: any) => {
    const id = m.request_id as string;
    for (const job of [planJob, stillJob, renderJob]) {
      job.update((j) => (j.requestId === id ? { ...j, running: false, requestId: null, message: m.message ?? 'Cancelled' } : j));
    }
    ops.delete(id);
  });
  wsManager.onMessage('animate_error', (m: any) => {
    const id = m.request_id as string;
    const text = String(m.message ?? 'The movie could not be made');
    for (const job of [planJob, stillJob, renderJob]) {
      job.update((j) => (j.requestId === id ? { ...j, running: false, requestId: null, error: text } : j));
    }
    ops.get(id)?.fail(text);
    ops.delete(id);
    if (id?.startsWith('movie-caps')) capabilitiesError.set(text);
    if (id?.startsWith('movie-video') || id?.startsWith('movie-copy')) addToast('error', text);
  });
  wsManager.onMessage('animate_video_ready', (m: any) => {
    const bytes = m.data as Uint8Array;
    const url = URL.createObjectURL(new Blob([bytes as BlobPart], { type: 'video/mp4' }));
    const old = get(videoUrl);
    if (old) URL.revokeObjectURL(old);
    videoUrl.set(url);
  });
  wsManager.onMessage('animate_copied', (m: any) => {
    addToast('success', `Copy saved: ${String(m.path).split(/[\\/]/).pop()}`);
  });
}

/** Mark a job finished when the message answers its current request; false for a stale answer. */
function finish(job: typeof planJob, id: string): boolean {
  const j = get(job);
  if (j.requestId !== id) return false;
  job.set({ ...j, running: false, requestId: null, percent: 100, error: null });
  ops.get(id)?.done();
  ops.delete(id);
  return true;
}

// ── Options -> the engine's long option names ───────────────────────────────

/** The options as `rxd animate` understands them (nothing the user did not set is sent). */
export function engineOptions(o: MovieOptions, caps: Capabilities | null, forStill = false): Record<string, unknown> {
  const out: Record<string, unknown> = {
    camera: o.camera,
    track: o.track,
    cell_dbz: o.cellDbz,
    crop_km: o.focus === 'storm' ? o.cropKm : Math.max(o.cropKm, 160),
    duration: o.duration,
    fps: o.fps,
    width: o.width,
    height: o.height,
    map_theme: o.mapTheme,
    frame_format: o.frameFormat,
  };
  if (o.focus === 'scene' && o.camera === 'orbit') out.camera = 'wide';
  if (o.start.trim()) out.start = o.start.trim();
  if (o.end.trim()) out.end = o.end.trim();
  if (o.reference.trim() && o.reference.trim().toLowerCase() !== 'auto') out.reference = o.reference.trim();
  if (o.stormStyle === 'realistic' && o.ground === 'realistic') out.realistic = true;
  else {
    out.storm_style = o.stormStyle;
    out.ground = o.ground;
  }
  if (o.stormStyle === 'realistic') {
    out.light = o.light;
    out.palette = o.palette;
  }
  if (o.lookPreset) out.look_preset = o.lookPreset;
  if (o.title.trim()) out.title = o.title.trim();
  if (o.subtitle.trim()) out.subtitle = o.subtitle.trim();
  if (o.lightningModel) out.lightning_model = true;
  if (caps?.sound && o.sound && !forStill) {
    out.sound = true;
    if (o.soundThunderDb !== 0) out.sound_thunder_db = o.soundThunderDb;
    if (o.flashesFromGlm.trim()) out.flashes_from_glm = [o.flashesFromGlm.trim()];
    if (o.drawFlashes && (o.flashesFromGlm.trim() || o.lightningModel)) out.draw_flashes = true;
  }
  return out;
}

// ── Actions ────────────────────────────────────────────────────────────────

export function openMovieStudio(inputs?: string[]): void {
  wire();
  if (inputs && inputs.length) movieInputs.set(dedupe(inputs));
  movieStudioOpen.set(true);
  if (!get(capabilities)) loadCapabilities();
}

export function closeMovieStudio(): void {
  movieStudioOpen.set(false);
}

export function addInputs(paths: string[]): void {
  movieInputs.update((list) => dedupe([...list, ...paths]));
  plan.set(null);
  still.set(null);
}

export function removeInput(path: string): void {
  movieInputs.update((list) => list.filter((p) => p !== path));
  plan.set(null);
  still.set(null);
}

function dedupe(paths: string[]): string[] {
  const seen = new Set<string>();
  return paths.filter((p) => !!p && !seen.has(p) && (seen.add(p), true));
}

export function loadCapabilities(): void {
  wire();
  capabilitiesError.set(null);
  wsManager.send({ type: 'animate_capabilities', request_id: newId('caps') }).catch((err) => capabilitiesError.set(String(err)));
}

function start(job: typeof planJob, kind: string, label: string, payload: Record<string, unknown>, timeoutMs: number): string | null {
  wire();
  const inputs = get(movieInputs);
  if (!inputs.length) {
    job.update((j) => ({ ...j, error: 'Choose the radar files to film first.' }));
    return null;
  }
  const requestId = newId(kind);
  job.set({ running: true, requestId, percent: 0, message: 'Starting...', error: null });
  const handle = beginOp({
    kind: 'movie',
    label,
    slowMs: 15000,
    timeoutMs,
    onCancel: () => cancel(requestId),
  });
  ops.set(requestId, handle);
  wsManager.send({ ...payload, type: `animate_${kind}`, request_id: requestId, inputs }).catch((err) => {
    job.update((j) => (j.requestId === requestId ? { ...j, running: false, requestId: null, error: String(err) } : j));
    handle.fail(String(err));
    ops.delete(requestId);
  });
  return requestId;
}

/** The plan only (no browser): steps, storm track, frames, camera. */
export function requestPlan(): void {
  const o = get(movieOptions);
  start(planJob, 'plan', 'Movie: planning the film', { options: engineOptions(o, get(capabilities)) }, 0);
}

/** One frame at clip time `at` seconds, small, to try the camera and the look. */
export function requestStill(at: number): void {
  const o = get(movieOptions);
  const w = 960;
  const h = Math.round((960 * o.height) / Math.max(1, o.width));
  start(stillJob, 'still', `Movie: preview frame at ${at.toFixed(0)} s`, { options: engineOptions(o, get(capabilities), true), still_at: at, width: w, height: h }, 0);
}

export function startRender(): void {
  const o = get(movieOptions);
  result.set(null);
  const old = get(videoUrl);
  if (old) URL.revokeObjectURL(old);
  videoUrl.set(null);
  start(renderJob, 'render', 'Movie: rendering the film', { options: engineOptions(o, get(capabilities)), output: o.outputName.trim() || null }, 0);
}

export function cancel(requestId?: string | null): void {
  const ids = requestId ? [requestId] : [get(planJob).requestId, get(stillJob).requestId, get(renderJob).requestId];
  for (const id of ids) {
    if (!id) continue;
    wsManager.send({ type: 'animate_cancel', request_id: id }).catch(() => {});
    for (const job of [planJob, stillJob, renderJob]) {
      job.update((j) => (j.requestId === id ? { ...j, message: 'Stopping after the current frame...' } : j));
    }
  }
}

export function cancelRender(): void {
  cancel(get(renderJob).requestId);
}

function fetchVideo(path: string): void {
  wsManager.send({ type: 'animate_video', request_id: newId('video'), path }).catch(() => {});
}

/** "Export a copy": a save dialog, then the engine copies the MP4 and its record. */
export async function exportCopy(): Promise<void> {
  const r = get(result);
  if (!r?.video) return;
  try {
    const { save } = await import('@tauri-apps/plugin-dialog');
    const dst = await save({
      title: 'Save a copy of the movie',
      defaultPath: r.video.split(/[\\/]/).pop(),
      filters: [{ name: 'MP4 video', extensions: ['mp4'] }],
    });
    if (!dst) return;
    await wsManager.send({ type: 'animate_copy', request_id: newId('copy'), src: r.video, dst });
  } catch (err) {
    addToast('error', `Could not save a copy: ${err}`);
  }
}

/** Open the folder that holds the movie in the system file manager. */
export async function showInFolder(): Promise<void> {
  const r = get(result);
  if (!r?.video) return;
  const folder = r.video.replace(/[\\/][^\\/]*$/, '');
  try {
    const { open } = await import('@tauri-apps/plugin-shell');
    await open(folder);
  } catch (err) {
    addToast('info', `The movie is in ${folder}`);
  }
}

/** Pick a folder of radar files (or a case folder with one sub-folder per radar). */
export async function pickFolder(): Promise<void> {
  try {
    const { open } = await import('@tauri-apps/plugin-dialog');
    const picked = await open({ directory: true, multiple: false, title: 'Choose the folder with the radar files' });
    const p = typeof picked === 'string' ? picked : null;
    if (p) addInputs([p]);
  } catch (err) {
    addToast('error', `Could not open the folder dialog: ${err}`);
  }
}

/** Pick radar files. */
export async function pickFiles(): Promise<void> {
  try {
    const { open } = await import('@tauri-apps/plugin-dialog');
    const picked = await open({ multiple: true, title: 'Choose radar files to film' });
    if (!picked) return;
    const list: unknown[] = Array.isArray(picked) ? picked : [picked];
    addInputs(list.map((p) => (typeof p === 'string' ? p : (p as any)?.path)).filter((p): p is string => typeof p === 'string' && !!p));
  } catch (err) {
    addToast('error', `Could not open the file dialog: ${err}`);
  }
}

/** Pick the folder with GOES GLM files (local, nothing is downloaded). */
export async function pickGlmFolder(): Promise<void> {
  try {
    const { open } = await import('@tauri-apps/plugin-dialog');
    const picked = await open({ directory: true, multiple: false, title: 'Choose the folder with GOES GLM files (already on this computer)' });
    if (typeof picked === 'string' && picked) movieOptions.update((o) => ({ ...o, flashesFromGlm: picked }));
  } catch (err) {
    addToast('error', `Could not open the folder dialog: ${err}`);
  }
}

/** Reset the results (keeps the inputs and options). */
export function clearResults(): void {
  plan.set(null);
  still.set(null);
  result.set(null);
  const old = get(videoUrl);
  if (old) URL.revokeObjectURL(old);
  videoUrl.set(null);
}
