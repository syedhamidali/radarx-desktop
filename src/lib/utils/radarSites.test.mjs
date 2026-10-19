// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  parseSiteList,
  nexradSitesFrom,
  mergeCatalogue,
  isFetchable,
  scanState,
  RECENT_S,
  formatAge,
  formatBytes,
  liveAge,
  placeSites,
  nearestSite,
  NO_CONSENT,
  needsPrompt,
  resolveChoice,
  describeRequest,
  splitCsvLine,
} from './radarSites.ts';

const DOC = JSON.parse(readFileSync(new URL('../data/nexradSites.json', import.meta.url), 'utf8'));
const BUILTIN = nexradSitesFrom(DOC);
const IDS = new Set(BUILTIN.map((s) => s.id));

// ── Catalogue integrity ─────────────────────────────────────────────────────────────────────

test('catalogue: header cites the source, date and licence', () => {
  assert.match(DOC._comment, /ncei\.noaa\.gov/);
  assert.match(DOC._comment, /public domain/);
  assert.match(DOC.retrieved, /^\d{4}-\d{2}-\d{2}$/);
  assert.ok(DOC._comment.includes(DOC.retrieved));
});

test('catalogue: 156 sites, unique valid ids, sensible coordinates', () => {
  assert.equal(DOC.sites.length, 156);
  assert.equal(BUILTIN.length, 156, 'every record validates');
  assert.equal(IDS.size, 156, 'ids are unique');
  for (const s of BUILTIN) {
    assert.match(s.id, /^[A-Z0-9]{4}$/);
    assert.ok(s.lat >= -90 && s.lat <= 90, s.id);
    assert.ok(s.lon >= -180 && s.lon <= 180, s.id);
    assert.ok(s.name.length > 0, s.id);
    assert.equal(s.network, 'NEXRAD');
  }
});

test('catalogue: well-known sites are where they should be', () => {
  const by = Object.fromEntries(BUILTIN.map((s) => [s.id, s]));
  assert.ok(Math.abs(by.KTLX.lat - 35.333) < 0.01 && Math.abs(by.KTLX.lon + 97.278) < 0.01);
  assert.equal(by.KVWX.name, 'Evansville');
  // Guam is east of the antimeridian-adjacent longitude 144 E, Alaska is west of -140
  assert.ok(by.PGUA.lon > 140 && by.PAHG.lon < -140);
});

// ── User catalogues ─────────────────────────────────────────────────────────────────────────

test('parse: CSV with header, quotes, semicolons and a comment line', () => {
  const csv = '# my radars\nid;name;lat;lon;elev_m\nPAR1;"Paris, Trappes";48.77;2.01;167\nLYO1;Lyon;45.7;4.9;\n';
  const r = parseSiteList(csv, 'x.csv');
  assert.equal(r.format, 'csv');
  assert.equal(r.sites.length, 2);
  assert.equal(r.sites[0].name, 'Paris, Trappes');
  assert.equal(r.sites[0].elev_m, 167);
  assert.equal(r.sites[1].elev_m, null, 'a missing elevation stays missing, it is not 0');
  assert.ok(r.sites.every((s) => s.custom === true));
});

test('parse: JSON array and {"sites": [...]} with alias keys and a network', () => {
  const a = parseSiteList('[{"id":"A1","name":"Alpha","latitude":10,"longitude":20}]');
  assert.equal(a.sites[0].lat, 10);
  const b = parseSiteList(JSON.stringify({ network: 'DWD', sites: [{ site: 'ESS', lat: 51.4, lng: 6.97, elevation: 185 }] }));
  assert.equal(b.sites[0].network, 'DWD');
  assert.equal(b.sites[0].lon, 6.97);
  assert.equal(b.sites[0].name, 'ESS', 'the id stands in for a missing name');
});

test('parse: bad rows are reported, good rows kept, duplicates ignored', () => {
  const r = parseSiteList('id,name,lat,lon\nOK1,a,1,2\n,noid,1,2\nBAD,b,95,2\nBAD2,b,1,xx\nOK1,dup,3,4\nbad id,b,1,2\n', 'a.csv');
  assert.deepEqual(r.sites.map((s) => s.id), ['OK1']);
  assert.equal(r.rejected.length, 5);
  assert.ok(r.rejected.some((x) => /duplicate/.test(x.reason)));
  assert.ok(r.rejected.some((x) => /latitude/.test(x.reason)));
});

test('parse: unusable files give an error, never throw', () => {
  for (const [text, name] of [['', 'a.csv'], ['{nope', 'a.json'], ['id,lat,lon\n', 'a.csv'], ['{"x":1}', 'a.json'], ['id,name,lat,lon\nA,b,x,y\n', 'a.csv']]) {
    const r = parseSiteList(text, name);
    assert.ok(r.error, `${name}: ${text.slice(0, 12)}`);
    assert.equal(r.sites.length, 0);
  }
});

test('parse: longitudes above 180 wrap, markup in names stays plain text', () => {
  const r = parseSiteList('id,name,lat,lon\nX1,"<img src=x onerror=alert(1)>",0,190\n', 'a.csv');
  assert.equal(r.sites[0].lon, -170);
  assert.equal(r.sites[0].name, '<img src=x onerror=alert(1)>'); // rendered through text bindings only
});

test('splitCsvLine handles "" escapes', () => {
  assert.deepEqual(splitCsvLine('a,"b ""c"" d",e', ','), ['a', 'b "c" d', 'e']);
});

test('merge: a user site replaces a built-in one with the same id; built-ins can be hidden', () => {
  const mine = [{ id: 'kvwx', name: 'Mine', lat: 1, lon: 2, elev_m: null, custom: true, network: 'x' }];
  const all = mergeCatalogue(BUILTIN, mine, true);
  assert.equal(all.length, 156);
  assert.equal(all.find((s) => s.id.toUpperCase() === 'KVWX').name, 'Mine');
  assert.equal(mergeCatalogue(BUILTIN, mine, false).length, 1);
});

test('only built-in NEXRAD ids can be fetched', () => {
  assert.ok(isFetchable(BUILTIN[0], IDS));
  assert.ok(!isFetchable({ id: 'PAR1', name: '', lat: 0, lon: 0, elev_m: null, custom: true }, IDS));
});

// ── State, never invented ───────────────────────────────────────────────────────────────────

test('scanState: unknown unless an age is known', () => {
  for (const v of [undefined, null, NaN, -5, Infinity, '12']) assert.equal(scanState(v), 'unknown');
  assert.equal(scanState(0), 'recent');
  assert.equal(scanState(RECENT_S - 1), 'recent');
  assert.equal(scanState(RECENT_S), 'stale');
  assert.equal(scanState(86400), 'stale');
});

test('liveAge only exists for an ok reply and grows with the time since the reply', () => {
  const ok = { status: 'ok', station: 'KTLX', age_s: 120 };
  assert.equal(liveAge(ok, 1000, 1000), 120);
  assert.equal(liveAge(ok, 1000, 31000), 150);
  assert.equal(liveAge(ok, 5000, 1000), 120, 'a clock that went back does not make the scan younger');
  for (const bad of [undefined, { status: 'not_found', station: 'K' }, { status: 'offline', station: 'K' }, { status: 'ok', station: 'K' }]) {
    assert.equal(liveAge(bad, 0, 1e6), null);
  }
});

test('formatAge / formatBytes', () => {
  assert.equal(formatAge(42), '42 s');
  assert.equal(formatAge(7 * 60), '7 min');
  assert.equal(formatAge(3 * 3600 + 5 * 60), '3 h 05 min');
  assert.equal(formatAge(3 * 86400 + 4 * 3600), '3 d 4 h');
  assert.equal(formatAge(-1), 'unknown');
  assert.equal(formatBytes(14.2 * 1024 ** 2), '14.2 MB');
  assert.equal(formatBytes(undefined), 'unknown size');
});

// ── Placement ───────────────────────────────────────────────────────────────────────────────

const grid = (n, step) => Array.from({ length: n }, (_, i) => ({ id: `S${String(i).padStart(3, '0')}`, name: 'n', lat: 0, lon: i, elev_m: null }));
const proj = (k) => (lat, lon) => [100 + lon * k, 100 + lat];

test('placeSites: culls what is outside the view', () => {
  const sites = grid(10, 1);
  const out = placeSites(sites, proj(100), { width: 400, height: 200 });
  assert.ok(out.length < 10 && out.length > 0);
  assert.ok(out.every((p) => p.x > -20 && p.x < 420));
});

test('placeSites: zoomed out hides colliding labels, zoomed in shows all', () => {
  const sites = grid(40, 1);
  const tight = placeSites(sites, proj(6), { width: 600, height: 200 });
  const wide = placeSites(sites, proj(80), { width: 4000, height: 200 });
  const nTight = tight.filter((p) => p.label).length;
  const nWide = wide.filter((p) => p.label).length;
  assert.equal(tight.length, 40);
  assert.ok(nTight < tight.length / 2, `labels thinned (${nTight})`);
  assert.equal(nWide, wide.length, 'plenty of room: every label shown');
  // no two shown labels overlap (labels are to the right of their dot, glyph 6.5 px, 3 chars of "S001" = 4)
  const xs = tight.filter((p) => p.label).map((p) => p.x).sort((a, b) => a - b);
  for (let i = 1; i < xs.length; i++) assert.ok(xs[i] - xs[i - 1] >= 4 * 6.5 + 8 - 0.01);
});

test('placeSites: priority wins a contested label and pinned sites are always labelled', () => {
  const sites = grid(3, 1);
  const out = placeSites(sites, proj(10), { width: 300, height: 200, priority: (s) => (s.id === 'S001' ? 5 : 0), pinned: new Set(['S002']) });
  const lab = Object.fromEntries(out.map((p) => [p.site.id, p.label]));
  assert.equal(lab.S001, true);
  assert.equal(lab.S002, true);
});

test('placeSites: respects maxLabels and ignores non-finite projections', () => {
  const sites = grid(30, 1);
  const out = placeSites(sites, proj(60), { width: 4000, height: 200, maxLabels: 5 });
  assert.equal(out.filter((p) => p.label).length, 5);
  assert.equal(placeSites(sites, () => [NaN, 0], { width: 10, height: 10 }).length, 0);
});

test('nearestSite', () => {
  const tlx = BUILTIN.find((s) => s.id === 'KTLX');
  assert.equal(nearestSite(BUILTIN, tlx.lat + 0.01, tlx.lon, 5).site.id, 'KTLX');
  assert.equal(nearestSite(BUILTIN, 0, 0, 100), null);
});

// ── Consent flow ────────────────────────────────────────────────────────────────────────────

test('consent: nothing is allowed by default, for either step', () => {
  assert.equal(needsPrompt(NO_CONSENT, 'list'), true);
  assert.equal(needsPrompt(NO_CONSENT, 'download'), true);
});

test('consent: "once" proceeds but asks again; "session" proceeds and stops asking; "cancel" never proceeds', () => {
  const once = resolveChoice(NO_CONSENT, 'once');
  assert.equal(once.proceed, true);
  assert.equal(needsPrompt(once.state, 'list'), true);
  const sess = resolveChoice(NO_CONSENT, 'session');
  assert.equal(sess.proceed, true);
  assert.equal(needsPrompt(sess.state, 'list'), false);
  assert.equal(needsPrompt(sess.state, 'download'), false);
  const cancel = resolveChoice(NO_CONSENT, 'cancel');
  assert.equal(cancel.proceed, false);
  assert.deepEqual(cancel.state, NO_CONSENT);
  // cancelling after a session grant does not revoke it (the user can only grant for the session)
  assert.equal(resolveChoice(sess.state, 'cancel').state.session, true);
});

test('consent: the states are not shared objects (a grant cannot leak into the default)', () => {
  resolveChoice(NO_CONSENT, 'session');
  assert.equal(NO_CONSENT.session, false);
});

test('consent text: the list step names the host, the folders and an upper bound on the size', () => {
  const t = describeRequest('list', 'KTLX', new Date(Date.UTC(2026, 0, 5, 12)));
  assert.match(t.host, /unidata-nexrad-level2/);
  assert.match(t.lines.join(' '), /2026\/01\/05\/KTLX\//);
  assert.match(t.lines.join(' '), /2026\/01\/04\/KTLX\//, 'yesterday is named too');
  assert.match(t.lines.join(' '), /No radar data/);
  assert.match(t.size, /0\.1 MB/);
});

test('consent text: the download step states the file and its real size', () => {
  const t = describeRequest('download', 'KTLX', new Date(), { name: 'KTLX20260105_120000_V06', size: 15 * 1024 ** 2 });
  assert.match(t.lines.join(' '), /KTLX20260105_120000_V06/);
  assert.match(t.size, /15\.0 MB/);
  assert.match(describeRequest('download', 'KTLX', new Date(), {}).size, /unknown size/);
});
