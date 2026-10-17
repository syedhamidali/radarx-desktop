/**
 * UI responsiveness monitor.
 *
 * Publishes a small readable store (`uiHealth`) describing how busy the main thread is, so the
 * operations tray can say "UI busy" instead of the app just feeling stuck.
 *
 *  - Preferred source: PerformanceObserver({type:'longtask'}) (Chromium / WebView2).
 *  - Fallback (WKWebView / Safari / Firefox have no longtask): a requestAnimationFrame-gap
 *    heuristic measured with performance.now(): a frame gap > 100 ms is recorded as a long task.
 *  - fps is estimated from short requestAnimationFrame bursts (continuous in the fallback mode,
 *    where the rAF loop is running anyway).
 *
 * Cost control: nothing runs while the page is hidden, the store is only written when a value
 * changes, and the state is recomputed on a 500 ms tick, not per frame.
 */
import { readable, type Readable } from 'svelte/store';

export interface UiHealth {
  /** A task longer than BUSY_TASK_MS finished within the last BUSY_WINDOW_MS. */
  busy: boolean;
  /** Longest task (ms) seen in the last 10 s (0 when none). */
  longestMs: number;
  /** Number of long tasks (> 50 ms) seen in the last 10 s. */
  longTasksLast10s: number;
  /** Estimated frames per second (0 until first measured; null when unknown). */
  fps: number | null;
  /** How long tasks are detected. */
  source: 'longtask' | 'raf' | 'none';
}

export const BUSY_TASK_MS = 200;
export const BUSY_WINDOW_MS = 2000;
const HISTORY_MS = 10_000;
const TICK_MS = 500;
const RAF_GAP_LONG_MS = 100; // fallback: frame gap treated as a long task
const FPS_BURST_MS = 500;
const FPS_BURST_EVERY_MS = 3000;

const INITIAL: UiHealth = { busy: false, longestMs: 0, longTasksLast10s: 0, fps: null, source: 'none' };

let current: UiHealth = INITIAL;
let setStore: ((v: UiHealth) => void) | null = null;

/** Readable store; the monitor is started/stopped explicitly with startUiHealth()/stopUiHealth(). */
export const uiHealth: Readable<UiHealth> = readable<UiHealth>(INITIAL, (set) => {
  setStore = set;
  set(current);
  return () => {
    setStore = null;
  };
});

interface Task {
  at: number; // performance.now() when it ended
  dur: number;
}

let tasks: Task[] = [];
let observer: PerformanceObserver | null = null;
let source: UiHealth['source'] = 'none';
let tickTimer: ReturnType<typeof setInterval> | null = null;
let burstTimer: ReturnType<typeof setInterval> | null = null;
let rafId = 0;
let rafRunning = false;
let rafContinuous = false;
let lastFrame = 0;
let burstStart = 0;
let burstFrames = 0;
let fps: number | null = null;
let started = false;

function publish(next: UiHealth): void {
  if (
    next.busy === current.busy &&
    next.longestMs === current.longestMs &&
    next.longTasksLast10s === current.longTasksLast10s &&
    next.fps === current.fps &&
    next.source === current.source
  ) {
    return;
  }
  current = next;
  setStore?.(next);
}

function recompute(): void {
  const now = performance.now();
  if (tasks.length && now - tasks[0].at > HISTORY_MS) {
    tasks = tasks.filter((t) => now - t.at <= HISTORY_MS);
  }
  let longest = 0;
  let busy = false;
  for (const t of tasks) {
    if (t.dur > longest) longest = t.dur;
    if (t.dur > BUSY_TASK_MS && now - t.at <= BUSY_WINDOW_MS) busy = true;
  }
  publish({
    busy,
    longestMs: Math.round(longest),
    longTasksLast10s: tasks.length,
    fps,
    source,
  });
}

function record(dur: number): void {
  tasks.push({ at: performance.now(), dur });
  if (tasks.length > 200) tasks.splice(0, tasks.length - 200);
}

// ── rAF (fallback long-task detection + fps) ───────────────────────────────

function frame(now: number): void {
  rafId = 0;
  if (!rafRunning) return;
  if (lastFrame) {
    const gap = now - lastFrame;
    // Only the fallback treats a frame gap as a long task (longtask API does it otherwise).
    // The gap includes the 16 ms frame itself; report the excess like a long task duration.
    if (source === 'raf' && gap > RAF_GAP_LONG_MS) record(gap);
  }
  lastFrame = now;
  burstFrames++;
  if (!rafContinuous && now - burstStart >= FPS_BURST_MS) {
    fps = Math.round((burstFrames * 1000) / (now - burstStart));
    stopRaf();
    return;
  }
  rafId = requestAnimationFrame(frame);
}

function startRaf(continuous: boolean): void {
  if (rafRunning) return;
  rafRunning = true;
  rafContinuous = continuous;
  lastFrame = 0;
  burstFrames = 0;
  burstStart = performance.now();
  rafId = requestAnimationFrame(frame);
}

function stopRaf(): void {
  rafRunning = false;
  lastFrame = 0;
  if (rafId) cancelAnimationFrame(rafId);
  rafId = 0;
}

function continuousFps(): void {
  // Fallback mode: refresh the fps figure from the continuous loop once per tick.
  const now = performance.now();
  if (!rafContinuous || !burstStart) return;
  const span = now - burstStart;
  if (span >= 1000) {
    fps = Math.round((burstFrames * 1000) / span);
    burstFrames = 0;
    burstStart = now;
  }
}

// ── lifecycle ──────────────────────────────────────────────────────────────

function runMonitors(): void {
  if (tickTimer) return;
  tickTimer = setInterval(() => {
    continuousFps();
    recompute();
  }, TICK_MS);
  if (source === 'raf') {
    startRaf(true);
  } else {
    burstTimer = setInterval(() => startRaf(false), FPS_BURST_EVERY_MS);
    startRaf(false);
  }
}

function pauseMonitors(): void {
  if (tickTimer) clearInterval(tickTimer);
  if (burstTimer) clearInterval(burstTimer);
  tickTimer = burstTimer = null;
  stopRaf();
}

function onVisibility(): void {
  if (!started) return;
  if (document.hidden) pauseMonitors();
  else runMonitors();
}

function longtaskSupported(): boolean {
  try {
    if (typeof PerformanceObserver === 'undefined') return false;
    const types = (PerformanceObserver as any).supportedEntryTypes as string[] | undefined;
    return Array.isArray(types) && types.includes('longtask');
  } catch {
    return false;
  }
}

/** Start monitoring (idempotent). Call once from App.svelte. */
export function startUiHealth(): void {
  if (started || typeof window === 'undefined' || typeof document === 'undefined') return;
  started = true;
  tasks = [];
  fps = null;
  source = 'raf';
  if (longtaskSupported()) {
    try {
      observer = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) record(e.duration);
      });
      observer.observe({ type: 'longtask', buffered: false } as PerformanceObserverInit);
      source = 'longtask';
    } catch {
      observer = null;
      source = 'raf';
    }
  }
  document.addEventListener('visibilitychange', onVisibility);
  if (!document.hidden) runMonitors();
  recompute();
}

/** Stop monitoring and release every timer / observer. */
export function stopUiHealth(): void {
  if (!started) return;
  started = false;
  document.removeEventListener('visibilitychange', onVisibility);
  pauseMonitors();
  observer?.disconnect();
  observer = null;
  tasks = [];
  fps = null;
  source = 'none';
  publish(INITIAL);
}
