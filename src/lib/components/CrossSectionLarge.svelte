<script lang="ts">
  import { themeRevision } from '../utils/themeColors';
  /**
   * CrossSectionLarge -- the comfortable cross-section view: a canvas drawn at the real device
   * pixel size with axes, colour bar and title, hover read-out, zoom/pan, vertical exaggeration,
   * height range, a line editor (azimuth / range from the radar), an RHI-style quick cut and PNG
   * export. Used both docked under the PPI and in the full-size dialog.
   *
   * It owns no requests: it edits the shared crossSection store and CrossSectionPanel (always
   * mounted) runs the extraction.
   */
  import { onMount, untrack } from 'svelte';
  import { radarData, connectionStatus } from '../stores/radarData';
  import {
    crossSectionStore,
    patchSectionStyle,
    requestRhi,
    requestSection,
    setCrossSectionLayout,
    setSectionLine,
    closeCrossSectionModal,
    type CrossSectionLinePoints,
  } from '../stores/crossSection';
  import { getActiveFile } from '../stores/fileManager';
  import { COLORMAP_NAMES } from '../utils/colormaps';
  import { getDefaultCmap, getDefaultRange } from '../utils/ppiRenderer';
  import { dataSpaceToLatLon, latLonToDataSpace } from '../utils/geoProjection';
  import {
    azRangeToXY,
    autoHeightTop,
    buildCells,
    cellAt,
    columnAt,
    describeSection,
    effectiveExaggeration,
    haversineKm,
    isFullView,
    panView,
    safeStem,
    wrapAzimuth,
    xyToAzRange,
    zoomView,
    type SectionCell,
    type View,
  } from '../utils/crossSectionGeometry';
  import { drawCrossSection, layoutFor, type DrawSpec } from '../utils/crossSectionDraw';
  import { canvasToBlob, savePng } from '../utils/crossSectionExport';
  import { addToast } from '../stores/toasts';
  import { smoothingStore } from '../stores/crossSectionSmoothing';
  import { addPngText, buildSectionGrid, isPlain, smoothingLabel } from '../utils/crossSectionRaster';
  import CrossSectionSmoothing from './CrossSectionSmoothing.svelte';
  import ValueMaskControl from './ValueMaskControl.svelte';
  import { maskDescription, maskOf, maskPredicate, valueMasks } from '../stores/valueMask';

  let { mode = 'dock' }: { mode?: 'dock' | 'modal' } = $props();

  // ---- shared state -------------------------------------------------------------------------
  const cs = $derived($crossSectionStore);
  const data = $derived($crossSectionStore.crossSectionData);
  const style = $derived($crossSectionStore.style);
  // Narrow deriveds: effects below must not re-run for unrelated store updates.
  const lineNow = $derived($crossSectionStore.line);
  const rhiReq = $derived($crossSectionStore.rhi);
  const rhiUserRange = $derived($crossSectionStore.rhiRangeKm);
  const ppiMaxRange = $derived($crossSectionStore.ppiMaxRangeKm);
  const variables = $derived($radarData.variables);
  const connected = $derived($connectionStatus === 'connected');
  const hasFile = $derived(variables.length > 0);
  const busy = $derived(cs.isLoading);

  const radar = $derived.by(() => {
    const a = $radarData.attributes ?? {};
    const lat = parseFloat(a.latitude ?? a.origin_latitude ?? '');
    const lon = parseFloat(a.longitude ?? a.origin_longitude ?? '');
    if (Number.isFinite(lat) && Number.isFinite(lon) && (lat !== 0 || lon !== 0)) return { lat, lon };
    if (data?.radar_lat != null && data?.radar_lon != null) return { lat: data.radar_lat, lon: data.radar_lon };
    return null;
  });
  const altitudeKm = $derived(data?.radar_altitude_m != null ? data.radar_altitude_m / 1000 : null);

  // ---- display preferences (remembered) -----------------------------------------------------
  const PREFS_KEY = 'xradar.cs.view.v1';
  type VeSetting = 'fit' | 1 | 2 | 3 | 4 | 5;
  const HEIGHT_CHOICES = [5, 8, 10, 12, 15, 20, 25, 30, 40];

  function loadPrefs(): { ve: VeSetting; top: 'auto' | number; fill: boolean; light: boolean | null; asl: boolean } {
    // light = null: the plot follows the app theme until the user chooses otherwise (then it is remembered)
    const d = { ve: 'fit' as VeSetting, top: 'auto' as 'auto' | number, fill: true, light: null as boolean | null, asl: false };
    try {
      const p = JSON.parse(localStorage.getItem(PREFS_KEY) ?? '{}');
      if (p.ve === 'fit' || [1, 2, 3, 4, 5].includes(p.ve)) d.ve = p.ve;
      if (p.top === 'auto' || (typeof p.top === 'number' && p.top >= 1 && p.top <= 100)) d.top = p.top;
      if (typeof p.fill === 'boolean') d.fill = p.fill;
      if (typeof p.light === 'boolean') d.light = p.light;
      if (typeof p.asl === 'boolean') d.asl = p.asl;
    } catch {
      /* no stored preferences */
    }
    return d;
  }
  const initial = loadPrefs();
  let ve = $state<VeSetting>(initial.ve);
  let top = $state<'auto' | number>(initial.top);
  let fill = $state(initial.fill);
  let lightPref = $state<boolean | null>(initial.light);
  const appLight = $derived.by(() => {
    void $themeRevision; // re-read when the theme changes
    return typeof document !== 'undefined' && document.documentElement.dataset.theme === 'light';
  });
  const light = $derived(lightPref ?? appLight);
  let asl = $state(initial.asl);

  $effect(() => {
    const p = JSON.stringify({ ve, top, fill, light: lightPref, asl });
    try {
      localStorage.setItem(PREFS_KEY, p);
    } catch {
      /* storage unavailable: ignore */
    }
  });

  // ---- geometry -----------------------------------------------------------------------------
  // The value mask of the section's variable: masked gates are dropped before anything else, so the
  // smoothing below never blends across them.
  const maskVariable = $derived(data?.variable || style.variable);
  const mask = $derived(maskOf($valueMasks, maskVariable));
  const maskNote = $derived(maskDescription(mask, data?.units ?? ''));
  const sc = $derived(data && data.n_sweeps > 0 ? buildCells(data, { fill, masked: maskPredicate(mask, { f32: true }) }) : null);
  const heightTop = $derived(top === 'auto' ? (sc ? autoHeightTop(sc.cells) : 15) : top);
  const full: View = $derived.by(() => {
    const dist = data?.distance_km ?? [];
    const d1 = dist.length > 1 ? dist[dist.length - 1] : 1;
    return { d0: dist[0] ?? 0, d1: d1 > (dist[0] ?? 0) ? d1 : (dist[0] ?? 0) + 1, h0: 0, h1: heightTop };
  });
  const veValue = $derived(ve === 'fit' ? null : ve);

  let viewState = $state<View | null>(null);
  let viewKey = '';
  $effect(() => {
    const f = full;
    const key = `${f.d0}|${f.d1}|${f.h0}|${f.h1}`;
    untrack(() => {
      if (key !== viewKey) {
        viewKey = key;
        viewState = { ...f };
      }
    });
  });
  const view = $derived(viewState ?? full);
  const zoomed = $derived(!isFullView(view, full));

  // ---- canvas -------------------------------------------------------------------------------
  let stageEl = $state<HTMLDivElement | undefined>();
  let canvasEl = $state<HTMLCanvasElement | undefined>();
  let cssW = $state(800);
  let cssH = $state(400);

  onMount(() => {
    if (!stageEl) return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r && r.width > 0 && r.height > 0) {
        cssW = Math.floor(r.width);
        cssH = Math.floor(r.height);
      }
    });
    ro.observe(stageEl);
    const wheel = (e: WheelEvent) => onWheel(e);
    stageEl.addEventListener('wheel', wheel, { passive: false });
    return () => {
      ro.disconnect();
      stageEl?.removeEventListener('wheel', wheel);
      if (raf) cancelAnimationFrame(raf);
    };
  });

  const radarName = $derived(String($radarData.attributes?.instrument_name ?? '').trim() || null);
  const fileName = $derived.by(() => {
    const f = getActiveFile();
    return f?.filename ?? ($radarData.filePath ? $radarData.filePath.split(/[\\/]/).pop() : null) ?? null;
  });

  const lengthKm = $derived.by(() => {
    if (!data || data.distance_km.length === 0) return null;
    return data.distance_km[data.distance_km.length - 1];
  });
  const text = $derived.by(() => {
    if (!data) return { title: '', subtitle: '' };
    return describeSection({
      variable: data.variable,
      instrument: radarName,
      time: data.time,
      fileName,
      start: data.start,
      end: data.end,
      lengthKm,
      rhiAzimuth: data.rhi_azimuth,
      maxRangeKm: lengthKm,
    });
  });

  const smoothing = $derived($smoothingStore);
  const gateKm = $derived(
    data && data.distance_km.length > 1
      ? (data.distance_km[data.distance_km.length - 1] - data.distance_km[0]) / (data.distance_km.length - 1)
      : null,
  );
  // The smoothed picture is built once per section / setting, not per frame.
  const grid = $derived(
    sc && data && !isPlain(smoothing) ? buildSectionGrid(sc.columns, data.distance_km, full, smoothing) : null,
  );
  const smoothNote = $derived(smoothingLabel(smoothing, gateKm));
  const captionNote = $derived(
    [maskNote ? `Masked: ${maskNote}` : null, smoothNote].filter(Boolean).join('  \u00B7  ') || null,
  );

  const heightOffsetKm = $derived(asl && altitudeKm != null ? altitudeKm : 0);
  const heightLabel = $derived(
    asl && altitudeKm != null ? 'Height above sea level (km)' : 'Height above the radar (km)',
  );
  const distanceLabel = $derived(
    data?.rhi_azimuth != null ? 'Range from the radar along the beam azimuth (km)' : 'Distance along the line (km)',
  );

  const spec: Omit<DrawSpec, 'dpr' | 'theme'> | null = $derived(
    sc && data
      ? {
          cells: sc,
          cmapName: style.cmap,
          vmin: Number.isFinite(style.vmin) ? style.vmin : 0,
          vmax: Number.isFinite(style.vmax) && style.vmax !== style.vmin ? style.vmax : style.vmin + 1,
          units: data.units,
          view,
          full,
          ve: veValue,
          heightOffsetKm,
          title: text.title,
          subtitle: text.subtitle,
          note: captionNote,
          grid,
          interp: smoothing.interp,
          xLabel: distanceLabel,
          yLabel: heightLabel,
        }
      : null,
  );
  const layout = $derived(spec ? layoutFor(cssW, cssH, spec) : null);
  const shownVe = $derived(
    layout ? effectiveExaggeration(layout.plot, full.d1 - full.d0, full.h1 - full.h0) : 1,
  );

  let raf = 0;
  function schedule() {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      paint();
    });
  }
  $effect(() => {
    void spec;
    void cssW;
    void cssH;
    void canvasEl;
    schedule();
  });

  function paint() {
    const canvas = canvasEl;
    if (!canvas) return;
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    const w = Math.max(1, Math.round(cssW * dpr));
    const h = Math.max(1, Math.round(cssH * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!spec) {
      ctx.clearRect(0, 0, cssW, cssH);
      return;
    }
    drawCrossSection(ctx, cssW, cssH, { ...spec, theme: 'dark', dpr });
  }

  // ---- hover, zoom, pan ---------------------------------------------------------------------
  interface Hover {
    x: number;
    y: number;
    d: number;
    h: number;
    cell: SectionCell | null;
    col: number;
  }
  let hover = $state<Hover | null>(null);
  let dragging = $state(false);

  function localXY(e: MouseEvent): [number, number] {
    const r = stageEl!.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }

  function plotFraction(px: number, py: number): { fx: number; fy: number } | null {
    if (!layout) return null;
    const { plot } = layout;
    const fx = (px - plot.x) / plot.w;
    const fy = 1 - (py - plot.y) / plot.h;
    if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return null;
    return { fx, fy };
  }

  function onMove(e: MouseEvent) {
    if (!sc || !data || dragging) return;
    const [px, py] = localXY(e);
    const f = plotFraction(px, py);
    if (!f) {
      hover = null;
      return;
    }
    const d = view.d0 + f.fx * (view.d1 - view.d0);
    const h = view.h0 + f.fy * (view.h1 - view.h0);
    hover = { x: px, y: py, d, h, cell: cellAt(sc, data.distance_km, d, h), col: columnAt(data.distance_km, d) };
  }

  function onWheel(e: WheelEvent) {
    if (!layout || !spec) return;
    const [px, py] = localXY(e);
    const f = plotFraction(px, py);
    if (!f) return;
    e.preventDefault();
    const factor = Math.exp(-e.deltaY * 0.0016);
    viewState = zoomView(view, full, f.fx, f.fy, factor);
  }

  let dragStart: { x: number; y: number; view: View } | null = null;
  function onDown(e: MouseEvent) {
    if (e.button !== 0 || !layout || !plotFraction(...localXY(e))) return;
    dragStart = { x: e.clientX, y: e.clientY, view };
    dragging = true;
    hover = null;
    window.addEventListener('mousemove', onDrag);
    window.addEventListener('mouseup', onUp);
  }
  function onDrag(e: MouseEvent) {
    if (!dragStart || !layout) return;
    const { plot } = layout;
    viewState = panView(
      dragStart.view,
      full,
      -(e.clientX - dragStart.x) / plot.w,
      (e.clientY - dragStart.y) / plot.h,
    );
  }
  function onUp() {
    dragStart = null;
    dragging = false;
    window.removeEventListener('mousemove', onDrag);
    window.removeEventListener('mouseup', onUp);
  }
  function resetView() {
    viewState = { ...full };
  }

  function onKey(e: KeyboardEvent) {
    if (!layout) return;
    const step = e.shiftKey ? 0.25 : 0.1;
    const k = e.key;
    if (k === '+' || k === '=') viewState = zoomView(view, full, 0.5, 0.5, 1.25);
    else if (k === '-' || k === '_') viewState = zoomView(view, full, 0.5, 0.5, 0.8);
    else if (k === '0') resetView();
    else if (k === 'ArrowLeft') viewState = panView(view, full, -step, 0);
    else if (k === 'ArrowRight') viewState = panView(view, full, step, 0);
    else if (k === 'ArrowUp') viewState = panView(view, full, 0, step);
    else if (k === 'ArrowDown') viewState = panView(view, full, 0, -step);
    else return;
    e.preventDefault();
  }

  const readout = $derived.by(() => {
    if (!hover || !data) return null;
    const { cell, col } = hover;
    const agl = hover.h;
    const parts = {
      value: cell ? cell.v : null,
      units: data.units,
      distance: hover.d,
      heightAgl: agl,
      heightAsl: altitudeKm != null ? agl + altitudeKm : null,
      elevation: cell != null ? (data.elevations_deg?.[cell.sweep] ?? null) : null,
      ground: col >= 0 ? (data.ground_range_km?.[col] ?? null) : null,
      azimuth: col >= 0 ? (data.azimuth_deg?.[col] ?? null) : null,
    };
    return parts;
  });

  // ---- variable / colour controls -----------------------------------------------------------
  function onVariable(e: Event) {
    const v = (e.target as HTMLSelectElement).value;
    const range = getDefaultRange(v);
    patchSectionStyle(
      { variable: v, cmap: getDefaultCmap(v), ...(range ? { vmin: range[0], vmax: range[1] } : {}) },
      { link: true },
    );
    if (lineNow || rhiReq) requestSection();
  }
  function onCmap(e: Event) {
    patchSectionStyle({ cmap: (e.target as HTMLSelectElement).value });
  }
  function onRange(which: 'vmin' | 'vmax', e: Event) {
    const v = parseFloat((e.target as HTMLInputElement).value);
    if (Number.isFinite(v)) patchSectionStyle({ [which]: v });
  }
  function matchPpi() {
    const p = cs.ppiStyle;
    if (!p) return;
    const changedVariable = p.variable !== style.variable;
    patchSectionStyle({ variable: p.variable, cmap: p.cmap, vmin: p.vmin, vmax: p.vmax }, { link: true });
    if (changedVariable && (lineNow || rhiReq)) requestSection();
  }
  const matchesPpi = $derived(
    !!cs.ppiStyle &&
      cs.ppiStyle.variable === style.variable &&
      cs.ppiStyle.cmap === style.cmap &&
      cs.ppiStyle.vmin === style.vmin &&
      cs.ppiStyle.vmax === style.vmax,
  );

  // ---- the line: endpoints as azimuth / range from the radar --------------------------------
  type Field = string | number | null;
  let aAz = $state<Field>('');
  let aRng = $state<Field>('');
  let bAz = $state<Field>('');
  let bRng = $state<Field>('');
  let lineKey = '';

  function fmt(n: number, d = 1): string {
    return Number.isFinite(n) ? n.toFixed(d) : '';
  }

  function toAzRange(lat: number, lon: number): { azimuth: number; rangeKm: number } | null {
    if (!radar) return null;
    const [x, y] = latLonToDataSpace(lat, lon, radar.lat, radar.lon);
    return xyToAzRange(x, y);
  }

  $effect(() => {
    const l = lineNow;
    const r = radar;
    untrack(() => {
      if (!l || !r) {
        lineKey = '';
        aAz = aRng = bAz = bRng = '';
        return;
      }
      const key = `${l.startLat},${l.startLon},${l.endLat},${l.endLon},${r.lat},${r.lon}`;
      if (key === lineKey) return;
      lineKey = key;
      const a = toAzRange(l.startLat, l.startLon);
      const b = toAzRange(l.endLat, l.endLon);
      // Keep what was just typed when it already describes the new end point (a point on the radar
      // has no azimuth of its own, so reading it back would wipe the typed one).
      const same = (typed: [number, number] | null, lat: number, lon: number) =>
        !!typed && Math.abs(typed[0] - lat) < 1e-5 && Math.abs(typed[1] - lon) < 1e-5;
      if (a && !same(pointFrom(aAz, aRng), l.startLat, l.startLon)) {
        aAz = fmt(a.azimuth);
        aRng = fmt(a.rangeKm);
      }
      if (b && !same(pointFrom(bAz, bRng), l.endLat, l.endLon)) {
        bAz = fmt(b.azimuth);
        bRng = fmt(b.rangeKm);
      }
    });
  });

  function pointFrom(azText: Field, rngText: Field): [number, number] | null {
    if (!radar) return null;
    const az = parseFloat(String(azText));
    const rng = parseFloat(String(rngText));
    if (!Number.isFinite(az) || !Number.isFinite(rng) || rng < 0) return null;
    const [x, y] = azRangeToXY(az, rng);
    return dataSpaceToLatLon(x, y, radar.lat, radar.lon);
  }

  function commitLine() {
    const a = pointFrom(aAz, aRng);
    const b = pointFrom(bAz, bRng);
    if (!a || !b) {
      addToast('info', 'Enter an azimuth and a range for both ends of the line');
      return;
    }
    if (haversineKm(a[0], a[1], b[0], b[1]) < 0.5) {
      addToast('info', 'The two ends of the line are on top of each other');
      return;
    }
    const cur = lineNow;
    const same = cur &&
      Math.abs(cur.startLat - a[0]) < 1e-5 && Math.abs(cur.startLon - a[1]) < 1e-5 &&
      Math.abs(cur.endLat - b[0]) < 1e-5 && Math.abs(cur.endLon - b[1]) < 1e-5;
    if (same && !rhiReq) return;
    setSectionLine(makeLine(a, b));
  }

  function makeLine(a: [number, number], b: [number, number]): CrossSectionLinePoints {
    return { startX: 0, startY: 0, endX: 0, endY: 0, startLat: a[0], startLon: a[1], endLat: b[0], endLon: b[1] };
  }

  // ---- RHI-style quick cut ------------------------------------------------------------------
  let rhiAz = $state<Field>('0');
  let rhiRng = $state<Field>('150');
  $effect(() => {
    const r = rhiReq;
    const pm = ppiMaxRange;
    const user = rhiUserRange;
    untrack(() => {
      if (r) {
        rhiAz = fmt(r.azimuth);
        rhiRng = fmt(r.maxRangeKm, 0);
      } else {
        rhiRng = fmt(user ?? Math.min(pm ?? 150, 150), 0);
      }
    });
  });

  function runRhi(deltaAz = 0) {
    if (!radar) {
      addToast('info', 'This file has no radar position, so an RHI cannot be placed');
      return;
    }
    let az = parseFloat(String(rhiAz));
    let rng = parseFloat(String(rhiRng));
    if (!Number.isFinite(az) || !Number.isFinite(rng) || rng <= 0) {
      addToast('info', 'Enter an azimuth and a maximum range for the RHI');
      return;
    }
    az = wrapAzimuth(az + deltaAz);
    rng = Math.min(600, Math.max(1, rng));
    rhiAz = fmt(az);
    rhiRng = fmt(rng, 0);
    const [x, y] = azRangeToXY(az, rng);
    const end = dataSpaceToLatLon(x, y, radar.lat, radar.lon);
    crossSectionStore.update((s) => ({ ...s, rhiRangeKm: rng }));
    requestRhi(az, rng, makeLine([radar.lat, radar.lon], end));
  }

  function togglePick() {
    crossSectionStore.update((s) => ({
      ...s,
      rhiToolActive: !s.rhiToolActive,
      lineToolActive: false,
      probeToolActive: false,
    }));
  }
  function toggleDraw() {
    crossSectionStore.update((s) => ({
      ...s,
      lineToolActive: !s.lineToolActive,
      rhiToolActive: false,
      probeToolActive: false,
    }));
  }

  function enter(fn: () => void) {
    return (e: KeyboardEvent) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        fn();
      }
    };
  }

  // ---- export -------------------------------------------------------------------------------
  let exporting = $state(false);
  async function exportPng() {
    if (!spec || !data || exporting) return;
    exporting = true;
    try {
      const scale = Math.min(4, Math.max(2, Math.ceil(1800 / Math.max(cssW, 1))));
      const c = document.createElement('canvas');
      c.width = Math.round(cssW * scale);
      c.height = Math.round(cssH * scale);
      const ctx = c.getContext('2d');
      if (!ctx) throw new Error('No 2D canvas available');
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
      drawCrossSection(ctx, cssW, cssH, { ...spec, theme: light ? 'light' : 'dark', dpr: scale });
      const raw = await canvasToBlob(c);
      // The figure's description travels with the file (PNG tEXt): what, where and how it was smoothed.
      const description = [
        text.title,
        text.subtitle,
        maskNote ? `Masked: ${maskNote}` : 'No value mask',
        smoothNote ?? 'No smoothing: gates as measured',
      ]
        .filter(Boolean)
        .join('. ');
      const tagged = addPngText(new Uint8Array(await raw.arrayBuffer()), 'Description', description);
      const blob = new Blob([tagged as unknown as BlobPart], { type: 'image/png' });
      const stem = safeStem((fileName ?? 'radar').replace(/\.[^./\\]+$/, ''));
      const tail = data.rhi_azimuth != null ? `_rhi${Math.round(data.rhi_azimuth)}` : '_section';
      await savePng(`${stem}_${safeStem(data.variable)}${tail}`, blob);
    } catch (err) {
      addToast('error', `Could not create the image: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      exporting = false;
    }
  }

  const ariaLabel = $derived(
    data ? `Cross-section of ${data.variable}, ${text.subtitle}` : 'Cross-section: no section yet',
  );
</script>

<div class="csl" class:modal={mode === 'modal'} data-testid="cs-large">
  <div class="csl-bar">
    <div class="csl-row">
      <label class="csl-field" title="Variable">
        <select value={style.variable} onchange={onVariable} disabled={!hasFile} data-testid="cs-var" aria-label="Variable">
          {#each variables as v (v)}<option value={v}>{v}</option>{/each}
        </select>
      </label>
      <label class="csl-field" title="Colormap">
        <select value={style.cmap} onchange={onCmap} data-testid="cs-cmap" aria-label="Colormap">
          {#each COLORMAP_NAMES as c (c)}<option value={c}>{c}</option>{/each}
        </select>
      </label>
      <div class="csl-field">
        <span>Range</span>
        <input type="number" class="csl-num" value={style.vmin} step="1" aria-label="Colour range minimum"
               onchange={(e) => onRange('vmin', e)} />
        <input type="number" class="csl-num" value={style.vmax} step="1" aria-label="Colour range maximum"
               onchange={(e) => onRange('vmax', e)} />
        <button class="csl-btn" onclick={matchPpi} disabled={!cs.ppiStyle || matchesPpi}
                title="Use the PPI's variable, colormap and range">
          {matchesPpi ? 'Matches PPI' : 'Match PPI'}
        </button>
      </div>

      <span class="csl-sep"></span>

      <div class="csl-field" role="group" aria-label="Vertical exaggeration">
        <span>Vertical scale</span>
        <div class="csl-seg">
          {#each ['fit', 1, 2, 3, 4, 5] as v (v)}
            <button class:on={ve === v} aria-pressed={ve === v} onclick={() => (ve = v as VeSetting)}
                    title={v === 'fit' ? 'Fill the window' : `${v}x vertical exaggeration`}>
              {v === 'fit' ? 'Fit' : `${v}×`}
            </button>
          {/each}
        </div>
      </div>
      <label class="csl-field">
        <span>Height</span>
        <select value={String(top)} onchange={(e) => { const v = (e.target as HTMLSelectElement).value; top = v === 'auto' ? 'auto' : Number(v); }}
                data-testid="cs-height">
          <option value="auto">Auto</option>
          {#each HEIGHT_CHOICES as h (h)}<option value={String(h)}>0 &ndash; {h} km</option>{/each}
        </select>
      </label>
      <details class="csl-more" data-testid="cs-mask-menu">
        <summary>Mask: {maskNote ?? 'off'}</summary>
        <div class="csl-pop"><ValueMaskControl variable={maskVariable} units={data?.units ?? ''} /></div>
      </details>
      <details class="csl-more" data-testid="cs-smooth-menu">
        <summary>Smoothing: {smoothNote ? (smoothing.interp === 'nearest' ? 'on' : smoothing.interp) : 'off'}</summary>
        <div class="csl-pop"><CrossSectionSmoothing /></div>
      </details>
      <details class="csl-more">
        <summary>Options</summary>
        <div class="csl-pop">
          <label class="csl-check" title="Close the gaps between two elevation angles; off shows only the beam itself">
            <input type="checkbox" bind:checked={fill} /> Fill gaps between elevations
          </label>
          <label class="csl-check" title={altitudeKm == null ? 'This file does not record the radar altitude' : `Radar altitude ${Math.round(altitudeKm * 1000)} m`}>
            <input type="checkbox" bind:checked={asl} disabled={altitudeKm == null} /> Height above sea level
          </label>
          <label class="csl-check" title="White plot background, on screen and in the saved image. Default: follows the app theme">
            <input type="checkbox" checked={light} onchange={(e) => (lightPref = e.currentTarget.checked)} /> Light background (screen and PNG)
          </label>
        </div>
      </details>

      <span class="csl-grow"></span>

      <button class="csl-btn" onclick={resetView} disabled={!zoomed} title="Show the whole section (double-click the plot)">Reset zoom</button>
      <button class="csl-btn" onclick={exportPng} disabled={!spec || exporting} title="Save the figure as a PNG image" data-testid="cs-save">
        {exporting ? 'Saving…' : 'Save PNG'}
      </button>
      {#if mode === 'dock'}
        <button class="csl-btn" onclick={() => setCrossSectionLayout('modal')} title="Open the cross-section in a large window" data-testid="cs-expand">Expand</button>
        <button class="csl-btn icon csl-close" onclick={() => setCrossSectionLayout('panel')} title="Close this view (the small panel stays in the sidebar)" aria-label="Close the cross-section view">&times;</button>
      {:else}
        <button class="csl-btn" onclick={() => setCrossSectionLayout('dock')} title="Put the cross-section back under the PPI" data-testid="cs-dock">Dock</button>
        <button class="csl-btn icon csl-close" onclick={closeCrossSectionModal} title="Close (Esc)" aria-label="Close the cross-section">&times;</button>
      {/if}
    </div>

    <div class="csl-row">
      <div class="csl-field" role="group" aria-label="RHI-style cut">
        <span>RHI</span>
        <button class="csl-btn icon" onclick={() => runRhi(-1)} disabled={!hasFile || !connected} title="1° anticlockwise" aria-label="Azimuth minus 1 degree">&lsaquo;</button>
        <input type="number" class="csl-num" bind:value={rhiAz} step="1" min="0" max="360"
               aria-label="RHI azimuth in degrees" onkeydown={enter(() => runRhi())} data-testid="cs-rhi-az" />
        <span class="unit">&deg;</span>
        <button class="csl-btn icon" onclick={() => runRhi(1)} disabled={!hasFile || !connected} title="1° clockwise" aria-label="Azimuth plus 1 degree">&rsaquo;</button>
        <span class="unit">to</span>
        <input type="number" class="csl-num" bind:value={rhiRng} step="10" min="1" max="600"
               aria-label="RHI maximum range in kilometres" onkeydown={enter(() => runRhi())} data-testid="cs-rhi-range" />
        <span class="unit">km</span>
        <button class="csl-btn primary" onclick={() => runRhi()} disabled={!hasFile || !connected}
                title="A vertical section out of the radar along this azimuth" data-testid="cs-rhi-run">Run RHI</button>
        {#if mode === 'dock'}
          <button class="csl-btn" class:on={cs.rhiToolActive} onclick={togglePick} disabled={!hasFile || !connected}
                  title="Click the PPI to choose the azimuth">{cs.rhiToolActive ? 'Click the PPI…' : 'Pick on PPI'}</button>
        {/if}
      </div>

      <span class="csl-sep"></span>

      <div class="csl-field" role="group" aria-label="Line endpoints">
        <span>Line</span>
        <b class="pt">A</b>
        <input type="number" class="csl-num" bind:value={aAz} step="1" aria-label="Point A azimuth" onkeydown={enter(commitLine)} onchange={commitLine} data-testid="cs-a-az" />
        <span class="unit">&deg;</span>
        <input type="number" class="csl-num" bind:value={aRng} step="1" aria-label="Point A range in kilometres" onkeydown={enter(commitLine)} onchange={commitLine} data-testid="cs-a-rng" />
        <span class="unit">km</span>
        <b class="pt">B</b>
        <input type="number" class="csl-num" bind:value={bAz} step="1" aria-label="Point B azimuth" onkeydown={enter(commitLine)} onchange={commitLine} data-testid="cs-b-az" />
        <span class="unit">&deg;</span>
        <input type="number" class="csl-num" bind:value={bRng} step="1" aria-label="Point B range in kilometres" onkeydown={enter(commitLine)} onchange={commitLine} data-testid="cs-b-rng" />
        <span class="unit">km</span>
        {#if mode === 'dock'}
          <button class="csl-btn" class:on={cs.lineToolActive} onclick={toggleDraw} disabled={!hasFile || !connected}
                  title="Draw a new line on the PPI">{cs.lineToolActive ? 'Drawing…' : 'Draw'}</button>
        {/if}
      </div>

    </div>
  </div>

  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <div class="csl-stage" bind:this={stageEl} role="application" tabindex="0" aria-label={ariaLabel}
       onmousemove={onMove} onmouseleave={() => (hover = null)} onmousedown={onDown}
       ondblclick={resetView} onkeydown={onKey}
       class:grabbing={dragging} data-testid="cs-stage">
    <canvas bind:this={canvasEl} class="csl-canvas" data-testid="cs-canvas"></canvas>

    {#if hover && layout && readout}
      <div class="csl-cross v" style="left:{hover.x}px; top:{layout.plot.y}px; height:{layout.plot.h}px"></div>
      <div class="csl-cross h" style="top:{hover.y}px; left:{layout.plot.x}px; width:{layout.plot.w}px"></div>
      <div class="csl-tip" class:left={hover.x > cssW * 0.6} style="left:{hover.x}px; top:{hover.y}px" data-testid="cs-tip">
        {#if readout.value != null}
          <b class="val">{readout.value.toFixed(1)} {readout.units}</b>
        {:else}
          <span class="none">no echo</span>
        {/if}
        <span>{readout.distance.toFixed(1)} km</span>
        <span>{readout.heightAgl.toFixed(2)} km</span>
      </div>
    {/if}

    {#if !data}
      <div class="csl-empty">
        {#if !hasFile}
          <p>Open a radar file first.</p>
        {:else if busy}
          <p>Extracting the cross-section&hellip;</p>
        {:else}
          <p><b>No cross-section yet.</b></p>
          <p>{mode === 'dock' ? 'Draw a line on the PPI, pick an azimuth on it,' : 'Type an azimuth and a range for an RHI-style cut,'} or run an RHI above.</p>
        {/if}
      </div>
    {:else if busy}
      <div class="csl-busy">Updating&hellip;</div>
    {/if}
    {#if data && sc && layout}
      <div class="csl-ve" title="Vertical exaggeration of the picture">VE {shownVe.toFixed(1)}&times;</div>
    {/if}
  </div>

  <div class="csl-status" aria-live="off" data-testid="cs-status">
    {#if readout}
      {#if readout.value != null}
        <b class="val">{readout.value.toFixed(2)} {readout.units}</b>
      {:else}
        <span class="none">no echo</span>
      {/if}
      <span>distance <b>{readout.distance.toFixed(1)}</b> km</span>
      <span>height <b>{readout.heightAgl.toFixed(2)}</b> km{#if readout.heightAsl != null}{' '}(<b>{readout.heightAsl.toFixed(2)}</b> km ASL){/if}</span>
      {#if readout.elevation != null}<span>beam <b>{readout.elevation.toFixed(1)}&deg;</b></span>{/if}
      {#if readout.ground != null}<span>{readout.ground.toFixed(1)} km from the radar{#if readout.azimuth != null}{' '}at <b>{readout.azimuth.toFixed(0)}&deg;</b>{/if}</span>{/if}
    {:else if data}
      <span class="hint">Hover for values &middot; wheel zooms &middot; drag pans &middot; double-click resets</span>
    {:else}
      <span class="hint">&nbsp;</span>
    {/if}
  </div>
</div>

<style>
  .csl {
    display: flex;
    flex-direction: column;
    width: 100%;
    height: 100%;
    min-height: 0;
    background: var(--bg-secondary);
    color: var(--text-primary);
  }

  .csl-bar {
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 6px 8px;
    border-bottom: 1px solid var(--glass-border);
  }

  .csl-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 5px 10px;
  }

  .csl-field {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 11px;
    color: var(--text-muted);
    white-space: nowrap;
  }
  .csl-field > span:first-child {
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 600;
    font-size: 10px;
  }
  .csl-field select,
  .csl-field input {
    height: 26px;
    font-size: 12px;
  }
  .csl-num {
    width: 62px;
    padding: 2px 6px;
    font-family: var(--font-mono, monospace);
  }
  .unit {
    color: var(--text-muted);
    font-size: 11px;
  }
  .pt {
    color: var(--accent-hover);
    font-size: 11px;
    margin-left: 2px;
  }

  .csl-check {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 11px;
    white-space: nowrap;
  }

  .csl-more {
    position: relative;
  }
  .csl-more summary {
    list-style: none;
    height: 26px;
    display: inline-flex;
    align-items: center;
    padding: 0 10px;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    color: var(--text-primary);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm, 6px);
  }
  .csl-more summary::-webkit-details-marker {
    display: none;
  }
  .csl-more summary::after {
    content: '\25BE';
    margin-left: 6px;
    font-size: 9px;
  }
  .csl-more[open] summary {
    background: var(--bg-active);
  }
  .csl-pop {
    position: absolute;
    top: 30px;
    left: 0;
    z-index: 40;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 12px;
    background: var(--bg-surface);
    border: 1px solid var(--border-light);
    border-radius: var(--radius-md, 8px);
    box-shadow: 0 8px 28px rgba(var(--c-shadow-rgb), 0.45);
  }

  .csl-sep {
    width: 1px;
    align-self: stretch;
    background: var(--glass-border);
  }
  .csl-grow {
    flex: 1 1 auto;
  }

  .csl-btn {
    height: 26px;
    padding: 0 10px;
    font-size: 11px;
    font-weight: 600;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }
  .csl-btn.icon {
    padding: 0 8px;
    font-size: 14px;
    line-height: 1;
  }
  .csl-btn.on,
  .csl-btn.primary {
    background: rgba(var(--c-accent-rgb), 0.2);
    border-color: rgba(var(--c-accent-rgb), 0.45);
    color: var(--accent-hover);
    box-shadow: none;
  }
  .csl-btn.primary:hover:not(:disabled) {
    background: rgba(var(--c-accent-rgb), 0.34);
    color: var(--c-text);
  }

  .csl-seg {
    display: inline-flex;
  }
  .csl-seg button {
    height: 26px;
    padding: 0 8px;
    font-size: 11px;
    border-radius: 0;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }
  .csl-seg button:first-child {
    border-radius: var(--radius-sm, 6px) 0 0 var(--radius-sm, 6px);
  }
  .csl-seg button:last-child {
    border-radius: 0 var(--radius-sm, 6px) var(--radius-sm, 6px) 0;
  }
  .csl-seg button + button {
    margin-left: -1px;
  }
  .csl-seg button.on {
    background: rgba(var(--c-accent-rgb), 0.28);
    border-color: rgba(var(--c-accent-rgb), 0.55);
    color: var(--c-text);
    position: relative;
  }

  .csl-stage {
    position: relative;
    flex: 1 1 0;
    min-height: 120px;
    overflow: hidden;
    cursor: crosshair;
    user-select: none;
    background: var(--c-surface-0);
    outline: none;
  }
  .csl-stage:focus-visible {
    box-shadow: inset 0 0 0 2px rgba(var(--c-accent-rgb), 0.5);
  }
  .csl-stage.grabbing {
    cursor: grabbing;
  }
  .csl-canvas {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    display: block;
  }

  .csl-cross {
    position: absolute;
    pointer-events: none;
    background: rgba(var(--c-ink-rgb), 0.35);
  }
  .csl-cross.v {
    width: 1px;
    transform: translateX(-0.5px);
  }
  .csl-cross.h {
    height: 1px;
    transform: translateY(-0.5px);
  }

  .csl-tip {
    position: absolute;
    display: flex;
    gap: 8px;
    padding: 3px 8px;
    transform: translate(14px, -130%);
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    background: rgba(var(--c-surface-1-rgb), 0.92);
    border: 1px solid var(--accent-primary);
    border-radius: 4px;
    color: var(--c-text);
    pointer-events: none;
    white-space: nowrap;
    z-index: 5;
  }
  .csl-tip.left {
    transform: translate(calc(-100% - 14px), -130%);
  }
  .val {
    color: var(--accent-hover);
  }
  .none {
    color: var(--text-muted);
  }

  .csl-empty {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 4px;
    text-align: center;
    padding: 0 24px;
    color: var(--text-secondary);
    font-size: 13px;
    pointer-events: none;
  }
  .csl-busy {
    position: absolute;
    top: 8px;
    right: 12px;
    padding: 2px 10px;
    font-size: 11px;
    border-radius: 10px;
    background: rgba(var(--c-accent-rgb), 0.25);
    color: var(--c-accent-strong);
    pointer-events: none;
  }
  .csl-ve {
    position: absolute;
    bottom: 6px;
    right: 12px;
    font-size: 10px;
    font-family: var(--font-mono, monospace);
    color: rgba(var(--c-ink-rgb), 0.45);
    pointer-events: none;
  }

  .csl-status {
    flex: 0 0 auto;
    display: flex;
    flex-wrap: wrap;
    gap: 4px 16px;
    min-height: 26px;
    padding: 4px 12px;
    align-items: center;
    font-size: 11px;
    font-family: var(--font-mono, monospace);
    color: var(--text-secondary);
    border-top: 1px solid var(--glass-border);
  }
  .csl-status b {
    color: var(--text-primary);
    font-weight: 600;
  }
  .csl-status .val {
    color: var(--accent-hover);
  }
  .hint {
    color: var(--text-muted);
  }
</style>
