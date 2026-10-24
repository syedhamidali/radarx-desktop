/**
 * Is the engine still answering?
 *
 * The shell only notices an engine process that exited. An engine that is stuck (its event loop
 * blocked) stays "connected" while nothing it was asked ever comes back. The app therefore pings
 * the engine every few seconds; if no pong arrives for `warnAfterMs` the user is told, and when
 * pongs come back they are told that too. The engine ends itself after `XRADAR_WATCHDOG_S`
 * (60 s by default) without a heartbeat on its own loop, and the shell restarts it (see
 * docs/STABILITY.md), so the warning says that help is on its way.
 *
 * Pure logic with an injected clock so it can be unit tested (heartbeat.test.mjs).
 */
export type HeartbeatEvent = 'silent' | 'recovered' | null;

export class Heartbeat {
  private lastPong: number;
  private silent = false;
  private readonly warnAfterMs: number;

  constructor(warnAfterMs = 30_000, now = Date.now()) {
    this.warnAfterMs = warnAfterMs;
    this.lastPong = now;
  }

  /** A pong arrived. Returns 'recovered' when the engine had been reported silent. */
  pong(now = Date.now()): HeartbeatEvent {
    this.lastPong = now;
    if (!this.silent) return null;
    this.silent = false;
    return 'recovered';
  }

  /** Called on every ping interval. Returns 'silent' once, when the silence first passes the limit. */
  tick(now = Date.now()): HeartbeatEvent {
    if (this.silent || now - this.lastPong < this.warnAfterMs) return null;
    this.silent = true;
    return 'silent';
  }

  /** A new connection starts with a clean slate. */
  reset(now = Date.now()): void {
    this.lastPong = now;
    this.silent = false;
  }

  get isSilent(): boolean {
    return this.silent;
  }
}
