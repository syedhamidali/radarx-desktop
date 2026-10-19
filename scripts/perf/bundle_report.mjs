// Summarise a build: per output file raw/gzip size, and (with dist-analyze/stats.json from
// vite.analyze.config.ts) the biggest source modules per chunk.
//   node scripts/perf/bundle_report.mjs [distDir=dist] [--modules]
import { readdirSync, readFileSync, statSync, existsSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { join } from "node:path";

const args = process.argv.slice(2);
const dir = args.find((a) => !a.startsWith("--")) ?? "dist";
const showModules = args.includes("--modules");
const kb = (n) => (n / 1024).toFixed(1).padStart(8);

function walk(d) {
  return readdirSync(d).flatMap((f) => {
    const p = join(d, f);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

let totalJs = 0, totalJsGz = 0, totalCss = 0, totalCssGz = 0, totalAll = 0;
const rows = [];
for (const p of walk(dir)) {
  if (p.endsWith("stats.json")) continue;
  const buf = readFileSync(p);
  const gz = /\.(js|css|html|svg|json)$/.test(p) ? gzipSync(buf).length : null;
  rows.push([p.slice(dir.length + 1), buf.length, gz]);
  totalAll += buf.length;
  if (p.endsWith(".js")) { totalJs += buf.length; totalJsGz += gz; }
  if (p.endsWith(".css")) { totalCss += buf.length; totalCssGz += gz; }
}
rows.sort((a, b) => b[1] - a[1]);
console.log("file".padEnd(60), "raw KB".padStart(9), "gzip KB".padStart(9));
for (const [f, raw, gz] of rows) console.log(f.padEnd(60), kb(raw), gz == null ? "".padStart(9) : kb(gz));
console.log("-".repeat(80));
console.log("JS total".padEnd(60), kb(totalJs), kb(totalJsGz));
console.log("CSS total".padEnd(60), kb(totalCss), kb(totalCssGz));
console.log("everything (incl. fonts/images)".padEnd(60), kb(totalAll));

const statsPath = join(dir, "stats.json");
if (showModules && existsSync(statsPath)) {
  const s = JSON.parse(readFileSync(statsPath, "utf8"));
  const parts = s.nodeParts, metas = s.nodeMetas;
  const perChunk = new Map();
  for (const m of Object.values(metas)) {
    for (const [chunk, uid] of Object.entries(m.moduleParts)) {
      const part = parts[uid];
      const id = m.id.replace(/^.*\/node_modules\//, "node_modules/").replace(/^.*\/src\//, "src/");
      const lib = id.startsWith("node_modules/") ? id.split("/").slice(0, id.startsWith("node_modules/@") ? 3 : 2).join("/") : id;
      const c = perChunk.get(chunk) ?? new Map();
      const e = c.get(lib) ?? [0, 0];
      e[0] += part.renderedLength; e[1] += part.gzipLength;
      c.set(lib, e);
      perChunk.set(chunk, c);
    }
  }
  for (const [chunk, c] of perChunk) {
    console.log(`\n== ${chunk}`);
    for (const [k, [r, g]] of [...c].sort((a, b) => b[1][0] - a[1][0]).slice(0, 25))
      console.log(k.padEnd(70), kb(r), kb(g));
  }
}
