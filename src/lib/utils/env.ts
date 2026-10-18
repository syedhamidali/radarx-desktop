/**
 * Runtime environment: desktop (Tauri) vs. plain browser ("web mode").
 *
 * Web mode = the frontend runs as a static site in an ordinary browser and talks to a remote
 * engine over a WebSocket. Everything that is web-only is gated behind {@link isWebMode} so the
 * Tauri code paths stay exactly as they were, and nothing here imports `@tauri-apps/*`.
 *
 * The URL helpers are pure (they take their inputs as arguments) so they can be unit-tested.
 */

/** True when running inside the Tauri webview (the desktop app). */
export function inTauri(): boolean {
  return typeof window !== 'undefined' && !!(window as any).__TAURI_INTERNALS__;
}

/** True when NOT running inside Tauri, i.e. a normal browser tab. */
export function isWebMode(): boolean {
  return !inTauri();
}

/**
 * Normalise a user / build supplied engine address to a `ws:` / `wss:` URL without trailing slash.
 * Accepts ws(s):// and http(s):// (mapped to ws/wss) and bare `host[:port]` (-> wss, or ws for
 * localhost). Returns null for anything unusable.
 */
export function normalizeEngineUrl(raw: string | null | undefined): string | null {
  if (typeof raw !== 'string') return null;
  let s = raw.trim();
  if (!s) return null;
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) {
    const local = /^(localhost|127\.0\.0\.1|\[::1\])(:|\/|$)/i.test(s);
    s = `${local ? 'ws' : 'wss'}://${s}`;
  }
  let u: URL;
  try {
    u = new URL(s);
  } catch {
    return null;
  }
  const map: Record<string, string> = { 'ws:': 'ws:', 'wss:': 'wss:', 'http:': 'ws:', 'https:': 'wss:' };
  const proto = map[u.protocol];
  if (!proto || !u.hostname) return null;
  const path = u.pathname.replace(/\/+$/, '');
  return `${proto}//${u.host}${path}${u.search}`;
}

/** `wss://host/x` -> `https://host/x`, `ws://host` -> `http://host` (same host, no trailing slash). */
export function engineHttpBase(wsUrl: string | null): string | null {
  if (!wsUrl) return null;
  try {
    const u = new URL(wsUrl);
    const proto = u.protocol === 'wss:' || u.protocol === 'https:' ? 'https:' : 'http:';
    return `${proto}//${u.host}${u.pathname.replace(/\/+$/, '')}`;
  } catch {
    return null;
  }
}

/**
 * Turn the engine's relative `download_url` ('/download/<token>/<filename>') into an absolute
 * http(s) URL on the engine host. Absolute http(s) URLs are returned unchanged. Anything else
 * (other schemes such as `javascript:`) yields null.
 */
export function resolveDownloadUrl(engineWsUrl: string | null, downloadUrl: string): string | null {
  if (typeof downloadUrl !== 'string' || !downloadUrl) return null;
  if (/^https?:\/\//i.test(downloadUrl)) return downloadUrl;
  if (/^[a-z][a-z0-9+.-]*:/i.test(downloadUrl) || downloadUrl.startsWith('//')) return null;
  const base = engineHttpBase(engineWsUrl);
  if (!base) return null;
  // Download urls are rooted at the engine origin ('/download/...'), not at a path prefix.
  const origin = new URL(base).origin;
  return downloadUrl.startsWith('/') ? `${origin}${downloadUrl}` : `${base}/${downloadUrl}`;
}

/**
 * Engine WebSocket URL for web builds: `?engine=` (development override) wins over the build-time
 * `VITE_ENGINE_URL`. Null when neither is set (or when running in Tauri).
 */
export function engineUrl(): string | null {
  if (!isWebMode()) return null;
  let fromQuery: string | null = null;
  try {
    fromQuery = new URLSearchParams(window.location.search).get('engine');
  } catch { /* no window / location */ }
  const q = normalizeEngineUrl(fromQuery);
  if (q) return q;
  return normalizeEngineUrl((import.meta as any).env?.VITE_ENGINE_URL);
}

/** Basename of a path or URL path (handles both separators). */
export function basename(p: string): string {
  const s = String(p ?? '');
  return s.split(/[\\/]/).filter(Boolean).pop() ?? s;
}
