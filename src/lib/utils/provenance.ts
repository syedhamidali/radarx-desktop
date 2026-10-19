/**
 * Provenance of a layer: *what kind of numbers are these, and where do they come from?*
 *
 * The engine (python/engine/layer_provenance.py) sends a small `provenance` object with every field
 * and layer. This module is the pure part of the front end: the vocabulary (five classes), a
 * defensive parser (a malformed object never breaks the UI), time formatting and the mapping from
 * an engine message to the layer it describes. No Svelte, no DOM: it is unit tested with node.
 */

export const KINDS = ['observed', 'model', 'retrieved', 'simulated', 'stylised'] as const;
export type Kind = (typeof KINDS)[number];

/** Short chip text, a name, and the one-sentence definition (kept in step with the engine). */
export const KIND_INFO: Record<Kind, { short: string; label: string; definition: string }> = {
  observed: {
    short: 'OBS',
    label: 'Observed',
    definition:
      "Measured by an instrument; the values are what the instrument reported (after the quality control named in the method).",
  },
  model: {
    short: 'MODEL',
    label: 'Model',
    definition:
      'Output of a numerical weather prediction or reanalysis model; it is an estimate of the atmosphere on a grid, not a measurement.',
  },
  retrieved: {
    short: 'RETR',
    label: 'Retrieved',
    definition:
      "Derived from observations by an algorithm or physical model; carries the algorithm's assumptions and errors on top of the measurement's.",
  },
  simulated: {
    short: 'SIM',
    label: 'Simulated',
    definition: 'Produced by a forward model or synthetic generator; not a real observation.',
  },
  stylised: {
    short: 'STYLE',
    label: 'Stylised',
    definition: 'Drawn for appearance (shading, clouds, exaggeration); not a data value.',
  },
};

export interface Provenance {
  kind: Kind;
  source: string;
  title?: string;
  run_time?: string;
  valid_time?: string;
  valid_end?: string;
  resolution?: string;
  method?: string;
  parameters?: Record<string, unknown>;
  uncertainty?: string;
  references?: string[];
  attribution?: string;
  licence?: string;
  inputs?: string[];
}

const TEXT_FIELDS = [
  'title',
  'run_time',
  'valid_time',
  'valid_end',
  'resolution',
  'method',
  'uncertainty',
  'attribution',
  'licence',
] as const;

const str = (v: unknown, max = 4000): string | undefined =>
  typeof v === 'string' && v.trim() ? v.trim().slice(0, max) : undefined;

/**
 * Validate an engine `provenance` object. Returns null when it is not one (missing, wrong class);
 * a layer without usable provenance is shown as "unknown", never guessed.
 */
export function parseProvenance(raw: unknown): Provenance | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  if (typeof o.kind !== 'string' || !(KINDS as readonly string[]).includes(o.kind)) return null;
  const out: Provenance = { kind: o.kind as Kind, source: str(o.source, 300) ?? 'unknown' };
  for (const k of TEXT_FIELDS) {
    const v = str(o[k]);
    if (v) out[k] = v;
  }
  if (o.parameters && typeof o.parameters === 'object' && !Array.isArray(o.parameters)) {
    const entries = Object.entries(o.parameters as Record<string, unknown>).slice(0, 40);
    if (entries.length) out.parameters = Object.fromEntries(entries);
  }
  for (const k of ['references', 'inputs'] as const) {
    const list = o[k];
    if (Array.isArray(list)) {
      const items = list.map((x) => str(x, 600)).filter((x): x is string => !!x).slice(0, 20);
      if (items.length) out[k] = items;
    }
  }
  return out;
}

/** Milliseconds of an ISO-8601 UTC time, or null. */
export function isoMs(iso: string | undefined | null): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms;
}

/** Seconds from `reference` (e.g. the active radar scan) to `valid`; null if either is unknown. */
export function offsetSeconds(valid: string | undefined | null, reference: string | undefined | null): number | null {
  const a = isoMs(valid);
  const b = isoMs(reference);
  return a === null || b === null ? null : Math.round((a - b) / 1000);
}

/** "+12 min", "-1 h 05 min", "same time", "unknown" (same wording as the engine). */
export function formatOffset(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return 'unknown';
  const s = Math.abs(Math.round(seconds));
  if (s < 30) return 'same time';
  const sign = seconds > 0 ? '+' : '-';
  if (s < 3600) return `${sign}${Math.round(s / 60)} min`;
  const total = Math.round(s / 60);
  const h = Math.floor(total / 60);
  const m = total % 60;
  if (h < 48) return m ? `${sign}${h} h ${String(m).padStart(2, '0')} min` : `${sign}${h} h`;
  return `${sign}${Number((s / 86400).toFixed(1))} d`;
}

/** "2022-03-30 00:05 UTC" for an ISO time; "unknown" when missing. Never converts to local time. */
export function formatUtc(iso: string | undefined | null): string {
  const ms = isoMs(iso);
  if (ms === null) return 'unknown';
  return new Date(ms).toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
}

export function formatValue(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(5)));
  if (typeof v === 'string') return v;
  if (Array.isArray(v)) return '[' + v.map(formatValue).join(', ') + ']';
  if (typeof v === 'object') {
    return (
      '{' +
      Object.entries(v as Record<string, unknown>)
        .map(([k, x]) => `${k}=${formatValue(x)}`)
        .join(', ') +
      '}'
    );
  }
  return String(v);
}

// ── Layers: which engine message describes which layer ───────────────────────────────────────────

export type LayerGroup = 'Radar' | 'Derived' | 'Mosaic' | 'Winds' | 'Cells' | 'Sections' | 'Environment' | 'Appearance';

export interface LayerKey {
  id: string;
  group: LayerGroup;
  title: string;
  /** Layers of a file disappear with it. */
  fileId?: string;
}

const GROUP_ORDER: LayerGroup[] = ['Radar', 'Derived', 'Mosaic', 'Winds', 'Cells', 'Sections', 'Environment', 'Appearance'];
export const groupRank = (g: LayerGroup): number => GROUP_ORDER.indexOf(g);

/** Where the provenance object sits in a message (most messages: top level). */
export function provenanceOf(msg: any): unknown {
  return msg?.provenance ?? msg?.sounding?.provenance ?? msg?.result?.provenance ?? null;
}

/**
 * The layer a message belongs to, or null when the message carries no layer provenance.
 * `derived_variables` carries many layers and is handled by the store (see `derivedLayerKeys`).
 */
export function layerKeyFromMessage(type: string, msg: any, prov: Provenance | null): LayerKey | null {
  const fid = typeof msg?.file_id === 'string' ? msg.file_id : undefined;
  const title = prov?.title;
  switch (type) {
    case 'file_opened':
      return fid ? { id: `file:${fid}`, group: 'Radar', title: title ?? 'Radar moments', fileId: fid } : null;
    case 'sweep_data':
      // a derived variable shown on the PPI; raw moments are the file's own layer
      return msg?.derived && fid && typeof msg.variable === 'string'
        ? { id: `derived:${fid}:${msg.variable}`, group: 'Derived', title: title ?? String(msg.label ?? msg.variable), fileId: fid }
        : null;
    case 'module_field':
      return typeof msg?.module === 'string' && typeof msg?.name === 'string'
        ? { id: `module:${fid ?? ''}:${msg.module}:${msg.name}`, group: 'Derived', title: title ?? `${msg.module}: ${msg.name}`, fileId: fid }
        : null;
    case 'mosaic_result':
      return typeof msg?.mosaic_id === 'string' ? { id: `mosaic:${msg.mosaic_id}`, group: 'Mosaic', title: title ?? 'Radar mosaic' } : null;
    case 'multidoppler_result':
    case 'wind_volume':
      return typeof msg?.wind_id === 'string' ? { id: `winds:${msg.wind_id}`, group: 'Winds', title: title ?? 'Multi-Doppler winds' } : null;
    case 'cells_identified':
    case 'cells_tracked':
      return { id: 'cells', group: 'Cells', title: title ?? 'Storm cells' };
    case 'cross_section_result':
      return { id: 'xsec', group: 'Sections', title: title ?? 'Cross-section' };
    case 'cross_section_3d_data':
      return { id: 'xsec3d', group: 'Sections', title: title ?? '3D cross-section' };
    case 'volume_data':
      return { id: 'volume3d', group: 'Sections', title: title ?? '3D volume' };
    case 'sounding_result':
      return { id: 'sounding', group: 'Environment', title: title ?? 'Sounding' };
    case 'env_fetch_result':
      return typeof msg?.request_id === 'string'
        ? { id: `env:${msg.request_id}`, group: 'Environment', title: title ?? prov?.source ?? 'Environment data' }
        : null;
    default:
      return null;
  }
}

/** One layer per derived variable of a file (`derived_variables`). */
export function derivedLayerKeys(msg: any): { key: LayerKey; raw: unknown }[] {
  const fid = typeof msg?.file_id === 'string' ? msg.file_id : undefined;
  if (!fid || !Array.isArray(msg?.variables)) return [];
  return msg.variables
    .filter((v: any) => v && typeof v.name === 'string')
    .map((v: any) => ({
      key: { id: `derived:${fid}:${v.name}`, group: 'Derived' as const, title: String(v.label ?? v.name), fileId: fid },
      raw: v.provenance,
    }));
}
