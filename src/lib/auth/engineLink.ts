/**
 * Wires the engine-session logic (engineSession.ts) to the real WebSocket and Supabase client.
 *
 * The WebSocket module is imported on demand, so the sign-in screen never loads it. All decisions
 * live in EngineSessionSync (unit-tested); this file only connects things.
 */
import { get } from 'svelte/store';
import { getClient, STORAGE_KEY } from './client';
import { AUTH_DISABLED } from './config';
import { EngineSessionSync, type EngineReply, type SessionLike } from './engineSession';
import { parseStoredSession, sessionForEngine, storedTokenIsCurrent } from './startup';
import { connectionStatus } from '../stores/radarData';

export interface LinkHooks {
  /** The engine will not accept this account: show the sign-in screen. */
  onRequired(reason: string, message: string): void;
  deviceId(): string;
}

let sync: EngineSessionSync | null = null;
let socket: { setEngineAuthorised(ok: boolean): void; clearHeld(): void } | null = null;
let starting: Promise<EngineSessionSync | null> | null = null;

function storedSession() {
  try {
    return parseStoredSession(localStorage.getItem(STORAGE_KEY));
  } catch {
    return null;
  }
}

/**
 * The live Supabase session, or else the one in storage: supabase-js returns null when the access token
 * has expired and cannot be renewed (offline), but the engine accepts that token inside the 30-day
 * allowance, so it must still be presented (docs/ACCOUNTS.md).
 */
async function currentSession(): Promise<SessionLike | null> {
  const stored = storedSession();
  // supabase-js keeps the storage up to date when it renews the token, so a current stored token needs no
  // call at all (and getSession() can take minutes to give up when the server is unreachable)
  if (storedTokenIsCurrent(stored, Date.now())) return sessionForEngine(null, stored);
  let live: SessionLike | null = null;
  try {
    const sb = await getClient();
    const answer = await Promise.race([
      sb.auth.getSession(),
      new Promise<null>((resolve) => setTimeout(() => resolve(null), 4000)),
    ]);
    if (answer?.data.session) live = { access_token: answer.data.session.access_token, expires_at: answer.data.session.expires_at };
  } catch {
    /* fall back to the stored one */
  }
  return sessionForEngine(live, storedSession());
}

const PENDING_KEY = 'radarx.signoutPending';
const pendingSignOut = {
  get(): boolean {
    try {
      return localStorage.getItem(PENDING_KEY) === '1';
    } catch {
      return false;
    }
  },
  set(value: boolean): void {
    try {
      if (value) localStorage.setItem(PENDING_KEY, '1');
      else localStorage.removeItem(PENDING_KEY);
    } catch {
      /* storage blocked: nothing to remember */
    }
  },
};

/** True when an in-app sign-out has not yet reached the engine (it clears the shared session file). */
export function hasPendingSignOut(): boolean {
  return pendingSignOut.get();
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
      pendingSignOut,
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
  if (sync) sync.signOut();
  else pendingSignOut.set(true); // the link never started: the next start delivers it
  socket?.setEngineAuthorised(false);
  socket?.clearHeld(); // requests queued under this account must not replay under the next one
}

export function engineSignedIn(): void {
  sync?.signedIn();
}

export function engineTokenRefreshed(): void {
  sync?.tokenRefreshed();
}
