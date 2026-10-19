#!/usr/bin/env node
/**
 * Lists hard-coded colours in the Svelte components.
 *
 * Every colour in the UI must come from a design token (src/lib/styles/tokens.css), so that both
 * themes ("Mission control" dark, "Clean room" light) and a custom accent colour work everywhere.
 * This script greps src/**\/*.svelte for colour literals -- #hex, rgb()/rgba()/hsl()/hsla() with
 * numbers, and the named colours white / black -- and prints each one with its file and line,
 * unless it is allow-listed in scripts/color-allowlist.json (genuine data colours: colormap
 * swatches, per-radar identity colours, class colours of a legend ...).
 *
 *   node scripts/check-colors.mjs            list the violations, exit 1 if there are any
 *   node scripts/check-colors.mjs --report   list them but always exit 0
 *   node scripts/check-colors.mjs --json     machine-readable
 *   npm run check:colors
 *
 * Not flagged: rgba(var(--c-accent-rgb), .2) and every other use of a token; #hex inside the
 * allow-listed `ignoreLines` patterns; entities such as &#9650;; Svelte blocks ({#if ...}); url(#id).
 * The token files themselves (src/lib/styles/*.css) are never scanned: they are where colours live.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const args = new Set(process.argv.slice(2));

/** A literal colour. Group 1 = the text matched. */
const PATTERNS = [
  // #rgb #rgba #rrggbb #rrggbbaa, not part of a word / entity / url fragment / Svelte block
  { kind: 'hex', re: /(?<![\w&{/-])(#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3}))(?![\w-])/g },
  // rgb(1, 2, 3) rgba(0,0,0,.5) rgb(${r}, ...) hsl(...) -- but not rgba(var(--token-rgb), .5)
  { kind: 'rgb', re: /\b((?:rgba?|hsla?)\(\s*(?!var\()[^)]*\))/g },
  // named colours used as a value
  { kind: 'name', re: /(?:color|background|fill|stroke|border[a-z-]*|outline|fillStyle|strokeStyle)\s*[:=]\s*['"]?\s*(white|black)\b/g },
];

function load() {
  try {
    return JSON.parse(readFileSync(join(root, 'scripts', 'color-allowlist.json'), 'utf8'));
  } catch {
    return { files: {}, matches: [] };
  }
}
const allow = load();

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) yield* walk(p);
    else if (name.endsWith('.svelte')) yield p;
  }
}

/** Is this finding allow-listed? */
function allowed(file, text, line) {
  const f = allow.files?.[file];
  if (f === 'all' || (f && f.all)) return true;
  for (const m of allow.matches ?? []) {
    if (m.file !== file) continue;
    if (m.text && m.text !== text) continue;
    if (m.line && !line.includes(m.line)) continue;
    if (m.lineRegex && !new RegExp(m.lineRegex).test(line.trim())) continue;
    return true;
  }
  return false;
}

const findings = [];
let scanned = 0;
for (const abs of walk(join(root, 'src'))) {
  const file = relative(root, abs).split('\\').join('/');
  const lines = readFileSync(abs, 'utf8').split('\n');
  scanned++;
  lines.forEach((line, i) => {
    for (const { kind, re } of PATTERNS) {
      re.lastIndex = 0;
      for (const m of line.matchAll(re)) {
        const text = m[1];
        if (text.includes('${')) continue; // computed from data at run time (colormap swatches ...), not a literal
        if (allowed(file, text, line)) continue;
        findings.push({ file, line: i + 1, kind, text, code: line.trim().slice(0, 110) });
      }
    }
  });
}

if (args.has('--json')) {
  console.log(JSON.stringify({ scanned, count: findings.length, findings }, null, 2));
} else {
  const byFile = new Map();
  for (const f of findings) byFile.set(f.file, [...(byFile.get(f.file) ?? []), f]);
  for (const [file, list] of [...byFile].sort((a, b) => b[1].length - a[1].length)) {
    console.log(`\n${file}  (${list.length})`);
    for (const f of list) console.log(`  ${String(f.line).padStart(5)}  ${f.kind.padEnd(4)} ${f.text.padEnd(22)} ${f.code}`);
  }
  console.log(`\n${findings.length} hard-coded colour${findings.length === 1 ? '' : 's'} in ${byFile.size} of ${scanned} .svelte files` +
    (findings.length ? '  (use a token from src/lib/styles/tokens.css, or allow-list a data colour in scripts/color-allowlist.json)' : '  -- clean'));
}
process.exit(findings.length && !args.has('--report') ? 1 : 0);
