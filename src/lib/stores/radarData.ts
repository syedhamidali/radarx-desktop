import { writable } from 'svelte/store';

export interface SweepInfo {
  index: number;
  elevation: number | null;
  variables?: string[];
  /** 'rhi' for a range-height scan (fixed azimuth, rays differ in elevation); 'ppi' otherwise. */
  kind?: 'ppi' | 'rhi';
  /** RHI only: azimuth of the scan plane, degrees. */
  azimuth?: number;
  /** RHI only: lowest / highest ray elevation (deg), ray count, and radar altitude ASL (m) when known. */
  elevation_min?: number;
  elevation_max?: number;
  n_rays?: number;
  altitude?: number;
}

export interface RadarDataState {
  variables: string[];
  dimensions: Record<string, number>;
  attributes: Record<string, any>;
  sweeps: SweepInfo[];
  filePath: string | null;
}

export interface RenderResultState {
  image: string | null;
  metadata: any;
}

export interface ProcessingProgressState {
  percent: number;
  message: string;
}

export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected';

export const radarData = writable<RadarDataState>({
  variables: [],
  dimensions: {},
  attributes: {},
  sweeps: [],
  filePath: null,
});

export const selectedVariable = writable<string | null>(null);

export const selectedSweep = writable<number>(0);

export const renderResult = writable<RenderResultState>({
  image: null,
  metadata: null,
});

export const processingProgress = writable<ProcessingProgressState | null>(null);

export const connectionStatus = writable<ConnectionStatus>('disconnected');

export const exportNotification = writable<string | null>(null);
