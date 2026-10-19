// Production build + the window.__xr test hook (scripts/perf/xr_hook.ts), written to dist-perf/.
//   npx vite build -c scripts/perf/vite.perf.config.ts
// Used by measure_runtime.py so the benchmark exercises the real minified, code-split bundle.
import { mergeConfig, type Plugin } from "vite";
import { resolve } from "node:path";
import base from "../../vite.config";

const hook = resolve(__dirname, "xr_hook.ts");

function injectHook(): Plugin {
  return {
    name: "xr-perf-hook",
    enforce: "pre",
    transform(code, id) {
      if (id.endsWith("/src/main.ts")) return `import ${JSON.stringify(hook)};\n${code}`;
    },
  };
}

export default (env: any) =>
  mergeConfig((base as any)(env), {
    build: { outDir: "dist-perf", emptyOutDir: true },
    plugins: [injectHook()],
  });
