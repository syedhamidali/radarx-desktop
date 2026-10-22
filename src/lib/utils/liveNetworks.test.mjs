// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  canonicalRadarId,
  liveNetworkOf,
  liveSiteCode,
  liveIdOf,
  liveUnavailableReason,
  normalizeRadarIds,
  toggleRadarId,
  planDownloads,
  describeLiveListing,
  describeLiveDownload,
} from './liveLogic.ts';

test('ids are unambiguous across networks and older saved NEXRAD ids stay as they are', () => {
  assert.equal(canonicalRadarId('ktlx'), 'KTLX');
  assert.equal(canonicalRadarId(' KVNX '), 'KVNX');
  assert.equal(canonicalRadarId('nexrad:kinx'), 'KINX');
  assert.equal(canonicalRadarId('DWD:ESS'), 'dwd:ess');
  assert.equal(canonicalRadarId('dwd:essen'), null);
  assert.equal(canonicalRadarId('fmi:vim'), null);
  assert.equal(canonicalRadarId('saws:pta'), null);
  assert.equal(canonicalRadarId(7), null);
  // a saved list of the 0.4 form is read unchanged
  assert.deepEqual(normalizeRadarIds(['KTLX', 'KVNX', 'KINX']), ['KTLX', 'KVNX', 'KINX']);
  assert.deepEqual(normalizeRadarIds(['ktlx', 'dwd:ess', 'DWD:ESS', 'nexrad:ktlx', 'bad id', 'dwd:toolong', 'dwd:fld']), ['KTLX', 'dwd:ess', 'dwd:fld']);
  assert.equal(liveNetworkOf('KTLX'), 'nexrad');
  assert.equal(liveNetworkOf('dwd:ess'), 'dwd');
  assert.equal(liveSiteCode('dwd:ess'), 'ess');
  assert.equal(liveSiteCode('KTLX'), 'KTLX');
});

test('toggling accepts both networks in one list', () => {
  assert.deepEqual(toggleRadarId(['KTLX'], 'DWD:ESS'), ['KTLX', 'dwd:ess']);
  assert.deepEqual(toggleRadarId(['KTLX', 'dwd:ess'], 'dwd:ess'), ['KTLX']);
});

test('a catalogue site is a live radar only for NEXRAD and DWD', () => {
  assert.equal(liveIdOf({ id: 'KTLX', provider: 'nexrad', provider_site: 'KTLX', data_access: 'open-loadable' }), 'KTLX');
  assert.equal(liveIdOf({ id: 'deess', provider: 'dwd', provider_site: 'ess', data_access: 'open-loadable' }), 'dwd:ess');
  assert.equal(liveIdOf({ id: 'fi', provider: 'fmi', provider_site: 'vimpeli', data_access: 'open-loadable' }), null);
  assert.equal(liveIdOf({ id: 'za', data_access: 'unknown' }), null);
  assert.equal(liveIdOf({ id: 'x', provider: 'dwd', provider_site: 'ess', data_access: 'open-link' }), null);
});

test('the reason a radar cannot be live says what is true, never invents a feed', () => {
  assert.equal(liveUnavailableReason({ provider: 'dwd', provider_site: 'ess', data_access: 'open-loadable' }), '');
  assert.match(liveUnavailableReason({ provider: 'fmi', data_access: 'open-loadable' }), /fixed two-day sample/);
  assert.match(liveUnavailableReason({ network: 'OSCAR', data_access: 'unknown' }), /No open volume feed is known for OSCAR/);
  assert.match(liveUnavailableReason({ network: 'OPERA', data_access: 'open-link' }), /no loader/);
  assert.match(liveUnavailableReason({ network: 'OPERA', data_access: 'key-required' }), /key or registration/);
  assert.match(liveUnavailableReason({ network: 'OSCAR', data_access: 'unknown' }), /NEXRAD \(USA\) and DWD \(Germany\)/);
});

const CHECK = [
  { station: 'KTLX', provider: 'nexrad', status: 'ok', name: 'KTLX20261022_084852_V06', path: 's3://b/k', size: 10907564, scan_time: '2026-10-22T08:48:52Z' },
  { station: 'dwd:ess', provider: 'dwd', status: 'ok', name: 'DWD ESS vol5minng01 2026-10-22 09:30 UTC', key: '202610220930', size: 2361313, scan_time: '2026-10-22T09:30:57Z' },
  { station: 'dwd:fld', provider: 'dwd', status: 'not_found', message: 'none' },
];

test('the plan carries the volume key of a DWD radar and the path of a NEXRAD one', () => {
  const plan = planDownloads(CHECK);
  assert.equal(plan.files.length, 2);
  assert.equal(plan.files[0].path, 's3://b/k');
  assert.equal(plan.files[0].key, undefined);
  assert.equal(plan.files[1].key, '202610220930');
  assert.equal(plan.files[1].path, undefined);
  assert.equal(plan.files[1].provider, 'dwd');
  assert.equal(plan.bytes, 10907564 + 2361313);
  assert.deepEqual(plan.failed.map((f) => f.station), ['dwd:fld']);
});

const DWD = {
  host: 'opendata.dwd.de (anonymous, read-only)',
  listing_text: 'Two folder indexes: names, times and sizes only. No radar data.',
  listing_bytes: 2_200_000,
  attribution: 'Source: Deutscher Wetterdienst (DWD), Open Data radar volume sweeps',
  licence: 'CC BY 4.0 with the source acknowledged (DWD legal notice)',
};

test('the listing dialog names each server and the size of each network', () => {
  const t = describeLiveListing(['KTLX', 'dwd:ess', 'dwd:fld'], new Date(Date.UTC(2026, 9, 22, 9, 0, 0)), { dwd: DWD });
  assert.match(t.host, /unidata-nexrad-level2/);
  assert.match(t.host, /opendata\.dwd\.de/);
  assert.match(t.lines[0], /^NEXRAD: a listing of today's folder 2026\/10\/22\/<SITE>\/.*KTLX/);
  assert.match(t.lines[1], /DWD \(ess, fld\): Two folder indexes/);
  assert.match(t.lines[1], /2\.1 MB per radar/);
  assert.match(t.size, /4\.\d MB per update in all \(1 NEXRAD at about 0\.1 MB, 2 DWD at about 2\.1 MB\)/);
  // DWD alone does not mention the NEXRAD bucket
  const d = describeLiveListing(['dwd:ess'], new Date(), { dwd: DWD });
  assert.doesNotMatch(d.host, /nexrad/);
  assert.match(d.size, /per update in all \(1 DWD/);
});

test('the download dialog lists exact sizes and carries the DWD credit and licence', () => {
  const t = describeLiveDownload(planDownloads(CHECK), 5, { dwd: DWD });
  assert.equal(t.title, 'Download 2 volumes for the live map');
  assert.match(t.lines[0], /KTLX: KTLX20261022_084852_V06 \(10\.4 MB\)/);
  assert.match(t.lines[1], /dwd:ess: DWD ESS vol5minng01 2026-10-22 09:30 UTC \(2\.3 MB\)/);
  assert.ok(t.lines.some((l) => /Licence: CC BY 4\.0 with the source acknowledged/.test(l) && /Deutscher Wetterdienst/.test(l)));
  assert.ok(t.lines.some((l) => /20 small files/.test(l)));
  assert.match(t.size, /12\.7 MB now/);
  assert.match(t.host, /unidata-nexrad-level2.* and opendata\.dwd\.de/);
  // a NEXRAD-only plan keeps its old words
  const n = describeLiveDownload(planDownloads([CHECK[0]]), 5);
  assert.equal(n.title, 'Download 1 Level II volume for the live map');
  assert.doesNotMatch(n.host, /dwd/);
});
