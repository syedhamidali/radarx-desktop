/**
 * Hands the signed-in session to the engine and reacts to its answer (docs/ACCOUNTS.md).
 *
 * The engine (the Python process behind the window) refuses every request until a connection has
 * presented a valid session with `auth_session`. This file holds the whole decision logic with no
 * browser, WebSocket or Supabase import, so it is unit-tested (`engineSession.test.mjs`); the
 * wiring to the real socket and Supabase client is `engineLink.ts`.
 *
 * What is sent: only the short-lived ACCESS token. The refresh token never leaves the app: two
 * programs rotating one refresh token would make the server end the session for both.
 */

export interface SessionLike {
  access_token: string;
  /** seconds since epoch, as Supabase reports it */
  expires_at?: number | null;
}

/** Why a session is being sent; the engine uses it for the activity log. */
export type PushReason = 'start' | 'sign_in' | 'refresh' | 'reconnect';

export interface AuthSessionMessage {
  type: 'auth_session';
  access_token: string;
  reason: PushReason;
  expires_at?: number;
  device_id?: string;
  app_version?: string;
}

export function buildAuthSessionMessage(
  s: SessionLike,
  reason: PushReason,
  extra: { deviceId?: string; appVersion?: string } = {},
): AuthSessionMessage {
  const msg: AuthSessionMessage = { type: 'auth_session', access_token: s.access_token, reason };
  if (typeof s.expires_at === 'number' && Number.isFinite(s.expires_at)) msg.expires_at = s.expires_at;
  if (extra.deviceId) msg.device_id = extra.deviceId;
  if (extra.appVersion) msg.app_version = extra.appVersion;
  return msg;
}

export const SIGN_OUT_MESSAGE = { type: 'auth_signout' } as const;

export interface AuthOk {
  type: 'auth_ok';
  email?: string;
  stale?: boolean;
  bypass?: boolean;
  grace_days_left?: number | null;
}
export interface AuthRequired {
  type: 'auth_required';
  reason?: string;
  message?: string;
  request_type?: string;
}
export type EngineReply = AuthOk | AuthRequired;

/** Reasons a fresh token from Supabase can fix. Anything else is final and shows the sign-in screen. */
export const RENEWABLE_REASONS = new Set(['expired', 'token_expired', 'invalid']);

/** Seconds to wait for the engine's answer before sending the session again. */
export const RETRY_DELAYS_MS = [2000, 4000, 8000, 16000];

export interface SyncDeps {
  send(msg: Record<string, unknown>): void;
  isConnected(): boolean;
  getSession(): Promise<SessionLike | null>;
  /** Ask Supabase for a new access token. Null when that fails. */
  refreshSession(): Promise<SessionLike | null>;
  schedule(fn: () => void, ms: number): () => void;
  online(): boolean;
  /** The engine accepted the session: let the held requests go. */
  onAuthorised(reply: AuthOk): void;
  /** The engine will not accept this account: show the sign-in screen with `message`. */
  onRequired(reason: string, message: string): void;
  deviceId?(): string | undefined;
  appVersion?(): string | undefined;
  /**
   * A sign-out that could not be delivered (no connection to the engine) is remembered here and sent
   * as soon as the connection is up, so the shared session file never survives an in-app sign-out.
   */
  pendingSignOut?: { get(): boolean; set(value: boolean): void };
}

export class EngineSessionSync {
  private attempts = 0;
  private waiting = false;
  private renewedOnce = false;
  private cancelTimer: (() => void) | null = null;
  private nextReason: PushReason = 'start';

  private readonly d: SyncDeps;

  constructor(deps: SyncDeps) {
    this.d = deps;
  }

  /** True while an answer from the engine is awaited. */
  get pending(): boolean {
    return this.waiting;
  }

  /** The socket to the engine is open (first time or again after a restart). */
  connected(): void {
    this.attempts = 0;
    this.renewedOnce = false;
    if (this.d.pendingSignOut?.get()) {
      this.d.send({ ...SIGN_OUT_MESSAGE });
      this.d.pendingSignOut.set(false);
    }
    const reason = this.nextReason;
    this.nextReason = 'reconnect';
    void this.push(reason);
  }

  disconnected(): void {
    this.clearTimer();
    this.waiting = false;
  }

  /** The user just signed in (not a silent restore). */
  signedIn(): void {
    this.d.pendingSignOut?.set(false); // a new sign-in replaces any sign-out still waiting
    this.nextReason = 'sign_in';
    if (this.d.isConnected()) {
      this.nextReason = 'reconnect';
      void this.push('sign_in');
    }
  }

  /** Supabase renewed the access token. */
  tokenRefreshed(): void {
    if (this.d.isConnected()) void this.push('refresh');
  }

  /** The user signed out: tell the engine (it forgets the session and clears the shared file). */
  signOut(): void {
    this.clearTimer();
    this.waiting = false;
    this.nextReason = 'start';
    if (this.d.isConnected()) {
      this.d.send({ ...SIGN_OUT_MESSAGE });
      this.d.pendingSignOut?.set(false);
    } else {
      this.d.pendingSignOut?.set(true);
    }
  }

  async push(reason: PushReason): Promise<void> {
    if (!this.d.isConnected()) return;
    const session = await this.d.getSession();
    if (!session) return; // signed out meanwhile: nothing to present
    this.d.send({
      ...buildAuthSessionMessage(session, reason, {
        deviceId: this.d.deviceId?.(),
        appVersion: this.d.appVersion?.(),
      }),
    });
    this.waiting = true;
    this.arm();
  }

  private arm(): void {
    this.clearTimer();
    const ms = RETRY_DELAYS_MS[Math.min(this.attempts, RETRY_DELAYS_MS.length - 1)];
    this.cancelTimer = this.d.schedule(() => this.timedOut(), ms);
  }

  private clearTimer(): void {
    this.cancelTimer?.();
    this.cancelTimer = null;
  }

  private timedOut(): void {
    this.cancelTimer = null;
    if (!this.waiting) return;
    this.attempts += 1;
    if (this.attempts > RETRY_DELAYS_MS.length) {
      this.waiting = false; // give up quietly; the next (re)connect tries again
      return;
    }
    void this.push('reconnect');
  }

  /** Feed every `auth_ok` / `auth_required` message from the engine here. */
  async handle(reply: EngineReply): Promise<void> {
    if (reply.type === 'auth_ok') {
      this.clearTimer();
      this.waiting = false;
      this.attempts = 0;
      this.renewedOnce = false;
      this.d.onAuthorised(reply);
      // An expired token carried by the offline grace: renew it when the network is back.
      if (reply.stale && this.d.online()) {
        this.renewedOnce = true;
        const fresh = await this.d.refreshSession();
        if (fresh) await this.push('refresh');
      }
      return;
    }
    const reason = reply.reason ?? 'no_session';
    if (reason === 'no_session') {
      // a request overtook the session message; if ours is not on its way, send it now
      if (!this.waiting) await this.push('reconnect');
      return;
    }
    if (RENEWABLE_REASONS.has(reason) && !this.renewedOnce) {
      this.renewedOnce = true;
      const fresh = await this.d.refreshSession();
      if (fresh) {
        await this.push('refresh');
        return;
      }
    }
    this.clearTimer();
    this.waiting = false;
    this.d.onRequired(reason, reply.message ?? 'Please sign in again.');
  }
}
