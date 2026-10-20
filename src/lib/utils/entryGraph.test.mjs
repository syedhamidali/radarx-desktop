// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
//
// First-paint budget guard. Everything reachable from src/main.ts and src/App.svelte through STATIC imports is
// downloaded and parsed before the first paint (dynamic `import()` / `lazy(() => import(...))`
// chunks are not). docs/PERFORMANCE_FRONTEND.md describes what was moved out of that path; a single
// innocent `import { x } from '../stores/modules'` in an always-loaded file silently puts it all back
// (the updater did exactly that: it pulled the module, mosaic, wind and pipeline stores, the WebGL PPI
// renderer and the wind maths into the entry chunk, +64 KB raw / +20 KB gzip).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');

function resolveImport(from, spec) {
  if (!spec.startsWith('.')) return null; // packages are not what this guards
  const base = resolve(dirname(from), spec);
  for (const c of [base, base + '.ts', base + '.js', base + '.svelte', base + '.svelte.ts', join(base, 'index.ts')]) {
    if (existsSync(c) && /\.(ts|js|svelte)$/.test(c)) return c;
  }
  return null;
}

/** Static (non-type, non-dynamic) imports and re-exports of a source file. */
function staticImports(file) {
  let src = readFileSync(file, 'utf8');
  src = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  const out = [];
  const re = /(?:^|[\n;])\s*(?:import|export)\s+(type\s+)?(?:[^'";]*?\s+from\s+)?['"]([^'"]+)['"]/g;
  let m;
  while ((m = re.exec(src))) {
    if (m[1]) continue; // `import type` / `export type ... from` is erased
    // `import { type A } from` still loads the module when it also has a value import; the rare
    // all-type form is treated as a real import (conservative).
    const r = resolveImport(file, m[2]);
    if (r) out.push(r);
  }
  return out;
}

function closure(entry) {
  const seen = new Set();
  const stack = [entry];
  while (stack.length) {
    const f = stack.pop();
    if (seen.has(f)) continue;
    seen.add(f);
    for (const d of staticImports(f)) stack.push(d);
  }
  return [...seen].map((f) => relative(root, f));
}

// main.ts loads App.svelte with a dynamic import, so the app's first-paint code is App.svelte's closure.
const entry = [...new Set([...closure(join(root, 'src', 'main.ts')), ...closure(join(root, 'src', 'App.svelte'))])];

test('the first-paint path stays small: heavy modules are loaded on demand', () => {
  const heavy = [
    'src/lib/utils/ppiRenderer.ts',
    'src/lib/utils/cartesianRenderer.ts',
    'src/lib/utils/flowRenderer.ts',
    'src/lib/utils/volumeRenderer.ts',
    'src/lib/stores/modules.ts',
    'src/lib/stores/mosaic.ts',
    'src/lib/stores/wind.ts',
    'src/lib/stores/pipeline.ts',
    'src/lib/stores/qpe.ts',
    'src/lib/utils/wind.ts',
    'src/lib/components/RadarViewer.svelte',
    'src/lib/components/RadarViewer3D.svelte',
    'src/lib/components/ModulesPanel.svelte',
  ];
  const offenders = heavy.filter((h) => entry.includes(h));
  assert.deepEqual(offenders, [], `statically imported on the first-paint path: ${offenders.join(', ')}`);
});

test('the entry closure is found (the guard itself works)', () => {
  assert.ok(entry.includes('src/App.svelte'));
  assert.ok(entry.includes('src/lib/utils/websocket.ts'));
  assert.ok(entry.length > 40, `only ${entry.length} files reached`);
});
