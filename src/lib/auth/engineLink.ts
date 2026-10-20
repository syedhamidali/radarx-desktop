/**
 * Wires the engine-session logic (engineSession.ts) to the real WebSocket and Supabase client.
 *
 * The WebSocket module is imported on demand, so the sign-in screen never loads it. All decisions
 * live in EngineSessionSync (unit-tested); this file only connects things.
 */
import { get } from 'svelte/store';
import { getClient } from './client';
import { AUTH_DISABLED } from './config';
import { EngineSessionSync, type EngineReply, type SessionLike } from './engineSession';
import { connectionStatus } from '../stores/radarData';

export interface LinkHooks {
  /** The engine will not accept this account: show the sign-in screen. */
  onRequired(reason: string, message: string): void;
  deviceId(): string;
}

let sync: EngineSessionSync | null = null;
let socket: { setEngineAuthorised(ok: boolean): void } | null = null;
let starting: Promise<EngineSessionSync | null> | null = null;

async function currentSession(): Promise<SessionLike | null> {
  const sb = await getClient();
  const { data } = await sb.auth.getSession();
  return data.session ? { access_token: data.session.access_token, expires_at: data.session.expires_at } : null;
}

async function renewedSession(): Promise<SessionLike | null> {
  try {
    const sb = await getClient();
    const { data, error } = await sb.auth.refreshSession();
    if (error || !data.session) return null;
    return { access_token: data.session.access_token, expires_at: data.session.expires_at };
  } catch {
    return null;
  }
}

/** Start (once) the hand-over of the signed-in session to the engine. */
export function startEngineLink(hooks: LinkHooks): Promise<EngineSessionSync | null> {
  if (AUTH_DISABLED) return Promise.resolve(null);
  if (starting) return starting;
  starting = (async () => {
    const { wsManager } = await import('../utils/websocket');
    socket = wsManager;
    const link = new EngineSessionSync({
      send: (m) => void wsManager.send(m as Record<string, any>),
      isConnected: () => get(connectionStatus) === 'connected',
      getSession: currentSession,
      refreshSession: renewedSession,
      schedule: (fn, ms) => {
        const t = setTimeout(fn, ms);
        return () => clearTimeout(t);
      },
      online: () => (typeof navigator === 'undefined' ? true : navigator.onLine),
      onAuthorised: () => wsManager.setEngineAuthorised(true),
      onRequired: (reason, message) => {
        wsManager.setEngineAuthorised(false);
        hooks.onRequired(reason, message);
      },
      deviceId: () => hooks.deviceId(),
      appVersion: () => (typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : undefined),
    });
    wsManager.onMessage('auth_ok', (m) => void link.handle(m as EngineReply));
    wsManager.onMessage('auth_required', (m) => void link.handle(m as EngineReply));
    let was: string | null = null;
    connectionStatus.subscribe((status) => {
      if (status === 'connected' && was !== 'connected') link.connected();
      else if (status !== 'connected' && was === 'connected') link.disconnected();
      was = status;
    });
    sync = link;
    return link;
  })();
  return starting;
}

/** Tell the engine the user signed out (it forgets the account and clears the shared session file). */
export function engineSignedOut(): void {
  sync?.signOut();
  socket?.setEngineAuthorised(false);
}

export function engineSignedIn(): void {
  sync?.signedIn();
}

export function engineTokenRefreshed(): void {
  sync?.tokenRefreshed();
}
