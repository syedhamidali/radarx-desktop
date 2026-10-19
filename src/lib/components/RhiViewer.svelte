<script lang="ts">
  /**
   * Range-height display of an RHI sweep: x = ground range (km), y = height (km).
   *
   * Used by RadarViewer instead of the map PPI when the selected sweep is an RHI. Variable / sweep
   * selection lives in RadarViewer's control bar; colormap and colour range are owned by RadarViewer
   * too and arrive as props, so both views stay in step.
   *
   * Data flow (see utils/rhiGeometry.ts): entry -> `field` (values may be transformed here, e.g. a
   * future smoothing / value mask) -> lookup tables -> `rasterizeRhi` (inverse mapped, one pixel at a
   * time) -> canvas. Hover uses the same lookup, so the read-out matches the picture.
   *
   * Colours are CSS variables with fallbacks (--rhi-*), so a theme can restyle the plot.
   */
  import { onMount } from 'svelte';
  import ColorBar from './ColorBar.svelte';
  import type { SweepDataEntry } from '../utils/ppiRenderer';
  import { COLORMAP_DATA } from '../utils/colormaps';
  import {
    buildLookup,
    dataExtent,
    formatTick,
    gridLines,
    niceTicks,
    rasterizeRhi,
    sampleAt,
    type RhiField,
    type RhiSample,
  } from '../utils/rhiGeometry';

  interface Props {
    /** The sweep to draw; an RHI entry carries `kind === 'rhi'` and per-ray `elevation`. */
    entry: SweepDataEntry | null;
    /** Radar site altitude in metres ASL, when the file states it (enables the ASL height axis). */
    altitudeM?: number | null;
    /** Label of the shown variable (legend title / read-out). */
    variableLabel?: string;
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
  const MARGIN = { left: 58, right: 16, top: 14, bottom: 42 };
  const EXAGGERATIONS = [1, 1.5, 2, 3];
  /** Fraction of the data extent kept as padding when fitting the view. */
  const FIT_PAD = 0.06;

  // ── State ────────────────────────────────────────────────────────────────
  let container: HTMLDivElement | undefined = $state();
  let baseCanvas: HTMLCanvasElement | undefined = $state();
  let overlayHost: HTMLDivElement | undefined = $state();
  let overlayCanvas: HTMLCanvasElement | undefined = $state();
  let width = $state(800);
  let height = $state(500);

  let exag = $state(1);
  let showGrid = $state(true);
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

  // ── Data -> field -> lookup ──────────────────────────────────────────────
  /**
   * Hook for value-level processing between the wire data and the picture (smoothing, value mask,
   * ...). Must return values on the same ray x gate grid.
   */
  function prepareValues(e: SweepDataEntry): ArrayLike<number> {
    return e.values;
  }

  const field: RhiField | null = $derived.by(() => {
    const e = entry;
    if (!e || e.kind !== 'rhi' || !e.elevation || e.elevation.length !== e.nAz) return null;
    return {
      elevation: e.elevation,
      range: e.range,
      values: prepareValues(e),
      nRays: e.nAz,
      nRange: e.nRange,
    };
  });
  const lookup = $derived(field ? buildLookup(field) : null);
  const extent = $derived(lookup ? dataExtent(lookup, vmin) : null);
  const plotW = $derived(Math.max(10, width - MARGIN.left - MARGIN.right));
  const plotH = $derived(Math.max(10, height - MARGIN.top - MARGIN.bottom));
  const elRange = $derived(
    lookup && lookup.rays.n > 0
      ? { lo: lookup.rays.lo[0], hi: lookup.rays.hi[lookup.rays.n - 1] }
      : { lo: 0, hi: 0 },
  );

  const planeAz = $derived(entry?.rhiAzimuth ?? null);
  const lut = $derived(COLORMAP_DATA[cmap] ?? COLORMAP_DATA['turbo']);

  // ── View fitting ─────────────────────────────────────────────────────────
  /** Show the whole data extent with equal-aspect pixels (times the vertical exaggeration). */
  function fit() {
    const ex = extent;
    if (!ex) return;
    userAdjusted = false;
    const xSpan = Math.max(ex.xMaxKm - ex.xMinKm, 1);
    const ySpan = Math.max(ex.yMaxKm - ex.yMinKm, 1);
    const k = Math.min(plotW / (xSpan * (1 + 2 * FIT_PAD)), plotH / (ySpan * exag * (1 + 2 * FIT_PAD)));
    pxPerKm = k;
    cx = (ex.xMinKm + ex.xMaxKm) / 2;
    // Spare vertical room goes above the data: the ground line stays near the bottom edge.
    cy = ex.yMinKm - FIT_PAD * ySpan + plotH / (2 * k * exag);
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
    const key = `${e.sweep}:${e.nAz}:${e.nRange}:${e.rhiAzimuth ?? ''}:${lk.rays.centers[0]}:${e.maxRange}`;
    if (key !== fitKey || !userAdjusted) {
      fitKey = key;
      fit();
    }
  });

  function changeExaggeration(v: number) {
    exag = v;
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
    ctx.translate(MARGIN.left, MARGIN.top);
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
    ctx.fillText('Ground range (km)', plotW / 2, plotH + MARGIN.bottom - 4);
    ctx.save();
    ctx.translate(-MARGIN.left + 14, plotH / 2);
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
    ctx.translate(MARGIN.left, MARGIN.top);
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
    void [field, lookup, lut, vmin, vmax, showGrid, offsetKm, exag, cx, cy, pxPerKm, width, height, plotW, plotH];
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
    return { x: e.clientX - r.left - MARGIN.left, y: e.clientY - r.top - MARGIN.top };
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
    const base = fitScale();
    const next = Math.min(Math.max(pxPerKm * factor, base * 0.2), base * 80);
    userAdjusted = true;
    // keep the point under the cursor fixed
    const gx = xKm(px);
    const gy = yKm(py);
    pxPerKm = next;
    cx = gx - (px - plotW / 2) / next;
    cy = gy + (py - plotH / 2) / (next * exag);
    interacting = true;
    sharpenSoon();
  }

  /** The px/km of the fitted view: the zoom limits are relative to it. */
  function fitScale(): number {
    const ex = extent;
    if (!ex) return pxPerKm;
    const xSpan = Math.max(ex.xMaxKm - ex.xMinKm, 1);
    const ySpan = Math.max(ex.yMaxKm - ex.yMinKm, 1);
    return Math.min(plotW / xSpan, plotH / (ySpan * exag));
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

  // ── Read-out ─────────────────────────────────────────────────────────────
  const readout = $derived.by(() => {
    const h = hover;
    if (!h) return null;
    const s = h.sample;
    const heightShown = h.heightKm + offsetKm;
    return {
      value: s && Number.isFinite(s.value) ? s.value : null,
      inScan: s !== null,
      range: s ? s.rangeM / 1000 : null,
      el: s ? s.elDeg : null,
      ground: h.groundKm,
      height: heightShown,
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
    parts.push(`${lookup!.rays.centers[0].toFixed(1)}–${lookup!.rays.centers[lookup!.rays.n - 1].toFixed(1)}° elevation`);
    parts.push(`${field.nRays} rays × ${field.nRange} gates`);
    return parts.join(' · ');
  });
</script>

<div class="rhi-viewer" data-testid="rhi-viewer">
  <div class="rhi-toolbar">
    {#if summary}<span class="rhi-summary" title="Range-height scan">{summary}</span>{/if}
    <label class="rhi-ctl" title="Vertical exaggeration (the picture keeps equal x/y scale at 1×)">
      <span>Vertical</span>
      <select value={exag} onchange={(e) => changeExaggeration(parseFloat(e.currentTarget.value))} data-testid="rhi-exag">
        {#each EXAGGERATIONS as v}
          <option value={v}>{v}×</option>
        {/each}
      </select>
    </label>
    <label class="rhi-ctl" title={hasAltitude ? 'Height reference' : 'The file does not state the radar altitude'}>
      <span>Height</span>
      <select bind:value={heightRef} disabled={!hasAltitude} data-testid="rhi-height-ref">
        <option value="agl">above radar</option>
        <option value="asl">above sea level</option>
      </select>
    </label>
    <label class="rhi-ctl check" title="Lines of constant range and constant elevation">
      <input type="checkbox" bind:checked={showGrid} />
      <span>Grid</span>
    </label>
    <button class="rhi-btn" onclick={fit} disabled={!field} title="Show the whole scan (key 0, or double-click). Scroll to zoom, drag to pan.">Fit</button>
  </div>

  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
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
    gap: var(--spacing-md, 12px);
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

  .rhi-colorbar {
    flex-shrink: 0;
    display: flex;
    justify-content: center;
    padding: 6px var(--spacing-md, 12px) 8px;
    position: relative;
    z-index: 5;
  }
</style>
