// One-off bundle analysis (not part of the app build, not a dependency of the project):
//   npm i --no-save rollup-plugin-visualizer && npx vite build -c scripts/perf/vite.analyze.config.ts
// Writes dist-analyze/stats.json (raw + gzip per module); scripts/perf/bundle_report.mjs summarises it.
import { mergeConfig } from "vite";
import base from "../../vite.config";
import { visualizer } from "rollup-plugin-visualizer";

export default (env: any) =>
  mergeConfig((base as any)(env), {
    build: { outDir: "dist-analyze", emptyOutDir: true },
    plugins: [visualizer({ filename: "dist-analyze/stats.json", template: "raw-data", gzipSize: true })],
  });
