// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
//
// File names shown in tabs, toasts, the status bar and the recent list come from a path. The
// desktop app on Windows hands over backslash paths (C:\Users\me\KTLX20130520_201643_V06), and
// splitting on '/' only showed the whole path as the "name".
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { basename } from './env.ts';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');

test('basename handles posix, windows, UNC and URL paths', () => {
  assert.equal(basename('/data/radar/KTLX20130520_201643_V06.gz'), 'KTLX20130520_201643_V06.gz');
  assert.equal(basename('C:\\Users\\me\\radar\\KTLX20130520_201643_V06'), 'KTLX20130520_201643_V06');
  assert.equal(basename('\\\\server\\share\\scan.nc'), 'scan.nc');
  assert.equal(basename('s3://unidata-nexrad-level2/2013/05/20/KTLX/KTLX20130520_201643_V06.gz'), 'KTLX20130520_201643_V06.gz');
  assert.equal(basename('scan.nc'), 'scan.nc');
  assert.equal(basename('/data/radar/'), 'radar');
  assert.equal(basename(''), '');
});

function* sources(dir) {
  for (const n of readdirSync(dir)) {
    const p = join(dir, n);
    if (statSync(p).isDirectory()) yield* sources(p);
    else if (/\.(ts|svelte)$/.test(n) && !/\.test\./.test(n)) yield p;
  }
}

test("no source file derives a file name with split('/').pop() (use basename from utils/env)", () => {
  const offenders = [];
  for (const f of sources(join(root, 'src'))) {
    if (/split\(\s*['"]\/['"]\s*\)\s*\.pop\(\)/.test(readFileSync(f, 'utf8'))) offenders.push(f.slice(root.length + 1));
  }
  assert.deepEqual(offenders, []);
});
