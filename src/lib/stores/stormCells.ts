import { writable } from 'svelte/store';

export interface CellData {
  id: string;
  centroid_az: number;
  centroid_range: number;
  area_km2: number;
  max_dbz: number;
  mean_dbz: number;
  boundary_points: [number, number][];
}

export interface TrackData {
  track_id: string;
  cell_history: CellData[];
  speed_mps: number;
  direction_deg: number;
}

/** Where the cells were found: they only line up with the data of that file and sweep. */
export interface CellSource {
  file: string | null;
  variable: string;
  sweep: number;
}

export interface StormCellsState {
  cells: CellData[];
  tracks: TrackData[];
  selectedCellId: string | null;
  overlayVisible: boolean;
  isIdentifying: boolean;
  source: CellSource | null;
}

export const stormCells = writable<StormCellsState>({
  cells: [],
  tracks: [],
  selectedCellId: null,
  overlayVisible: true,
  isIdentifying: false,
  source: null,
});

/** Forget the cells and tracks (a different file was opened, or the user cleared them). */
export function clearStormCells(): void {
  stormCells.update((s) => ({
    ...s,
    cells: [],
    tracks: [],
    selectedCellId: null,
    isIdentifying: false,
    source: null,
  }));
}
