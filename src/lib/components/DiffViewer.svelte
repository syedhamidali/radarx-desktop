<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { wsManager } from '../utils/websocket';
  import { openFiles, type FileEntry } from '../stores/fileManager';
  import { selectedVariable, selectedSweep, processingProgress } from '../stores/radarData';
  import type { SweepDataEntry } from '../utils/ppiRenderer';
  import {
    diffMode,
    diffBaseFileId,
    diffCompareFileId,
    type DiffMode,
  } from '../stores/temporal';

  // ── State ──────────────────────────────────────────────────────────────
  let canvasEl: HTMLCanvasElement;
  let containerEl: HTMLDivElement;
  let ctx: CanvasRenderingContext2D | null = null;
  let width = 600;
  let height = 600;
  let mode: DiffMode = 'off';
  let baseId: string | null = null;
  let compareId: string | null = null;
  let files: FileEntry[] = [];
  let variable: string | null = null;
  let sweep = 0;
  let diffRange = 50; // configurable max diff magnitude for colormap
  let isLoading = false;
  let errorMsg = '';

  // Difference grid (compare - base) received from the server: a polar sweep entry whose values are
  // float32 with -9999 marking cells that are missing in either file.
  const DIFF_NODATA = -9999;
  let entry: SweepDataEntry | null = null;
  let probe: { x: number; y: number; az: number; rangeKm: number; value: number | null } | null = null;
  // Fast polar lookup: azimuth (0.1 degree bins) -> nearest ray index
  let rayLut: Int32Array | null = null;

  $: mode = $diffMode;
  $: baseId = $diffBaseFileId;
  $: compareId = $diffCompareFileId;
  $: files = $openFiles;
  $: variable = $selectedVariable;
  $: sweep = $selectedSweep;

  // `sweep` is listed so that changing sweep (or either file) recomputes the difference
  $: if (mode === 'diff' && baseId && compareId && variable && sweep >= 0) {
    requestDiff();
  }

  // Repaint (without asking the server again) when the data, the colour range or the size changes
  $: if (ctx && entry) paint(entry, diffRange, width, height);

  let unsubDiff: (() => void) | null = null;
  let unsubErr: (() => void) | null = null;
  let ro: ResizeObserver | null = null;

  // The canvas and container only exist while the diff view is on (they are inside {#if}), so they
  // are picked up reactively rather than once in onMount.
  $: if (canvasEl && !ctx) ctx = canvasEl.getContext('2d');
  $: if (!canvasEl) ctx = null;
  $: if (containerEl && !ro) {
    ro = new ResizeObserver((entries) => {
      for (const e of entries) {
        // Square drawing area that fits the space left under the controls
        const sz = Math.floor(Math.min(e.contentRect.width, e.contentRect.height));
        if (sz > 0) {
          width = sz;
          height = sz;
        }
      }
    });
    ro.observe(containerEl);
  }
  $: if (!containerEl && ro) {
    ro.disconnect();
    ro = null;
  }

  onMount(() => {
    unsubDiff = wsManager.onMessage('difference_data_ready', (e: SweepDataEntry) => {
      if (e.variable !== variable || e.sweep !== sweep) return; // a stale answer for an earlier selection
      setEntry(e);
      isLoading = false;
      errorMsg = '';
      processingProgress.set(null); // the server's "Computing difference... 10%" is never followed by a 100%
    });
    unsubErr = wsManager.onMessage('error', (msg: any) => {
      if (isLoading) {
        isLoading = false;
        processingProgress.set(null);
        errorMsg = String(msg?.message ?? msg?.error ?? 'Difference failed');
      }
    });
  });

  onDestroy(() => {
    ro?.disconnect();
    unsubDiff?.();
    unsubErr?.();
  });

  function setEntry(e: SweepDataEntry) {
    // Detach from the shared sweep buffer so a cache eviction cannot change what is on screen
    const az = new Float32Array(e.azimuth);
    const lut = new Int32Array(3600).fill(-1);
    for (let i = 0; i < az.length; i++) {
      const a = ((az[i] % 360) + 360) % 360;
      lut[Math.round(a * 10) % 3600] = i;
    }
    // Fill gaps with the nearest ray so every bearing maps somewhere (sector scans stay blank below)
    let last = -1;
    let firstFilled = lut.findIndex((v) => v >= 0);
    if (firstFilled >= 0) {
      for (let pass = 0; pass < 2; pass++) {
        for (let i = 0; i < 3600; i++) {
          if (lut[i] >= 0) last = lut[i];
          else if (last >= 0 && pass === 1) lut[i] = last;
        }
      }
    }
    rayLut = lut;
    entry = { ...e, azimuth: az, values: new Float32Array(e.values) };
  }

  function requestDiff() {
    if (!baseId || !compareId || !variable) return;
    if (baseId === compareId) {
      entry = null;
      errorMsg = 'Pick two different files to compare.';
      return;
    }
    errorMsg = '';
    isLoading = true;
    wsManager.send({
      type: 'get_difference',
      variable,
      sweep,
      file_id_1: baseId,
      file_id_2: compareId,
    });
  }

  /** Nearest gate/ray for a canvas pixel, or null outside the sweep. */
  function sample(e: SweepDataEntry, px: number, py: number): { az: number; rangeM: number; v: number } | null {
    const half = width / 2;
    const maxR = e.maxRange || 1;
    const dx = px - half;
    const dy = half - py;
    const rM = (Math.hypot(dx, dy) / half) * maxR;
    if (rM > maxR || !rayLut || e.nRange < 1) return null;
    const az = ((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360;
    const ray = rayLut[Math.round(az * 10) % 3600];
    if (ray < 0) return null;
    const r0 = e.range[0];
    const dr = e.nRange > 1 ? (e.range[e.nRange - 1] - r0) / (e.nRange - 1) : 1;
    const gate = Math.round((rM - r0) / (dr || 1));
    if (gate < 0 || gate >= e.nRange) return null;
    return { az, rangeM: rM, v: e.values[ray * e.nRange + gate] };
  }

  function paint(e: SweepDataEntry, range: number, w: number, h: number) {
    if (!ctx || !canvasEl) return;
    if (canvasEl.width !== w || canvasEl.height !== h) {
      canvasEl.width = w;
      canvasEl.height = h;
    }
    const img = ctx.createImageData(w, h);
    const d = img.data;
    const span = range > 0 ? range : 1;
    for (let py = 0; py < h; py++) {
      for (let px = 0; px < w; px++) {
        const s = sample(e, px + 0.5, py + 0.5);
        if (!s || !Number.isFinite(s.v) || s.v === DIFF_NODATA) continue;
        const t = Math.max(0, Math.min(1, (s.v / span + 1) / 2));
        const [r, g, b] = divergingColor(t);
        const o = (py * w + px) * 4;
        d[o] = r;
        d[o + 1] = g;
        d[o + 2] = b;
        d[o + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
    // Outer range circle + north tick so the sweep's extent and orientation are readable
    ctx.strokeStyle = 'rgba(160, 170, 220, 0.35)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(w / 2, h / 2, w / 2 - 1, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = 'rgba(200, 205, 240, 0.8)';
    ctx.font = '11px system-ui';
    ctx.textAlign = 'center';
    ctx.fillText('N', w / 2, 13);
  }

  function onMove(ev: MouseEvent) {
    if (!entry) return;
    const r = canvasEl.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * width;
    const py = ((ev.clientY - r.top) / r.height) * height;
    const s = sample(entry, px, py);
    probe = s
      ? {
          x: ev.clientX - r.left,
          y: ev.clientY - r.top,
          az: s.az,
          rangeKm: s.rangeM / 1000,
          value: Number.isFinite(s.v) && s.v !== DIFF_NODATA ? s.v : null,
        }
      : null;
  }

  /**
   * Diverging blue-white-red colormap.
   * t in [0, 1]: 0 = blue (decrease), 0.5 = white, 1 = red (increase).
   */
  function divergingColor(t: number): [number, number, number] {
    if (t < 0.5) {
      // Blue to white
      const s = t * 2; // 0..1
      const r = Math.round(30 + 225 * s);
      const g = Math.round(80 + 175 * s);
      const b = Math.round(220 + 35 * s);
      return [r, g, b];
    } else {
      // White to red
      const s = (t - 0.5) * 2; // 0..1
      const r = 255;
      const g = Math.round(255 - 195 * s);
      const b = Math.round(255 - 210 * s);
      return [r, g, b];
    }
  }

  function handleBaseChange(e: Event) {
    const val = (e.target as HTMLSelectElement).value;
    diffBaseFileId.set(val || null);
  }

  function handleCompareChange(e: Event) {
    const val = (e.target as HTMLSelectElement).value;
    diffCompareFileId.set(val || null);
  }

  function handleRangeChange(e: Event) {
    diffRange = parseFloat((e.target as HTMLInputElement).value) || 50;
  }
</script>

{#if mode === 'diff'}
  <div class="diff-viewer">
    <div class="diff-controls">
      <div class="diff-select-group">
        <label class="diff-label" for="diff-base">Base</label>
        <select id="diff-base" class="diff-select" on:change={handleBaseChange} value={baseId ?? ''}>
          <option value="">-- select --</option>
          {#each files as f}
            <option value={f.id}>{f.filename}</option>
          {/each}
        </select>
      </div>

      <span class="diff-arrow">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M5 12h14m-7-7 7 7-7 7" />
        </svg>
      </span>

      <div class="diff-select-group">
        <label class="diff-label" for="diff-compare">Compare</label>
        <select id="diff-compare" class="diff-select" on:change={handleCompareChange} value={compareId ?? ''}>
          <option value="">-- select --</option>
          {#each files as f}
            <option value={f.id}>{f.filename}</option>
          {/each}
        </select>
      </div>

      <div class="diff-range-group">
        <label class="diff-label" for="diff-range">Range</label>
        <input
          id="diff-range"
          class="diff-range-input"
          type="number"
          value={diffRange}
          on:change={handleRangeChange}
          min="1"
          max="200"
          step="5"
        />
      </div>
    </div>

    <div class="diff-canvas-container" bind:this={containerEl}>
      {#if isLoading}
        <div class="diff-loading">Computing difference...</div>
      {:else if errorMsg}
        <div class="diff-loading" role="alert">{errorMsg}</div>
      {:else if !entry && (!baseId || !compareId)}
        <div class="diff-loading">Choose a base and a comparison file.</div>
      {/if}
      <canvas
        bind:this={canvasEl}
        width={width}
        height={height}
        class="diff-canvas"
        on:mousemove={onMove}
        on:mouseleave={() => (probe = null)}
      ></canvas>
      {#if probe}
        <div class="diff-probe" style="left: {Math.min(probe.x + 12, width - 150)}px; top: {Math.max(0, probe.y - 30)}px">
          <strong>{probe.value === null ? 'no data' : `${probe.value > 0 ? '+' : ''}${probe.value.toFixed(2)}`}</strong>
          <span>{probe.az.toFixed(1)}&deg; / {probe.rangeKm.toFixed(1)} km</span>
        </div>
      {/if}
    </div>

    <!-- Colorbar legend -->
    <div class="diff-legend">
      <div class="diff-legend-bar">
        <div class="diff-legend-gradient"></div>
      </div>
      <div class="diff-legend-labels">
        <span>-{diffRange} (decrease)</span>
        <span>0</span>
        <span>+{diffRange} (increase)</span>
      </div>
    </div>
  </div>
{/if}

<style>
  .diff-viewer {
    display: flex;
    flex-direction: column;
    width: 100%;
    height: 100%;
    background: var(--bg-primary);
    gap: 8px;
  }

  .diff-controls {
    display: flex;
    align-items: flex-end;
    gap: 8px;
    padding: 8px 12px;
    background: var(--glass-bg);
    border-bottom: 1px solid var(--glass-border);
    flex-wrap: wrap;
  }

  .diff-select-group {
    display: flex;
    flex-direction: column;
    gap: 2px;
    flex: 1;
    min-width: 100px;
  }

  .diff-label {
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
    font-weight: 600;
  }

  .diff-select {
    font-size: 11px;
    padding: 4px 8px;
    background: rgba(var(--c-surface-0-rgb), 0.5);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    cursor: pointer;
    outline: none;
    transition: border-color var(--transition-fast);
  }

  .diff-select:hover,
  .diff-select:focus {
    border-color: var(--accent-primary);
  }

  .diff-arrow {
    color: var(--text-muted);
    padding-bottom: 4px;
  }

  .diff-range-group {
    display: flex;
    flex-direction: column;
    gap: 2px;
    width: 60px;
  }

  .diff-range-input {
    font-size: 11px;
    padding: 4px 6px;
    background: rgba(var(--c-surface-0-rgb), 0.5);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    width: 100%;
    text-align: center;
    outline: none;
  }

  .diff-range-input:focus {
    border-color: var(--accent-primary);
  }

  .diff-canvas-container {
    flex: 1;
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 200px;
  }

  .diff-canvas {
    max-width: 100%;
    max-height: 100%;
    border-radius: var(--radius-sm);
  }

  .diff-probe {
    position: absolute;
    display: flex;
    flex-direction: column;
    padding: 3px 8px;
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    color: var(--text-primary);
    background: var(--glass-bg-heavy);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    pointer-events: none;
    white-space: nowrap;
    z-index: 3;
  }

  .diff-probe span {
    color: var(--text-muted);
    font-size: 10px;
  }

  .diff-loading {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
    font-size: 12px;
    background: rgba(var(--c-surface-0-rgb), 0.6);
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    z-index: 2;
    border-radius: var(--radius-sm);
  }

  .diff-legend {
    padding: 4px 12px 8px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
  }

  .diff-legend-bar {
    width: 80%;
    height: 10px;
    border-radius: 5px;
    overflow: hidden;
    border: 1px solid var(--glass-border);
  }

  .diff-legend-gradient {
    width: 100%;
    height: 100%;
    background: linear-gradient(
      to right,
      rgb(30, 80, 220),
      rgb(140, 170, 245),
      rgb(255, 255, 255),
      rgb(255, 140, 100),
      rgb(255, 60, 45)
    );
  }

  .diff-legend-labels {
    display: flex;
    justify-content: space-between;
    width: 80%;
    font-size: 9px;
    color: var(--text-muted);
    font-variant-numeric: tabular-nums;
  }
</style>
