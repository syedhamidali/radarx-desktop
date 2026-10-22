/**
 * Mosaic store: merge several open radar files (or one) into a georeferenced
 * image that is drawn on the map.
 *
 *   -> {type:'create_mosaic', request_id, file_ids, variable, product, grid, merge, ...}
 *   <- {type:'progress', ...}* then {type:'mosaic_result', request_id, ...} + PNG frames
 *      (the websocket manager reassembles the frames and emits `mosaic_result_ready`
 *      with `{header, png}`), or {type:'mosaic_error', request_id, message}
 *   -> {type:'mosaic_probe', mosaic_id, lon, lat}  <- {type:'mosaic_probe_result', ...}
 *   -> {type:'export_mosaic', mosaic_id, format, path}  <- {type:'file_saved', path}
 *
 * Only the most recent request_id is honoured: results of older runs are dropped.
 */

import { writable, derived, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { beginOp, cancelOp, type OpHandle } from '../utils/requestScheduler';
import { openFiles, type FileEntry } from './fileManager';
import { connectionStatus } from './radarData';
import { addToast } from './toasts';
import { registerJobSource } from './jobSources';
import { getMask, isMaskActive, type ValueMask } from './valueMask';
import type { FrameLabel } from '../utils/nowcastLogic';

// ── Types ──────────────────────────────────────────────────────────────────

export type MosaicProduct = 'cappi' | 'composite' | 'echo_top';

export interface MosaicSettings {
  variable: string;
  product: MosaicProduct;
  heightM: number;
  thresholdDbz: number;
  resolutionM: number;
  extentMode: 'auto' | 'view';
  maxRangeKm: number;
  zMaxM: number;
  zStepM: number;
  mergeEnabled: boolean;
  rangeScaleKm: number;
  timeScaleS: number;
  beamwidthDeg: number;
  calibrate: boolean;
  /** File id used as the calibration reference ('' = first selected file). */
  referenceFileId: string;
  advect: boolean;
  /** Keep the merged 3D field so the mosaic can be shown in the 3D view. */
  keepVolume: boolean;
  /** Optional colour range override (null = automatic). */
  vmin: number | null;
  vmax: number | null;
}

export const DEFAULT_SETTINGS: MosaicSettings = {
  variable: 'DBZH',
  product: 'composite',
  heightM: 3000,
  thresholdDbz: 18,
  resolutionM: 1000,
  extentMode: 'auto',
  maxRangeKm: 230,
  zMaxM: 15000,
  zStepM: 500,
  mergeEnabled: true,
  rangeScaleKm: 50,
  timeScaleS: 300,
  beamwidthDeg: 1.0,
  calibrate: false,
  referenceFileId: '',
  advect: false,
  keepVolume: true,
  vmin: null,
  vmax: null,
};

export interface MosaicRadarInfo {
  file_id: string;
  name: string;
  latitude: number;
  longitude: number;
  altitude_m: number;
  volume_time: string | null;
  coverage_fraction: number;
  share: number;
  bias_db: number | null;
}

export interface MosaicHeader {
  type: 'mosaic_result';
  request_id: string;
  mosaic_id: string;
  product: MosaicProduct;
  variable: string;
  units: string;
  height_m: number | null;
  grid: {
    nx: number;
    ny: number;
    resolution_m: number;
    west: number;
    south: number;
    east: number;
    north: number;
  };
  /** [lon, lat] for NW, NE, SE, SW. */
  corners: [number, number][];
  vmin: number;
  vmax: number;
  cmap: string;
  legend: [number, string][];
  stats: { valid_fraction: number; min: number; max: number; mean: number };
  radars: MosaicRadarInfo[];
  overlap_fraction: number;
  /** The merged 3D field was kept (see get_mosaic_volume / the 3D view). */
  has_volume?: boolean;
  volume_shape?: [number, number, number] | null;
  volume_note?: string | null;
  /** The value mask the product was made with (hidden cells are empty), and its wording. */
  value_mask?: ValueMask | null;
  value_mask_text?: string | null;
  /** Set by a QPE map layer (shown through this layer): RETRIEVED, or ADJUSTED to rain gauges. */
  provenance_type?: string;
  /**
   * Set when this layer is a nowcast frame (a FORECAST, never an observation): the label that the map
   * prints on it (class chip, origin, valid time, lead, guidance, skill). Such a layer has no engine
   * grid to probe, and the map always draws its label.
   */
  forecast?: ForecastTag;
  elapsed_ms: number;
  png_bytes: number;
  chunks: number;
}

/** The label of a nowcast frame on the map, with where it sits in its run. */
export interface ForecastTag extends FrameLabel {
  nowcastId: string;
  /** Position of the frame in the run (for the slider). */
  leadIndex: number;
  leadCount: number;
}

export interface MosaicResult extends MosaicHeader {
  /** Object URL of the PNG (revoked when the result is replaced or cleared). */
  url: string;
  /** Max range (km) the mosaic was requested with: drawn as the range ring. */
  ringKm: number;
}

export interface MosaicRunState {
  running: boolean;
  requestId: string | null;
  percent: number;
  message: string;
  error: string | null;
}

export interface MosaicDisplay {
  opacity: number;
  visible: boolean;
  /** Hide the PPI canvas so the map with the mosaic is what you see. */
  mosaicView: boolean;
}

export interface MosaicProbeResult {
  lon: number;
  lat: number;
  value: number | null;
  units: string;
  radars: { name: string; value: number | null; range_km: number; elevation_deg: number }[];
}

export interface MosaicLayer {
  image: HTMLImageElement;
  corners: [number, number][];
  /** `ringKm` overrides the layer's ring for one radar (the live map: each radar's own range). */
  radars: (MosaicRadarInfo & { color: string; ringKm?: number })[];
  ringKm: number;
  opacity: number;
  visible: boolean;
  view: boolean;
  units: string;
  vmin: number;
  vmax: number;
  legend: [number, string][];
  mosaicId: string;
  variable: string;
  /** A short note drawn beside the legend (the live map: "echo below 5 dBZ not drawn"). */
  legendNote?: string;
  /** Draw the range rings subtly / not at all (the live map's setting); default: as before. */
  ringStyle?: 'normal' | 'subtle' | 'off';
}

export interface FileRow {
  id: string;
  name: string;
  filename: string;
  lat: number | null;
  lon: number | null;
  /** Volume start time as epoch ms (null if unknown). */
  timeMs: number | null;
  timeLabel: string;
  variables: string[];
  selected: boolean;
  /** Warning chips shown on the row. */
  warnings: string[];
}

/** Per-radar colours (chips in the panel and markers/rings on the map). */
export const RADAR_COLORS = [
  '#38bdf8',
  '#f472b6',
  '#a3e635',
  '#fb923c',
  '#c084fc',
  '#2dd4bf',
  '#facc15',
  '#f87171',
];

export const TIME_WARN_MS = 10 * 60 * 1000;
const SAME_SITE_DEG = 0.02; // ~2 km

// ── Persistence ────────────────────────────────────────────────────────────

const STORAGE_KEY = 'xradar.mosaic.settings.v1';

function loadSettings(): MosaicSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_SETTINGS };
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return { ...DEFAULT_SETTINGS };
    const out: any = { ...DEFAULT_SETTINGS };
    for (const k of Object.keys(DEFAULT_SETTINGS) as (keyof MosaicSettings)[]) {
      if (k === 'referenceFileId') continue; // file ids do not survive a restart
      if (k in parsed && typeof parsed[k] === typeof DEFAULT_SETTINGS[k]) out[k] = parsed[k];
      else if (k in parsed && parsed[k] === null && DEFAULT_SETTINGS[k] === null) out[k] = null;
      else if ((k === 'vmin' || k === 'vmax') && typeof parsed[k] === 'number') out[k] = parsed[k];
    }
    if (!['cappi', 'composite', 'echo_top'].includes(out.product)) out.product = DEFAULT_SETTINGS.product;
    if (out.extentMode !== 'auto' && out.extentMode !== 'view') out.extentMode = 'auto';
    return out as MosaicSettings;
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

function saveSettings(s: MosaicSettings): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch {
    /* storage unavailable: ignore */
  }
}

// ── Stores ─────────────────────────────────────────────────────────────────

export const mosaicSettings = writable<MosaicSettings>(loadSettings());
mosaicSettings.subscribe(saveSettings);

/** File ids the user switched OFF (default: every open file takes part). */
export const mosaicDeselected = writable<string[]>([]);

export const mosaicRun = writable<MosaicRunState>({
  running: false,
  requestId: null,
  percent: 0,
  message: '',
  error: null,
});
registerJobSource('mosaic', derived(mosaicRun, (r) => (r.running ? 1 : 0)));

export const mosaicResult = writable<MosaicResult | null>(null);

export const mosaicDisplay = writable<MosaicDisplay>({ opacity: 0.85, visible: true, mosaicView: true });

/** The decoded image of the current result (set once the PNG finished loading). */
const mosaicImage = writable<{ url: string; image: HTMLImageElement } | null>(null);

/** Everything the map component needs to draw the mosaic; null when there is nothing to draw. */
export const mosaicLayer = derived(
  [mosaicResult, mosaicImage, mosaicDisplay],
  ([$res, $img, $disp]): MosaicLayer | null => {
    if (!$res || !$img || $img.url !== $res.url) return null;
    return {
      image: $img.image,
      corners: $res.corners,
      radars: $res.radars.map((r, i) => ({ ...r, color: RADAR_COLORS[i % RADAR_COLORS.length] })),
      ringKm: $res.ringKm,
      opacity: $disp.opacity,
      visible: $disp.visible,
      view: $disp.mosaicView,
      units: $res.units,
      vmin: $res.vmin,
      vmax: $res.vmax,
      legend: $res.legend,
      mosaicId: $res.mosaic_id,
      variable: $res.variable,
    };
  },
);

/** Latest hover probe (position in the map container's CSS pixels). */
export const mosaicProbe = writable<{ x: number; y: number; result: MosaicProbeResult | null } | null>(null);

export const mosaicExporting = writable<boolean>(false);

// ── Derived: file rows ─────────────────────────────────────────────────────

const TIME_ATTR_KEYS = [
  'scan_time',
  'time_coverage_start',
  'volume_time',
  'start_time',
  'volume_start_time',
  'time',
];

/** Best-effort parse of an ISO-ish string or "YYYYMMDD_HHMMSS" into epoch ms. */
export function parseTimeMs(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value > 1e11 ? value : value * 1000;
  }
  const s = String(value).trim();
  const m = s.match(/^(\d{4})(\d{2})(\d{2})[_T-]?(\d{2})(\d{2})(\d{2})/);
  if (m) return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  const iso = Date.parse(/[zZ]|[+-]\d{2}:?\d{2}$/.test(s) ? s : s.replace(' ', 'T') + 'Z');
  if (Number.isFinite(iso)) return iso;
  return null;
}

export function fileTimeMs(f: FileEntry): number | null {
  const attrs = f.attributes ?? {};
  for (const k of TIME_ATTR_KEYS) {
    const t = parseTimeMs(attrs[k]);
    if (t !== null) return t;
  }
  // NEXRAD-style names: KATX20130717_195021_V06
  const m = f.filename.match(/(\d{8})[_-]?(\d{6})/);
  if (m) return parseTimeMs(`${m[1]}_${m[2]}`);
  return null;
}

export function fileSite(f: FileEntry): { lat: number; lon: number } | null {
  const a = f.attributes ?? {};
  const num = (v: any): number => (v === null || v === undefined || v === '' ? NaN : Number(v));
  const lat = [a.latitude, a.origin_latitude, a.siteLat].map(num).find(Number.isFinite);
  const lon = [a.longitude, a.origin_longitude, a.siteLon].map(num).find(Number.isFinite);
  if (lat === undefined || lon === undefined) return null;
  if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
  if (lat === 0 && lon === 0) return null;
  return { lat, lon };
}

function fileName(f: FileEntry): string {
  const a = f.attributes ?? {};
  const n = a.instrument_name ?? a.station_name ?? a.station ?? a.site_name;
  return n ? String(n) : f.filename;
}

export function formatTimeMs(ms: number | null): string {
  if (ms === null) return 'time unknown';
  return new Date(ms).toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
}

export const mosaicFileRows = derived([openFiles, mosaicDeselected], ([$files, $off]): FileRow[] => {
  const rows: FileRow[] = $files.map((f) => {
    const site = fileSite(f);
    const timeMs = fileTimeMs(f);
    return {
      id: f.id,
      name: fileName(f),
      filename: f.filename,
      lat: site?.lat ?? null,
      lon: site?.lon ?? null,
      timeMs,
      timeLabel: formatTimeMs(timeMs),
      variables: f.variables,
      selected: !$off.includes(f.id),
      warnings: [],
    };
  });

  const sel = rows.filter((r) => r.selected);
  // Volume times that differ by more than ~10 minutes (against the median of the selection)
  const times = sel.map((r) => r.timeMs).filter((t): t is number => t !== null).sort((a, b) => a - b);
  if (times.length >= 2 && times[times.length - 1] - times[0] > TIME_WARN_MS) {
    const med = times[Math.floor(times.length / 2)];
    for (const r of sel) {
      if (r.timeMs !== null && Math.abs(r.timeMs - med) > TIME_WARN_MS) {
        r.warnings.push(`volume time differs by ${Math.round(Math.abs(r.timeMs - med) / 60000)} min`);
      }
    }
    if (!sel.some((r) => r.warnings.length)) {
      // spread is large but no single outlier vs the median: flag the earliest and latest
      for (const r of sel) {
        if (r.timeMs === times[0] || r.timeMs === times[times.length - 1]) r.warnings.push('volume times differ > 10 min');
      }
    }
  }
  // Same site opened twice
  for (let i = 0; i < sel.length; i++) {
    for (let j = i + 1; j < sel.length; j++) {
      const a = sel[i];
      const b = sel[j];
      if (a.lat === null || b.lat === null) continue;
      if (Math.abs(a.lat - b.lat) < SAME_SITE_DEG && Math.abs((a.lon ?? 0) - (b.lon ?? 0)) < SAME_SITE_DEG) {
        for (const r of [a, b]) {
          const msg = 'same site as another selected file';
          if (!r.warnings.includes(msg)) r.warnings.push(msg);
        }
      }
    }
  }
  for (const r of sel) {
    if (r.lat === null) r.warnings.push('no site location');
  }
  return rows;
});

export const mosaicSelectedIds = derived(mosaicFileRows, ($rows) => $rows.filter((r) => r.selected).map((r) => r.id));

/** Variables present in EVERY selected file (intersection), in a stable order. */
export const mosaicVariableChoices = derived(mosaicFileRows, ($rows) => {
  const sel = $rows.filter((r) => r.selected);
  if (sel.length === 0) return [] as string[];
  let common = new Set(sel[0].variables);
  for (const r of sel.slice(1)) common = new Set(r.variables.filter((v) => common.has(v)));
  const preferred = ['DBZH', 'DBZ', 'DBZHC', 'REFL', 'reflectivity'];
  return [...common].sort((a, b) => {
    const ia = preferred.indexOf(a);
    const ib = preferred.indexOf(b);
    if (ia >= 0 || ib >= 0) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    return a.localeCompare(b);
  });
});

// Keep the selected variable valid for the current selection.
mosaicVariableChoices.subscribe((choices) => {
  if (choices.length === 0) return;
  const cur = get(mosaicSettings).variable;
  if (!choices.includes(cur)) mosaicSettings.update((s) => ({ ...s, variable: choices[0] }));
});

// Forget deselections of files that were closed; a stale reference falls back to the first file.
openFiles.subscribe((files) => {
  const ids = new Set(files.map((f) => f.id));
  const off = get(mosaicDeselected);
  if (off.some((id) => !ids.has(id))) mosaicDeselected.set(off.filter((id) => ids.has(id)));
  const ref = get(mosaicSettings).referenceFileId;
  if (ref && !ids.has(ref)) mosaicSettings.update((s) => ({ ...s, referenceFileId: '' }));
});

// ── Actions: selection / settings ──────────────────────────────────────────

export function toggleMosaicFile(id: string): void {
  mosaicDeselected.update((off) => (off.includes(id) ? off.filter((x) => x !== id) : [...off, id]));
}

export function selectAllMosaicFiles(all: boolean): void {
  mosaicDeselected.set(all ? [] : get(openFiles).map((f) => f.id));
}

export function updateMosaicSettings(patch: Partial<MosaicSettings>): void {
  mosaicSettings.update((s) => ({ ...s, ...patch }));
}

export function resetMosaicSettings(): void {
  mosaicSettings.update((s) => ({ ...DEFAULT_SETTINGS, variable: s.variable }));
}

// ── View bounds provider (registered by the PPI viewer) ────────────────────

export interface GeoBounds {
  west: number;
  south: number;
  east: number;
  north: number;
}

let viewBoundsProvider: (() => GeoBounds | null) | null = null;

/** The viewer registers a getter for the lon/lat box currently visible on the map. */
export function registerViewBoundsProvider(fn: (() => GeoBounds | null) | null): void {
  viewBoundsProvider = fn;
}

export function getViewBounds(): GeoBounds | null {
  try {
    return viewBoundsProvider ? viewBoundsProvider() : null;
  } catch {
    return null;
  }
}

/** Ask the viewer to pan/zoom so the box fits. */
export function fitMapToBounds(b: GeoBounds): void {
  window.dispatchEvent(new CustomEvent('xradar:mosaic-fit', { detail: b }));
}

export function fitMapToMosaic(): void {
  const r = get(mosaicResult);
  if (!r) return;
  fitMapToBounds({ west: r.grid.west, south: r.grid.south, east: r.grid.east, north: r.grid.north });
}

// ── Running ────────────────────────────────────────────────────────────────

let requestCounter = 0;
let runSafetyTimer: ReturnType<typeof setTimeout> | null = null;
const RUN_TIMEOUT_MS = 10 * 60 * 1000;

function newRequestId(): string {
  requestCounter += 1;
  return `mosaic-${Date.now().toString(36)}-${requestCounter}`;
}

/** Operations-tray entry of the in-flight run (null when none / already finished). */
let mosaicOp: OpHandle | null = null;

function finishRun(patch: Partial<MosaicRunState>): void {
  if (runSafetyTimer) {
    clearTimeout(runSafetyTimer);
    runSafetyTimer = null;
  }
  const op = mosaicOp;
  mosaicOp = null;
  mosaicRun.update((r) => ({ ...r, running: false, ...patch }));
  if (op) {
    if (patch.error) op.fail(patch.error);
    else if (patch.percent === 100) op.done();
    else cancelOp(op.id); // cancelMosaicRun(): onCancel finds mosaicOp already cleared
  }
}

export interface RunOptions {
  fileIds?: string[];
}

/** Build and send a create_mosaic request. Returns the request_id, or null if it could not start. */
export function runMosaic(opts: RunOptions = {}): string | null {
  const s = get(mosaicSettings);
  const ids = opts.fileIds ?? get(mosaicSelectedIds);
  if (get(connectionStatus) !== 'connected') {
    mosaicRun.update((r) => ({ ...r, error: 'Sidecar not connected' }));
    return null;
  }
  if (ids.length === 0) {
    mosaicRun.update((r) => ({ ...r, error: 'Select at least one open radar file' }));
    return null;
  }
  if (!s.variable) {
    mosaicRun.update((r) => ({ ...r, error: 'No variable common to the selected files' }));
    return null;
  }

  let extent: 'auto' | GeoBounds = 'auto';
  if (s.extentMode === 'view') {
    const b = getViewBounds();
    if (b) extent = b;
    else {
      mosaicRun.update((r) => ({ ...r, error: 'Current map view is not available yet (render a radar sweep first)' }));
      return null;
    }
  }

  let referenceIndex = ids.indexOf(s.referenceFileId);
  if (referenceIndex < 0) referenceIndex = 0;

  const requestId = newRequestId();
  const msg: Record<string, any> = {
    type: 'create_mosaic',
    request_id: requestId,
    file_ids: ids,
    variable: s.variable,
    product: s.product,
    height_m: s.heightM,
    threshold_dbz: s.thresholdDbz,
    grid: {
      resolution_m: s.resolutionM,
      extent,
      z_max_m: s.zMaxM,
      z_step_m: s.zStepM,
      max_range_km: s.maxRangeKm,
    },
    merge: {
      enabled: s.mergeEnabled,
      range_scale_km: s.rangeScaleKm,
      time_scale_s: s.timeScaleS,
      beamwidth_deg: s.beamwidthDeg,
      calibrate: s.calibrate,
      reference_index: referenceIndex,
      advect: s.advect,
    },
  };
  if (s.vmin !== null) msg.vmin = s.vmin;
  if (s.vmax !== null) msg.vmax = s.vmax;
  msg.keep_volume = s.keepVolume;
  // The shared value mask of this variable: hidden values are empty in the image, probes and exports.
  const vmask = getMask(s.variable);
  if (isMaskActive(vmask)) msg.value_mask = vmask;

  pendingRingKm.set(requestId, s.maxRangeKm);
  if (mosaicOp) {
    // A new run supersedes the previous one (its result will be dropped as stale).
    const prev = mosaicOp;
    mosaicOp = null;
    cancelOp(prev.id);
  }
  // The 10 min safety timer below ends the tray entry, so no second timeout here.
  mosaicOp = beginOp({
    kind: 'mosaic',
    label: `Building mosaic (${ids.length} radar${ids.length === 1 ? '' : 's'})`,
    slowMs: 20_000,
    timeoutMs: 0,
    onCancel: () => {
      mosaicOp = null;
      cancelMosaicRun();
    },
    onRetry: () => {
      runMosaic(opts);
    },
  });
  mosaicRun.set({ running: true, requestId, percent: 0, message: 'Starting…', error: null });
  if (runSafetyTimer) clearTimeout(runSafetyTimer);
  runSafetyTimer = setTimeout(() => {
    runSafetyTimer = null;
    mosaicRun.update((r) =>
      r.requestId === requestId && r.running
        ? { ...r, running: false, error: 'No response from the sidecar (timed out)' }
        : r,
    );
    if (get(mosaicRun).requestId === requestId && mosaicOp) {
      const op = mosaicOp;
      mosaicOp = null;
      op.fail('No response from the sidecar (timed out)');
    }
  }, RUN_TIMEOUT_MS);
  void wsManager.send(msg);
  return requestId;
}

/** Stop waiting for the current run: its (late) result will be ignored. */
export function cancelMosaicRun(): void {
  finishRun({ requestId: null, percent: 0, message: '', error: null });
}

const pendingRingKm = new Map<string, number>();

function handleResultReady(payload: { header: MosaicHeader; png: Uint8Array }): void {
  const { header, png } = payload;
  const run = get(mosaicRun);
  if (header.request_id !== run.requestId) {
    pendingRingKm.delete(header.request_id);
    return; // stale or cancelled
  }
  const ringKm = pendingRingKm.get(header.request_id) ?? get(mosaicSettings).maxRangeKm;
  pendingRingKm.delete(header.request_id);

  const url = URL.createObjectURL(new Blob([png as BlobPart], { type: 'image/png' }));
  const img = new Image();
  img.onload = () => {
    // Another run may have superseded this one while the PNG decoded.
    if (get(mosaicRun).requestId !== header.request_id) {
      URL.revokeObjectURL(url);
      return;
    }
    const old = get(mosaicResult);
    mosaicResult.set({ ...header, url, ringKm });
    mosaicImage.set({ url, image: img });
    if (old) setTimeout(() => URL.revokeObjectURL(old.url), 2000);
    mosaicProbe.set(null);
    mosaicDisplay.update((d) => ({ ...d, visible: true, mosaicView: true }));
    finishRun({ percent: 100, message: 'Done', error: null });
    // Show the whole mosaic
    fitMapToMosaic();
  };
  img.onerror = () => {
    URL.revokeObjectURL(url);
    if (get(mosaicRun).requestId === header.request_id) {
      finishRun({ error: 'The mosaic image could not be decoded' });
    }
  };
  img.src = url;
}

function handleProgress(msg: any): void {
  const run = get(mosaicRun);
  if (!run.running) return;
  if (msg.request_id && msg.request_id !== run.requestId) return;
  mosaicRun.update((r) => ({
    ...r,
    percent: typeof msg.percent === 'number' ? Math.max(0, Math.min(100, msg.percent)) : r.percent,
    message: typeof msg.message === 'string' && msg.message ? msg.message : r.message,
  }));
  const after = get(mosaicRun);
  mosaicOp?.update(after.message ? `${after.message} (${Math.round(after.percent)}%)` : `${Math.round(after.percent)}%`);
}

let probeInflight = 0;
let exportPending: { resolve: (path: string) => void; reject: (e: Error) => void } | null = null;

function handleError(msg: any): void {
  const text = String(msg.message ?? 'Mosaic failed');
  const run = get(mosaicRun);
  if (msg.request_id) {
    if (msg.request_id !== run.requestId) return; // stale
    finishRun({ error: text });
    return;
  }
  // No request_id: belongs to a probe or an export, else to the running mosaic
  if (exportPending) {
    const p = exportPending;
    exportPending = null;
    p.reject(new Error(text));
    return;
  }
  if (probeInflight > 0) {
    probeInflight = 0;
    return;
  }
  if (run.running) finishRun({ error: text });
}

function handleGenericError(msg: any): void {
  // A generic server error while waiting for an export or a mosaic must not leave the UI hanging.
  const text = String(msg.message ?? 'Server error');
  if (exportPending) {
    const p = exportPending;
    exportPending = null;
    p.reject(new Error(text));
    return;
  }
  if (get(mosaicRun).running) finishRun({ error: text });
}

// ── Probe ──────────────────────────────────────────────────────────────────

const PROBE_INTERVAL_MS = 100;
let lastProbeSent = 0;
let probeTimer: ReturnType<typeof setTimeout> | null = null;
let probeQueued: { lon: number; lat: number; x: number; y: number } | null = null;
let probeLatest: { lon: number; lat: number } | null = null;

function sendProbe(p: { lon: number; lat: number; x: number; y: number }): void {
  const res = get(mosaicResult);
  if (!res) return;
  lastProbeSent = performance.now();
  probeLatest = { lon: p.lon, lat: p.lat };
  probeInflight += 1;
  mosaicProbe.update((cur) => ({ x: p.x, y: p.y, result: cur?.result ?? null }));
  void wsManager.send({ type: 'mosaic_probe', mosaic_id: res.mosaic_id, lon: p.lon, lat: p.lat });
}

/** Throttled (~10/s, trailing edge kept) probe of the mosaic at a position. */
export function probeMosaic(lon: number, lat: number, x: number, y: number): void {
  if (get(mosaicResult)?.forecast) return; // a nowcast frame has no engine grid to ask
  const now = performance.now();
  const p = { lon, lat, x, y };
  // Keep the tooltip glued to the cursor even while a reply is pending
  mosaicProbe.update((cur) => ({ x, y, result: cur?.result ?? null }));
  if (now - lastProbeSent >= PROBE_INTERVAL_MS && !probeTimer) {
    sendProbe(p);
    return;
  }
  probeQueued = p;
  if (!probeTimer) {
    probeTimer = setTimeout(() => {
      probeTimer = null;
      const q = probeQueued;
      probeQueued = null;
      if (q) sendProbe(q);
    }, Math.max(0, PROBE_INTERVAL_MS - (now - lastProbeSent)));
  }
}

export function clearMosaicProbe(): void {
  probeQueued = null;
  probeLatest = null;
  if (probeTimer) {
    clearTimeout(probeTimer);
    probeTimer = null;
  }
  mosaicProbe.set(null);
}

function handleProbeResult(msg: any): void {
  if (probeInflight > 0) probeInflight -= 1;
  const res = get(mosaicResult);
  if (!res || msg.mosaic_id !== res.mosaic_id) return;
  // Drop stale replies: only the latest requested position matters
  if (!probeLatest || Math.abs(msg.lon - probeLatest.lon) > 1e-6 || Math.abs(msg.lat - probeLatest.lat) > 1e-6) return;
  mosaicProbe.update((cur) => {
    if (!cur) return cur;
    return {
      ...cur,
      result: {
        lon: msg.lon,
        lat: msg.lat,
        value: typeof msg.value === 'number' ? msg.value : null,
        units: String(msg.units ?? res.units ?? ''),
        radars: Array.isArray(msg.radars) ? msg.radars : [],
      },
    };
  });
}

// ── Result management ──────────────────────────────────────────────────────

export function clearMosaic(): void {
  const old = get(mosaicResult);
  mosaicResult.set(null);
  mosaicImage.set(null);
  clearMosaicProbe();
  if (old) setTimeout(() => URL.revokeObjectURL(old.url), 2000);
  finishRun({ requestId: null, percent: 0, message: '', error: null });
}

/**
 * Draw a georeferenced PNG built by another panel (the rainfall panel) with the mosaic map layer:
 * same opacity, legend and hover probe (the engine registered the float grid under `mosaic_id`).
 * `header` is an engine `MosaicRecord.header()`; `pngBase64` the image.
 */
export function showExternalLayer(
  header: Partial<MosaicHeader> & { mosaic_id: string },
  pngBase64: string,
  opts: { fit?: boolean } = {},
): Promise<void> {
  return new Promise((resolve, reject) => {
    let bytes: Uint8Array;
    try {
      const bin = atob(pngBase64);
      bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    } catch {
      reject(new Error('The map image could not be decoded'));
      return;
    }
    const url = URL.createObjectURL(new Blob([bytes as BlobPart], { type: 'image/png' }));
    const img = new Image();
    img.onload = () => {
      const old = get(mosaicResult);
      const full = {
        type: 'mosaic_result',
        request_id: '',
        elapsed_ms: 0,
        png_bytes: bytes.length,
        chunks: 1,
        radars: [],
        overlap_fraction: 0,
        ...header,
        url,
        ringKm: 0,
      } as MosaicResult;
      mosaicResult.set(full);
      mosaicImage.set({ url, image: img });
      if (old) setTimeout(() => URL.revokeObjectURL(old.url), 2000);
      mosaicProbe.set(null);
      mosaicDisplay.update((d) => ({ ...d, visible: true, mosaicView: true }));
      if (opts.fit !== false) fitMapToMosaic();
      resolve();
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('The map image could not be decoded'));
    };
    img.src = url;
  });
}

// ── Export ─────────────────────────────────────────────────────────────────

export function inTauri(): boolean {
  return typeof window !== 'undefined' && !!(window as any).__TAURI_INTERNALS__;
}

function sanitizeFilename(name: string): string {
  return name.replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^_+|_+$/g, '') || 'mosaic';
}

/** Choose a path with the Tauri dialog and let the sidecar write the file. */
export async function exportMosaic(format: 'netcdf' | 'png'): Promise<boolean> {
  const res = get(mosaicResult);
  if (!res) return false;
  return exportMosaicById(res.mosaic_id, format, `mosaic_${res.product}_${res.variable}`);
}

/**
 * Save the composite the engine holds under `mosaicId` (the mosaic panel's result, or a frame of
 * the live map): NetCDF (the merged grid with its provenance) or a georeferenced PNG (with `.pgw`
 * and `.prj` sidecars for GIS). The engine's attributes carry the data credit of the volumes.
 */
export async function exportMosaicById(mosaicId: string, format: 'netcdf' | 'png', baseName: string): Promise<boolean> {
  if (!inTauri()) {
    addToast('info', 'Exporting a mosaic needs the desktop app (file dialog)');
    return false;
  }
  if (exportPending) return false;
  const ext = format === 'netcdf' ? 'nc' : 'png';
  const label = format === 'netcdf' ? 'NetCDF' : 'PNG';
  const defaultName = `${sanitizeFilename(baseName)}.${ext}`;
  mosaicExporting.set(true);
  try {
    const { save } = await import('@tauri-apps/plugin-dialog');
    const path = await save({
      title: `Export mosaic as ${label}`,
      defaultPath: defaultName,
      filters: [{ name: label, extensions: [ext] }],
    });
    if (!path) return false; // cancelled
    const saved = await new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => {
        if (exportPending) {
          exportPending = null;
          offSaved();
          reject(new Error('No response from the sidecar'));
        }
      }, 60_000);
      const offSaved = wsManager.onMessage('file_saved', (m) => {
        if (!exportPending) return;
        clearTimeout(timer);
        exportPending = null;
        offSaved();
        resolve(String(m.path ?? path));
      });
      exportPending = {
        resolve,
        reject: (e) => {
          clearTimeout(timer);
          offSaved();
          reject(e);
        },
      };
      // The OS dialog already asked the user to confirm replacing an existing file.
      void wsManager.send({ type: 'export_mosaic', mosaic_id: mosaicId, format, path, overwrite: true });
    });
    addToast('success', `Saved ${saved}`, 5000);
    return true;
  } catch (err) {
    addToast('error', `Export failed: ${err instanceof Error ? err.message : String(err)}`, 8000);
    return false;
  } finally {
    exportPending = null;
    mosaicExporting.set(false);
  }
}

// ── External request (cloud panel) ─────────────────────────────────────────

export interface MosaicRequestDetail {
  fileIds?: string[];
  product?: MosaicProduct;
  variable?: string;
}

let requestWaitTimer: ReturnType<typeof setInterval> | null = null;

function applyExternalRequest(d: MosaicRequestDetail): void {
  const files = get(openFiles);
  const wanted = d.fileIds?.length ? d.fileIds : files.map((f) => f.id);
  const present = wanted.filter((id) => files.some((f) => f.id === id));
  const patch: Partial<MosaicSettings> = {};
  if (d.product && ['cappi', 'composite', 'echo_top'].includes(d.product)) patch.product = d.product;
  if (d.variable) patch.variable = d.variable;
  if (Object.keys(patch).length) updateMosaicSettings(patch);
  if (d.fileIds?.length) {
    mosaicDeselected.set(files.filter((f) => !present.includes(f.id)).map((f) => f.id));
  }
  if (present.length === 0) {
    mosaicRun.update((r) => ({ ...r, error: 'No matching open files for the mosaic request' }));
    return;
  }
  // Variable must exist in every selected file
  const choices = get(mosaicVariableChoices);
  if (choices.length && !choices.includes(get(mosaicSettings).variable)) {
    updateMosaicSettings({ variable: choices[0] });
  }
  runMosaic({ fileIds: present });
}

function handleExternalRequest(e: Event): void {
  const d = ((e as CustomEvent).detail ?? {}) as MosaicRequestDetail;
  if (requestWaitTimer) {
    clearInterval(requestWaitTimer);
    requestWaitTimer = null;
  }
  const missing = () => (d.fileIds ?? []).filter((id) => !get(openFiles).some((f) => f.id === id));
  if (missing().length === 0) {
    applyExternalRequest(d);
    return;
  }
  // Files requested by the cloud panel may still be opening: wait for them (max ~20 s)
  let waited = 0;
  requestWaitTimer = setInterval(() => {
    waited += 250;
    if (missing().length === 0 || waited >= 20_000) {
      if (requestWaitTimer) clearInterval(requestWaitTimer);
      requestWaitTimer = null;
      applyExternalRequest(d);
    }
  }, 250);
}

// ── Initialisation ─────────────────────────────────────────────────────────

let initialised = false;

/** Register WebSocket / window handlers. Idempotent; they live for the lifetime of the app. */
export function initMosaic(): void {
  if (initialised || typeof window === 'undefined') return;
  initialised = true;

  wsManager.onMessage('mosaic_result_ready', handleResultReady);
  wsManager.onMessage('mosaic_error', handleError);
  wsManager.onMessage('mosaic_probe_result', handleProbeResult);
  wsManager.onMessage('progress', handleProgress);
  wsManager.onMessage('error', handleGenericError);
  window.addEventListener('xradar:mosaic-request', handleExternalRequest);

  connectionStatus.subscribe((status) => {
    if (status !== 'connected' && get(mosaicRun).running) {
      finishRun({ error: 'Connection to the sidecar was lost' });
    }
  });

  // Close the sidecar's mosaic when its source files are all gone? Keep the image: it is still valid.
}

initMosaic();

/** Loading-time helpers for the panel. */
export function formatPercent(x: number | null | undefined, digits = 0): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return '–';
  return `${(x * 100).toFixed(digits)}%`;
}
