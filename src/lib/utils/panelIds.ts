/**
 * Every right-hand panel, in the default order. One list for the panel manager (which owns the
 * components) and the workspace store (which saves and restores the order): a panel missing here is
 * dropped from a saved workspace when it is loaded, and the panel ends up at the bottom again.
 */
export const PANEL_IDS = [
  'layers',
  'cloud',
  'mosaic',
  'qpe',
  'nowcast',
  'wind',
  'volume',
  'timeseries',
  'crosssection',
  'sounding',
  'qc',
  'storm',
  'lightning',
  'modules',
  'pipeline',
  'processing',
  'export',
] as const;

export type PanelId = (typeof PANEL_IDS)[number];
