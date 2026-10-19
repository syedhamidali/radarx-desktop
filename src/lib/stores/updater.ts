import { writable, derived } from 'svelte/store';
import { settings } from './settings';
import { isSkipped } from '../utils/updateLogic';

/** What the Rust side reports (`updater_status` in src-tauri/src/updates.rs). */
export interface UpdaterStatus {
  configured: boolean;
  reason: string | null;
  currentVersion: string;
  install: { mode: 'auto' | 'manual'; note: string | null };
  releasesUrl: string | null;
  os: 'macos' | 'windows' | 'linux' | string;
  checkTarget: string | null;
}

export type UpdatePhase = 'idle' | 'checking' | 'available' | 'downloading' | 'ready' | 'installing';

export interface UpdateInfo {
  version: string;
  notes: string;
  date: string | null;
}

export interface UpdaterState {
  /** `updater_status` has been answered (desktop only). */
  loaded: boolean;
  status: UpdaterStatus | null;
  phase: UpdatePhase;
  update: UpdateInfo | null;
  /** Why the last check failed (shown in Settings only, never as a toast). */
  checkError: string | null;
  /** The last check said we are up to date / found something / failed. */
  lastResult: 'none' | 'up-to-date' | 'available' | 'failed';
  /** A download or install failed (shown in the update dialog). */
  actionError: string | null;
  received: number;
  total: number | null;
  dialogOpen: boolean;
  /** The user wants the restart prompt, but a job may be running: it opens when idle. */
  promptPending: boolean;
  /** Windows: the engine was stopped for the installer and the install then failed. */
  engineStopped: boolean;
}

export const INITIAL_UPDATER_STATE: UpdaterState = {
  loaded: false,
  status: null,
  phase: 'idle',
  update: null,
  checkError: null,
  lastResult: 'none',
  actionError: null,
  received: 0,
  total: null,
  dialogOpen: false,
  promptPending: false,
  engineStopped: false,
};

export const updater = writable<UpdaterState>({ ...INITIAL_UPDATER_STATE });

/** The update, unless the user skipped that exact version. */
export const visibleUpdate = derived([updater, settings], ([$u, $s]) =>
  $u.update && !isSkipped($s.updates.skippedVersion, $u.update.version) ? $u.update : null,
);

/** Updating is available in this build (desktop, key + endpoint configured). */
export const updaterEnabled = derived(updater, ($u) => !!$u.status?.configured);
