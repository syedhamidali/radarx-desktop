import test from 'node:test';
import assert from 'node:assert/strict';
import { scrubLine, scrubLines, shortenPath, buildDiagnosticsText, DIAGNOSTICS_MAX } from './reportScrub.ts';

const ctx = { home: '/Users/ada', user: 'ada', host: 'Adas-MacBook-Pro' };

test('paths under the home folder start with ~ and keep two segments', () => {
  assert.equal(shortenPath('/Users/ada/Git/radarx/python/engine/reader.py', ctx), '~/.../engine/reader.py');
  assert.equal(shortenPath('/Users/ada/Documents', ctx), '~/Documents');
  assert.equal(shortenPath('/Users/ada', ctx), '~');
  assert.equal(shortenPath('/Users/ada/', ctx), '~');
  assert.equal(shortenPath('/home/grace/projects/x/y.py'), '~/.../x/y.py');
  assert.equal(shortenPath('/root/.config/radarx/a.log'), '~/.../radarx/a.log');
  assert.equal(shortenPath('~/Documents/radar/run.log'), '~/.../radar/run.log');
});

test('other absolute paths keep only the last two segments', () => {
  assert.equal(shortenPath('/opt/radarx/engine/lib/python3.12/site-packages/xradar/io/reader.py'), '/.../io/reader.py');
  assert.equal(shortenPath('/usr/lib'), '/usr/lib');
  assert.equal(shortenPath('/tmp/x.py'), '/tmp/x.py');
  assert.equal(shortenPath('C:\\Windows\\System32\\drivers\\etc\\hosts'), 'C:/.../etc/hosts');
  assert.equal(shortenPath('C:\\Users\\Grace\\Documents\\radar\\run.log'), '~/.../radar/run.log');
  assert.equal(shortenPath('\\\\fileserver\\share\\radar\\run.log'), '//[host]/.../radar/run.log');
});

test('the name of a data file is masked, a source or log file name is kept', () => {
  assert.equal(shortenPath('/Users/ada/Downloads/KTLX20130520_201643_V06.gz', ctx), '~/Downloads/[file].gz');
  assert.equal(shortenPath('/Users/ada/data/KTLX20130520_201643_V06', ctx), '~/data/[file]');
  assert.equal(shortenPath('/Users/ada/data/volume.nc', ctx), '~/data/[file].nc');
  assert.equal(shortenPath('/Users/ada/Git/app/engine.py', ctx), '~/.../app/engine.py');
  assert.equal(shortenPath('/Users/ada/Library/Logs/radarx.log', ctx), '~/.../Logs/radarx.log');
  assert.equal(shortenPath('/usr/lib/python3.12'), '/.../lib/python3.12'); // a version folder is not taken for a file
});

test('shortening twice changes nothing more', () => {
  for (const p of ['/Users/ada/Git/radarx/python/engine/reader.py', '/Users/ada/Downloads/KTLX20130520_201643_V06.gz', 'C:\\Users\\Grace\\a\\b\\c\\d.nc', '/opt/a/b/c/d.py']) {
    const once = shortenPath(p, ctx);
    assert.equal(shortenPath(once, ctx), once, p);
  }
});

test('a known home folder is replaced even outside a path', () => {
  assert.equal(scrubLine('cwd is /Users/ada now', ctx), 'cwd is ~ now');
  assert.equal(scrubLine('open /Users/ada/Downloads/a/b/KTLX20130520_201643_V06.gz failed', ctx), 'open ~/.../b/[file].gz failed');
  assert.equal(scrubLine('open C:\\Users\\Grace\\Downloads\\x.nc', { home: 'C:\\Users\\Grace' }), 'open ~/Downloads/[file].nc');
  assert.equal(scrubLine('open c:\\users\\grace\\Downloads\\x.nc', { home: 'C:\\Users\\Grace' }), 'open ~/Downloads/[file].nc');
});

test('paths in tracebacks and messages', () => {
  assert.equal(scrubLine('File "/Users/ada/Git/radarx/python/engine/reader.py", line 12, in open', ctx), 'File "~/.../engine/reader.py", line 12, in open');
  assert.equal(scrubLine('at (/Users/ada/x/y/z/w.py:12) done.', ctx), 'at (~/.../z/w.py:12) done.');
  assert.equal(scrubLine('"C:\\Program Files\\radarx\\engine\\x.dll" missing'), '"C:/.../engine/x.dll" missing');
  assert.equal(scrubLine('could not open /Users/ada/Downloads/KTLX20130520_201643_V06.gz: bad magic', ctx), 'could not open ~/Downloads/[file].gz: bad magic');
  assert.equal(scrubLine('see /etc/hosts.', {}), 'see /etc/hosts.');
});

test('no path of the home folder or a file name survives in any shape', () => {
  const inputs = [
    '/Users/ada/Downloads/KTLX20130520_201643_V06.gz',
    'failed /Volumes/data/storm 2013/KTLX20130520_201643_V06 now',
    'cannot open KTLX20130520_201643_V06.gz now',
    'cannot open KTLX20130520_201643_V06 now',
    'read data/volume.nc ok',
    "FileNotFoundError: [Errno 2] No such file or directory: '/Users/ada/Desktop/hurricane ian/KTBW20220928_190000_V06'",
    'C:\\Users\\Grace\\Desktop\\cases\\KTBW20220928_190000_V06.gz',
  ];
  for (const i of inputs) {
    const o = scrubLine(i, ctx);
    assert.equal(/KT[A-Z]{2}\d{8}/.test(o), false, o);
    assert.equal(/ada|Grace|volume\.nc/.test(o), false, o);
  }
});

test('e-mail addresses and user@host forms', () => {
  assert.equal(scrubLine('contact ada@example.org or grace.h+x@mail.example.co.uk'), 'contact [email] or [email]');
  assert.equal(scrubLine('ada@Adas-MacBook-Pro ~ %'), '[email] ~ %');
  assert.equal(scrubLine('@property decorators and a@ b stay'), '@property decorators and a@ b stay');
});

test('JWT-like strings, bearer values and key=value secrets', () => {
  const jwt = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c';
  const o = scrubLine(`session ${jwt} ok`);
  assert.equal(o, 'session [token] ok');
  assert.equal(scrubLine('x-auth: Bearer abcdefghijklmnop12345').includes('abcdefghij'), false);
  assert.equal(scrubLine('token=abc123 password: "hunter2" api_key=XYZ secret = s3cr3t'), 'token=[removed] password: [removed] api_key=[removed] secret = [removed]');
  assert.equal(scrubLine('{"access_token": "abc.def.ghi", "n": 1}').includes('abc.def'), false);
  assert.equal(scrubLine('GET /x?token=abc123&page=2').includes('abc123'), false);
  assert.equal(scrubLine('GET /x?token=abc123&page=2').includes('page=2'), true);
  assert.equal(scrubLine('sb_publishable_PiB4d1KKf-BEMEIXS5t6AQ_WfLZXlbA'), '[token]');
  assert.equal(scrubLine('ghp_abcdefghijklmnopqrstuvwxyz0123456789').includes('abcdefghij'), false);
});

test('long hex strings and long base64-like strings, but not ordinary words', () => {
  assert.equal(scrubLine('sha deadbeefdeadbeefdeadbeefdeadbeefdeadbeef end'), 'sha [hex] end');
  assert.equal(scrubLine('b64 QWxhZGRpbjpvcGVuIHNlc2FtZQQWxhZGRpbjpvcGVuIHNlc2FtZQ12 end'), 'b64 [token] end');
  const ident = 'a_very_long_function_name_without_any_digits_in_it_at_all';
  assert.equal(scrubLine(`call ${ident}()`), `call ${ident}()`);
  assert.equal(scrubLine('uuid 3f2b6c1e-8a4d-4b7e-9f10-0123456789ab'), 'uuid 3f2b6c1e-8a4d-4b7e-9f10-0123456789ab'.replace('0123456789ab', '0123456789ab'));
});

test('IP addresses, v4 and v6, but not times or versions', () => {
  assert.equal(scrubLine('connect 192.168.1.20:8080 and 10.0.0.1'), 'connect [ip]:8080 and [ip]');
  assert.equal(scrubLine('peer 2001:db8::1 and fe80::1 and 2001:db8:aaaa:bbbb:1:2:3:4 and ::1'), 'peer [ip] and [ip] and [ip] and [ip]');
  assert.equal(scrubLine('at 12:30:45 on 2026-10-24T12:30:45Z version 0.3.0 build 1.2.3'), 'at 12:30:45 on 2026-10-24T12:30:45Z version 0.3.0 build 1.2.3');
  assert.equal(scrubLine('ratio 3:1 and 16:9'), 'ratio 3:1 and 16:9');
});

test('machine and user names: the ones we are told, and .local style names', () => {
  assert.equal(scrubLine('on Adas-MacBook-Pro as ada', ctx), 'on [host] as [user]');
  assert.equal(scrubLine('adapter adapted', ctx), 'adapter adapted'); // not whole words
  assert.equal(scrubLine('ada_lovelace', ctx), '[user]_lovelace');
  assert.equal(scrubLine('host mac.local and printer.lan and srv.corp'), 'host [host] and [host] and [host]');
  assert.equal(scrubLine('hi', { user: 'hi' }), 'hi'); // too short to be safe to replace
});

test('control characters become spaces and long lines are cut', () => {
  assert.equal(scrubLine('a\u0000b\u0007c\u001bd'), 'a b c d');
  const long = scrubLine('x '.repeat(500));
  assert.ok(long.length <= 310 && long.endsWith('[cut]'));
});

test('scrubLines keeps the last 200 lines, drops empty ones, splits embedded newlines', () => {
  const lines = Array.from({ length: 300 }, (_, i) => `line ${i}`);
  const out = scrubLines(lines, {}, 200);
  assert.equal(out.length, 200);
  assert.equal(out[0], 'line 100');
  assert.equal(out[199], 'line 299');
  assert.deepEqual(scrubLines(['a\nb', '', '   ', 'c'], {}), ['a', 'b', 'c']);
});

test('the diagnostics text lists version, system, engine and scrubbed engine messages', () => {
  const t = buildDiagnosticsText({ appVersion: '0.3.0', os: 'macOS arm64 (desktop)', engineVersion: null, engineLines: ['10:00:01 error: cannot open /Users/ada/data/KTLX20130520_201643_V06.gz for ada@example.org'] }, ctx);
  assert.match(t, /^app: 0\.3\.0\nsystem: macOS arm64 \(desktop\)\nengine: version not reported\nengine messages this session \(last 1\):\n/);
  assert.ok(t.includes('~/data/[file].gz'));
  assert.equal(/ada|KTLX/.test(t.replace('radarx', '')), false);
  assert.match(buildDiagnosticsText({ appVersion: '1', os: 'Web' }), /engine messages this session: none/);
  assert.match(buildDiagnosticsText({ appVersion: '1', os: 'Web', engineVersion: '2.0' }), /engine: 2\.0/);
});

test('the diagnostics never exceed the server limit, keeping the newest lines', () => {
  const lines = Array.from({ length: 200 }, (_, i) => `msg ${i} ` + 'z'.repeat(290));
  const t = buildDiagnosticsText({ appVersion: '0.3.0', os: 'Linux (desktop)', engineLines: lines }, {});
  assert.ok(t.length <= DIAGNOSTICS_MAX);
  assert.ok(t.includes('msg 199'));
  assert.ok(t.includes('[older lines cut]'));
});

test('a mix of everything at once (the machine in the field)', () => {
  const raw = [
    'ERROR session eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.abcdefghijklmnop for ada@example.org from 203.0.113.9',
    'Traceback: File "/Users/ada/Git/radarx/python/engine/reader.py", line 99',
    'open("/Users/ada/Desktop/KTLX20130520_201643_V06.gz") on Adas-MacBook-Pro.local',
  ].join('\n');
  const o = scrubLine(raw.split('\n')[0], ctx) + scrubLine(raw.split('\n')[1], ctx) + scrubLine(raw.split('\n')[2], ctx);
  for (const bad of ['ada', 'eyJ', '203.0.113.9', 'KTLX', 'Adas', 'example.org']) assert.equal(o.includes(bad), false, bad);
});
