// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  RadarCatalogue,
  ACCESS_INFO,
  ACCESS_ORDER,
  clusterSites,
  hitCluster,
  lonLatToWorld,
  worldToLonLat,
  haversineKm,
  networkGroup,
} from './radarCatalogue.ts';

const DOC = JSON.parse(readFileSync(new URL('../data/radarCatalogue.json', import.meta.url), 'utf8'));
const cat = new RadarCatalogue(DOC);

// ── Catalogue integrity (the shipped file) ──────────────────────────────────────────────────

test('every site has a position, a source with a licence, a date and an access class', () => {
  assert.ok(cat.sites.length >= 900);
  for (const s of cat.sites) {
    assert.ok(s.lat >= -90 && s.lat <= 90 && s.lon >= -180 && s.lon <= 180, s.id);
    assert.ok(s.data_access in ACCESS_INFO, s.id);
    assert.match(s.retrieved, /^\d{4}-\d{2}-\d{2}$/);
    const src = cat.sourceOf(s);
    assert.ok(src.name && src.licence && src.url.startsWith('https://'), s.id);
    if (s.data_access === 'open-loadable') assert.ok(s.provider && s.provider_site, s.id);
  }
  assert.equal(cat.sites.length, DOC.sites.length, 'the loader drops nothing from our own file');
});

test('no two sites share an id, ignoring case; every network has its sources recorded', () => {
  const ids = cat.sites.map((s) => s.id.toLowerCase());
  assert.equal(new Set(ids).size, ids.length);
  for (const s of cat.sites) assert.ok(s.source in DOC.sources, `${s.id}: unknown source ${s.source}`);
  const net = Object.fromEntries(cat.networks().map((n) => [n.id, n.count]));
  assert.equal(net.NEXRAD, 156);
  assert.equal(net.TDWR, 47);
  assert.equal(net.BoM, 82);
  assert.equal(net.DWD, 17);
  assert.equal(net.FMI, 10);
  assert.equal(cat.sites.length, 1072);
});

test('TDWR: the 47 FAA Terminal Doppler radars of the HOMR station list', () => {
  const tdwr = cat.filter({ networks: ['TDWR'] });
  assert.equal(tdwr.length, 47);
  for (const s of tdwr) {
    assert.match(s.id, /^T[A-Z]{3}$/);
    assert.equal(s.country, 'United States of America');
    assert.equal(s.source, 'homr');
    assert.equal(s.data_access, 'open-link', 'no loader exists for TDWR yet');
    assert.ok(s.url.startsWith('https://www.ncei.noaa.gov/'));
    assert.ok(['operational', 'unknown'].includes(s.status), `${s.id}: ${s.status}`);
  }
  assert.ok(cat.sourceOf(tdwr[0]).licence.toLowerCase().includes('public domain'));
  // known answer: the Denver TDWR; HOMR gives 39.728056, -104.52611 and the radar's own Level II
  // file header (a real TDEN volume, 2026-10-21) says 39.727, -104.526: within a few hundred metres.
  const den = cat.byId('tden');
  assert.equal(den.name, 'Denver');
  assert.equal(den.lat, 39.72806);
  assert.equal(den.lon, -104.52611);
  assert.ok(haversineKm(den.lat, den.lon, 39.727, -104.526) < 0.3);
  assert.equal(den.elev_m, 1738); // 5702 ft
  // TDWR ids never collide with NEXRAD ids, and the nearest radar to the Denver airport TDWR is itself
  const nexrad = new Set(cat.filter({ networks: ['NEXRAD'] }).map((s) => s.id));
  assert.ok(tdwr.every((s) => !nexrad.has(s.id)));
  assert.equal(cat.nearest(39.728, -104.526, 1)[0].site.id, 'TDEN');
  assert.equal(cat.search('tdwr', 200).filter((s) => s.network === 'TDWR').length, 47);
});

test('Australia: the BoM/NCI radar list replaces the OSCAR rows it duplicates', () => {
  const au = cat.filter({ networks: ['BoM'] });
  assert.equal(au.length, 82);
  for (const s of au) {
    assert.equal(s.country, 'Australia');
    assert.equal(s.source, 'aura');
    assert.equal(s.data_access, 'open-link', `${s.id}: nothing here is loadable`);
    assert.ok(s.url.startsWith('https://www.bom.gov.au/australia/radar/'), s.id);
    assert.ok(s.note.includes('NCI'), s.id);
    assert.ok(['operational', 'closed', 'not operational'].includes(s.status), s.id);
  }
  assert.ok(cat.sourceOf(au[0]).licence.startsWith('CC BY 4.0'));
  // known answer: Melbourne (Laverton), archive radar 2, S-band dual-pol; the volume read from the
  // archive on 2026-10-22 carries latitude -37.852, longitude 144.752 in its ODIM header.
  const mel = cat.byId('au-2');
  assert.equal(mel.name, 'Melbourne');
  assert.equal(mel.band, 'S');
  assert.equal(mel.polarisation, 'dual');
  assert.equal(mel.wigos, '0-20000-0-94865');
  assert.ok(haversineKm(mel.lat, mel.lon, -37.852, 144.752) < 0.6);
  assert.ok(mel.note.includes('verified'), 'radar 2 is the one whose archive access was tested');
  // one row per physical site: the old Townsville (Hervey Range) radar 73 is merged into 106
  assert.equal(cat.byId('au-73'), null);
  assert.ok(cat.byId('au-106').note.includes('access not verified'));
  // an OSCAR row is never kept next to the BoM entry for the same radar (within 3 km)
  const oscarAu = cat.sites.filter((s) => s.network === 'OSCAR/Australia');
  assert.equal(oscarAu.length, 4);
  for (const o of oscarAu) {
    assert.equal(o.data_access, 'unknown');
    assert.ok(au.every((a) => haversineKm(o.lat, o.lon, a.lat, a.lon) >= 3), o.id);
  }
  // 66 OSCAR rows were listed: 62 merged into the list's sites, 4 kept (above)
});

test('South Africa: names without the OSCAR "Radar " prefix, no data access claimed', () => {
  const za = cat.filter({ countries: ['South Africa'] });
  assert.equal(za.length, 14);
  for (const s of za) {
    assert.ok(!s.name.startsWith('Radar '), s.name);
    assert.equal(s.data_access, 'unknown');
    assert.equal(s.source, 'oscar');
    assert.ok(s.note.includes('No data access was verified'));
  }
  assert.ok(za.some((s) => s.name === 'Cape Town'));
});

test('the loader drops malformed entries instead of guessing', () => {
  const c = new RadarCatalogue({
    retrieved: '2026-10-22',
    sources: {},
    sites: [
      { id: 'ok', name: 'Ok', country: 'X', network: 'N', lat: 1, lon: 2, data_access: 'unknown' },
      { id: 'nolat', name: 'x', lon: 2, data_access: 'unknown' },
      { id: 'ok', name: 'dup', lat: 3, lon: 4, data_access: 'unknown' },
      { id: 'bad', name: 'x', lat: 1, lon: 2, data_access: 'magic' },
      { id: 'far', name: 'x', lat: 95, lon: 2, data_access: 'unknown' },
    ],
  });
  assert.deepEqual(c.sites.map((s) => s.id), ['ok']);
  assert.equal(c.sites[0].elev_m, null);
  assert.equal(c.sourceOf(c.sites[0]).licence, 'not recorded');
});

test('loadable sites are the engine providers: NEXRAD, DWD and FMI', () => {
  const by = {};
  for (const s of cat.filter({ access: ['open-loadable'] })) (by[s.provider] ??= []).push(s.provider_site);
  assert.equal(by.nexrad.length, 156);
  assert.equal(by.dwd.length, 17);
  assert.equal(by.fmi.length, 10);
  assert.ok(by.dwd.includes('ess') && by.fmi.includes('fianj') && by.nexrad.includes('KLOT'));
});

// ── API ─────────────────────────────────────────────────────────────────────────────────────

test('byId is case-insensitive and search ranks exact ids first', () => {
  assert.equal(cat.byId('DEESS').name, 'Essen');
  assert.equal(cat.byId('klot').id, 'KLOT');
  assert.equal(cat.byId('nope'), null);
  assert.equal(cat.search('klot')[0].id, 'KLOT');
  assert.equal(cat.search('essen')[0].id, 'deess');
  const germany = cat.search('germany');
  assert.ok(germany.length >= 17 && germany.every((s) => s.country === 'Germany'));
  assert.ok(cat.search('dwd ess').some((s) => s.id === 'deess'));
  assert.equal(cat.search('zzzz-no-such-site').length, 0);
  assert.equal(cat.search('', 5).length, 5);
});

test('filter by access, network and country', () => {
  const dwd = cat.filter({ networks: ['DWD'] });
  assert.equal(dwd.length, 17);
  assert.ok(dwd.every((s) => networkGroup(s) === 'DWD' && s.data_access === 'open-loadable'));
  const keyed = cat.filter({ access: ['key-required'] });
  assert.ok(keyed.length >= 3 && keyed.every((s) => s.url.startsWith('https://')));
  assert.ok(cat.filter({ countries: ['Japan'] }).every((s) => s.country === 'Japan'));
  const counts = cat.countByAccess();
  assert.equal(ACCESS_ORDER.reduce((n, k) => n + counts[k], 0), cat.sites.length);
  assert.ok(cat.networks()[0].count >= cat.networks()[1].count);
  assert.ok(cat.countries().includes('Germany'));
});

test('nearest uses great-circle distance and works in sparse places', () => {
  const [n] = cat.nearest(51.4, 6.95, 1);
  assert.equal(n.site.id, 'deess');
  assert.ok(n.km < 5);
  const three = cat.nearest(41.6, -88.1, 3);
  assert.equal(three[0].site.id, 'KLOT');
  assert.ok(three[0].km <= three[1].km && three[1].km <= three[2].km);
  // Mid-Pacific: nothing within 2 degrees, the full scan still finds the nearest.
  const far = cat.nearest(-30, -140, 2);
  assert.equal(far.length, 2);
  assert.ok(far[0].km > 1000);
  assert.deepEqual(cat.nearest(NaN, 0, 1), []);
  assert.equal(cat.nearest(51.4, 6.95, 5, 2).length, 1); // maxKm: only Essen within 2 km
  assert.equal(Math.round(haversineKm(0, 0, 0, 1)), 111);
});

// ── Map helpers ─────────────────────────────────────────────────────────────────────────────

test('Web Mercator round-trips', () => {
  for (const [lon, lat] of [[0, 0], [6.9671, 51.4056], [-161.9, 60.8], [144.8, 13.5]]) {
    const [x, y] = lonLatToWorld(lon, lat, 3);
    const [lon2, lat2] = worldToLonLat(x, y, 3);
    assert.ok(Math.abs(lon - lon2) < 1e-9 && Math.abs(lat - lat2) < 1e-9);
  }
  assert.deepEqual(lonLatToWorld(0, 0, 0), [128, 128]);
});

test('grid clustering merges nearby markers, keeps the best access class, drops off-screen sites', () => {
  const project = (lat, lon) => [lon * 10, lat * 10]; // crude: 10 px per degree
  const sites = cat.sites.filter((s) => s.lon >= 0 && s.lon <= 20 && s.lat >= 40 && s.lat <= 60);
  const fine = clusterSites(sites, project, 400, 800, 1);
  assert.equal(fine.length, sites.length);
  const coarse = clusterSites(sites, project, 400, 800, 40);
  assert.ok(coarse.length < fine.length);
  assert.equal(coarse.reduce((n, c) => n + c.sites.length, 0), sites.length);
  const withDwd = coarse.find((c) => c.sites.some((s) => s.network === 'DWD'));
  assert.equal(withDwd.access, 'open-loadable');
  const none = clusterSites(sites, project, 10, 10, 40);
  assert.equal(none.length, 0);
  const hit = hitCluster(coarse, coarse[0].x + 2, coarse[0].y - 2, 8);
  assert.equal(hit, coarse[0]);
  assert.equal(hitCluster(coarse, -500, -500, 8), null);
});

test('a thousand sites cluster quickly', () => {
  const project = (lat, lon) => lonLatToWorld(lon, lat, 2);
  const t0 = performance.now();
  for (let i = 0; i < 50; i++) clusterSites(cat.sites, project, 1024, 1024, 24);
  const ms = (performance.now() - t0) / 50;
  assert.ok(ms < 5, `clustering took ${ms.toFixed(2)} ms per frame`);
});
