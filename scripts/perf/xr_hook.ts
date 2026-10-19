// Test hook injected by vite.perf.config.ts ONLY (never part of the normal build): exposes the
// stores and the websocket manager on window.__xr so the benchmark scripts can drive the
// production bundle (open / close files, switch sweeps) without going through native dialogs.
import { wsManager } from "../../src/lib/utils/websocket";
import { openFiles, activeFileId, removeFile, setActiveFile } from "../../src/lib/stores/fileManager";
import { selectedSweep, selectedVariable, radarData } from "../../src/lib/stores/radarData";
import { get } from "svelte/store";

(window as any).__xr = {
  wsManager,
  openFiles,
  activeFileId,
  removeFile,
  setActiveFile,
  selectedSweep,
  selectedVariable,
  radarData,
  get,
};
