<script lang="ts">
  /**
   * Range-height display of an RHI sweep: x = ground range (km), y = height (km).
   *
   * Used by RadarViewer instead of the map PPI when the selected sweep is an RHI. Variable / sweep
   * selection lives in RadarViewer's control bar; colormap and colour range are owned by RadarViewer
   * too and arrive as props, so both views stay in step.
   *
   * Data flow (see utils/rhiGeometry.ts, rhiClean.ts): entry -> `cleaned` (quality masks from the
   * sweep's SQI / SNR / RHOHV, the variable's value mask, despeckle; "Raw data" skips all of it) ->
   * lookup tables -> picture. With no smoothing the picture is `rasterizeRhi` (inverse mapped, one
   * pixel at a time, exact gate edges at any zoom); with smoothing the field is sampled onto a fine
   * regular grid once, smoothed with the cross-section's NaN-aware Gaussian and resampled per view
   * (nearest / bilinear / bicubic). Hover uses the same lookup, so the read-out matches the picture.
   * A caption says what was applied; Save PNG writes it into the image.
   *
   * Colours are CSS variables with fallbacks (--rhi-*), so a theme can restyle the plot.
   */
  import { onMount } from 'svelte';
  import ColorBar from './ColorBar.svelte';
  import ValueMaskControl from './ValueMaskControl.svelte';
  import CrossSectionSmoothing from './CrossSectionSmoothing.svelte';
  import { getCachedSweep, type SweepDataEntry } from '../utils/ppiRenderer';
  import { wsManager } from '../utils/websocket';
  import { COLORMAP_DATA } from '../utils/colormaps';
  import {
    buildLookup,
    colorizeValues,
    autoExtent,
    DEFAULT_HEIGHT_CAP_KM,
    TROPOSPHERE_KM,
    formatTick,
    gridLines,
    niceTicks,
    rasterizeRhi,
    rasterizeRhiGrid,
    sampleAt,
    type RhiField,
    type RhiSample,
  } from '../utils/rhiGeometry';
  import {
    addPngText,
    gaussianSmoothNaN,
    gridSize,
    isPlain,
    resampleGrid,
  } from '../utils/crossSectionRaster';
  import {
    QUALITY_LABEL,
    QUALITY_STEP,
    cleanField,
    findCompanions,
    noiseCaption,
    type QualityKind,
  } from '../utils/rhiClean';
  import { rhiNoise, setNoise, resetNoise } from '../stores/rhiNoise';
  import { maskOf, valueMasks } from '../stores/valueMask';
  import { canvasToBlob, savePng } from '../utils/crossSectionExport';

  interface Props {
    /** The sweep to draw; an RHI entry carries `kind === 'rhi'` and per-ray `elevation`. */
    entry: SweepDataEntry | null;
    /** Radar site altitude in metres ASL, when the file states it (enables the ASL height axis). */
    altitudeM?: number | null;
    /** Label of the shown variable (legend title / read-out). */
    variableLabel?: string;
    /** Name of the shown variable and the variables the sweep holds (quality fields are found among them). */
    variable?: string;
    variables?: string[];
    cmap: string;
    vmin: number;
    vmax: number;
    units: string;
    loading?: boolean;
    /** Why the sweep could not be loaded (shown with a Retry button). */
    error?: string | null;
    onCmapChange?: (name: string) => void;
    onRangeChange?: (vmin: number, vmax: number) => void;
    onRetry?: () => void;
  }

  let {
    entry,
    altitudeM = null,
    variableLabel = '',
    variable = '',
    variables = [],
    cmap,
    vmin,
    vmax,
    units,
    loading = false,
    error = null,
    onCmapChange,
    onRangeChange,
    onRetry,
  }: Props = $props();

  // ── Layout constants ─────────────────────────────────────────────────────
  const MARGIN = { left: 58, right: 16, top: 14, bottom: 52 };
  /** Said on the axes and in the exported image: how beam height is computed. */
  const BEAM_MODEL = 'beam path: standard refraction, 4/3 effective Earth radius';
  const EXAGGERATIONS = [1, 1.5, 2, 3];

  // ── State ────────────────────────────────────────────────────────────────
  let container: HTMLDivElement | undefined = $state();
  let baseCanvas: HTMLCanvasElement | undefined = $state();
  let overlayHost: HTMLDivElement | undefined = $state();
  let overlayCanvas: HTMLCanvasElement | undefined = $state();
  let width = $state(800);
  let height = $state(500);

  /** 'auto' picks the smallest exaggeration (1, 1.5, 2 or 3) that gives a shallow echo a usable height. */
  let exagChoice = $state<'auto' | number>('auto');
  let showGrid = $state(true);
  /** The height axis never goes above this (km) unless 'Full beam extent' is on. */
  let heightMaxKm = $state(DEFAULT_HEIGHT_CAP_KM);
  /** Show the whole geometric extent of the scan instead of the part that holds valid gates. */
  let fullExtent = $state(false);
  let heightRef = $state<'agl' | 'asl'>('agl');
  /** View centre (ground km, height-above-radar km) and horizontal scale (CSS px per km). */
  let cx = $state(0);
  let cy = $state(0);
  let pxPerKm = $state(10);
  let dragging = $state(false);
  let hover: { sample: RhiSample | null; x: number; y: number; groundKm: number; heightKm: number } | null =
    $state.raw(null);

  const hasAltitude = $derived(altitudeM != null && Number.isFinite(altitudeM));
  const offsetKm = $derived(heightRef === 'asl' && hasAltitude ? (altitudeM as number) / 1000 : 0);

  // ── Data -> cleaned field -> lookup ──────────────────────────────────────
  const noise = $derived($rhiNoise);

  /** The sweep as measured (rays in ascending elevation). */
  const rawField: RhiField | null = $derived.by(() => {
    const e = entry;
    if (!e || e.kind !== 'rhi' || !e.elevation || e.elevation.length !== e.nAz) return null;
    return { elevation: e.elevation, range: e.range, values: e.values, nRays: e.nAz, nRange: e.nRange };
  });

  // Quality fields of the same sweep (SQI / NCP, SNR, RHOHV): fetched quietly into the sweep cache.
  let qualityRev = $state(0);
  const pendingQuality = new Set<string>();
  const companions = $derived(findCompanions(variables.filter((v) => v !== variable)));
  $effect(() => {
    const e = entry;
    if (!e || noise.raw) return;
    for (const kind of ['sqi', 'snr', 'rhohv'] as QualityKind[]) {
      const name = companions[kind];
      if (!name || !noise[kind].on) continue;
      const c = getCachedSweep(name, e.sweep);
      const key = `${name}:${e.sweep}`;
      if ((c && c.nAz === e.nAz && c.nRange === e.nRange) || pendingQuality.has(key)) continue;
      pendingQuality.add(key);
      wsManager.requestSweepData(name, e.sweep, undefined, {
        priority: 'prefetch',
        group: 'rhi-quality',
        onSettled: () => {
          pendingQuality.delete(key);
          qualityRev++;
        },
      });
    }
  });
  const quality = $derived.by(() => {
    void qualityRev;
    const e = entry;
    const out: Partial<Record<QualityKind, { name: string; values: ArrayLike<number> }>> = {};
    if (!e || noise.raw) return out;
    for (const kind of ['sqi', 'snr', 'rhohv'] as QualityKind[]) {
      const name = companions[kind];
      if (!name || !noise[kind].on) continue;
      const c = getCachedSweep(name, e.sweep);
      if (c && c.nAz === e.nAz && c.nRange === e.nRange) out[kind] = { name, values: c.values };
    }
    return out;
  });
  /** Quality masks that are switched on but whose field is still on its way. */
  const qualityLoading = $derived(
    !noise.raw && (['sqi', 'snr', 'rhohv'] as QualityKind[]).some((k) => companions[k] && noise[k].on && !quality[k]),
  );

  const valueMask = $derived(maskOf($valueMasks, variable));
  const cleaned = $derived(
    rawField
      ? cleanField(
          { values: rawField.values, nRays: rawField.nRays, nRange: rawField.nRange, quality, mask: valueMask },
          noise,
          units,
        )
      : null,
  );
  /** What is drawn: the cleaned values on the sweep's ray x gate grid. */
  const field: RhiField | null = $derived(rawField && cleaned ? { ...rawField, values: cleaned.values } : null);
  const lookup = $derived(field ? buildLookup(field) : null);
  const rawLookup = $derived(lookup && rawField ? { ...lookup, field: rawField } : null);
  /**
   * The window the view shows: the ground range and height of the valid gates at the current colour
   * range, rounded up and cut at the height cap (the beams reach ~95 km, the weather does not).
   */
  const extent = $derived(lookup ? autoExtent(lookup, vmin, { capKm: heightMaxKm, full: fullExtent }) : null);

  const gateKm = $derived(lookup ? lookup.gates.dr / 1000 : null);
  const smoothing = $derived(noise.raw || isPlain(noise.smoothing) ? null : noise.smoothing);
  /** The cleaned field on a fine regular grid, Gaussian-smoothed; null when nothing is smoothed. */
  const smoothGrid = $derived.by(() => {
    const lk = lookup;
    const sm = smoothing;
    if (!lk || !sm || !gateKm) return null;
    const ex = extent;
    if (!ex) return null;
    const full = {
      d0: ex.xMinKm - gateKm,
      d1: ex.xMaxKm + gateKm,
      h0: ex.yMinKm - gateKm,
      h1: ex.yMaxKm + gateKm,
    };
    const { nx, ny } = gridSize(full, Math.ceil((full.d1 - full.d0) / gateKm), sm.upsample);
    const grid = rasterizeRhiGrid(lk, full, nx, ny);
    if (sm.sigma > 0) {
      const sigmaKm = sm.sigma * gateKm;
      grid.data = gaussianSmoothNaN(
        grid.data,
        nx,
        ny,
        sigmaKm / ((full.d1 - full.d0) / nx),
        sigmaKm / ((full.h1 - full.h0) / ny),
      );
    }
    return grid;
  });

  const caption = $derived(noiseCaption(noise, noise.raw ? null : cleaned, gateKm));
  // The plot box has the aspect of the extent (equal km per pixel in x, times the vertical
  // exaggeration in y), so no empty axis range is shown around the data.
  const availW = $derived(Math.max(40, width - MARGIN.left - MARGIN.right));
  const availH = $derived(Math.max(40, height - MARGIN.top - MARGIN.bottom));
  const xSpanKm = $derived(extent ? Math.max(extent.xMaxKm - extent.xMinKm, 1) : 1);
  const ySpanKm = $derived(extent ? Math.max(extent.yMaxKm - extent.yMinKm, 1) : 1);
  const exag = $derived.by(() => {
    if (exagChoice !== 'auto') return exagChoice;
    if (!extent) return 1;
    // true equal aspect (1x) unless the echo would fill less than half the pane's height
    const k1 = Math.min(availW / xSpanKm, availH / ySpanKm);
    for (const e of EXAGGERATIONS) if (ySpanKm * k1 * e >= 0.55 * availH) return e;
    return EXAGGERATIONS[EXAGGERATIONS.length - 1];
  });
  /** CSS px per km of the fitted view. */
  const fitK = $derived(extent ? Math.min(availW / xSpanKm, availH / (ySpanKm * exag)) : 10);
  const plotW = $derived(extent ? Math.max(40, Math.round(xSpanKm * fitK)) : availW);
  const plotH = $derived(extent ? Math.max(40, Math.round(ySpanKm * fitK * exag)) : availH);
  /** Top-left of the plot box on the canvas (centred in the free area). */
  const ox = $derived(MARGIN.left + Math.max(0, (availW - plotW) / 2));
  const oy = $derived(MARGIN.top + Math.max(0, (availH - plotH) / 2));
  const elRange = $derived(
    lookup && lookup.rays.n > 0
      ? { lo: lookup.rays.lo[0], hi: lookup.rays.hi[lookup.rays.n - 1] }
      : { lo: 0, hi: 0 },
  );

  const planeAz = $derived(entry?.rhiAzimuth ?? null);
  const lut = $derived(COLORMAP_DATA[cmap] ?? COLORMAP_DATA['turbo']);

  // ── View fitting ─────────────────────────────────────────────────────────
  /** Show the whole extent (the plot box is sized to it, so it fills the box exactly). */
  function fit() {
    const ex = extent;
    if (!ex) return;
    userAdjusted = false;
    pxPerKm = fitK;
    cx = (ex.xMinKm + ex.xMaxKm) / 2;
    cy = (ex.yMinKm + ex.yMaxKm) / 2;
  }

  /** Keep the window inside the extent: nothing outside the valid range is ever shown. */
  function clampView() {
    const ex = extent;
    if (!ex) return;
    const hw = plotW / (2 * pxPerKm);
    const hh = plotH / (2 * pxPerKm * exag);
    cx = hw * 2 >= ex.xMaxKm - ex.xMinKm ? (ex.xMinKm + ex.xMaxKm) / 2 : Math.min(Math.max(cx, ex.xMinKm + hw), ex.xMaxKm - hw);
    cy = hh * 2 >= ex.yMaxKm - ex.yMinKm ? (ex.yMinKm + ex.yMaxKm) / 2 : Math.min(Math.max(cy, ex.yMinKm + hh), ex.yMaxKm - hh);
  }

  // Refit when another sweep / file arrives or the pane is resized (until the user pans or zooms);
  // a new variable of the same sweep keeps pan and zoom.
  let fitKey = '';
  let userAdjusted = false;
  $effect(() => {
    const e = entry;
    const lk = lookup;
    void plotW;
    void plotH;
    if (!e || !lk) return;
    void [heightMaxKm, fullExtent, extent];
    const key = `${e.sweep}:${e.nAz}:${e.nRange}:${e.rhiAzimuth ?? ''}:${lk.rays.centers[0]}:${e.maxRange}`;
    if (key !== fitKey || !userAdjusted) {
      fitKey = key;
      fit();
    }
  });

  function changeExaggeration(v: string) {
    exagChoice = v === 'auto' ? 'auto' : parseFloat(v);
    fit();
  }

  // ── Coordinate mapping (plot-local CSS px <-> km) ────────────────────────
  const xKm = (px: number) => cx + (px - plotW / 2) / pxPerKm;
  const yKm = (py: number) => cy - (py - plotH / 2) / (pxPerKm * exag);
  const pxX = (km: number) => (km - cx) * pxPerKm + plotW / 2;
  const pxY = (km: number) => plotH / 2 - (km - cy) * pxPerKm * exag;

  // ── Drawing ──────────────────────────────────────────────────────────────
  let raf = 0;
  let idleTimer: ReturnType<typeof setTimeout> | undefined;
  /** True while the user pans / zooms: draw at reduced resolution, then sharpen when idle. */
  let interacting = false;
  let rasterCanvas: HTMLCanvasElement | null = null;
  let rasterImage: ImageData | null = null;

  function cssVar(name: string, fallback: string): string {
    if (!container) return fallback;
    const v = getComputedStyle(container).getPropertyValue(name).trim();
    return v || fallback;
  }

  function schedule() {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      draw();
    });
  }

  function sharpenSoon() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      interacting = false;
      schedule();
    }, 140);
  }

  function prepareCanvas(c: HTMLCanvasElement, dpr: number): CanvasRenderingContext2D | null {
    const w = Math.round(width * dpr);
    const h = Math.round(height * dpr);
    if (c.width !== w || c.height !== h) {
      c.width = w;
      c.height = h;
    }
    const ctx = c.getContext('2d');
    ctx?.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }

  function draw() {
    if (!baseCanvas) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const ctx = prepareCanvas(baseCanvas, dpr);
    if (!ctx) return;

    const bg = cssVar('--rhi-bg', cssVar('--bg-primary', '#080a14'));
    const plotBg = cssVar('--rhi-plot-bg', cssVar('--bg-secondary', '#0d1020'));
    const fg = cssVar('--rhi-text', cssVar('--text-secondary', '#9da2c2'));
    const fgStrong = cssVar('--rhi-text-strong', cssVar('--text-primary', '#e4e6f4'));
    const axisLine = cssVar('--rhi-axis', cssVar('--border-light', 'rgba(140,160,250,0.35)'));
    const gridCol = cssVar('--rhi-grid', 'rgba(170,180,230,0.30)');
    const halo = cssVar('--rhi-label-halo', plotBg);

    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, width, height);
    ctx.save();
    ctx.translate(ox, oy);
    ctx.fillStyle = plotBg;
    ctx.fillRect(0, 0, plotW, plotH);

    // 1. the field
    if (lookup && lut) {
      const s = interacting ? 0.5 : Math.min(dpr, 1.5);
      const rw = Math.max(1, Math.round(plotW * s));
      const rh = Math.max(1, Math.round(plotH * s));
      if (!rasterCanvas) rasterCanvas = document.createElement('canvas');
      if (!rasterImage || rasterImage.width !== rw || rasterImage.height !== rh) {
        rasterImage = new ImageData(rw, rh);
        rasterCanvas.width = rw;
        rasterCanvas.height = rh;
      }
      if (smoothGrid && smoothing) {
        const vals = resampleGrid(
          smoothGrid,
          { d0: xKm(0), d1: xKm(plotW), h0: yKm(plotH), h1: yKm(0) },
          rw,
          rh,
          smoothing.interp,
        );
        colorizeValues(vals, lut, vmin, vmax, rasterImage.data);
      } else {
        rasterizeRhi(
          lookup,
          {
            xLeftKm: xKm(0),
            yTopKm: yKm(0),
            kmPerPxX: 1 / (pxPerKm * s),
            kmPerPxY: 1 / (pxPerKm * exag * s),
            width: rw,
            height: rh,
          },
          lut,
          vmin,
          vmax,
          rasterImage.data,
        );
      }
      rasterCanvas.getContext('2d')?.putImageData(rasterImage, 0, 0);
      ctx.imageSmoothingEnabled = s < 1;
      ctx.drawImage(rasterCanvas, 0, 0, plotW, plotH);
    }

    // 2. grid of constant range and constant elevation, clipped to the plot
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, plotW, plotH);
    ctx.clip();
    // ground line (height 0 above the radar, or the sea level when ASL)
    const groundY = pxY(-offsetKm);
    if (groundY >= 0 && groundY <= plotH) {
      ctx.strokeStyle = axisLine;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, groundY);
      ctx.lineTo(plotW, groundY);
      ctx.stroke();
    }
    if (showGrid && lookup) {
      ctx.font = '11px var(--font-mono, ui-monospace, monospace)';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = gridCol;
      const lines = gridLines(elRange.lo, elRange.hi, lookup.gates.maxM);
      const labels: { text: string; x: number; y: number; align: CanvasTextAlign; base: CanvasTextBaseline }[] = [];
      for (const ln of lines) {
        ctx.beginPath();
        const visible: [number, number][] = [];
        let started = false;
        for (const [gx, gy] of ln.points) {
          const X = pxX(gx);
          const Y = pxY(gy);
          if (!started) {
            ctx.moveTo(X, Y);
            started = true;
          } else ctx.lineTo(X, Y);
          if (X >= 4 && X <= plotW - 4 && Y >= 12 && Y <= plotH - 4) visible.push([X, Y]);
        }
        ctx.stroke();
        if (visible.length === 0) continue;
        if (ln.kind === 'range') {
          // where the arc leaves the top of the picture
          const p = visible[visible.length - 1];
          labels.push({ text: `${ln.value} km`, x: p[0], y: p[1] + 2, align: 'center', base: 'top' });
        } else {
          // part-way along the visible ray, clear of the range labels
          const p = visible[Math.floor(0.7 * (visible.length - 1))];
          labels.push({ text: `${ln.value}°`, x: p[0] - 3, y: p[1], align: 'right', base: 'bottom' });
        }
      }
      ctx.setLineDash([]);
      ctx.lineWidth = 3;
      for (const l of labels) {
        ctx.textAlign = l.align;
        ctx.textBaseline = l.base;
        ctx.strokeStyle = halo;
        ctx.strokeText(l.text, l.x, l.y);
        ctx.fillStyle = fg;
        ctx.fillText(l.text, l.x, l.y);
      }
    }
    if (extent?.capped && !fullExtent) {
      ctx.font = '10.5px var(--font-sans, system-ui, sans-serif)';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'top';
      const note = `height axis cut at ${fmtKm(extent.yMaxKm)} km - data reach higher`;
      ctx.lineWidth = 3;
      ctx.strokeStyle = halo;
      ctx.strokeText(note, plotW - 6, 5);
      ctx.fillStyle = cssVar('--accent-warning', '#fbbf24');
      ctx.fillText(note, plotW - 6, 5);
    }
    ctx.restore();

    // 3. axes
    ctx.strokeStyle = axisLine;
    ctx.lineWidth = 1;
    ctx.strokeRect(0.5, 0.5, plotW - 1, plotH - 1);
    ctx.fillStyle = fg;
    ctx.font = '11px var(--font-sans, system-ui, sans-serif)';

    const xTicks = niceTicks(xKm(0), xKm(plotW), Math.max(3, plotW / 90));
    const xStep = xTicks.length > 1 ? xTicks[1] - xTicks[0] : 1;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (const t of xTicks) {
      const X = Math.round(pxX(t)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(X, plotH);
      ctx.lineTo(X, plotH + 4);
      ctx.stroke();
      ctx.fillText(formatTick(t, xStep), X, plotH + 7);
    }
    const yTicks = niceTicks(yKm(plotH) + offsetKm, yKm(0) + offsetKm, Math.max(3, plotH / 50));
    const yStep = yTicks.length > 1 ? yTicks[1] - yTicks[0] : 1;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (const t of yTicks) {
      const Y = Math.round(pxY(t - offsetKm)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(-4, Y);
      ctx.lineTo(0, Y);
      ctx.stroke();
      ctx.fillText(formatTick(t, yStep), -7, Y);
    }
    ctx.fillStyle = fgStrong;
    ctx.font = '600 11px var(--font-sans, system-ui, sans-serif)';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    ctx.fillText('Ground range (km)', plotW / 2, plotH + MARGIN.bottom - 18);
    ctx.font = '10px var(--font-sans, system-ui, sans-serif)';
    ctx.fillStyle = fg;
    ctx.fillText(exag === 1 ? BEAM_MODEL : `${BEAM_MODEL}  \u00B7  vertical exaggeration ${exag}\u00D7`, plotW / 2, plotH + MARGIN.bottom - 4);
    ctx.save();
    ctx.translate(-46, plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textBaseline = 'top';
    ctx.fillText(offsetKm ? 'Height above sea level (km)' : 'Height above radar (km)', 0, -6);
    ctx.restore();

    ctx.restore();
    drawOverlay();
  }

  /** Crosshair + hover marker on the transparent top canvas (cheap, no re-rasterising). */
  function drawOverlay() {
    if (!overlayCanvas) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const ctx = prepareCanvas(overlayCanvas, dpr);
    if (!ctx) return;
    ctx.clearRect(0, 0, width, height);
    const h = hover;
    if (!h) return;
    ctx.save();
    ctx.translate(ox, oy);
    ctx.beginPath();
    ctx.rect(0, 0, plotW, plotH);
    ctx.clip();
    ctx.strokeStyle = cssVar('--rhi-crosshair', 'rgba(255,255,255,0.55)');
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(h.x + 0.5, 0);
    ctx.lineTo(h.x + 0.5, plotH);
    ctx.moveTo(0, h.y + 0.5);
    ctx.lineTo(plotW, h.y + 0.5);
    ctx.stroke();
    ctx.restore();
  }

  // Redraw when anything that shapes the picture changes.
  $effect(() => {
    void [field, lookup, smoothGrid, smoothing, lut, vmin, vmax, showGrid, offsetKm, exag, cx, cy, pxPerKm, width, height, plotW, plotH, ox, oy, extent];
    schedule();
  });
  $effect(() => {
    void hover;
    drawOverlay();
  });

  // ── Interaction ──────────────────────────────────────────────────────────
  let dragStart: { x: number; y: number; cx: number; cy: number } | null = null;

  function plotPoint(e: PointerEvent | WheelEvent | MouseEvent): { x: number; y: number } | null {
    if (!baseCanvas) return null;
    const r = baseCanvas.getBoundingClientRect();
    return { x: e.clientX - r.left - ox, y: e.clientY - r.top - oy };
  }

  function onPointerDown(e: PointerEvent) {
    if (e.button !== 0 || !field) return;
    const p = plotPoint(e);
    if (!p) return;
    dragStart = { x: e.clientX, y: e.clientY, cx, cy };
    userAdjusted = true;
    dragging = true;
    interacting = true;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }

  function onPointerMove(e: PointerEvent) {
    const p = plotPoint(e);
    if (dragStart) {
      cx = dragStart.cx - (e.clientX - dragStart.x) / pxPerKm;
      cy = dragStart.cy + (e.clientY - dragStart.y) / (pxPerKm * exag);
      clampView();
      sharpenSoon();
    }
    if (!p || !lookup || p.x < 0 || p.y < 0 || p.x > plotW || p.y > plotH) {
      hover = null;
      return;
    }
    const gKm = xKm(p.x);
    const hKm = yKm(p.y);
    hover = { sample: sampleAt(lookup, gKm * 1000, hKm * 1000), x: p.x, y: p.y, groundKm: gKm, heightKm: hKm };
  }

  function onPointerUp(e: PointerEvent) {
    if (dragStart) {
      dragStart = null;
      dragging = false;
      sharpenSoon();
      try {
        (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
      } catch {
        /* already released */
      }
    }
  }

  function onPointerLeave() {
    if (!dragStart) hover = null;
  }

  function zoomAt(factor: number, px: number, py: number) {
    const next = Math.min(Math.max(pxPerKm * factor, fitK), fitK * 80);
    userAdjusted = true;
    // keep the point under the cursor fixed
    const gx = xKm(px);
    const gy = yKm(py);
    pxPerKm = next;
    cx = gx - (px - plotW / 2) / next;
    cy = gy + (py - plotH / 2) / (next * exag);
    clampView();
    interacting = true;
    sharpenSoon();
  }

  function onWheel(e: WheelEvent) {
    if (!field) return;
    const p = plotPoint(e);
    if (!p) return;
    e.preventDefault();
    zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, Math.min(Math.max(p.x, 0), plotW), Math.min(Math.max(p.y, 0), plotH));
  }

  function onKeyDown(e: KeyboardEvent) {
    if (!field) return;
    if (e.key === '+' || e.key === '=') zoomAt(1.25, plotW / 2, plotH / 2);
    else if (e.key === '-' || e.key === '_') zoomAt(1 / 1.25, plotW / 2, plotH / 2);
    else if (e.key === '0' || e.key.toLowerCase() === 'f') fit();
    else return;
    e.preventDefault();
  }

  onMount(() => {
    const ro = new ResizeObserver((entries) => {
      for (const r of entries) {
        const w = Math.round(r.contentRect.width);
        const h = Math.round(r.contentRect.height);
        if (w > 0 && h > 0) {
          width = w;
          height = h;
        }
      }
    });
    if (container) ro.observe(container);
    // Theme switches change the CSS variables the canvas reads.
    const mo = new MutationObserver(() => schedule());
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme', 'style'] });
    return () => {
      ro.disconnect();
      mo.disconnect();
      cancelAnimationFrame(raf);
      clearTimeout(idleTimer);
    };
  });

  $effect(() => {
    const el = overlayHost;
    if (!el) return;
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  });

  const KINDS: QualityKind[] = ['sqi', 'snr', 'rhohv'];
  const fmtKm = (v: number) => String(Number(v.toFixed(1)));

  // ── Noise panel + export ─────────────────────────────────────────────────
  let noiseOpen = $state(false);
  let noiseHost: HTMLDivElement | undefined = $state();
  $effect(() => {
    if (!noiseOpen) return;
    const away = (e: PointerEvent) => {
      if (noiseHost && !noiseHost.contains(e.target as Node)) noiseOpen = false;
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') noiseOpen = false;
    };
    window.addEventListener('pointerdown', away, true);
    window.addEventListener('keydown', esc);
    return () => {
      window.removeEventListener('pointerdown', away, true);
      window.removeEventListener('keydown', esc);
    };
  });

  function setThreshold(kind: QualityKind, patch: { on?: boolean; min?: number }) {
    setNoise({ [kind]: { ...noise[kind], ...patch } });
  }

  /** The picture with a title, colour bar and the caption, as a PNG (the caption is also stored in it). */
  async function exportPng() {
    if (!baseCanvas || !field || !lut) return;
    const dpr = baseCanvas.width / width;
    const topBar = 28;
    const bottomBar = 78;
    const out = document.createElement('canvas');
    out.width = baseCanvas.width;
    out.height = Math.round((height + topBar + bottomBar) * dpr);
    const ctx = out.getContext('2d');
    if (!ctx) return;
    const bg = cssVar('--rhi-bg', '#080a14');
    const fg = cssVar('--rhi-text', '#9da2c2');
    const fgStrong = cssVar('--rhi-text-strong', '#e4e6f4');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, width, height + topBar + bottomBar);
    ctx.drawImage(baseCanvas, 0, topBar, width, height);
    const sans = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
    ctx.textBaseline = 'middle';
    ctx.font = `600 13px ${sans}`;
    ctx.fillStyle = fgStrong;
    ctx.textAlign = 'left';
    const title = [variableLabel || variable, planeAz != null ? `RHI ${planeAz.toFixed(1)}\u00B0` : 'RHI', entry ? `sweep ${entry.sweep}` : '']
      .filter(Boolean)
      .join('  \u00B7  ');
    ctx.fillText(title, MARGIN.left, topBar / 2 + 2);
    // colour bar
    const barW = Math.min(360, width - MARGIN.left - MARGIN.right);
    const barX = MARGIN.left;
    const barY = topBar + height + 8;
    for (let i = 0; i < barW; i++) {
      const t = Math.round((i / Math.max(1, barW - 1)) * 255) * 3;
      ctx.fillStyle = `rgb(${lut[t]},${lut[t + 1]},${lut[t + 2]})`;
      ctx.fillRect(barX + i, barY, 1, 10);
    }
    ctx.font = `10.5px ${sans}`;
    ctx.fillStyle = fg;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (const t of niceTicks(vmin, vmax, 5)) {
      const x = barX + ((t - vmin) / (vmax - vmin)) * barW;
      ctx.fillText(formatTick(t, Math.abs(vmax - vmin) / 5), x, barY + 13);
    }
    ctx.textAlign = 'left';
    ctx.fillText(units, barX + barW + 8, barY - 1);
    // caption, wrapped
    ctx.font = `11px ${sans}`;
    const maxW = width - MARGIN.left - MARGIN.right;
    const words = caption.split(' ');
    let line = '';
    let y = barY + 34;
    for (const w of words) {
      const test = line ? `${line} ${w}` : w;
      if (ctx.measureText(test).width > maxW && line) {
        ctx.fillText(line, MARGIN.left, y);
        y += 14;
        line = w;
      } else line = test;
    }
    if (line) ctx.fillText(line, MARGIN.left, y);
    const blob = await canvasToBlob(out);
    const bytes = addPngText(new Uint8Array(await blob.arrayBuffer()), 'Description', `${title}. ${caption}`);
    const name = `rhi_${(variable || 'field').replace(/[^\w.-]+/g, '_')}_az${planeAz != null ? planeAz.toFixed(0) : 'x'}_sweep${entry?.sweep ?? 0}`;
    await savePng(name, new Blob([bytes as BlobPart], { type: 'image/png' }));
  }

  // ── Read-out ─────────────────────────────────────────────────────────────
  const readout = $derived.by(() => {
    const h = hover;
    if (!h) return null;
    const s = h.sample;
    const heightShown = h.heightKm + offsetKm;
    // a gate the noise settings hide: say so, with the measured value
    const rawS = s && rawLookup && !Number.isFinite(s.value) ? sampleAt(rawLookup, h.groundKm * 1000, h.heightKm * 1000) : null;
    return {
      value: s && Number.isFinite(s.value) ? s.value : null,
      hidden: rawS && Number.isFinite(rawS.value) ? rawS.value : null,
      inScan: s !== null,
      range: s ? s.rangeM / 1000 : null,
      el: s ? s.elDeg : null,
      ground: h.groundKm,
      height: heightShown,
      aboveTroposphere: heightShown + (offsetKm ? 0 : hasAltitude ? (altitudeM as number) / 1000 : 0) > TROPOSPHERE_KM,
    };
  });

  function fmtValue(v: number): string {
    const a = Math.abs(v);
    return a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2);
  }

  const summary = $derived.by(() => {
    if (!entry || !field) return '';
    const parts: string[] = [];
    if (planeAz != null) parts.push(`RHI ${planeAz.toFixed(1)}°`);
    parts.push(`${lookup!.rays.centers[0].toFixed(1)}–${lookup!.rays.centers[lookup!.rays.n - 1].toFixed(1)}°`);
    return parts.join(' · ');
  });
  const summaryTitle = $derived(
    field && lookup
      ? `Range-height scan, elevation ${lookup.rays.centers[0].toFixed(1)} to ${lookup.rays.centers[lookup.rays.n - 1].toFixed(1)} degrees, ${field.nRays} rays x ${field.nRange} gates`
      : 'Range-height scan',
  );
</script>

<div class="rhi-viewer" data-testid="rhi-viewer">
  <div class="rhi-toolbar">
    {#if summary}<span class="rhi-summary" title={summaryTitle}>{summary}</span>{/if}
    <label class="rhi-ctl" title="Vertical exaggeration. 1× is true equal aspect; Auto uses 1× unless that would draw a very flat picture. The factor is stated on the axis.">
      <span>Vertical</span>
      <select value={String(exagChoice)} onchange={(e) => changeExaggeration(e.currentTarget.value)} data-testid="rhi-exag">
        <option value="auto">Auto ({exag}×)</option>
        {#each EXAGGERATIONS as v}
          <option value={String(v)}>{v}×</option>
        {/each}
      </select>
    </label>
    <label class="rhi-ctl" title="Highest height shown (km). The beams reach much higher than any weather: the default view shows the heights that hold data, cut here.">
      <span>Top</span>
      <input type="number" class="rhi-num" min="2" max={Math.ceil(extent?.geometric.yMaxKm ?? 100)} step="5" value={heightMaxKm}
             disabled={fullExtent}
             onchange={(e) => { heightMaxKm = Math.max(2, Number(e.currentTarget.value) || DEFAULT_HEIGHT_CAP_KM); fit(); }}
             data-testid="rhi-height-max" aria-label="Highest height shown in km" />
      <span class="rhi-unit">km</span>
    </label>
    <label class="rhi-ctl check" title="Show the whole geometric extent of the beams instead of the valid data (fit to data)">
      <input type="checkbox" checked={fullExtent} onchange={(e) => { fullExtent = e.currentTarget.checked; fit(); }} data-testid="rhi-full-extent" />
      <span>Full beam extent</span>
    </label>
    <label class="rhi-ctl" title={hasAltitude ? 'Height reference' : 'The file does not state the radar altitude'}>
      <span>Height</span>
      <select bind:value={heightRef} disabled={!hasAltitude} data-testid="rhi-height-ref">
        <option value="agl" title="Height above the radar">AGL</option>
        <option value="asl" title="Height above sea level">ASL</option>
      </select>
    </label>
    <label class="rhi-ctl check" title="Lines of constant range and constant elevation">
      <input type="checkbox" bind:checked={showGrid} />
      <span>Grid</span>
    </label>
    <button class="rhi-btn" onclick={fit} disabled={!field} title="Show the whole scan (key 0, or double-click). Scroll to zoom, drag to pan.">Fit</button>

    <span class="rhi-sep" aria-hidden="true"></span>
    <label class="rhi-ctl check" title="Show the data exactly as measured: no quality masks, despeckling, value mask or smoothing">
      <input type="checkbox" checked={noise.raw} onchange={(e) => setNoise({ raw: e.currentTarget.checked })} data-testid="rhi-raw" />
      <span>Raw data</span>
    </label>
    <div class="rhi-noise-host" bind:this={noiseHost}>
      <button class="rhi-btn" class:on={noiseOpen} onclick={() => (noiseOpen = !noiseOpen)} aria-expanded={noiseOpen}
              title="Noise masks, despeckle, value mask and smoothing" data-testid="rhi-noise-btn">
        Noise &amp; smoothing {noiseOpen ? '\u25B4' : '\u25BE'}
      </button>
      {#if noiseOpen}
        <div class="rhi-pop" role="dialog" aria-label="Noise and smoothing" data-testid="rhi-noise-panel">
          <label class="rhi-row check strong">
            <input type="checkbox" checked={noise.raw} onchange={(e) => setNoise({ raw: e.currentTarget.checked })} />
            <span>Raw data <em>(turns everything below off)</em></span>
          </label>
          <fieldset class="rhi-group" disabled={noise.raw}>
            <legend>Hide low-quality gates</legend>
            {#each KINDS as kind (kind)}
              <div class="rhi-row">
                <label class="check">
                  <input type="checkbox" checked={noise[kind].on} disabled={!companions[kind]}
                         onchange={(e) => setThreshold(kind, { on: e.currentTarget.checked })} data-testid="rhi-q-{kind}" />
                  <span>{QUALITY_LABEL[kind]} &ge;</span>
                </label>
                <input type="number" class="rhi-num" value={noise[kind].min} step={QUALITY_STEP[kind]} disabled={!companions[kind]}
                       aria-label="{QUALITY_LABEL[kind]} lower limit"
                       onchange={(e) => setThreshold(kind, { min: Number(e.currentTarget.value) })} />
                <span class="rhi-hint">{companions[kind] ? companions[kind] : 'not in this sweep'}</span>
              </div>
            {/each}
          </fieldset>
          <fieldset class="rhi-group" disabled={noise.raw}>
            <legend>Despeckle</legend>
            <div class="rhi-row">
              <label class="check">
                <input type="checkbox" checked={noise.despeckle.on}
                       onchange={(e) => setNoise({ despeckle: { ...noise.despeckle, on: e.currentTarget.checked } })} data-testid="rhi-despeckle" />
                <span>Remove groups smaller than</span>
              </label>
              <input type="number" class="rhi-num" min="2" max="200" step="1" value={noise.despeckle.minGates}
                     aria-label="Smallest group of gates to keep"
                     onchange={(e) => setNoise({ despeckle: { ...noise.despeckle, minGates: Number(e.currentTarget.value) } })} />
              <span class="rhi-hint">gates</span>
            </div>
          </fieldset>
          <fieldset class="rhi-group" disabled={noise.raw}>
            <legend>Value mask ({variable || 'variable'})</legend>
            <ValueMaskControl {variable} {units} compact />
          </fieldset>
          <fieldset class="rhi-group" disabled={noise.raw}>
            <legend>Smoothing</legend>
            <CrossSectionSmoothing compact uid="rhi" value={noise.smoothing}
                                   onChange={(patch) => setNoise({ smoothing: { ...noise.smoothing, ...patch } })} />
          </fieldset>
          <div class="rhi-row end">
            <button class="rhi-btn" onclick={resetNoise} title="Back to the defaults: SQI / SNR / RHOHV masks, despeckle, light smoothing">Reset to defaults</button>
          </div>
        </div>
      {/if}
    </div>
    <button class="rhi-btn" onclick={exportPng} disabled={!field} title="Save the picture with its colour bar and a caption saying what was applied" data-testid="rhi-save">Save PNG</button>
  </div>

  <!-- svelte-ignore a11y_no_noninteractive_tabindex a11y_no_noninteractive_element_interactions -->
  <div
    class="rhi-plot"
    class:dragging
    bind:this={container}
    tabindex="0"
    role="application"
    aria-label="Range-height plot: x ground range, y height. Scroll to zoom, drag to pan, plus and minus keys zoom, 0 fits."
    onkeydown={onKeyDown}
  >
    <canvas bind:this={baseCanvas} class="rhi-canvas" data-testid="rhi-canvas"></canvas>
    <!-- transparent hit layer above the base canvas: hover, pan, wheel -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <div
      class="rhi-hit"
      bind:this={overlayHost}
      role="presentation"
      onpointerdown={onPointerDown}
      onpointermove={onPointerMove}
      onpointerup={onPointerUp}
      onpointercancel={onPointerUp}
      onpointerleave={onPointerLeave}
      ondblclick={fit}
    >
      <canvas bind:this={overlayCanvas} class="rhi-canvas rhi-overlay"></canvas>
    </div>

    {#if readout}
      <div class="rhi-readout" data-testid="rhi-readout">
        <div class="rhi-readout-value">
          {#if readout.value !== null}
            <strong>{fmtValue(readout.value)}</strong> {units}
            {#if variableLabel}<span class="rhi-readout-var">{variableLabel}</span>{/if}
          {:else if readout.hidden !== null}
            <span class="rhi-readout-none">hidden by the noise settings (measured {fmtValue(readout.hidden)} {units})</span>
          {:else if readout.inScan}
            <span class="rhi-readout-none">no data</span>
          {:else}
            <span class="rhi-readout-none">outside the scan</span>
          {/if}
        </div>
        <div class="rhi-readout-row">
          {#if readout.range !== null}<span>range {readout.range.toFixed(1)} km</span>{/if}
          <span>x {readout.ground.toFixed(1)} km</span>
          <span>h {readout.height.toFixed(2)} km {offsetKm ? 'ASL' : 'AGL'}</span>
          {#if readout.aboveTroposphere}<span class="rhi-warn" title="Above about {TROPOSPHERE_KM} km there is no weather: gates here are noise">beyond the troposphere</span>{/if}
          {#if readout.el !== null}<span>el {readout.el.toFixed(2)}°</span>{/if}
          {#if planeAz !== null}<span>az {planeAz.toFixed(1)}°</span>{/if}
        </div>
      </div>
    {/if}

    {#if loading}
      <div class="rhi-overlay-msg" data-testid="rhi-loading">
        <div class="rhi-spinner"></div>
        <p>Loading sweep data...</p>
      </div>
    {:else if error}
      <div class="rhi-overlay-msg" role="alert">
        <p>{error}</p>
        {#if onRetry}<button class="rhi-btn" onclick={onRetry}>Retry</button>{/if}
      </div>
    {:else if entry && !field}
      <div class="rhi-overlay-msg" role="alert">
        <p>This range-height sweep arrived without per-ray elevation, so it cannot be drawn.</p>
      </div>
    {:else if !entry}
      <div class="rhi-overlay-msg"><p>Select a variable to render</p></div>
    {/if}
  </div>

  {#if field}
    <div class="rhi-caption" class:raw={noise.raw} data-testid="rhi-caption" title={caption}>
      {caption}{#if qualityLoading}<span class="rhi-hint"> (loading quality fields...)</span>{/if}
    </div>
    <div class="rhi-colorbar">
      <ColorBar cmapName={cmap} {vmin} {vmax} {units} {onCmapChange} {onRangeChange} />
    </div>
  {/if}
</div>

<style>
  .rhi-viewer {
    /* Theme hooks; each falls back to the app tokens */
    --rhi-bg: var(--bg-primary, #080a14);
    --rhi-plot-bg: var(--bg-secondary, #0d1020);
    --rhi-text: var(--text-secondary, #9da2c2);
    --rhi-text-strong: var(--text-primary, #e4e6f4);
    --rhi-axis: var(--border-light, rgba(140, 160, 250, 0.35));
    --rhi-grid: rgba(170, 180, 230, 0.3);
    --rhi-crosshair: rgba(255, 255, 255, 0.55);
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    background: var(--rhi-bg);
    position: relative;
  }

  .rhi-toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 10px;
    padding: 4px var(--spacing-md, 12px);
    border-bottom: 1px solid var(--border-color, rgba(140, 160, 250, 0.08));
    background: var(--bg-secondary, #0d1020);
    font-size: 11px;
    color: var(--text-muted, #5c6088);
    flex-shrink: 0;
  }
  .rhi-summary {
    font-family: var(--font-mono, ui-monospace, monospace);
    color: var(--text-secondary, #9da2c2);
  }
  .rhi-ctl {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    font-weight: 600;
    white-space: nowrap;
  }
  .rhi-ctl.check {
    cursor: pointer;
    user-select: none;
  }
  .rhi-ctl.check input {
    margin: 0;
    width: 14px;
    height: 14px;
  }
  .rhi-btn {
    font-size: 11px;
    padding: 2px 10px;
  }
  .rhi-ctl select {
    font-size: 11px;
    padding: 2px 6px;
    min-width: 0;
  }

  .rhi-plot {
    position: relative;
    flex: 1;
    min-height: 0;
    overflow: hidden;
    outline: none;
    user-select: none;
  }
  .rhi-plot:focus-visible {
    box-shadow: inset 0 0 0 1px var(--accent-primary, #5b6cf7);
  }
  .rhi-canvas {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    display: block;
  }
  .rhi-hit {
    position: absolute;
    inset: 0;
    cursor: crosshair;
    touch-action: none;
  }
  .rhi-plot.dragging .rhi-hit {
    cursor: grabbing;
  }
  .rhi-overlay {
    pointer-events: none;
  }

  .rhi-readout {
    position: absolute;
    top: 22px;
    left: 70px;
    z-index: 3;
    pointer-events: none;
    padding: 6px 10px;
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: 11.5px;
    color: var(--text-primary, #e4e6f4);
    background: var(--glass-bg-heavy, rgba(8, 10, 20, 0.85));
    border: 1px solid var(--glass-border, rgba(140, 160, 250, 0.1));
    border-radius: var(--radius-sm, 6px);
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .rhi-readout-value strong {
    font-size: 13px;
  }
  .rhi-readout-var,
  .rhi-readout-none {
    color: var(--text-muted, #5c6088);
    margin-left: 6px;
  }
  .rhi-readout-none {
    margin-left: 0;
  }
  .rhi-readout-row {
    display: flex;
    gap: 12px;
    color: var(--text-secondary, #9da2c2);
  }

  .rhi-overlay-msg {
    position: absolute;
    inset: 0;
    z-index: 4;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-md, 12px);
    color: var(--text-muted, #5c6088);
    font-size: 13px;
    pointer-events: none;
  }
  .rhi-overlay-msg button {
    pointer-events: auto;
  }
  .rhi-spinner {
    width: 32px;
    height: 32px;
    border: 3px solid var(--border-color, rgba(140, 160, 250, 0.08));
    border-top-color: var(--accent-primary, #5b6cf7);
    border-radius: 50%;
    animation: rhi-spin 800ms linear infinite;
  }
  @keyframes rhi-spin {
    to {
      transform: rotate(360deg);
    }
  }

  .rhi-unit {
    text-transform: none;
    font-weight: 400;
  }
  .rhi-warn {
    color: var(--accent-warning, #fbbf24);
  }
  .rhi-sep {
    width: 1px;
    align-self: stretch;
    background: var(--border-color, rgba(140, 160, 250, 0.08));
  }
  .rhi-btn.on {
    background: rgba(91, 108, 247, 0.28);
  }
  .rhi-noise-host {
    position: relative;
  }
  .rhi-pop {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    z-index: 30;
    width: 372px;
    max-height: min(70vh, 640px);
    overflow: auto;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    font-size: 11px;
    color: var(--text-secondary, #9da2c2);
    background: var(--overlay-bg, rgba(13, 16, 32, 0.96));
    border: 1px solid var(--border-light, rgba(140, 160, 250, 0.14));
    border-radius: var(--radius-md, 8px);
    box-shadow: var(--shadow-lg, 0 8px 32px rgba(0, 0, 0, 0.6));
    text-transform: none;
    letter-spacing: 0;
    font-weight: 400;
  }
  .rhi-group {
    margin: 0;
    padding: 6px 8px 8px;
    border: 1px solid var(--border-color, rgba(140, 160, 250, 0.08));
    border-radius: var(--radius-sm, 6px);
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .rhi-group:disabled {
    opacity: 0.45;
  }
  .rhi-group legend {
    padding: 0 4px;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted, #5c6088);
  }
  .rhi-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .rhi-row.end {
    justify-content: flex-end;
  }
  .rhi-row .check,
  .rhi-row.check {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
    user-select: none;
  }
  .rhi-row.strong {
    color: var(--text-primary, #e4e6f4);
    font-weight: 600;
  }
  .rhi-row em {
    font-weight: 400;
    font-style: normal;
    color: var(--text-muted, #5c6088);
  }
  .rhi-row input[type='checkbox'] {
    margin: 0;
    width: 14px;
    height: 14px;
  }
  .rhi-num {
    width: 60px;
    height: 24px;
    padding: 2px 6px;
    font-size: 11px;
    font-family: var(--font-mono, ui-monospace, monospace);
  }
  .rhi-row .rhi-hint {
    margin-left: 0;
    font-size: 10.5px;
    color: var(--text-muted, #5c6088);
  }
  .rhi-caption {
    flex-shrink: 0;
    padding: 3px var(--spacing-md, 12px) 0;
    text-align: center;
    font-size: 11px;
    line-height: 1.35;
    color: var(--text-secondary, #9da2c2);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .rhi-caption.raw {
    color: var(--accent-warning, #fbbf24);
  }
  .rhi-colorbar {
    flex-shrink: 0;
    display: flex;
    justify-content: center;
    padding: 6px var(--spacing-md, 12px) 8px;
    position: relative;
    z-index: 5;
  }
</style>
