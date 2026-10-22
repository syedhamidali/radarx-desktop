// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  parseRemoteAddress,
  engineAuthFrame,
  cleanToken,
  classifyClose,
  remoteStatusText,
  REMOTE_HOSTS,
  MAX_LINK_RETRIES,
  MAX_BUSY_RETRIES,
} from './remoteEngine.ts';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const read = (p) => readFileSync(join(root, p), 'utf8');
// the code without its comments (a comment may say what the code does not do)
const code = (p) => read(p).replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '').replace(/<!--[\s\S]*?-->/g, '');

test('the forwarded loopback address is accepted, with the url the socket uses', () => {
  const a = parseRemoteAddress('127.0.0.1:8765');
  assert.equal(a.ok, true);
  assert.deepEqual(a.value, { host: '127.0.0.1', port: 8765, hostPort: '127.0.0.1:8765', url: 'ws://127.0.0.1:8765' });
  const b = parseRemoteAddress('  LocalHost:50000 ');
  assert.equal(b.value.url, 'ws://localhost:50000');
  assert.equal(parseRemoteAddress('ws://127.0.0.1:1024').value.port, 1024);
  assert.equal(parseRemoteAddress('127.0.0.1:65535').value.port, 65535);
});

test('only loopback hosts are accepted: nothing that would reach another computer', () => {
  for (const bad of [
    '10.0.0.5:8765', '192.168.1.2:8765', 'login.cluster.example.org:8765', '0.0.0.0:8765', '[::1]:8765', '::1',
    'example.com:8765', '127.0.0.1.evil.example:8765', 'localhost.evil.example:8765', '127.0.0.2:8765', '8.8.8.8:80',
  ]) {
    assert.equal(parseRemoteAddress(bad).ok, false, bad);
  }
});

test('the port must be a plain number from 1024 to 65535', () => {
  for (const bad of ['127.0.0.1', '127.0.0.1:', '127.0.0.1:0', '127.0.0.1:80', '127.0.0.1:1023', '127.0.0.1:65536', '127.0.0.1:99999',
    '127.0.0.1:-1', '127.0.0.1:12.5', '127.0.0.1:0x1F90', '127.0.0.1:1e4', '127.0.0.1:123456', '127.0.0.1:abc', ':8765']) {
    assert.equal(parseRemoteAddress(bad).ok, false, bad);
  }
});

test('no path, query, user name or token can ride along in the address', () => {
  for (const bad of [
    '127.0.0.1:8765/path', '127.0.0.1:8765?token=abc', '127.0.0.1:8765#x', 'user:pw@127.0.0.1:8765', 'tok@127.0.0.1:8765',
    '127.0.0.1:8765 extra', 'wss://127.0.0.1:8765', 'http://127.0.0.1:8765', 'ws://127.0.0.1:8765/?token=abc',
  ]) {
    assert.equal(parseRemoteAddress(bad).ok, false, bad);
  }
  const ok = parseRemoteAddress('127.0.0.1:8765').value;
  assert.doesNotMatch(ok.url, /[?#@]/);
  assert.equal(new URL(ok.url).search, '');
});

test('non-strings and empty input are refused with a sentence', () => {
  for (const bad of [undefined, null, 5, {}, '', '   ']) {
    const r = parseRemoteAddress(bad);
    assert.equal(r.ok, false);
    assert.match(r.error, /address/i);
  }
});

test('every refusal tells the user what to enter instead', () => {
  assert.match(parseRemoteAddress('cluster.example.org:8765').error, /127\.0\.0\.1 or localhost/);
  assert.match(parseRemoteAddress('127.0.0.1:80').error, /1024 to 65535/);
});

test('the first message carries the token and nothing the engine could take for a request', () => {
  const frame = JSON.parse(engineAuthFrame('abc_DEF-123'));
  assert.deepEqual(frame, { type: 'engine_auth', token: 'abc_DEF-123' });
});

test('a token is accepted only as printed: 16+ url-safe characters, whitespace trimmed', () => {
  assert.equal(cleanToken('  Zb-vy_ALayDP5lmkQtBhsTabskqY23pTeUA1Px1za9c \n'), 'Zb-vy_ALayDP5lmkQtBhsTabskqY23pTeUA1Px1za9c');
  for (const bad of ['', 'short', 'has space inside the token here', 'quote"inside_the_token_xx', null, 12345678901234567890]) {
    assert.equal(cleanToken(bad), null, String(bad));
  }
});

test('a wrong token (1008) stops at once, forgets the token and never retries', () => {
  const v = classifyClose(1008, false, 0, '127.0.0.1:8765');
  assert.equal(v.phase, 'refused');
  assert.equal(v.retry, false);
  assert.equal(v.forgetToken, true);
  assert.equal(classifyClose(1008, true, 5).retry, false);
});

test('a dropped link retries a bounded number of times with growing waits, then gives up', () => {
  let attempt = 0;
  let last = 0;
  for (;;) {
    const v = classifyClose(1006, true, attempt, '127.0.0.1:8765');
    if (!v.retry) {
      assert.equal(v.phase, 'unreachable');
      assert.match(v.message, /rxd serve/);
      break;
    }
    assert.equal(v.phase, 'lost');
    assert.ok(v.delayMs >= last && v.delayMs <= 8000);
    assert.equal(v.forgetToken, false); // reconnects re-use the token held in memory
    last = v.delayMs;
    attempt++;
    assert.ok(attempt <= MAX_LINK_RETRIES + 1, 'must not loop');
  }
  assert.equal(attempt, MAX_LINK_RETRIES);
});

test('a first connection that cannot be made says connecting, then unreachable', () => {
  assert.equal(classifyClose(1006, false, 0).phase, 'connecting');
  assert.equal(classifyClose(1006, false, MAX_LINK_RETRIES).phase, 'unreachable');
});

test('a busy engine (1013) is retried a few times, then the user is told what to do', () => {
  assert.equal(classifyClose(1013, false, 0).retry, true);
  const last = classifyClose(1013, false, MAX_BUSY_RETRIES);
  assert.equal(last.retry, false);
  assert.equal(last.phase, 'busy');
  assert.match(last.message, /Another app/);
});

test('an engine that stopped or timed out (1000/1001) is not chased', () => {
  for (const code of [1000, 1001]) {
    const v = classifyClose(code, true, 0);
    assert.equal(v.phase, 'ended');
    assert.equal(v.retry, false);
    assert.equal(v.forgetToken, true); // its token died with it
  }
});

test('the status text always says REMOTE ENGINE with the address, and the built-in engine says neither', () => {
  assert.equal(remoteStatusText('connected', '127.0.0.1:8765'), 'REMOTE ENGINE 127.0.0.1:8765');
  assert.match(remoteStatusText('lost', '127.0.0.1:8765'), /REMOTE ENGINE 127\.0\.0\.1:8765 \(link lost/);
  assert.equal(remoteStatusText('off', null), 'Built-in engine');
});

test('no message or state text ever contains the token', () => {
  const token = 'SECRET-TOKEN-0123456789abcdef';
  for (const code of [1000, 1001, 1006, 1008, 1013]) {
    for (const attempt of [0, 3, 20]) {
      const v = classifyClose(code, true, attempt, '127.0.0.1:8765');
      assert.equal(JSON.stringify(v).includes(token), false);
    }
  }
});

test('the token is not stored: the remote-engine code never writes it to storage, settings, a url or the console', () => {
  const pure = code('src/lib/utils/remoteEngine.ts');
  const store = code('src/lib/stores/remoteEngine.ts');
  const ui = code('src/lib/components/EngineSettings.svelte');
  const ws = code('src/lib/utils/websocket.ts');
  for (const [name, text] of [['utils', pure], ['store', store], ['settings ui', ui]]) {
    assert.doesNotMatch(text, /localStorage|sessionStorage|indexedDB|document\.cookie/, name);
    assert.doesNotMatch(text, /console\.(log|info|warn|error|debug)/, name);
  }
  assert.doesNotMatch(store, /token/i, 'the store must not even have a token field');
  assert.doesNotMatch(ui, /settings\.updateSetting\(/, 'the Engine tab writes nothing to the settings file');
  // the manager sends it once, as a frame, and never logs or interpolates it
  assert.doesNotMatch(ws, /console\.[a-z]+\([^\n]*remoteToken/);
  assert.doesNotMatch(ws, /\$\{[^}]*remoteToken/);
  assert.doesNotMatch(ws, /new WebSocket\([^)]*token/i);
  assert.doesNotMatch(ws, /localStorage[^\n]*remoteToken|remoteToken[^\n]*localStorage/);
});

test('the settings file has no place for the token', () => {
  const settings = read('src/lib/stores/settings.ts');
  assert.doesNotMatch(settings, /engine_?token|remoteToken|remote_?engine/i);
});

test('the built-in engine handshake token is never sent to a remote engine', () => {
  const ws = read('src/lib/utils/websocket.ts');
  assert.match(ws, /this\.engineMode === 'remote' \? null/);
});

test('the desktop CSP lets the app open web sockets to this computer only', () => {
  const csp = JSON.parse(read('src-tauri/tauri.conf.json')).app.security.csp;
  const connect = csp.split(';').map((s) => s.trim()).find((s) => s.startsWith('connect-src ')).split(/\s+/).slice(1);
  const sockets = connect.filter((x) => /^wss?:/.test(x));
  assert.deepEqual(sockets.sort(), ['ws://127.0.0.1:*', 'ws://localhost:*']);
  assert.ok(!connect.includes('*') && !connect.includes('ws:') && !connect.includes('wss:'));
  // and the address check accepts no host the CSP would not allow
  assert.deepEqual([...REMOTE_HOSTS].sort(), ['127.0.0.1', 'localhost']);
});
