// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
//
// The software names its maker and its website in ONE module (brand.ts). The repository may be private,
// so nothing a user can see or follow may point at GitHub, and no source file names a person.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  WEBSITE_URL,
  COMPANY,
  PRODUCT_CREDIT,
  DOWNLOAD_PAGE_URL,
  TERMS_URL,
  PRIVACY_URL,
  XRADAR_DOCS_URL,
} from './brand.ts';
import { SHARE_URL } from './share.ts';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const srcDir = join(root, 'src');

// Third-party attribution links we are obliged to show. Each entry is a file and the exact URL it may hold.
const ALLOWED_GITHUB = [
  { file: 'src/lib/utils/terrain.ts', url: 'https://github.com/tilezen/joerd/blob/master/docs/attribution.md' },
];

function sourceFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...sourceFiles(p));
    else if (/\.(ts|js|mjs|svelte|html|css|json)$/.test(name) && !/\.test\.(mjs|ts|js)$/.test(name)) out.push(p);
  }
  return out;
}

const files = sourceFiles(srcDir).map((p) => ({ rel: relative(root, p).split(sep).join('/'), text: readFileSync(p, 'utf8') }));

test('the scan sees the sources', () => {
  assert.ok(files.length > 50, `only ${files.length} files scanned`);
  assert.ok(files.some((f) => f.rel === 'src/lib/utils/brand.ts'));
});

test('no source links to github.com except the listed attributions', () => {
  const offenders = [];
  for (const f of files) {
    for (const m of f.text.matchAll(/https?:\/\/[^\s'"`)>]*github\.com[^\s'"`)>]*|github\.com[^\s'"`)>]*/gi)) {
      const ok = ALLOWED_GITHUB.some((a) => a.file === f.rel && m[0].replace(/^https?:\/\//, '') === a.url.replace(/^https?:\/\//, ''));
      if (!ok) offenders.push(`${f.rel}: ${m[0]}`);
    }
  }
  assert.deepEqual(offenders, []);
});

test('the allow-list is exact: every entry is still present (no stale exemptions)', () => {
  for (const a of ALLOWED_GITHUB) {
    const f = files.find((x) => x.rel === a.file);
    assert.ok(f && f.text.includes(a.url), `${a.file} no longer holds ${a.url}`);
  }
});

test('no source file names a person', () => {
  const offenders = files.filter((f) => /hamid|syed/i.test(f.text)).map((f) => f.rel);
  assert.deepEqual(offenders, []);
});

test('the brand constants', () => {
  assert.equal(WEBSITE_URL, 'https://radarx.labshope.com');
  assert.equal(COMPANY, 'LabShope Technologies');
  assert.equal(PRODUCT_CREDIT, 'Radarx Desktop by LabShope Technologies');
  assert.equal(DOWNLOAD_PAGE_URL, 'https://radarx.labshope.com/#download');
  assert.equal(TERMS_URL, 'https://radarx.labshope.com/terms.html');
  assert.equal(PRIVACY_URL, 'https://radarx.labshope.com/privacy.html');
  assert.equal(SHARE_URL, WEBSITE_URL);
  assert.ok(XRADAR_DOCS_URL.startsWith('https://docs.openradarscience.org/'));
  for (const u of [WEBSITE_URL, DOWNLOAD_PAGE_URL, TERMS_URL, PRIVACY_URL]) assert.ok(u.startsWith('https://'), u);
});

test('the About dialog, sign-up gate and share links use the brand module', () => {
  const read = (rel) => files.find((f) => f.rel === rel)?.text ?? '';
  const about = read('src/lib/components/AboutDialog.svelte');
  assert.match(about, /PRODUCT_CREDIT/);
  assert.match(about, /openLink\(WEBSITE_URL\)/);
  assert.ok(!/GitHub/i.test(about));
  const gate = read('src/lib/components/AuthGate.svelte');
  assert.match(gate, /link\(TERMS_URL\)/);
  assert.match(gate, /link\(PRIVACY_URL\)/);
});
