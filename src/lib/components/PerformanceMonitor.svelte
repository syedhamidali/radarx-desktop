<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { renderCache } from '../utils/renderCache';
  import { perfStats } from '../stores/perfStats';

  // ── Visibility ──────────────────────────────────────────────────────────
  let visible = false;

  function toggle() {
    visible = !visible;
    // The sampling loops only run while the overlay is on screen.
    if (visible) startLoops(); else stopLoops();
  }

  function onKeyDown(e: KeyboardEvent) {
    if (e.ctrlKey && e.shiftKey && e.key === 'P') {
      e.preventDefault();
      toggle();
    }
  }

  // ── Stats state ─────────────────────────────────────────────────────────
  let fps = 0;
  let renderTimeMs = 0;
  let meshBuildTimeMs = 0;
  let dataTransferTimeMs = 0;
  let memoryMB = 0;
  let cacheHitRatio = 0;
  let cacheSizeMB = 0;
  let cacheEntries = 0;
  // 3D view (empty until a 3D view has rendered)
  let quality3d = '';
  let frame3dMs = 0;
  let renderScale3d = 1;
  let volumeMB = 0;
  let gpuRenderer = '';

  /** Frame time history — last 60 entries for the mini bar chart */
  let frameHistory: number[] = new Array(60).fill(0);

  // ── Update loop ─────────────────────────────────────────────────────────
  let frameCount = 0;
  let lastTimestamp = 0;
  let rafId = 0;
  let updateInterval: ReturnType<typeof setInterval> | null = null;

  function frameCallback(ts: number) {
    frameCount++;

    // Record frame time
    if (lastTimestamp > 0) {
      const dt = ts - lastTimestamp;
      frameHistory = [...frameHistory.slice(1), dt];
    }
    lastTimestamp = ts;

    rafId = requestAnimationFrame(frameCallback);
  }

  function updateStats() {
    const stats = $perfStats;

    // FPS from frame counter (updated every second)
    fps = frameCount;
    frameCount = 0;

    renderTimeMs = stats.renderTimeMs;
    meshBuildTimeMs = stats.meshBuildTimeMs;
    dataTransferTimeMs = stats.dataTransferTimeMs;
    quality3d = stats.quality;
    frame3dMs = stats.frameMsEma;
    renderScale3d = stats.renderScale;
    volumeMB = stats.volumeTexMB;
    gpuRenderer = stats.gpuRenderer;

    // Memory usage (Performance API if available)
    const perf = performance as any;
    if (perf.memory) {
      memoryMB = perf.memory.usedJSHeapSize / (1024 * 1024);
    }

    // Cache stats
    cacheHitRatio = renderCache.hitRatio;
    cacheSizeMB = renderCache.sizeMB;
    cacheEntries = renderCache.entryCount;
  }

  function barColor(ms: number): string {
    if (ms < 16) return 'var(--perf-green, #4ade80)';
    if (ms < 33) return 'var(--perf-yellow, #facc15)';
    return 'var(--perf-red, #f87171)';
  }

  function fpsColor(f: number): string {
    if (f >= 55) return 'var(--perf-green, #4ade80)';
    if (f >= 30) return 'var(--perf-yellow, #facc15)';
    return 'var(--perf-red, #f87171)';
  }

  function startLoops() {
    if (rafId || updateInterval) return;
    frameCount = 0;
    lastTimestamp = 0;
    rafId = requestAnimationFrame(frameCallback);
    updateInterval = setInterval(updateStats, 1000);
    updateStats();
  }

  function stopLoops() {
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    if (updateInterval) { clearInterval(updateInterval); updateInterval = null; }
  }

  onMount(() => {
    window.addEventListener('keydown', onKeyDown);
  });

  onDestroy(() => {
    window.removeEventListener('keydown', onKeyDown);
    stopLoops();
  });
</script>

{#if visible}
  <div class="perf-monitor">
    <div class="perf-header">
      <span class="perf-title">Performance</span>
      <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
      <span class="perf-close" on:click={toggle}>x</span>
    </div>

    <div class="perf-row">
      <span class="perf-label">FPS</span>
      <span class="perf-value" style="color: {fpsColor(fps)}">{fps}</span>
    </div>
    <div class="perf-row">
      <span class="perf-label">Render</span>
      <span class="perf-value">{renderTimeMs.toFixed(1)} ms</span>
    </div>
    <div class="perf-row">
      <span class="perf-label">Mesh</span>
      <span class="perf-value">{meshBuildTimeMs.toFixed(1)} ms</span>
    </div>
    <div class="perf-row">
      <span class="perf-label">Transfer</span>
      <span class="perf-value">{dataTransferTimeMs.toFixed(1)} ms</span>
    </div>
    <div class="perf-row">
      <span class="perf-label">Memory</span>
      <span class="perf-value">{memoryMB.toFixed(0)} MB</span>
    </div>

    {#if quality3d}
      <div class="perf-divider"></div>
      <div class="perf-row">
        <span class="perf-label">3D quality</span>
        <span class="perf-value">{quality3d}</span>
      </div>
      <div class="perf-row">
        <span class="perf-label">3D frame</span>
        <span class="perf-value">{frame3dMs.toFixed(0)} ms</span>
      </div>
      <div class="perf-row">
        <span class="perf-label">3D scale</span>
        <span class="perf-value">{(renderScale3d * 100).toFixed(0)}%</span>
      </div>
      <div class="perf-row">
        <span class="perf-label">3D volume</span>
        <span class="perf-value">{volumeMB.toFixed(1)} MB</span>
      </div>
      {#if gpuRenderer}
        <div class="perf-row" title={gpuRenderer}>
          <span class="perf-label">GPU</span>
          <span class="perf-value perf-gpu">{gpuRenderer.replace(/^ANGLE \((.*)\)$/, '$1')}</span>
        </div>
      {/if}
    {/if}

    <div class="perf-divider"></div>

    <div class="perf-row">
      <span class="perf-label">Cache</span>
      <span class="perf-value">{(cacheHitRatio * 100).toFixed(0)}% ({cacheEntries})</span>
    </div>
    <div class="perf-row">
      <span class="perf-label">Cache MB</span>
      <span class="perf-value">{cacheSizeMB.toFixed(1)} / {renderCache.maxSizeMB}</span>
    </div>

    <div class="perf-divider"></div>

    <div class="perf-chart">
      {#each frameHistory as ms, i}
        <div
          class="perf-bar"
          style="height: {Math.min(ms / 50 * 100, 100)}%; background: {barColor(ms)}"
          title="{ms.toFixed(1)}ms"
        ></div>
      {/each}
    </div>

    <div class="perf-chart-legend">
      <span style="color: var(--perf-green, #4ade80)">&lt;16ms</span>
      <span style="color: var(--perf-yellow, #facc15)">&lt;33ms</span>
      <span style="color: var(--perf-red, #f87171)">&gt;33ms</span>
    </div>
  </div>
{/if}

<style>
  .perf-monitor {
    position: fixed;
    top: 8px;
    right: 8px;
    width: 200px;
    background: rgba(10, 10, 20, 0.92);
    border: 1px solid rgba(var(--c-line-rgb), 0.1);
    border-radius: 6px;
    padding: 8px 10px;
    font-family: var(--font-mono, monospace);
    font-size: 10px;
    color: rgba(255, 255, 255, 0.8);
    z-index: 9999;
    backdrop-filter: blur(8px);
    pointer-events: auto;
    user-select: none;
  }

  .perf-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
  }

  .perf-title {
    font-weight: 700;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: rgba(255, 255, 255, 0.6);
  }

  .perf-close {
    cursor: pointer;
    color: rgba(255, 255, 255, 0.4);
    font-size: 12px;
    line-height: 1;
  }
  .perf-close:hover { color: rgba(255, 255, 255, 0.8); }

  .perf-row {
    display: flex;
    justify-content: space-between;
    padding: 1px 0;
  }

  .perf-label {
    color: rgba(255, 255, 255, 0.45);
  }

  .perf-value {
    color: rgba(255, 255, 255, 0.85);
    text-align: right;
  }

  .perf-gpu {
    max-width: 110px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .perf-divider {
    height: 1px;
    background: rgba(var(--c-line-rgb), 0.08);
    margin: 4px 0;
  }

  .perf-chart {
    display: flex;
    align-items: flex-end;
    gap: 1px;
    height: 30px;
    margin-top: 4px;
  }

  .perf-bar {
    flex: 1;
    min-width: 0;
    border-radius: 1px 1px 0 0;
    transition: height 0.1s ease;
  }

  .perf-chart-legend {
    display: flex;
    justify-content: space-between;
    font-size: 8px;
    margin-top: 2px;
    opacity: 0.6;
  }
</style>
