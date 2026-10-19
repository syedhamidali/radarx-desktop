/**
 * Self-update service (desktop only). The check, download, signature verification and install are
 * done in Rust by tauri-plugin-updater, so the webview never talks to the release host (no CSP
 * entry needed). This file decides WHEN: a quiet check at launch (at most once per 24 h), never
 * while a job is running, never in a browser, and failures stay silent (a line in Settings).
 *
 * The rules themselves are in updateLogic.ts (unit-tested).
 */
import { get, derived } from 'svelte/store';
import type { Update } from '@tauri-apps/plugin-updater';
import { settings } from '../stores/settings';
import { updater, type UpdaterState, type UpdaterStatus } from '../stores/updater';
import { operations } from './requestScheduler';
import { pipelineBusy } from '../stores/pipeline';
import { mosaicRun } from '../stores/mosaic';
import { windRun } from '../stores/wind';
import { runningModules } from '../stores/modules';
import { inTauri } from './env';
import {
  autoCheckDue,
  busyState,
  describeUpdateError,
  isNewer,
  isSkipped,
  normalizeVersion,
  restartGate,
} from './updateLogic';

/** Hard limit for the version check, as the user asked: 10 s. */
export const CHECK_TIMEOUT_MS = 10_000;
/** Wait this long after launch before the automatic check, so it never competes with startup. */
const LAUNCH_DELAY_MS = 8_000;
/** A long-running app re-evaluates the 24 h rule hourly. */
const RECHECK_EVERY_MS = 60 * 60 * 1000;

/** The pieces of the app that run jobs, folded into one "is anything running" signal. */
export const jobBusy = derived(
  [operations, pipelineBusy, mosaicRun, windRun, runningModules],
  ([$ops, $pipe, $mosaic, $wind, $mods]) =>
    busyState({
      operations: $ops,
      pipelineBusy: $pipe,
      mosaicRunning: !!$mosaic.running,
      windRunning: !!$wind.running,
      modulesRunning: Object.values($mods).filter(Boolean).length,
    }),
);

let handle: Update | null = null;
let started = false;
let promptChecking = false;

function patch(p: Partial<UpdaterState>): void {
  updater.update((s) => ({ ...s, ...p }));
}

async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke: call } = await import('@tauri-apps/api/core');
  return call<T>(cmd, args);
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('__timeout__')), ms);
    p.then(
      (v) => { clearTimeout(timer); resolve(v); },
      (e) => { clearTimeout(timer); reject(e); },
    );
  });
}

async function closeHandle(): Promise<void> {
  const h = handle;
  handle = null;
  try { await h?.close(); } catch { /* already released */ }
}

/** Ask the shell whether updating is configured here. Call once at startup (desktop). */
export async function initUpdater(): Promise<void> {
  if (started || !inTauri()) return; // never in web mode
  started = true;
  let status: UpdaterStatus | null = null;
  try {
    status = await invoke<UpdaterStatus>('updater_status');
  } catch (e) {
    console.warn('[updater] status unavailable:', e);
  }
  patch({ loaded: true, status });
  if (!status?.configured) return; // unconfigured dev / fork build: do nothing, ever

  // Restart prompt: opens by itself only once the update is downloaded AND nothing is running.
  const gate = () => queueMicrotask(maybePrompt);
  jobBusy.subscribe(gate);
  updater.subscribe(gate);

  setTimeout(() => void maybeAutoCheck(), LAUNCH_DELAY_MS);
  setInterval(() => void maybeAutoCheck(), RECHECK_EVERY_MS);
}

function maybePrompt(): void {
  if (promptChecking) return;
  promptChecking = true;
  try {
    const s = get(updater);
    const verdict = restartGate({
      ready: s.phase === 'ready',
      promptPending: s.promptPending,
      busy: get(jobBusy).busy,
    });
    if (verdict === 'show') patch({ dialogOpen: true, promptPending: false });
  } finally {
    promptChecking = false;
  }
}

async function maybeAutoCheck(): Promise<void> {
  const u = get(settings).updates;
  if (!autoCheckDue({ enabled: u.checkOnLaunch, lastCheckedAt: u.lastCheckedAt, now: Date.now() })) return;
  await checkForUpdates(false);
}

/**
 * Look for a newer version. `manual` = the user pressed "Check now": it also reports "up to date",
 * and shows a version they had skipped. Never throws.
 */
export async function checkForUpdates(manual: boolean): Promise<void> {
  const s = get(updater);
  if (!s.status?.configured || (s.phase !== 'idle' && s.phase !== 'available')) return;
  const prev = s.phase;
  patch({ phase: 'checking', checkError: null });
  try {
    const { check } = await import('@tauri-apps/plugin-updater');
    const target = s.status.checkTarget ?? undefined;
    const found = await withTimeout(
      check({ timeout: CHECK_TIMEOUT_MS, ...(target ? { target } : {}) }),
      CHECK_TIMEOUT_MS + 2_000,
    );
    settings.updateSetting('updates', { lastCheckedAt: Date.now() });

    const current = s.status.currentVersion;
    if (found && isNewer(current, found.version)) {
      await closeHandle();
      handle = found;
      if (manual && isSkipped(get(settings).updates.skippedVersion, found.version)) {
        settings.updateSetting('updates', { skippedVersion: '' }); // they asked: show it
      }
      patch({
        phase: 'available',
        update: { version: normalizeVersion(found.version), notes: found.body ?? '', date: found.date ?? null },
        lastResult: 'available',
        actionError: null,
        received: 0,
        total: null,
      });
      const o = get(settings).updates;
      if (
        o.downloadInBackground &&
        s.status.install.mode === 'auto' &&
        !isSkipped(o.skippedVersion, found.version)
      ) {
        void downloadUpdate(true);
      }
    } else {
      try { await found?.close(); } catch { /* nothing to release */ }
      await closeHandle();
      patch({ phase: 'idle', update: null, lastResult: 'up-to-date' });
    }
  } catch (e) {
    // Offline, DNS, 404, timeout: quiet. One line in Settings, no toast, no dialog.
    console.warn('[updater] check failed:', e);
    patch({
      phase: prev === 'available' ? 'available' : 'idle',
      checkError: describeUpdateError(e, 'check').message,
      lastResult: 'failed',
    });
  }
}

/** Download the found update (signature verified in Rust before it is kept). */
export async function downloadUpdate(background = false): Promise<boolean> {
  const s = get(updater);
  if (!handle || s.phase !== 'available' || s.status?.install.mode !== 'auto') return false;
  patch({ phase: 'downloading', received: 0, total: null, actionError: null });
  try {
    await handle.download((ev) => {
      if (ev.event === 'Started') updater.update((x) => ({ ...x, total: ev.data.contentLength ?? null }));
      else if (ev.event === 'Progress') updater.update((x) => ({ ...x, received: x.received + ev.data.chunkLength }));
    });
    patch({ phase: 'ready', promptPending: background });
    return true;
  } catch (e) {
    console.warn('[updater] download failed:', e);
    patch({ phase: 'available', actionError: describeUpdateError(e, 'download').message });
    return false;
  }
}

/**
 * "Install and restart". Downloads first if needed. If a job is running when the download
 * finishes, the restart waits and the prompt comes back by itself once the app is idle.
 */
export async function installAndRestart(): Promise<void> {
  let s = get(updater);
  if (s.status?.install.mode !== 'auto' || !handle) return;
  if (get(jobBusy).busy) return; // the dialog disables the button; this is the backstop
  if (s.phase === 'available') {
    if (!(await downloadUpdate(false))) return;
    s = get(updater);
  }
  if (s.phase !== 'ready') return;
  if (get(jobBusy).busy) {
    patch({ promptPending: true }); // a job started meanwhile: ask again when it ends
    return;
  }
  patch({ phase: 'installing', actionError: null, engineStopped: false });
  let stopped = false;
  try {
    await invoke('stop_engine_for_update'); // Windows only: the installer replaces the engine's files
    stopped = get(updater).status?.os === 'windows';
    await handle.install();
    await invoke('relaunch_app'); // macOS / Linux; on Windows the installer restarts the app itself
  } catch (e) {
    console.warn('[updater] install failed:', e);
    patch({
      phase: 'ready',
      engineStopped: stopped,
      actionError: stopped
        ? `${describeUpdateError(e, 'install').message} The analysis engine was stopped for the install: restart the app to continue.`
        : describeUpdateError(e, 'install').message,
    });
  }
}

/** Restart after a failed install on Windows, where the engine was already stopped. */
export async function restartApp(): Promise<void> {
  try { await invoke('relaunch_app'); } catch (e) { console.warn('[updater] restart failed:', e); }
}

/** "Skip this version": hide exactly this version; a newer one is offered again. */
export function skipVersion(): void {
  const v = get(updater).update?.version;
  if (!v) return;
  settings.updateSetting('updates', { skippedVersion: v });
  patch({ dialogOpen: false, promptPending: false });
}

/** "Later": close the dialog; the chip stays and nothing pops up again for this version. */
export function later(): void {
  patch({ dialogOpen: false, promptPending: false });
}

export function openUpdateDialog(): void {
  patch({ dialogOpen: true });
}

export function closeUpdateDialog(): void {
  patch({ dialogOpen: false });
}

/** Open a link in the system browser (http/https only; the caller passes safe URLs). */
export async function openExternal(url: string): Promise<void> {
  try {
    const { open } = await import('@tauri-apps/plugin-shell');
    await open(url);
  } catch (e) {
    console.warn('[updater] could not open link:', e);
  }
}
