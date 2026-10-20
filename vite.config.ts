import { defineConfig, loadEnv, type Plugin } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { readFileSync } from "node:fs";
import { codecovVitePlugin } from "@codecov/vite-plugin";

// The app version shown in the UI comes from package.json (kept in step with tauri.conf.json and
// Cargo.toml by the release bump), so it cannot go stale.
const appVersion: string = JSON.parse(readFileSync("./package.json", "utf-8")).version;

/**
 * Web build only (`vite build --mode web`): the desktop app gets its CSP from tauri.conf.json, a
 * static site has no such file, so the equivalent policy is injected as a <meta> tag.
 *
 * connect-src is limited to this origin, the account server (Supabase) and the engine in VITE_ENGINE_URL (ws/wss plus the same
 * host over http/https for downloads). Without VITE_ENGINE_URL no tag is written (the engine would
 * only be known at run time via ?engine=); set the equivalent header on the host instead.
 */
// Accounts: the same public project URL as src/lib/auth/config.ts (a build can override it).
const DEFAULT_SUPABASE_URL = "https://bfyimkgvnqiqedcwprpi.supabase.co";

function webCsp(engineUrl: string | undefined, supabaseUrl: string = DEFAULT_SUPABASE_URL): Plugin {
  return {
    name: "xradar-web-csp",
    apply: "build",
    transformIndexHtml(html) {
      if (!engineUrl) return html;
      let host: string;
      let secure: boolean;
      try {
        const u = new URL(engineUrl.replace(/^http/, "ws"));
        host = u.host;
        secure = u.protocol === "wss:";
      } catch {
        return html;
      }
      const engine = secure ? `wss://${host} https://${host}` : `ws://${host} http://${host}`;
      const csp = [
        "default-src 'self'",
        `connect-src 'self' ${engine} ${supabaseUrl}`,
        // Same tile servers as the desktop CSP (the map background loads them as images).
        "img-src 'self' data: blob: https://api.mapbox.com https://*.tiles.mapbox.com https://tile.openstreetmap.org https://*.tile.opentopomap.org",
        "style-src 'self' 'unsafe-inline' https://api.mapbox.com",
        "font-src 'self' data:",
        "script-src 'self' 'unsafe-inline' blob:",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
      ].join("; ");
      // After <meta charset> (which must stay within the first 1024 bytes), before any script.
      return html.replace(
        /(<meta charset[^>]*>)/i,
        `$1\n    <meta http-equiv="Content-Security-Policy" content="${csp}" />`,
      );
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  return {
    plugins: [
      svelte(),
      ...(mode === "web" ? [webCsp(env.VITE_ENGINE_URL, env.VITE_SUPABASE_URL || DEFAULT_SUPABASE_URL)] : []),
      // Bundle size report to Codecov: CI only (needs CODECOV_TOKEN), always the last plugin.
      codecovVitePlugin({
        enableBundleAnalysis: Boolean(process.env.CODECOV_TOKEN),
        bundleName: "radarx-desktop",
        uploadToken: process.env.CODECOV_TOKEN,
      }),
    ],
    define: {
      __APP_VERSION__: JSON.stringify(appVersion),
      // UI tests and offline-only builds skip the account gate; releases never set this.
      __AUTH_DISABLED__: JSON.stringify(process.env.RADARX_AUTH_DISABLED === "1"),
    },
    clearScreen: false,
    server: {
      port: 1420,
      strictPort: true,
    },
    envPrefix: ["VITE_", "TAURI_"],
  };
});
