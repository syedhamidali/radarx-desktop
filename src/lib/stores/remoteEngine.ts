/**
 * Which engine the app is using: its own (built in, started by the shell) or a remote one (`rxd serve`
 * on a cluster, reached through an ssh port forward).
 *
 * Only what the UI shows is kept here. The token is deliberately NOT part of this store (nor of the
 * settings, nor of localStorage): it lives in a private field of the WebSocket manager, in memory, and
 * is gone when the app closes or the user returns to the built-in engine.
 */
import { writable, get } from 'svelte/store';
import type { RemotePhase } from '../utils/remoteEngine';

export interface RemoteEngineState {
  /** 'remote' while the app is pointed at (or trying to reach) a remote engine. */
  mode: 'builtin' | 'remote';
  /** `127.0.0.1:8765`, the forwarded address on this computer. */
  hostPort: string | null;
  phase: RemotePhase;
  /** A sentence for the Engine tab when something is wrong; null otherwise. */
  message: string | null;
}

export const remoteEngine = writable<RemoteEngineState>({ mode: 'builtin', hostPort: null, phase: 'off', message: null });

export function setRemoteEngine(patch: Partial<RemoteEngineState>): void {
  remoteEngine.update((s) => ({ ...s, ...patch }));
}

export function resetRemoteEngine(): void {
  remoteEngine.set({ mode: 'builtin', hostPort: null, phase: 'off', message: null });
}

/** True while the engine in use is a remote one (files are then opened by their path on that computer). */
export function usingRemoteEngine(): boolean {
  return get(remoteEngine).mode === 'remote';
}
