// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
//
// Design tokens, both themes:
//  * no component hard-codes a colour (scripts/check-colors.mjs; genuine data colours are
//    allow-listed in scripts/color-allowlist.json),
//  * every var(--token) a component uses exists,
//  * every colour token of "Mission control" (dark) either has a "Clean room" (light) value or is
//    one of the few that are the same in both (the always-dark 3D scene, geometry, motion).
import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const read = (p) => readFileSync(join(root, p), 'utf8');

function* svelteFiles(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) yield* svelteFiles(p);
    else if (name.endsWith('.svelte')) yield p;
  }
}

test('no hard-coded colours in the components (allow-list: scripts/color-allowlist.json)', () => {
  let out = '';
  try {
    out = execFileSync(process.execPath, [join(root, 'scripts', 'check-colors.mjs'), '--json'], { encoding: 'utf8' });
  } catch (e) {
    out = e.stdout?.toString() ?? '';
  }
  const res = JSON.parse(out);
  assert.ok(res.scanned > 50, 'the script scanned the components');
  const list = res.findings.slice(0, 12).map((f) => `${f.file}:${f.line} ${f.text}`).join('\n');
  assert.equal(res.count, 0, `hard-coded colours (use a token, or allow-list a data colour):\n${list}`);
});

const tokens = read('src/lib/styles/tokens.css');
const theme = read('src/lib/styles/theme.css');
const darkBlock = tokens.slice(tokens.indexOf(':root {'), tokens.indexOf(":root[data-theme='light']"));
const lightBlock = tokens.slice(tokens.indexOf(":root[data-theme='light']"));
const defs = (block) => new Set([...block.matchAll(/^\s*(--[a-zA-Z0-9-]+)\s*:/gm)].map((m) => m[1]));
const dark = defs(darkBlock);
const light = defs(lightBlock);

test('every var(--token) used by a component is defined', () => {
  const known = new Set([...dark, ...defs(theme)]);
  const missing = new Map();
  for (const file of svelteFiles(join(root, 'src'))) {
    const src = readFileSync(file, 'utf8');
    // custom properties a component sets itself (<style> or style="--x: ...") are fine
    const local = new Set([...src.matchAll(/(--[a-zA-Z0-9-]+)\s*:/g)].map((m) => m[1]));
    for (const m of src.matchAll(/var\((--[a-zA-Z0-9-]+)/g)) {
      if (!known.has(m[1]) && !local.has(m[1])) missing.set(`${file.slice(root.length + 1)} ${m[1]}`, 1);
    }
  }
  assert.deepEqual([...missing.keys()], [], 'undefined tokens');
});

test('both themes define every colour token, except the ones that do not change', () => {
  // the same in both themes on purpose: the polar 3D scene is dark in both, and the scene text on it
  const sameInBoth = /^--c-(scene-|line-rgb$)/;
  const colourToken = /^--c-/;
  const onlyDark = [...dark].filter((t) => colourToken.test(t) && !light.has(t) && !sameInBoth.test(t));
  // tokens that are identical by design (typography, geometry) are not --c-* and are not listed here;
  // --c-panel-solid etc. must be redefined for the light theme
  assert.deepEqual(onlyDark, [], 'colour tokens without a Clean room value');
  for (const t of ['--c-ink-rgb', '--c-shadow-rgb', '--c-on-accent', '--c-series-1', '--c-series-8']) {
    assert.ok(dark.has(t) && light.has(t), `${t} in both themes`);
  }
});

test('text on the accent keeps AA contrast in both themes (accent vs on-accent)', () => {
  const val = (block, name) => new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`).exec(block)?.[1];
  const lum = (hex) => {
    const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  };
  const ratio = (a, b) => {
    const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
    return (x + 0.05) / (y + 0.05);
  };
  for (const [name, block] of [['dark', darkBlock], ['light', lightBlock]]) {
    const accent = val(block, '--c-accent');
    const on = val(block, '--c-on-accent');
    assert.ok(accent && on, `${name}: accent and on-accent are hex`);
    assert.ok(ratio(accent, on) >= 4.5, `${name}: ${accent} / ${on} = ${ratio(accent, on).toFixed(2)}`);
    // series colours are lines on the panel surface: at least 3:1
    const surface = val(block, '--c-surface-1') ?? val(darkBlock, '--c-surface-1');
    for (let i = 1; i <= 8; i++) {
      const s = val(block, `--c-series-${i}`);
      assert.ok(s && ratio(s, surface) >= 3, `${name}: series ${i} ${s} on ${surface} = ${s ? ratio(s, surface).toFixed(2) : '?'}`);
    }
  }
});
