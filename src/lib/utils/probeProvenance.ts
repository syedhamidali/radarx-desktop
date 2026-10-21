/**
 * The provenance class shown next to a probed value (PPI probe, map/mosaic probe, cross-section and
 * RHI read-outs): *is the number under the pointer measured, retrieved, modelled ...?*
 *
 * Pure functions (no Svelte, no DOM; unit tested with node). The viewers derive the tag once per
 * layer from the layer registry (`stores/layers.ts`), never per mouse move, and never ask the
 * engine: everything needed came with the layer's own messages.
 */
import { KIND_INFO, type Kind } from './provenance.ts';
import type { LayerState } from './layerRegistry.ts';

/** The five layer classes, ADJUSTED (gauge-adjusted QPE) and "unknown" (nothing was sent). */
export type ProbeKind = Kind | 'adjusted' | 'unknown';

export interface ProbeTag {
  kind: ProbeKind;
  /** Chip text: OBS, MODEL, RETR, SIM, STYLE, ADJ or "?". */
  short: string;
  /** Spoken / tooltip text, e.g. "Observed data". */
  label: string;
}

/** What the read-out is reading. */
export type ProbeTarget =
  /** A variable of a radar file: a raw moment (the file's own layer) or a derived variable. */
  | { type: 'variable'; fileId: string | null; variable: string | null; derived: boolean }
  /** A module output field drawn in place of the variable. */
  | { type: 'module'; fileId: string | null; module: string; name: string }
  /** The map layer: a mosaic, or a QPE field (which states RETRIEVED / ADJUSTED itself). */
  | { type: 'map'; mosaicId: string | null; provenanceType?: string | null };

export const UNKNOWN_TAG: ProbeTag = { kind: 'unknown', short: '?', label: 'Provenance unknown' };

export const ADJUSTED_TAG: ProbeTag = {
  kind: 'adjusted',
  short: 'ADJ',
  label: 'Adjusted data (retrieved, then adjusted to rain gauges)',
};

export function tagForKind(kind: Kind): ProbeTag {
  const info = KIND_INFO[kind];
  return { kind, short: info.short, label: `${info.label} data` };
}

/** The target for a variable of a file, given the file's derived variables (by name). */
export function variableTarget(
  fileId: string | null,
  variable: string | null | undefined,
  derived: readonly { name: string }[],
): ProbeTarget {
  const v = variable || null;
  return { type: 'variable', fileId, variable: v, derived: !!v && derived.some((d) => d.name === v) };
}

/** Layer ids that may describe the target, most specific first. */
export function probeLayerIds(target: ProbeTarget): string[] {
  switch (target.type) {
    case 'variable':
      if (!target.fileId || !target.variable) return [];
      return target.derived ? [`derived:${target.fileId}:${target.variable}`] : [`file:${target.fileId}`];
    case 'module':
      // `module_field` names its file when it has one; older replies leave the id empty.
      return [`module:${target.fileId ?? ''}:${target.module}:${target.name}`, `module::${target.module}:${target.name}`];
    case 'map':
      return target.mosaicId ? [`mosaic:${target.mosaicId}`] : [];
  }
}

/**
 * The tag for a target. A layer that was never described is "unknown", never guessed: a derived
 * variable is not called observed because its file is.
 */
export function probeTag(state: LayerState, target: ProbeTarget): ProbeTag {
  if (target.type === 'map' && typeof target.provenanceType === 'string') {
    const t = target.provenanceType.toUpperCase();
    if (t === 'ADJUSTED') return ADJUSTED_TAG;
    if (t === 'RETRIEVED') return tagForKind('retrieved');
  }
  for (const id of probeLayerIds(target)) {
    const kind = state[id]?.provenance?.kind;
    if (kind) return tagForKind(kind);
  }
  return UNKNOWN_TAG;
}
