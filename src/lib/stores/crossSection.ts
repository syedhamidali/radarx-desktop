import { writable } from 'svelte/store';

export interface CrossSectionData {
  distance_km: number[];
  height_km: number[][]; // per-sweep heights (km above the radar)
  values: number[][];    // per-sweep values
  variable: string;
  units: string;
  n_sweeps: number;
  /** Fixed elevation of each returned sweep. */
  elevations_deg?: number[];
  /** Per sample: ground distance and bearing from the radar. */
  ground_range_km?: number[];
  azimuth_deg?: number[];
  /** The line the engine actually used, [lat, lon]. */
  start?: [number, number];
  end?: [number, number];
  radar_lat?: number;
  radar_lon?: number;
  /** Radar site altitude above sea level, when the file records it. */
  radar_altitude_m?: number | null;
  /** Scan start, UTC ("YYYY-MM-DD HH:MM:SS"), when the file records it. */
  time?: string | null;
  /** Set when the section is an RHI-style cut at this bearing out of the radar. */
  rhi_azimuth?: number;
}

export interface VerticalProfileData {
  /** Where the profile was taken (echoed by the server), when known. */
  lat?: number;
  lon?: number;
  heights_km: number[];
  values: number[];
  elevations_deg: number[];
  variable: string;
  units: string;
}

export interface CrossSectionLinePoints {
  startX: number; // NDC or pixel coords on PPI
  startY: number;
  endX: number;
  endY: number;
  startLat: number;
  startLon: number;
  endLat: number;
  endLon: number;
}

/** Where the cross-section is shown: the small sidebar panel, under the PPI, or full-size on top. */
export type CrossSectionLayout = 'panel' | 'dock' | 'modal';

/** Colour settings shared by the panel, the dock and the large view. */
export interface CrossSectionStyle {
  variable: string;
  cmap: string;
  vmin: number;
  vmax: number;
}

/** What the PPI itself is showing (so the section can match it). */
export interface PpiStyle extends CrossSectionStyle {
  units: string;
}

export interface CrossSectionState {
  /** Whether the cross-section line drawing tool is active */
  lineToolActive: boolean;
  /** Whether the vertical profile click-to-probe mode is active */
  probeToolActive: boolean;
  /** Whether the next click on the PPI picks an azimuth for an RHI-style cut out of the radar */
  rhiToolActive: boolean;
  /** The drawn line on the PPI (null if not drawn) */
  line: CrossSectionLinePoints | null;
  /** Cross-section result data */
  crossSectionData: CrossSectionData | null;
  /** Vertical profile result data (multiple variables can be stacked) */
  verticalProfiles: VerticalProfileData[];
  /** Whether a request is in progress */
  isLoading: boolean;
  /** Bumped by the PPI line overlay to ask the panel to request the cross-section for `line`. */
  requestSeq: number;
  /** Active tab: 'cross-section' or 'vertical-profile' */
  activeTab: 'cross-section' | 'vertical-profile';
  /** Where the section is shown. */
  layout: CrossSectionLayout;
  /** Variable / colormap / range of the section (owned by CrossSectionPanel, editable anywhere). */
  style: CrossSectionStyle;
  /** The PPI's current variable / colormap / range, for "match the PPI". */
  ppiStyle: PpiStyle | null;
  /** When set, the next request is an RHI-style cut at this bearing out of the radar. */
  rhi: { azimuth: number; maxRangeKm: number } | null;
  /** Range (km) of an RHI-style cut; null = as far as the PPI shows. */
  rhiRangeKm: number | null;
  /** Range the PPI currently shows (km), to default the RHI range to. */
  ppiMaxRangeKm: number | null;
  /** The section's colormap / range follow the PPI's until they are changed on the section. */
  styleLinked: boolean;
}

export const crossSectionStore = writable<CrossSectionState>({
  lineToolActive: false,
  probeToolActive: false,
  rhiToolActive: false,
  line: null,
  crossSectionData: null,
  verticalProfiles: [],
  isLoading: false,
  requestSeq: 0,
  activeTab: 'cross-section',
  layout: 'panel',
  style: { variable: '', cmap: 'turbo', vmin: 0, vmax: 60 },
  ppiStyle: null,
  rhi: null,
  rhiRangeKm: null,
  ppiMaxRangeKm: null,
  styleLinked: true,
});

/** Forget the line, probe results and any running request (a different file was opened). */
export function resetCrossSection(): void {
  crossSectionStore.update((s) => ({
    ...s,
    lineToolActive: false,
    probeToolActive: false,
    rhiToolActive: false,
    line: null,
    crossSectionData: null,
    verticalProfiles: [],
    isLoading: false,
    rhi: null,
    layout: s.layout === 'modal' ? 'panel' : s.layout,
  }));
}

const DOCK_PREF_KEY = 'xradar.cs.dock.v1';

/** Whether a freshly drawn line should open the large view under the PPI (the default). */
export function dockPreferred(): boolean {
  try {
    return localStorage.getItem(DOCK_PREF_KEY) !== '0';
  } catch {
    return true;
  }
}

let layoutBeforeModal: CrossSectionLayout = 'panel';

/** Close the full-size view and go back to where it was opened from (the dock or the panel). */
export function closeCrossSectionModal(): void {
  crossSectionStore.update((s) => (s.layout === 'modal' ? { ...s, layout: layoutBeforeModal } : s));
}

export function setCrossSectionLayout(layout: CrossSectionLayout): void {
  // Closing the dock by hand means "keep it small from now on"; opening it brings the default back.
  if (layout === 'modal') {
    crossSectionStore.update((s) => {
      if (s.layout !== 'modal') layoutBeforeModal = s.layout;
      return s.layout === 'modal' ? s : { ...s, layout };
    });
    return;
  }
  if (layout === 'panel' || layout === 'dock') {
    try {
      localStorage.setItem(DOCK_PREF_KEY, layout === 'dock' ? '1' : '0');
    } catch {
      /* storage unavailable: ignore */
    }
  }
  crossSectionStore.update((s) => (s.layout === layout ? s : { ...s, layout }));
}

/** Change the section's variable / colormap / range. Editing the colours unlinks them from the PPI. */
export function patchSectionStyle(patch: Partial<CrossSectionStyle>, opts: { link?: boolean } = {}): void {
  crossSectionStore.update((s) => ({
    ...s,
    style: { ...s.style, ...patch },
    styleLinked: opts.link ?? false,
  }));
}

/** Ask CrossSectionPanel (which owns the request) to extract the section for the current line. */
export function requestSection(): void {
  crossSectionStore.update((s) => ({ ...s, requestSeq: s.requestSeq + 1 }));
}

/** Replace the line (a free line: not an RHI any more) and re-run the section. */
export function setSectionLine(line: CrossSectionLinePoints): void {
  crossSectionStore.update((s) => ({ ...s, line, rhi: null, requestSeq: s.requestSeq + 1 }));
}

/**
 * RHI-style cut: a line out of the radar on `azimuth` to `maxRangeKm`. The line to draw on the
 * PPI is `line` (computed by the caller); the engine builds the exact geodesic itself.
 */
export function requestRhi(
  azimuth: number,
  maxRangeKm: number,
  line: CrossSectionLinePoints,
): void {
  crossSectionStore.update((s) => ({
    ...s,
    line,
    rhi: { azimuth, maxRangeKm },
    requestSeq: s.requestSeq + 1,
  }));
}

