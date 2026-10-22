/**
 * Pure logic of "Connect to a remote engine" (Settings > Engine): the address check, the first
 * message, and what to do when the connection closes. No stores, no sockets, no `window`: unit-tested
 * in remoteEngine.test.mjs.
 *
 * The remote engine is `rxd serve` on a cluster, reached through `ssh -L PORT:localhost:PORT`. Its
 * address is therefore always this computer; the token is a secret that lives only in the memory of
 * the WebSocket manager. Nothing in this file keeps a token, and no function here returns one.
 */

/** The only host names the app connects to for a remote engine (what the SSH forward listens on). */
export const REMOTE_HOSTS = ['127.0.0.1', 'localhost'] as const;
export const REMOTE_MIN_PORT = 1024;
export const REMOTE_MAX_PORT = 65535;

export interface RemoteAddress {
  host: (typeof REMOTE_HOSTS)[number];
  port: number;
  /** `127.0.0.1:8765`: what the status bar shows. */
  hostPort: string;
  /** `ws://127.0.0.1:8765`: no path, no query, no credentials, never a token. */
  url: string;
}

export type AddressCheck = { ok: true; value: RemoteAddress } | { ok: false; error: string };

/**
 * Check what the user typed: `host:port` where host is `127.0.0.1` or `localhost` (the forwarded
 * port on this computer) and port is 1024-65535. A scheme (`ws://`) in front is tolerated and dropped;
 * anything else (another host, a path, a query, user:password@, a missing or odd port) is refused with a
 * sentence that says what to enter instead.
 */
export function parseRemoteAddress(input: unknown): AddressCheck {
  if (typeof input !== 'string') return { ok: false, error: 'Enter the address, for example 127.0.0.1:8765.' };
  let text = input.trim();
  if (!text) return { ok: false, error: 'Enter the address, for example 127.0.0.1:8765.' };
  text = text.replace(/^ws:\/\//i, '');
  if (/[/?#@\s]/.test(text)) {
    return { ok: false, error: 'Enter only host and port, for example 127.0.0.1:8765 (no path, no user name, no token).' };
  }
  const match = /^([^:]+):(\d+)$/.exec(text);
  if (!match) return { ok: false, error: 'Enter the address as host:port, for example 127.0.0.1:8765.' };
  const host = match[1].toLowerCase();
  if (!(REMOTE_HOSTS as readonly string[]).includes(host)) {
    return {
      ok: false,
      error: 'Only 127.0.0.1 or localhost is accepted: the engine on the cluster is reached through an ssh port forward on this computer.',
    };
  }
  const port = Number(match[2]);
  if (match[2].length > 5 || !Number.isInteger(port) || port < REMOTE_MIN_PORT || port > REMOTE_MAX_PORT) {
    return { ok: false, error: `The port must be a number from ${REMOTE_MIN_PORT} to ${REMOTE_MAX_PORT}.` };
  }
  const h = host as RemoteAddress['host'];
  return { ok: true, value: { host: h, port, hostPort: `${h}:${port}`, url: `ws://${h}:${port}` } };
}

/** The very first message on a remote connection: `rxd serve` closes the socket with 1008 unless it is this. */
export function engineAuthFrame(token: string): string {
  return JSON.stringify({ type: 'engine_auth', token });
}

/** True when a token is worth sending (the engine's tokens are 43 URL-safe characters; be tolerant of copy-paste whitespace). */
export function cleanToken(raw: unknown): string | null {
  if (typeof raw !== 'string') return null;
  const t = raw.trim();
  return t.length >= 16 && /^[A-Za-z0-9_-]+$/.test(t) ? t : null;
}

export type RemotePhase = 'off' | 'connecting' | 'connected' | 'refused' | 'busy' | 'lost' | 'ended' | 'unreachable';

export interface CloseVerdict {
  phase: RemotePhase;
  message: string;
  /** Try again (after `delayMs`)? Always false once the limit is reached: a failure never loops. */
  retry: boolean;
  delayMs: number;
  /** Drop the token from memory: the engine said it is wrong, so retrying with it cannot help. */
  forgetToken: boolean;
}

/** Most reconnect attempts after a dropped link (about a minute in all) and when the engine is busy. */
export const MAX_LINK_RETRIES = 10;
export const MAX_BUSY_RETRIES = 5;

/**
 * What a closed remote connection means and whether to try again. `attempt` is the number of retries
 * already made since the last success.
 */
export function classifyClose(code: number, wasConnected: boolean, attempt: number, hostPort = ''): CloseVerdict {
  const where = hostPort ? ` at ${hostPort}` : '';
  if (code === 1008) {
    return {
      phase: 'refused',
      message: 'The engine refused the connection. The token is wrong, or it belongs to an earlier run of rxd serve. Enter the token printed by the current run (Settings > Engine).',
      retry: false,
      delayMs: 0,
      forgetToken: true,
    };
  }
  if (code === 1013) {
    const retry = attempt < MAX_BUSY_RETRIES;
    return {
      phase: 'busy',
      message: retry
        ? 'Another app is still connected to this engine (a dropped link can take up to half a minute to clear). Trying again...'
        : 'Another app is connected to this engine. Close it, or start a new rxd serve, then connect again.',
      retry,
      delayMs: Math.min(2000 * 2 ** attempt, 30000),
      forgetToken: false,
    };
  }
  if (code === 1000 || code === 1001) {
    return {
      phase: 'ended',
      message: 'The remote engine closed the connection (it was stopped, or it reached its idle limit). Start rxd serve again and connect with the new token.',
      retry: false,
      delayMs: 0,
      forgetToken: true,
    };
  }
  const retry = attempt < MAX_LINK_RETRIES;
  return {
    phase: retry ? (wasConnected ? 'lost' : 'connecting') : 'unreachable',
    message: retry
      ? `Cannot reach the engine${where}. Trying again...`
      : `Cannot reach the engine${where}. Check that rxd serve is still running and that the ssh command is still open, then connect again.`,
    retry,
    delayMs: Math.min(500 * 2 ** attempt, 8000),
    forgetToken: false,
  };
}

/** The words for the status bar and the Engine tab. */
export function remoteStatusText(phase: RemotePhase, hostPort: string | null): string {
  const where = hostPort ?? '';
  switch (phase) {
    case 'connected':
      return `REMOTE ENGINE ${where}`;
    case 'connecting':
      return `REMOTE ENGINE ${where} (connecting)`;
    case 'lost':
      return `REMOTE ENGINE ${where} (link lost, retrying)`;
    case 'busy':
      return `REMOTE ENGINE ${where} (busy)`;
    case 'refused':
      return `REMOTE ENGINE ${where} (refused)`;
    case 'ended':
      return `REMOTE ENGINE ${where} (stopped)`;
    case 'unreachable':
      return `REMOTE ENGINE ${where} (unreachable)`;
    default:
      return 'Built-in engine';
  }
}
