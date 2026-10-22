// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_FLOOR_DBZ,
  FLOOR_CHOICES,
  clampFloorDbz,
  engineFloor,
  floorText,
  legendAtFloor,
  gridLabel,
  compositeLabel,
  footprintBounds,
  openableStations,
  openedText,
  multiDopplerBlocked,
  liveLayerLine,
} from './liveDisplay.ts';

test('the default floor is the NWS 5 dBZ and the setting is clamped', () => {
  assert.equal(DEFAULT_FLOOR_DBZ, 5);
  assert.equal(clampFloorDbz(undefined), 5);
  assert.equal(clampFloorDbz('x'), 5);
  assert.equal(clampFloorDbz(-100), -20);
  assert.equal(clampFloorDbz(99), 40);
  assert.equal(clampFloorDbz(7.6), 8);
  assert.ok(FLOOR_CHOICES.includes(5));
});

test('a floor at the start of the colour scale draws everything', () => {
  assert.equal(engineFloor(5, -20), 5);
  assert.equal(engineFloor(-20, -20), null);
  assert.equal(engineFloor(-30, -20), null);
  assert.equal(floorText(5), 'Hide echo below 5 dBZ');
  assert.equal(floorText(null), 'All echo drawn');
  assert.equal(floorText(-20), 'All echo drawn');
});

test('legendAtFloor starts the scale at the floor with an interpolated colour', () => {
  const legend = [
    [-20, '#000000'],
    [0, '#0000ff'],
    [10, '#00ff00'],
    [75, '#ffffff'],
  ];
  const r = legendAtFloor(legend, -20, 5);
  assert.equal(r.vmin, 5);
  assert.deepEqual(r.legend[0], [5, '#008080']); // halfway between blue and green
  assert.deepEqual(r.legend.slice(1).map((s) => s[0]), [10, 75]);
  // no floor, or a floor at the start: unchanged (sorted)
  assert.deepEqual(legendAtFloor(legend, -20, null).legend, legend);
  assert.equal(legendAtFloor(legend, -20, -20).vmin, -20);
  // unsorted input is sorted; empty input is returned as is
  assert.deepEqual(legendAtFloor([[10, '#00ff00'], [0, '#0000ff']], 0, null).legend[0][0], 0);
  assert.deepEqual(legendAtFloor([], -20, 5), { vmin: -20, legend: [] });
});

test('the grid is named as it is, including 1.5 km', () => {
  assert.equal(gridLabel(1000), '1 km');
  assert.equal(gridLabel(1500), '1.5 km');
  assert.equal(gridLabel(2000), '2 km');
  assert.equal(gridLabel(0), 'unknown grid');
  assert.equal(gridLabel(NaN), 'unknown grid');
});

test('the composite sentence states grid, floor, smoothing and that it is not official', () => {
  const t = compositeLabel({ variable: 'DBZH', radars: 3, resolutionM: 1000, floor: 5, smoothed: true, isLatest: true });
  assert.match(t, /3 radar volumes, 1 km grid, drawn smoothed/);
  assert.match(t, /Echo below 5 dBZ is not drawn/);
  assert.match(t, /Not an official product/);
  assert.doesNotMatch(t, /Recent loop/);
  const u = compositeLabel({ variable: 'DBZH', radars: 1, resolutionM: 1500, floor: null, smoothed: false, isLatest: false });
  assert.match(u, /1 radar volume, 1\.5 km grid\./);
  assert.match(u, /All echo drawn/);
  assert.match(u, /Recent loop: not the newest frame/);
});

test('footprintBounds covers every range disc and handles none', () => {
  assert.equal(footprintBounds([], 230), null);
  const b = footprintBounds([{ lat: 35.3, lon: -97.3 }, { lat: 36.7, lon: -98.1 }], 230);
  assert.ok(b.south < 35.3 - 230 / 111.2 + 0.01 && b.north > 36.7 + 230 / 111.2 - 0.01);
  assert.ok(b.west < -98.1 && b.east > -97.3);
  assert.ok(b.east - b.west > 2 * (230 / 111.2)); // longitude is wider than latitude at 36 degrees
  const polar = footprintBounds([{ lat: 84, lon: 0 }], 230);
  assert.ok(polar.north <= 85 && polar.west >= -180 && polar.east <= 180);
});

test('openableStations keeps the radar order and only radars that hold a volume', () => {
  const radars = [{ station: 'KTLX', status: 'ok' }, { station: 'KINX', status: 'error' }, { station: 'KVNX', status: 'ok' }];
  assert.deepEqual(openableStations(radars, ['KVNX', 'KTLX', 'KINX']), ['KTLX', 'KVNX']);
  assert.deepEqual(openableStations(radars, []), []);
});

test('the wording after opening says what, the disk used, and what failed', () => {
  const files = [
    { station: 'KTLX', name: 'a', size: 10, file_id: '1' },
    { station: 'KVNX', name: 'b', size: 20, file_id: '2' },
  ];
  const t = openedText(files, 22 * 1024 * 1024);
  assert.match(t, /Opened 2 volumes \(KTLX, KVNX\)/);
  assert.match(t, /22\.0 MB on disk until you close them/);
  assert.match(t, /does not replace them/);
  const again = openedText(files.map((f) => ({ ...f, already_open: true })), 100);
  assert.match(again, /already open/);
  assert.doesNotMatch(again, /on disk/);
  assert.match(openedText(files, 1, ['KINX (boom)']), /Could not open: KINX \(boom\)/);
  assert.match(openedText([files[0]], 5), /Opened 1 volume \(KTLX\)/);
});

test('a DWD volume is said to use memory, not disk', () => {
  const mixed = [
    { station: 'KTLX', name: 'a', size: 1, file_id: '1', kind: 'file' },
    { station: 'dwd:ess', name: 'b', size: 0, file_id: '2', kind: 'memory' },
  ];
  const t = openedText(mixed, 10 * 1024 * 1024);
  assert.match(t, /The NEXRAD files use 10\.0 MB on disk/);
  assert.match(t, /The DWD volumes are kept in memory and use no disk \(dwd:ess\)/);
  const onlyDwd = openedText([mixed[1]], 0);
  assert.match(onlyDwd, /They are kept in memory and use no disk/);
  assert.doesNotMatch(onlyDwd, /on disk/);
});

test('multi-Doppler needs two radars and says why not', () => {
  assert.match(multiDopplerBlocked(['KTLX']), /at least two radars/);
  assert.match(multiDopplerBlocked([]), /at least two radars/);
  assert.equal(multiDopplerBlocked(['KTLX', 'KVNX']), null);
});

test('the Layers line names the analysis time and the radars', () => {
  assert.equal(liveLayerLine('2026-10-21T22:04:51Z', ['KTLX', 'KVNX']), 'Live composite: analysis time 2026-10-21 22:04 UTC, radars KTLX, KVNX');
  assert.match(liveLayerLine(null, []), /time unknown, radars none/);
});
