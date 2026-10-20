// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
//
// The desktop app gets its Content-Security-Policy from src-tauri/tauri.conf.json, the web build from
// vite.config.ts. A tile server added to one and not the other works on the desktop and silently fails
// on the web (the realistic ground's satellite imagery and elevation tiles were blocked there).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');

const tauriCsp = JSON.parse(readFileSync(join(root, 'src-tauri/tauri.conf.json'), 'utf8')).app.security.csp;
const directive = (csp, name) => (csp.split(';').map((s) => s.trim()).find((s) => s.startsWith(name + ' ')) ?? '').split(/\s+/).slice(1);
const hosts = (list) => list.filter((x) => /^https:\/\//.test(x));

const vite = readFileSync(join(root, 'vite.config.ts'), 'utf8');
const tileHosts = [...vite.slice(vite.indexOf('const TILE_HOSTS'), vite.indexOf('].join', vite.indexOf('const TILE_HOSTS'))).matchAll(/"(https:\/\/[^"]+)"/g)].map((m) => m[1]);

// the account server: a variable in the web policy (a build can override it), a literal in the desktop one
const accountHost = /const DEFAULT_SUPABASE_URL = "(https:\/\/[^"]+)"/.exec(vite)?.[1];

test('the web CSP lists the tile hosts of the desktop CSP in connect-src and img-src', () => {
  assert.ok(tileHosts.length >= 4, 'TILE_HOSTS not found in vite.config.ts');
  assert.ok(accountHost, 'DEFAULT_SUPABASE_URL not found in vite.config.ts');
  // the hosts of the desktop policy: the tile servers (map providers, satellite, elevation) and the account server
  const desktopTiles = hosts(directive(tauriCsp, 'connect-src'));
  assert.deepEqual(desktopTiles.filter((h) => !tileHosts.includes(h) && h !== accountHost), []);
  const desktopImg = hosts(directive(tauriCsp, 'img-src')).filter((h) => /tile|eox|amazonaws/.test(h) && !/mapbox/.test(h));
  assert.deepEqual(desktopImg.filter((h) => !tileHosts.includes(h)), []);
  // and both web directives use the shared list
  assert.match(vite, /connect-src 'self' \$\{engine\} \$\{supabaseUrl\} \$\{TILE_HOSTS\}/);
  assert.match(vite, /img-src [^`]*\$\{TILE_HOSTS\}/);
});
