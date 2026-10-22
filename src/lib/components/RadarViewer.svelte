<script lang="ts">
  import { cssRgb01 } from '../utils/themeColors';
  import { onMount, onDestroy, tick, untrack } from 'svelte';
  import { get } from 'svelte/store';
  import { radarData, selectedVariable, selectedSweep, connectionStatus } from '../stores/radarData';
  import { activeFileId } from '../stores/fileManager';
  import { wsManager } from '../utils/websocket';
  import { retryOp, dismissOp, operations } from '../utils/requestScheduler';
  import { rayAtAzimuth } from '../utils/meshBuilder';
  import { PPIRenderer, getCachedSweep, getDefaultCmap, getDefaultRange, type SweepDataEntry } from '../utils/ppiRenderer';
  import ColorBar from './ColorBar.svelte';
  import StormOverlay from './StormOverlay.svelte';
  import LightningOverlay from './LightningOverlay.svelte';
  import { lightning } from '../stores/lightning';
  import ModuleOverlay from './ModuleOverlay.svelte';
  import { displayedField, hideField, type DisplayedField } from '../stores/modules';
  import { activeDerived } from '../stores/derived';
  import { styleFor, variableStylesRevision } from '../utils/variableStyles';
  import MapBackground from './MapBackground.svelte';
  import GeoOverlay from './GeoOverlay.svelte';
  import { lazy } from '../utils/lazy.svelte';
  import { showRadarSites } from '../stores/radarSitesPref';
  import MeasureTools from './MeasureTools.svelte';
  import Annotations from './Annotations.svelte';
  import MeasureToolbar from './MeasureToolbar.svelte';
  import { annotationsStore, addPendingPoint, finishAreaMeasurement, setMeasureMode, setAnnotationMode } from '../stores/annotations';
  import { crossSectionStore, type CrossSectionLinePoints } from '../stores/crossSection';
  import CrossSectionLine from './CrossSectionLine.svelte';
  import { setProbePoint } from '../stores/temporal';
  import PerformanceMonitor from './PerformanceMonitor.svelte';
  import BoxSelectTool from './BoxSelectTool.svelte';
  import { boxSelectActive, selectedBox } from '../stores/volumeSelect';
  import { mosaicLayer, mosaicDisplay, registerViewBoundsProvider, type GeoBounds } from '../stores/mosaic';
  import NowcastOverlay from './NowcastOverlay.svelte';
  import { windLayer } from '../stores/wind';
  import { effectiveCmap, chooseCmap } from '../stores/cmapChoice';
  import { dataSpaceToLatLon, latLonToDataSpace, pixelToLatLon } from '../utils/geoProjection';
  import type { SweepInfo } from '../stores/radarData';
  import RhiViewer from './RhiViewer.svelte';
  import { sweepLabel } from '../utils/rhiGeometry';
  import { maskOf, maskTest, float32Mask, isMaskActive, valueMasks } from '../stores/valueMask';
  import MaskPopover from './MaskPopover.svelte';
  import { mapHidden, setMapHidden } from '../stores/settings';
  import { getShortcutDisplay } from '../utils/shortcuts';
  import { layerMap } from '../stores/layers';
  import { probeTag, variableTarget } from '../utils/probeProvenance';
  import ProbeProvenanceTag from './ProbeProvenanceTag.svelte';

  // ── State ──────────────────────────────────────────────────────────────────
  let isLoading = $state(false);
  let hasRendered = $state(false);
  /** The request for the sweep on screen failed (timeout / error): offer a Retry. */
  let loadError: { variable: string; sweep: number; message: string; opId: string } | null = $state.raw(null);
  /** `variable:sweep` that turned out to be unavailable during the current fallback chain (loop guard). */
  const unavailableTried = new Set<string>();

  // Container sizing
  let canvasEl: HTMLDivElement;
  let webglCanvas: HTMLCanvasElement;
  let resizeObserver: ResizeObserver | null = null;
  let themeObserver: MutationObserver | null = null;
  let containerWidth = $state(800);
  let containerHeight = $state(600);

  // Zoom / pan state
  let scale = $state(1);
  let translateX = $state(0);
  let translateY = $state(0);
  let isDragging = $state(false);
  let dragStartX = 0;
  let dragStartY = 0;
  let dragStartTX = 0;
  let dragStartTY = 0;

  // Coordinate overlay
  let showCoords = $state(false);
  let coordRange = $state(0);
  let coordAzimuth = $state(0);

  // Current data info
  let currentVmin = $state(0);
  let currentVmax = $state(1);
  let currentUnits = $state('');
  let currentMaxRange = $state(1);
  let currentCmap = $state('turbo');

  // Map / geo overlay. The map on/off choice is a persisted setting shared with the 3D views.
  const showMap = $derived(!$mapHidden);
  let mapOpacity = $state(1.0);
  let showGeoOverlay = $state(true);
  // Radar sites layer (markers + site card): its code loads the first time it is switched on.
  const sitesLayer = lazy(() => import('./RadarSitesLayer.svelte'));
  $effect(() => { if ($showRadarSites) void sitesLayer.load(); });

  // Measurement / annotation mode
  let measureToolsVisible = $state(false);
  const measureActive = $derived($annotationsStore.measureMode !== 'none' || $annotationsStore.annotationMode !== 'none');

  // Cross-section / vertical-profile tool state
  const csLineToolActive = $derived($crossSectionStore.lineToolActive);
  const csProbeToolActive = $derived($crossSectionStore.probeToolActive);
  const csRhiToolActive = $derived($crossSectionStore.rhiToolActive);

  // Value probe
  let probeActive = $state(false);
  let probeValue: number | null = $state(null);
  let probeLabel: string | null = $state(null);
  /** The gate under the probe exists but the value mask hides it. */
  let probeHidden = $state(false);
  let probeX = $state(0);
  let probeY = $state(0);
  let currentEntry: SweepDataEntry | null = $state(null);
  /** Range-height sweep on screen (shown by RhiViewer instead of the map PPI). */
  let rhiEntry: SweepDataEntry | null = $state.raw(null);

  // WebGL renderer
  let ppi: PPIRenderer | null = $state(null);
  let unsubSweepData: (() => void) | null = null;
  let unsubSweepStore: (() => void) | null = null;
  let unsubVarStore: (() => void) | null = null;
  let unsubFileStore: (() => void) | null = null;
  let unsubSweepFailed: (() => void) | null = null;
  let lastRequestKey = '';
  let unsubField: (() => void) | null = null;

  // Module field currently replacing the variable (null = normal variable shown)
  let fieldShown: DisplayedField | null = $state.raw(null);

  // ── Derived state ─────────────────────────────────────────────────────────
  const variables = $derived($radarData.variables);
  /** Derived variables (module outputs) of the open file, offered under their own group in the select. */
  const derivedVars = $derived($activeDerived);
  /** Registered presentation of the selected variable (derived variables only). */
  const varStyle = $derived.by(() => {
    void $variableStylesRevision;
    return styleFor($selectedVariable);
  });
  const variableLabel = $derived(varStyle?.label ?? $selectedVariable ?? '');
  /** Class swatches for the legend: a shown module field, else a categorical derived variable. */
  const legendCategories = $derived.by(() => {
    const f = fieldShown as DisplayedField | null;
    return f ? f.spec.categories : (varStyle?.categories ?? null);
  });
  const legendTitle = $derived(((fieldShown as DisplayedField | null)?.title) ?? variableLabel);
  const sweeps = $derived($radarData.sweeps);
  const variable = $derived($selectedVariable);
  /** Provenance class of what the probe reads: once per layer, not per mouse move. */
  const probeProv = $derived.by(() => {
    const f = fieldShown as DisplayedField | null;
    const fileId = $activeFileId;
    if (f) return probeTag($layerMap, { type: 'module', fileId, module: f.module, name: f.name });
    return probeTag($layerMap, variableTarget(fileId, $selectedVariable, derivedVars));
  });
  /** Value mask of what is on screen: a module field has its own, else the variable's. */
  const maskKey = $derived(fieldShown ? `field:${(fieldShown as DisplayedField).name}` : ($selectedVariable ?? ''));
  const activeMask = $derived(maskOf($valueMasks, maskKey));
  const sweep = $derived($selectedSweep);
  /** The selected sweep is an RHI (range-height scan): RhiViewer replaces the map view. */
  const sweepMeta = $derived(sweeps.find((sw) => sw.index === sweep));
  const isRhi = $derived(sweepMeta?.kind === 'rhi');
  const hasData = $derived($radarData.filePath !== null || $radarData.variables.length > 0);
  const isConnected = $derived($connectionStatus === 'connected');
  const zoomLabel = $derived(Math.round(scale * 100) + '%');
  // Mosaic: show the map with the mosaic instead of the PPI canvas
  const mosaicViewOn = $derived(!!$mosaicLayer && $mosaicLayer.visible && $mosaicLayer.view);
  // Multi-Doppler wind analysis drawn on the map (optionally instead of the PPI canvas)
  const windViewOn = $derived(!!$windLayer && $windLayer.visible);
  const windHidesPpi = $derived(windViewOn && !!$windLayer?.hidePpi);

  // Radar site location from file attributes
  const radarLat = $derived(parseFloat($radarData.attributes?.latitude ?? $radarData.attributes?.origin_latitude ?? '0') || 0);
  const radarLon = $derived(parseFloat($radarData.attributes?.longitude ?? $radarData.attributes?.origin_longitude ?? '0') || 0);
  const radarName = $derived(($radarData.attributes?.instrument_name ?? $radarData.attributes?.station_name ?? '') as string);

  // Tell the cross-section what the PPI shows (variable, colormap, range, reach) so the section can
  // match it and an RHI defaults to the PPI's range.
  $effect(() => {
    const v = variable;
    const cm = currentCmap;
    const lo = currentVmin;
    const hi = currentVmax;
    const un = currentUnits;
    const km = currentMaxRange > 1 ? currentMaxRange / 1000 : null;
    const shown = hasRendered && !fieldShown;
    untrack(() => {
      if (!v || !shown) return;
      crossSectionStore.update((st) => {
        const p = st.ppiStyle;
        if (p && p.variable === v && p.cmap === cm && p.vmin === lo && p.vmax === hi && p.units === un && st.ppiMaxRangeKm === km) return st;
        return { ...st, ppiStyle: { variable: v, cmap: cm, vmin: lo, vmax: hi, units: un }, ppiMaxRangeKm: km };
      });
    });
  });

  // The value mask changed (or the variable did): redraw, the shader hides the gates
  $effect(() => {
    void activeMask;
    if (hasRendered) renderFrame();
  });

  // Re-render when map toggle changes (to update transparent background)
  $effect(() => {
    if (hasRendered) {
      void showMap;
      renderFrame();
    }
  });

  /** Forget the module-field override without re-rendering the variable (the caller does that). */
  function dropFieldOverride() {
    if (!fieldShown) return;
    fieldShown = null;
    hideField();
  }

  /** Field cleared from outside (Back button, file closed, module cleared): show the variable again. */
  function restoreVariable() {
    const v = $selectedVariable;
    if (!v) return;
    lastRequestKey = '';
    requestNewData(v, $selectedSweep);
  }

  /** Tell the request scheduler what is on screen so queued work for anything else is dropped. */
  function syncViewContext() {
    wsManager.setViewContext({
      fileId: get(activeFileId),
      variable: get(selectedVariable),
      sweep: get(selectedSweep),
    });
  }

  function requestNewData(v: string, s: number) {
    const key = `${v}:${s}`;
    if (!v || key === lastRequestKey) return;
    loadError = null;
    // Changing variable/sweep/file leaves the displayed module field.
    dropFieldOverride();
    lastRequestKey = key;
    currentCmap = effectiveCmap(v); // the user's choice for this variable (shared with the 3D view)
    const range = getDefaultRange(v);
    if (range) {
      currentVmin = range[0];
      currentVmax = range[1];
    }
    const cached = getCachedSweep(v, s);
    if (cached) {
      isLoading = false;
      renderSweepData(cached);
    } else {
      isLoading = true;
      wsManager.requestSweepData(v, s, undefined, { priority: 'visible' });
    }
  }

  /** The sweep on screen could not be loaded. */
  function onSweepRequestFailed(e: { variable: string; sweep: number; reason: string; message: string; opId: string }) {
    if (e.variable !== $selectedVariable || e.sweep !== $selectedSweep) return;
    isLoading = false;
    lastRequestKey = ''; // let the user (or the fallback) ask for it again
    if (e.reason === 'unavailable') {
      unavailableTried.add(`${e.variable}:${e.sweep}`);
      const sweepInfo = $radarData.sweeps.find((sw) => sw.index === e.sweep);
      const pool = (sweepInfo?.variables && sweepInfo.variables.length > 0 ? sweepInfo.variables : $radarData.variables)
        .filter((n) => n !== e.variable && !unavailableTried.has(`${n}:${e.sweep}`));
      const fallback = pool.includes('DBZH') ? 'DBZH' : pool[0];
      if (fallback) {
        selectedVariable.set(fallback); // → subscription → requestNewData
        return;
      }
    }
    loadError = {
      variable: e.variable,
      sweep: e.sweep,
      message: e.reason === 'unavailable' ? `${e.variable} is not available in this sweep` : e.message,
      opId: e.opId,
    };
  }

  function retryLoad() {
    const err = loadError;
    if (!err) return;
    loadError = null;
    isLoading = true;
    lastRequestKey = `${err.variable}:${err.sweep}`;
    if (get(operations).some((o) => o.id === err.opId && o.state === 'failed')) {
      retryOp(err.opId);
    } else {
      // The failed op is gone (dismissed / trimmed): ask again directly.
      wsManager.requestSweepData(err.variable, err.sweep, undefined, { priority: 'visible' });
    }
  }

  function dismissLoadError() {
    if (loadError) dismissOp(loadError.opId);
    loadError = null;
  }

  // ── Lifecycle ────────────���────────────────────────���────────────────────────
  onMount(() => {
    unsubSweepData = wsManager.onMessage('sweep_data_ready', (entry: SweepDataEntry) => {
      // A slow response for a sweep/variable the user already left must not repaint over the current view
      if (entry.variable !== $selectedVariable || entry.sweep !== $selectedSweep) return;
      loadError = null;
      unavailableTried.clear();
      // Data for the selected variable (e.g. a QC result) replaces a displayed module field.
      dropFieldOverride();
      renderSweepData(entry);
    });

    unsubSweepFailed = wsManager.onMessage('sweep_request_failed', onSweepRequestFailed);

    unsubField = displayedField.subscribe((f) => {
      if (f) {
        if (f.sweep !== $selectedSweep) {
          hideField();
          return;
        }
        fieldShown = f;
        void renderSweepData(f.entry, f);
      } else if (fieldShown) {
        fieldShown = null;
        restoreVariable();
      }
    });

    // Subscribe to store changes for animation / external sweep updates
    unsubFileStore = activeFileId.subscribe(() => {
      unavailableTried.clear();
      syncViewContext();
    });
    unsubSweepStore = selectedSweep.subscribe((s) => {
      syncViewContext();
      const v = $selectedVariable;
      if (!v) return;
      // If the sweep has a known variable list and the current variable isn't in it,
      // switch to DBZH (or first available) — handles multi-variable NEXRAD VCPs.
      const sweepInfo = $radarData.sweeps.find((sw) => sw.index === s);
      const available = sweepInfo?.variables;
      if (available && available.length > 0 && !available.includes(v)) {
        const fallback = available.includes('DBZH') ? 'DBZH' : available[0];
        selectedVariable.set(fallback); // triggers unsubVarStore → requestNewData
        return;
      }
      requestNewData(v, s);
    });
    unsubVarStore = selectedVariable.subscribe((v) => {
      syncViewContext();
      if (v) requestNewData(v, $selectedSweep);
    });

    resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (width > 0 && height > 0) {
          const w = Math.round(width);
          const h = Math.round(height);
          containerWidth = w;
          containerHeight = h;
          if (ppi) {
            ppi.resize(w, h);
            renderFrame();
          }
        }
      }
    });
    if (canvasEl) resizeObserver.observe(canvasEl);

    // The theme decides the plain background (map hidden): redraw when data-theme changes.
    themeObserver = new MutationObserver(() => renderFrame());
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

    // Mosaic hooks: the panel asks for the visible lon/lat box and for "fit to mosaic".
    registerViewBoundsProvider(currentViewBounds);
    window.addEventListener('xradar:mosaic-fit', onMosaicFit);
  });

  onDestroy(() => {
    registerViewBoundsProvider(null);
    window.removeEventListener('xradar:mosaic-fit', onMosaicFit);
    resizeObserver?.disconnect();
    themeObserver?.disconnect();
    unsubSweepData?.();
    unsubField?.();
    unsubSweepStore?.();
    unsubVarStore?.();
    unsubFileStore?.();
    unsubSweepFailed?.();
    ppi?.destroy();
  });

  // ── Rendering ─────────────��──────────────────────────��────────────────────
  function initWebGL(): boolean {
    if (ppi) return true;
    if (!webglCanvas) return false;
    ppi = new PPIRenderer();
    const ok = ppi.init(webglCanvas);
    if (!ok) {
      console.error('[RadarViewer] WebGL init failed');
      ppi = null;
      return false;
    }
    ppi.resize(containerWidth, containerHeight);
    return true;
  }

  /** An RHI sweep goes to RhiViewer; the PPI renderer never sees it (its rays share one azimuth). */
  function showRhi(entry: SweepDataEntry) {
    rhiEntry = entry;
    currentMaxRange = entry.maxRange;
    currentUnits = entry.units;
    if (!getDefaultRange(entry.variable)) {
      currentVmin = entry.vmin;
      currentVmax = entry.vmax;
    }
    isLoading = false;
  }

  async function renderSweepData(entry: SweepDataEntry, field: DisplayedField | null = null) {
    if (entry.kind === 'rhi') {
      showRhi(entry);
      return;
    }
    rhiEntry = null;
    // Wait for Svelte to flush DOM updates (canvas may not exist yet if hasData just became true)
    await tick();

    // Retry WebGL init a few times — canvas may still be binding
    let retries = 0;
    while (!initWebGL() && retries < 5) {
      retries++;
      await new Promise((r) => requestAnimationFrame(r));
    }

    if (!ppi) {
      console.error('[RadarViewer] WebGL init failed after retries — canvas:', webglCanvas);
      isLoading = false;
      return;
    }
    // The field was dismissed (or replaced) while we waited: do not paint it.
    if (field && fieldShown !== field) return;

    const t0 = performance.now();
    currentEntry = entry;
    ppi.uploadSweepData(entry);
    currentMaxRange = entry.maxRange;
    currentUnits = entry.units;

    if (field) {
      // Module field: colormap / range come from the module, not from the variable defaults
      currentCmap = field.spec.cmap;
      currentVmin = field.spec.vmin;
      currentVmax = field.spec.vmax;
      currentUnits = field.spec.units;
    } else if (entry.derived && styleFor(entry.variable)) {
      // Derived variable: its registered style (it may have arrived after the request was issued)
      currentCmap = effectiveCmap(entry.variable);
      const r = getDefaultRange(entry.variable);
      if (r) {
        currentVmin = r[0];
        currentVmax = r[1];
      }
    } else if (!getDefaultRange(entry.variable)) {
      // Use server-reported range as fallback if no defaults exist
      currentVmin = entry.vmin;
      currentVmax = entry.vmax;
    }

    ppi.setTransparentBackground(showMap);
    ppi.setPlainSurface(showMap ? null : plainSurface());
    ppi.setValueMask(activeMask);
    ppi.setColormap(currentCmap);
    ppi.setView(scale, translateX, translateY);
    ppi.render(currentVmin, currentVmax);

    isLoading = false;
    hasRendered = true;
    const dt = performance.now() - t0;
    console.log(`[PPI] Rendered ${entry.variable} sweep ${entry.sweep} in ${dt.toFixed(0)}ms (${entry.nAz}x${entry.nRange} gates)`);
  }

  /** Theme colours (canvas, range rings) for the plain background shown while the map is hidden. */
  function plainSurface() {
    const [r, g, b] = cssRgb01('--c-text', [0.88, 0.92, 0.97]);
    return { bg: cssRgb01('--c-surface-0', [0.01, 0.02, 0.05]), ring: [r, g, b, 0.28] as [number, number, number, number] };
  }

  function renderFrame() {
    if (!ppi || !hasRendered) return;
    ppi.setTransparentBackground(showMap);
    ppi.setPlainSurface(showMap ? null : plainSurface());
    ppi.setValueMask(activeMask);
    ppi.setColormap(currentCmap);
    ppi.setView(scale, translateX, translateY);
    ppi.render(currentVmin, currentVmax);
  }

  // ── Colormap/range callbacks ──────────────────────────────────────────────
  function handleCmapChange(name: string) {
    currentCmap = name;
    // remember it for the variable (not for a module field, which brings its own colours)
    if (!fieldShown && $selectedVariable) chooseCmap($selectedVariable, name);
    renderFrame();
  }

  function handleRangeChange(newVmin: number, newVmax: number) {
    currentVmin = newVmin;
    currentVmax = newVmax;
    renderFrame();
  }

  // ── Event handlers ────────────────────────��────────────────────────────────
  function onVariableChange(e: Event) {
    const v = (e.target as HTMLSelectElement).value;
    selectedVariable.set(v);
    requestNewData(v, sweep);
  }

  function onSweepChange(e: Event) {
    const s = parseInt((e.target as HTMLSelectElement).value, 10);
    selectedSweep.set(s);
    // Don't call requestNewData here — the selectedSweep subscription handles it
    // and does the variable compatibility check first.
  }

  function onWheel(e: WheelEvent) {
    if (!hasRendered) return;
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    const newScale = Math.min(Math.max(scale * factor, 0.02), 20);

    const rect = canvasEl.getBoundingClientRect();
    const ndcX = (e.clientX - rect.left) / rect.width * 2 - 1;
    const ndcY = 1 - (e.clientY - rect.top) / rect.height * 2;
    const mr = currentMaxRange;
    const aspect = rect.width / rect.height;

    const sxOld = (aspect >= 1 ? scale / aspect : scale) / mr;
    const syOld = (aspect >= 1 ? scale : scale * aspect) / mr;
    const dataX = (ndcX - translateX * sxOld) / sxOld;
    const dataY = (ndcY - translateY * syOld) / syOld;

    const sxNew = (aspect >= 1 ? newScale / aspect : newScale) / mr;
    const syNew = (aspect >= 1 ? newScale : newScale * aspect) / mr;
    translateX = (ndcX - dataX * sxNew) / sxNew;
    translateY = (ndcY - dataY * syNew) / syNew;
    scale = newScale;
    renderFrame();
  }

  function onMouseDown(e: MouseEvent) {
    if (!hasRendered || e.button !== 0) return;
    // When a measurement or annotation tool is active, clicks add points instead of dragging
    if (measureActive) return;
    // Box select owns the drag: panning here would move the view under the box being drawn
    if ($boxSelectActive) return;
    isDragging = true;
    dragStartX = e.clientX;
    dragStartY = e.clientY;
    dragStartTX = translateX;
    dragStartTY = translateY;
  }

  function onMouseMove(e: MouseEvent) {
    if (!hasRendered) return;
    updateCoords(e);
    updateProbe(e);
    if (!isDragging) return;

    const rect = canvasEl.getBoundingClientRect();
    const mr = currentMaxRange;
    const aspect = rect.width / rect.height;
    const sx = (aspect >= 1 ? scale / aspect : scale) / mr;
    const sy = (aspect >= 1 ? scale : scale * aspect) / mr;
    const ndcDx = (e.clientX - dragStartX) / rect.width * 2;
    const ndcDy = -(e.clientY - dragStartY) / rect.height * 2;
    translateX = dragStartTX + ndcDx / sx;
    translateY = dragStartTY + ndcDy / sy;
    renderFrame();
  }

  function onMouseUp() { isDragging = false; }
  function onMouseLeave() { isDragging = false; showCoords = false; probeActive = false; }
  function onMouseEnter() { if (hasRendered) showCoords = true; }

  function onCanvasClick(e: MouseEvent) {
    if (!hasRendered || e.button !== 0) return;
    // Vertical profile probe click
    if (csProbeToolActive) {
      handleVPProbeClick(e);
      return;
    }
    if (!measureActive) return;
    const [xm, ym] = screenToData(e.clientX, e.clientY);
    addPendingPoint({ x: xm, y: ym });
  }

  function onDblClick(e: MouseEvent) {
    e.preventDefault();
    // If area measurement is in progress, close the polygon
    if ($annotationsStore.measureMode === 'area' && $annotationsStore.pendingPoints.length >= 3) {
      finishAreaMeasurement();
      return;
    }
    scale = 1;
    translateX = 0;
    translateY = 0;
    renderFrame();
  }

  function toggleMeasureTools() {
    measureToolsVisible = !measureToolsVisible;
    if (!measureToolsVisible) {
      setMeasureMode('none');
      setAnnotationMode('none');
    }
  }

  function onRightClick(e: MouseEvent) {
    e.preventDefault();
    probeActive = !probeActive;
    if (probeActive) updateProbe(e);
  }

  function updateCoords(e: MouseEvent) {
    if (!canvasEl || !hasRendered) return;
    const [xm, ym] = screenToData(e.clientX, e.clientY);
    coordRange = Math.round(Math.sqrt(xm * xm + ym * ym) / 1000);
    coordAzimuth = Math.round(((Math.atan2(xm, ym) * 180 / Math.PI) + 360) % 360);
    showCoords = true;
  }

  function updateProbe(e: MouseEvent) {
    if (!probeActive || !currentEntry) return;
    const [xm, ym] = screenToData(e.clientX, e.clientY);
    probeX = e.clientX;
    probeY = e.clientY;

    // Find nearest gate value
    const rng = Math.sqrt(xm * xm + ym * ym);
    const az = ((Math.atan2(xm, ym) * 180 / Math.PI) + 360) % 360;

    const entry = currentEntry;
    // Find nearest azimuth index
    // (-1: nothing is drawn at this bearing, e.g. outside a sector scan or inside a hole)
    const bestAi = rayAtAzimuth(entry.azimuth, entry.nAz, az);
    if (bestAi < 0) {
      probeValue = null;
      probeLabel = null;
      probeHidden = false;
      return;
    }
    // Find nearest range index
    let bestRi = 0;
    let bestRngDist = Infinity;
    for (let ri = 0; ri < entry.nRange; ri++) {
      const d = Math.abs(entry.range[ri] - rng);
      if (d < bestRngDist) { bestRngDist = d; bestRi = ri; }
    }

    const val = entry.values[bestAi * entry.nRange + bestRi];
    probeValue = val <= -9998 ? null : val;
    // A gate the value mask hides reads like missing data, but says why.
    probeHidden = probeValue !== null && isMaskActive(activeMask) && maskTest(probeValue, float32Mask(activeMask));
    if (probeHidden) probeValue = null;
    const cat = probeValue !== null ? legendCategories?.find((c) => c.value === Math.round(probeValue!)) : undefined;
    probeLabel = cat ? cat.label : null;

    // Update the temporal probe point for time series analysis
    setProbePoint({
      azimuth: entry.azimuth[bestAi],
      rangeM: entry.range[bestRi],
      x: e.clientX,
      y: e.clientY,
    });
  }

  // ── Cross-section / VP handlers ──────────────────────────────────────────
  function handleCrossSectionRequest(e: CustomEvent<CrossSectionLinePoints>) {
    const line = e.detail;
    const variable = $selectedVariable;
    if (!variable) return;
    crossSectionStore.update((s) => ({ ...s, isLoading: true }));
    wsManager.send({
      type: 'get_cross_section',
      start: [line.startLat, line.startLon],
      end: [line.endLat, line.endLon],
      variable,
      n_points: 200,
    });
  }

  function handleVPProbeClick(e: MouseEvent) {
    if (!csProbeToolActive || !hasRendered) return;
    const [xm, ym] = screenToData(e.clientX, e.clientY);
    // Data space is azimuthal equidistant about the radar: invert it exactly (same math as the map).
    const [lat, lon] = dataSpaceToLatLon(xm, ym, radarLat, radarLon);
    // Dispatch VP request for the selected variable
    const variable = $selectedVariable;
    if (!variable) return;
    crossSectionStore.update((s) => ({ ...s, isLoading: true, verticalProfiles: [] }));
    wsManager.send({ type: 'get_vertical_profile', lat, lon, variable });
  }

  // ── Mosaic hooks ─────────────────────────────────────────────────────────
  /** Lon/lat box currently visible on the map (null until a sweep is rendered). */
  function currentViewBounds(): GeoBounds | null {
    if (!hasRendered || (radarLat === 0 && radarLon === 0)) return null;
    const w = containerWidth;
    const h = containerHeight;
    const pts = [[0, 0], [w, 0], [w, h], [0, h]].map(([x, y]) =>
      pixelToLatLon(x, y, radarLat, radarLon, currentMaxRange, scale, translateX, translateY, w, h),
    );
    const lats = pts.map((p) => p[0]);
    const lons = pts.map((p) => p[1]);
    return { west: Math.min(...lons), south: Math.min(...lats), east: Math.max(...lons), north: Math.max(...lats) };
  }

  /** Pan/zoom so a lon/lat box fills the view (10% margin). */
  function onMosaicFit(e: Event) {
    const b = (e as CustomEvent<GeoBounds>).detail;
    if (!b || !hasRendered || (radarLat === 0 && radarLon === 0)) return;
    const [x1, y1] = latLonToDataSpace(b.south, b.west, radarLat, radarLon);
    const [x2, y2] = latLonToDataSpace(b.north, b.east, radarLat, radarLon);
    const cx = (x1 + x2) / 2;
    const cy = (y1 + y2) / 2;
    const hx = Math.max(Math.abs(x2 - x1) / 2, 1) * 1.1;
    const hy = Math.max(Math.abs(y2 - y1) / 2, 1) * 1.1;
    const mr = currentMaxRange;
    const aspect = containerWidth / containerHeight;
    // Solve sx*hx <= 1 and sy*hy <= 1 for scale (see the NDC transform in screenToData)
    const fit = aspect >= 1 ? Math.min((aspect * mr) / hx, mr / hy) : Math.min(mr / hx, mr / (aspect * hy));
    scale = Math.min(Math.max(fit, 0.02), 20);
    translateX = -cx;
    translateY = -cy;
    renderFrame();
  }

  function screenToData(clientX: number, clientY: number): [number, number] {
    const rect = canvasEl.getBoundingClientRect();
    const ndcX = (clientX - rect.left) / rect.width * 2 - 1;
    const ndcY = 1 - (clientY - rect.top) / rect.height * 2;
    const mr = currentMaxRange;
    const aspect = rect.width / rect.height;
    const sx = (aspect >= 1 ? scale / aspect : scale) / mr;
    const sy = (aspect >= 1 ? scale : scale * aspect) / mr;
    const xm = (ndcX - translateX * sx) / sx;
    const ym = (ndcY - translateY * sy) / sy;
    return [xm, ym];
  }
</script>

<PerformanceMonitor />

<div class="radar-viewer">
  {#if hasData}
    <div class="viewer-controls">
      <div class="control-group">
        <label for="var-select">Variable</label>
        <select id="var-select" value={variable ?? ''} on:change={onVariableChange}
                disabled={!isConnected} title={!isConnected ? 'Not connected' : 'Select variable'}>
          <option value="" disabled>Select variable</option>
          {#each variables as v}
            <option value={v}>{v}</option>
          {/each}
          {#if derivedVars.length > 0}
            <optgroup label="Derived">
              {#each derivedVars as d (d.name)}
                <option value={d.name} title="{d.label} ({d.sweeps.length} sweeps)">{d.label}</option>
              {/each}
            </optgroup>
          {/if}
        </select>
      </div>

      <div class="control-group">
        <label for="sweep-select">Sweep</label>
        <select id="sweep-select" value={sweep} on:change={onSweepChange}
                disabled={!isConnected} title={!isConnected ? 'Not connected' : 'Select sweep'}>
          {#each sweeps as s}
            <option value={s.index}>{sweepLabel(s)}</option>
          {/each}
          {#if sweeps.length === 0}
            <option value={0}>Sweep 0</option>
          {/if}
        </select>
      </div>

      {#if variable}
        <div class="control-info">
          <span class="active-var" title={variable}>{variableLabel}</span>
          {#if isRhi && sweepMeta}
            <span class="active-sweep">RHI {sweepMeta.azimuth?.toFixed(1) ?? '?'}°</span>
          {:else if sweeps[sweep]}
            <span class="active-sweep">{sweeps[sweep]?.elevation?.toFixed(1) ?? '?'}°</span>
          {/if}
        </div>
      {/if}

      {#if hasRendered && !isRhi}
        <div class="control-group map-controls">
          <label class="toggle-label" title="Show or hide the map ({getShortcutDisplay('toggle-map')}). Hidden: a plain background with range rings.">
            <input type="checkbox" checked={showMap} on:change={(e) => setMapHidden(!e.currentTarget.checked)} data-testid="map-toggle" />
            Map
          </label>
          {#if showMap}
            <input
              type="range"
              min="0" max="100" step="1"
              value={Math.round(mapOpacity * 100)}
              on:input={(e) => { mapOpacity = parseInt(e.currentTarget.value) / 100; }}
              class="opacity-slider"
              title="Map opacity: {Math.round(mapOpacity * 100)}%"
            />
          {/if}
          <label class="toggle-label" title="Toggle geographic overlay">
            <input type="checkbox" bind:checked={showGeoOverlay} />
            Geo
          </label>
          <label class="toggle-label" title="Show NEXRAD and your own radar sites; click one for its latest scan">
            <input type="checkbox" bind:checked={$showRadarSites} />
            Sites
          </label>
          <MaskPopover variable={maskKey} units={currentUnits} testid="mask-2d" />
        </div>
        {#if $mosaicLayer}
          <div class="control-group map-controls">
            <label class="toggle-label" title="Show the map with the mosaic instead of the PPI">
              <input type="checkbox" checked={$mosaicDisplay.mosaicView}
                     on:change={(e) => mosaicDisplay.update((d) => ({ ...d, mosaicView: e.currentTarget.checked, visible: e.currentTarget.checked ? true : d.visible }))} />
              Mosaic view
            </label>
          </div>
        {/if}
        <button class="measure-toggle" class:active={measureToolsVisible}
                on:click={toggleMeasureTools} title="Measurement & annotation tools (M)">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M2 18L18 2"/><path d="M15 2h3v3"/><path d="M5 18H2v-3"/>
            <path d="M9 14l2-2" stroke-dasharray="2 2"/>
          </svg>
        </button>
        <button class="measure-toggle" class:active={$boxSelectActive}
                on:click={() => boxSelectActive.update(v => !v)}
                title="Draw box to clip 3D volume region">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="3" y="3" width="18" height="18" rx="1" stroke-dasharray="4 2"/>
            <path d="M12 8v8M8 12h8" stroke-width="1.5"/>
          </svg>
        </button>
        {#if $selectedBox}
          <button class="measure-toggle" on:click={() => selectedBox.set(null)} title="Clear 3D clip region">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="3" y="3" width="18" height="18" rx="1" stroke-dasharray="4 2"/>
              <path d="M9 9l6 6M15 9l-6 6" stroke-width="1.5"/>
            </svg>
          </button>
        {/if}
        <div class="zoom-badge">{zoomLabel}</div>
      {/if}
    </div>

    <!-- svelte-ignore a11y_no_noninteractive_element_interactions, a11y_no_noninteractive_tabindex -->
    <div class="viewer-canvas" class:dragging={isDragging} class:measure-active={measureActive}
         style:display={isRhi ? 'none' : null}
         bind:this={canvasEl}
         on:wheel={onWheel} on:mousedown={onMouseDown} on:mousemove={onMouseMove}
         on:mouseup={onMouseUp} on:mouseleave={onMouseLeave} on:mouseenter={onMouseEnter}
         on:click={onCanvasClick} on:dblclick={onDblClick} on:contextmenu={onRightClick}
         role="application" tabindex="0"
         aria-label="Radar PPI — scroll to zoom, drag to pan, double-click to reset, right-click to probe">

      {#if (showMap || mosaicViewOn || windViewOn || !!$mosaicLayer?.visible) && hasRendered && (radarLat !== 0 || radarLon !== 0)}
        <MapBackground
          showTiles={showMap}
          {radarLat}
          {radarLon}
          maxRange={currentMaxRange}
          {scale}
          {translateX}
          {translateY}
          {containerWidth}
          {containerHeight}
          opacity={mapOpacity}
        />
      {/if}

      <NowcastOverlay />

      <canvas bind:this={webglCanvas} class="webgl-canvas" class:has-map={showMap} class:mosaic-hidden={mosaicViewOn || windHidesPpi}></canvas>

      {#if showGeoOverlay && hasRendered && (radarLat !== 0 || radarLon !== 0)}
        <GeoOverlay
          {radarLat}
          {radarLon}
          {radarName}
          maxRange={currentMaxRange}
          {scale}
          {translateX}
          {translateY}
          {containerWidth}
          {containerHeight}
        />
      {/if}

      {#if $showRadarSites && sitesLayer.component && hasRendered && (radarLat !== 0 || radarLon !== 0)}
        <sitesLayer.component
          {radarLat}
          {radarLon}
          maxRange={currentMaxRange}
          {scale}
          {translateX}
          {translateY}
          {containerWidth}
          {containerHeight}
        />
      {/if}

      {#if hasRendered && $lightning.data && (radarLat !== 0 || radarLon !== 0)}
        <LightningOverlay
          {radarLat}
          {radarLon}
          maxRange={currentMaxRange}
          {scale}
          {translateX}
          {translateY}
          {containerWidth}
          {containerHeight}
        />
      {/if}

      {#if hasRendered}
        <StormOverlay
          {scale}
          {translateX}
          {translateY}
          maxRange={currentMaxRange}
          {containerWidth}
          {containerHeight}
        />
        <ModuleOverlay
          {scale}
          {translateX}
          {translateY}
          maxRange={currentMaxRange}
          {containerWidth}
          {containerHeight}
        />
        <CrossSectionLine
          active={csLineToolActive || csRhiToolActive}
          {scale}
          {translateX}
          {translateY}
          maxRange={currentMaxRange}
          {containerWidth}
          {containerHeight}
          {radarLat}
          {radarLon}
          on:requestCrossSection={handleCrossSectionRequest}
        />
      {/if}

      {#if hasRendered}
        <MeasureTools
          {scale}
          {translateX}
          {translateY}
          maxRange={currentMaxRange}
          {containerWidth}
          {containerHeight}
        />
        <Annotations
          {scale}
          {translateX}
          {translateY}
          maxRange={currentMaxRange}
          {containerWidth}
          {containerHeight}
        />
        <MeasureToolbar visible={measureToolsVisible} />
        <BoxSelectTool
          active={$boxSelectActive}
          radarRange={currentMaxRange}
          canvasWidth={containerWidth}
          canvasHeight={containerHeight}
          {scale}
          {translateX}
          {translateY}
          onboxselected={(box) => selectedBox.set(box)}
        />
        {#if $selectedBox && !$boxSelectActive}
          {@const clip = $selectedBox}
          {@const mr = currentMaxRange}
          {@const aspect = containerWidth / containerHeight}
          {@const sx = (aspect >= 1 ? scale / aspect : scale) / mr}
          {@const sy = (aspect >= 1 ? scale : scale * aspect) / mr}
          {@const x1 = ((clip.xMin + translateX) * sx + 1) / 2 * containerWidth}
          {@const x2 = ((clip.xMax + translateX) * sx + 1) / 2 * containerWidth}
          {@const y1 = (1 - (clip.yMax + translateY) * sy) / 2 * containerHeight}
          {@const y2 = (1 - (clip.yMin + translateY) * sy) / 2 * containerHeight}
          <svg class="clip-box-svg" width={containerWidth} height={containerHeight} style="pointer-events:none">
            <rect
              x={Math.min(x1, x2)} y={Math.min(y1, y2)}
              width={Math.abs(x2 - x1)} height={Math.abs(y2 - y1)}
              fill="rgba(var(--c-accent-rgb), 0.05)"
              stroke="rgba(var(--c-accent-rgb), 0.6)"
              stroke-width="1.5"
              stroke-dasharray="5 3"
            />
          </svg>
        {/if}
      {/if}

      {#if loadError && !isLoading}
        <div class="load-error" role="alert">
          <span>Couldn't load {loadError.variable} sweep {loadError.sweep}: {loadError.message}</span>
          <button class="load-error-btn" on:click={retryLoad}>Retry</button>
          <button class="load-error-x" on:click={dismissLoadError} aria-label="Dismiss">&times;</button>
        </div>
      {/if}

      {#if isLoading}
        <div class="viewer-loading">
          <div class="loading-spinner"></div>
          <p>Loading sweep data...</p>
        </div>
      {:else if !hasRendered}
        <div class="viewer-placeholder">
          <div class="placeholder-icon">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <circle cx="12" cy="12" r="10" />
              <path d="M12 2 C12 2 12 12 12 12" />
              <path d="M12 12 L18 8" />
              <circle cx="12" cy="12" r="3" />
            </svg>
          </div>
          <p>Select a variable to render</p>
        </div>
      {/if}

      {#if showCoords && hasRendered}
        <div class="coord-overlay">
          <span class="coord-label">R</span>
          <span class="coord-value">{coordRange} km</span>
          <span class="coord-sep">/</span>
          <span class="coord-label">Az</span>
          <span class="coord-value">{coordAzimuth}°</span>
        </div>
      {/if}

      {#if probeActive && probeValue !== null}
        <div class="probe-tooltip" style="left: {probeX + 12}px; top: {probeY - 28}px">
          {#if probeLabel}{probeLabel} ({probeValue})
          {:else}{probeValue.toFixed(2)} {currentUnits}{/if}<ProbeProvenanceTag tag={probeProv} />
        </div>
      {:else if probeActive && probeValue === null}
        <div class="probe-tooltip muted" style="left: {probeX + 12}px; top: {probeY - 28}px">
          {probeHidden ? 'Hidden by the value mask' : 'No data'}
        </div>
      {/if}

      {#if fieldShown}
        <div class="field-badge" role="status">
          <span class="field-badge-tag">Module field</span>
          <span class="field-badge-name" title="{fieldShown.moduleTitle} / {fieldShown.name}">
            {fieldShown.moduleTitle} &rsaquo; {fieldShown.title}
          </span>
          <span class="field-badge-sweep">sweep {fieldShown.sweep}</span>
          <button type="button" class="field-badge-back" on:click={() => hideField()}
                  title="Show {variable ?? 'the variable'} again">
            Back to {variable ?? 'variable'}
          </button>
        </div>
      {/if}

      {#if hasRendered && !mosaicViewOn}
        <div class="colorbar-wrapper">
          {#if legendCategories}
            <div class="cat-legend" role="list" aria-label="{legendTitle} categories">
              {#if legendTitle}<span class="cat-title">{legendTitle}</span>{/if}
              {#each legendCategories as c (c.value)}
                <span class="cat-item" role="listitem">
                  <span class="cat-swatch" style="background: {c.color}"></span>{c.label}
                </span>
              {/each}
            </div>
          {:else}
            {#if varStyle && !fieldShown}<div class="legend-title">{variableLabel}</div>{/if}
            <ColorBar
              cmapName={currentCmap}
              vmin={currentVmin}
              vmax={currentVmax}
              units={currentUnits}
              onCmapChange={handleCmapChange}
              onRangeChange={handleRangeChange}
            />
          {/if}
        </div>
      {/if}
    </div>

    {#if isRhi}
      <RhiViewer
        entry={rhiEntry && rhiEntry.sweep === sweep && rhiEntry.variable === variable ? rhiEntry : null}
        altitudeM={sweepMeta?.altitude ?? null}
        variableLabel={variableLabel}
        variable={variable ?? ""}
        variables={sweepMeta?.variables ?? variables}
        cmap={currentCmap}
        vmin={currentVmin}
        vmax={currentVmax}
        units={currentUnits}
        loading={isLoading}
        error={loadError && !isLoading ? `Couldn't load ${loadError.variable} sweep ${loadError.sweep}: ${loadError.message}` : null}
        onCmapChange={handleCmapChange}
        onRangeChange={handleRangeChange}
        onRetry={retryLoad}
        provenance={probeProv}
      />
    {/if}
  {:else}
    <div class="viewer-empty">
      <div class="empty-icon">
        <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
          <line x1="12" y1="18" x2="12" y2="12" />
          <line x1="9" y1="15" x2="15" y2="15" />
        </svg>
      </div>
      <h2>Open a radar file to begin</h2>
      <p class="text-muted">Supported: CfRadial, ODIM, NEXRAD Level II, Sigmet, and more</p>
      <div class="shortcut-hint">
        Press <kbd>Ctrl</kbd>+<kbd>O</kbd> to open a file, or drag and drop
      </div>
    </div>
  {/if}
</div>

<style>
  .radar-viewer {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--bg-primary);
    overflow: hidden;
  }

  .viewer-controls {
    display: flex;
    flex-wrap: wrap; /* narrow panes (both sidebars open on a small window) wrap instead of clipping controls */
    row-gap: var(--spacing-xs, 4px);
    align-items: center;
    gap: var(--spacing-lg);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border-color);
    flex-shrink: 0;
  }

  .control-group {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .control-group label {
    font-size: 11px;
    color: var(--text-muted);
    white-space: nowrap;
    text-transform: uppercase;
    letter-spacing: var(--ls-label);
    font-weight: 500;
    font-size: var(--fs-2xs);
  }

  .control-group select { min-width: 110px; max-width: 200px; }

  .control-info {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .active-var {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--accent-primary);
    background: rgba(var(--c-accent-rgb), 0.1);
    padding: 2px 8px;
    border-radius: var(--radius-sm);
    border: 1px solid rgba(var(--c-accent-rgb), 0.2);
  }

  .active-sweep {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-secondary);
  }

  .zoom-badge {
    margin-left: auto;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-muted);
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    padding: 2px 8px;
    min-width: 46px;
    text-align: center;
    user-select: none;
  }

  .viewer-canvas {
    flex: 1;
    position: relative;
    overflow: hidden;
    cursor: crosshair;
    user-select: none;
  }

  .viewer-canvas.dragging { cursor: grabbing; }
  .viewer-canvas.measure-active { cursor: crosshair; }

  .clip-box-svg {
    position: absolute;
    top: 0;
    left: 0;
    z-index: 18;
  }

  .measure-toggle {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    padding: 0;
    background: var(--glass-bg);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 150ms ease;
  }

  .measure-toggle:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
    border-color: var(--border-light);
  }

  .measure-toggle.active {
    background: rgba(var(--c-accent-rgb), 0.18);
    border-color: var(--accent-primary);
    color: var(--accent-primary);
    box-shadow: 0 0 8px rgba(var(--c-accent-rgb), 0.15);
  }

  .webgl-canvas {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    z-index: 1;
  }

  .webgl-canvas.mosaic-hidden {
    visibility: hidden;
  }

  .webgl-canvas.has-map {
    /* When map is behind, let transparent pixels show the tiles */
    background: transparent;
  }

  .map-controls {
    gap: var(--spacing-sm);
  }

  .toggle-label {
    font-size: 11px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.03em;
    font-weight: 600;
    gap: 4px;
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
  }

  .toggle-label input[type="checkbox"] {
    margin: 0;
    width: 14px;
    height: 14px;
  }

  .opacity-slider {
    width: 60px;
    height: 4px;
    accent-color: var(--accent-primary);
    cursor: pointer;
    vertical-align: middle;
  }

  .coord-overlay {
    position: absolute;
    top: var(--spacing-md);
    left: var(--spacing-md);
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 4px 10px;
    background: var(--c-overlay);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-secondary);
    backdrop-filter: blur(4px);
    pointer-events: none;
    white-space: nowrap;
    z-index: 10;
  }

  .coord-label { color: var(--text-muted); font-weight: 600; }
  .coord-value { color: var(--text-primary); min-width: 52px; text-align: right; }
  .coord-sep { color: var(--border-light); margin: 0 2px; }

  .colorbar-wrapper {
    position: absolute;
    bottom: var(--spacing-md);
    left: 50%;
    transform: translateX(-50%);
    z-index: 15;
    pointer-events: auto;
  }

  .field-badge {
    position: absolute;
    top: var(--spacing-md);
    left: 50%;
    transform: translateX(-50%);
    z-index: 16;
    display: flex;
    align-items: center;
    gap: 8px;
    max-width: calc(100% - 160px);
    padding: 4px 6px 4px 10px;
    background: var(--c-overlay);
    border: 1px solid var(--accent-primary);
    border-radius: var(--radius-sm);
    font-size: 11px;
    color: var(--text-primary);
    backdrop-filter: blur(4px);
  }

  .field-badge-tag {
    flex: none;
    font-size: 9px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--accent-primary);
  }

  .field-badge-name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .field-badge-sweep {
    flex: none;
    font-family: var(--font-mono);
    color: var(--text-muted);
  }

  .field-badge-back {
    flex: none;
    padding: 2px 8px;
    font-size: 11px;
    color: var(--text-primary);
    background: rgba(var(--c-accent-rgb), 0.18);
    border: 1px solid var(--accent-primary);
    border-radius: 3px;
    cursor: pointer;
  }

  .field-badge-back:hover { background: rgba(var(--c-accent-rgb), 0.35); }

  .cat-legend {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 4px 12px;
    max-width: min(640px, 80vw);
    padding: 6px 10px;
    background: var(--c-overlay);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    font-size: 11px;
    color: var(--text-secondary);
    backdrop-filter: blur(4px);
  }

  .cat-title { font-weight: 600; color: var(--text-primary); }

  .legend-title {
    text-align: center;
    margin-bottom: 2px;
    font-size: 11px;
    font-weight: 600;
    color: var(--text-primary);
    text-shadow: 0 0 4px rgba(var(--c-shadow-rgb), 0.8);
  }

  .cat-item { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }

  .cat-swatch {
    width: 12px;
    height: 12px;
    border-radius: 2px;
    border: 1px solid rgba(var(--c-ink-rgb), 0.25);
  }

  .probe-tooltip {
    position: fixed;
    padding: 3px 8px;
    background: var(--c-overlay);
    border: 1px solid var(--accent-primary);
    border-radius: 3px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-primary);
    pointer-events: none;
    z-index: 50;
    white-space: nowrap;
    backdrop-filter: blur(4px);
  }
  .probe-tooltip.muted {
    color: var(--text-muted);
    border-color: var(--border-color);
  }

  .viewer-loading {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-md);
    color: var(--text-muted);
    z-index: 5;
  }

  .loading-spinner {
    width: 32px;
    height: 32px;
    border: 3px solid var(--border-color);
    border-top-color: var(--accent-primary);
    border-radius: 50%;
    animation: spin 800ms linear infinite;
  }

  @keyframes spin { to { transform: rotate(360deg); } }
  .viewer-loading p { font-size: 13px; }

  .load-error {
    position: absolute;
    top: 12px;
    left: 50%;
    transform: translateX(-50%);
    z-index: 7;
    display: flex;
    align-items: center;
    gap: 10px;
    max-width: 80%;
    padding: 6px 8px 6px 12px;
    font-size: 12px;
    color: var(--text-primary);
    background: var(--glass-bg-heavy);
    border: 1px solid var(--accent-danger);
    border-radius: 6px;
    box-shadow: 0 2px 10px rgba(var(--c-shadow-rgb), 0.35);
  }
  .load-error span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .load-error-btn {
    flex: none;
    padding: 2px 10px;
    font-size: 12px;
    color: var(--c-on-accent);
    background: var(--accent-primary);
    border: none;
    border-radius: 4px;
    cursor: pointer;
  }
  .load-error-x {
    flex: none;
    padding: 0 4px;
    font-size: 16px;
    line-height: 1;
    color: var(--text-muted);
    background: none;
    border: none;
    cursor: pointer;
  }

  .viewer-placeholder, .viewer-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-md);
    color: var(--text-muted);
    height: 100%;
    width: 100%;
    position: absolute;
    inset: 0;
    z-index: 5;
  }

  .viewer-empty { gap: var(--spacing-lg); position: relative; }
  .placeholder-icon, .empty-icon { opacity: 0.2; }
  .viewer-empty h2 { font-size: 18px; font-weight: 500; color: var(--text-secondary); }
  .viewer-empty p { font-size: 13px; max-width: 400px; text-align: center; line-height: 1.6; }

  .shortcut-hint {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    color: var(--text-muted);
    margin-top: var(--spacing-sm);
  }

  .shortcut-hint :global(kbd) {
    display: inline-block;
    padding: 2px 6px;
    font-family: var(--font-mono);
    font-size: 11px;
    background: var(--bg-surface);
    border: 1px solid var(--border-color);
    border-radius: 3px;
    color: var(--text-secondary);
    box-shadow: 0 1px 0 var(--border-color);
  }
</style>
