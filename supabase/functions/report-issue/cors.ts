/**
 * Which browser origins may call report-issue, and the CORS headers for them.
 *
 *  - REPORT_ALLOWED_ORIGINS (comma separated; default https://radarx.labshope.com) are the sites allowed to submit.
 *  - The desktop app's own origins (Tauri) are allowed for source "app" (and for the preflight, which cannot know).
 *  - http://localhost:* and http://127.0.0.1:* only when REPORT_ALLOW_LOCALHOST=1 (local development).
 *  - Never "*", never credentials. A request without an Origin header (curl, the Python CLI) needs no CORS answer.
 */

export const DEFAULT_ORIGIN = 'https://radarx.labshope.com';
export const TAURI_ORIGINS = ['tauri://localhost', 'http://tauri.localhost', 'https://tauri.localhost'];
const LOCALHOST = /^http:\/\/(?:localhost|127\.0\.0\.1)(?::\d{1,5})?$/;

export type CorsEnv = { REPORT_ALLOWED_ORIGINS?: string; REPORT_ALLOW_LOCALHOST?: string };

/** An origin as the browser sends it: lowercase scheme and host, no path, no trailing slash. */
export function normalizeOrigin(s: string): string | null {
  const t = s.trim().replace(/\/+$/, '');
  if (!/^[a-z][a-z0-9+.-]*:\/\/[^\s/?#*]+$/i.test(t)) return null;
  return t.toLowerCase();
}

export function allowedOrigins(env: CorsEnv): string[] {
  const raw = env.REPORT_ALLOWED_ORIGINS?.trim() ? env.REPORT_ALLOWED_ORIGINS : DEFAULT_ORIGIN;
  return raw.split(',').map(normalizeOrigin).filter((o): o is string => o !== null);
}

/** Is a browser page from `origin` allowed to call the function for `source` (undefined: not known yet)? */
export function originAllowed(origin: string, source: 'app' | 'web' | undefined, env: CorsEnv): boolean {
  const o = normalizeOrigin(origin);
  if (!o) return false;
  if (allowedOrigins(env).includes(o)) return true;
  if (TAURI_ORIGINS.includes(o)) return source !== 'web';
  if (env.REPORT_ALLOW_LOCALHOST === '1' && LOCALHOST.test(o)) return true;
  return false;
}

/** The CORS headers to add to a response (empty when no Origin was sent or it is not allowed). */
export function corsHeaders(origin: string | null, source: 'app' | 'web' | undefined, env: CorsEnv): Record<string, string> {
  const base: Record<string, string> = { Vary: 'Origin' };
  if (!origin || !originAllowed(origin, source, env)) return base;
  return {
    ...base,
    'Access-Control-Allow-Origin': normalizeOrigin(origin) as string,
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type, apikey',
    'Access-Control-Max-Age': '600',
  };
}
