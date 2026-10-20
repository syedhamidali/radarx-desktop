<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { openFiles, setActiveFile, type FileEntry } from '../stores/fileManager';
  import { selectedVariable, selectedSweep } from '../stores/radarData';
  import { wsManager } from '../utils/websocket';
  import {
    sortedFiles,
    timelinePlaybackActive,
    timelineIndex,
    timelineIntervalMs,
  } from '../stores/temporal';

  // ── State ──────────────────────────────────────────────────────────────
  let files: FileEntry[] = [];
  let currentIdx = 0;
  let isPlaying = false;
  let intervalMs = 1000;
  let playTimer: ReturnType<typeof setInterval> | null = null;
  let isDragging = false;
  let trackEl: HTMLDivElement;
  let hoverIdx: number | null = null;

  $: files = $sortedFiles;
  $: currentIdx = $timelineIndex;
  $: isPlaying = $timelinePlaybackActive;
  $: intervalMs = $timelineIntervalMs;

  /** Frames to warm ahead of the playhead during playback (kept small: every one is a server round trip). */
  const LOOKAHEAD = 3;
  const GROUP = 'timeline';
  /** Frames already warmed recently (`fileId:variable:sweep` -> time), so playback does not re-request them every tick. */
  const warmed = new Map<string, number>();
  const WARM_TTL_MS = 30000;

  /** A frame is playable unless its variable is known not to exist in its sweep. */
  function isPlayable(file: FileEntry): boolean {
    const v = file.selectedVariable;
    return v !== null && !wsManager.isUnavailable(v, file.selectedSweep, file.id);
  }

  /** Next playable frame after `from` (wrapping), or -1 if none other than `from` is playable. */
  function nextPlayable(from: number): number {
    for (let step = 1; step < files.length; step++) {
      const idx = (from + step) % files.length;
      if (isPlayable(files[idx])) return idx;
    }
    return -1;
  }

  // ── Playback ───────────────────────────────────────────────────────────
  function togglePlay() {
    if (isPlaying) {
      stopPlayback();
    } else {
      startPlayback();
    }
  }

  function startPlayback() {
    if (files.length < 2) return;
    if (playTimer !== null) clearInterval(playTimer); // never stack timers
    timelinePlaybackActive.set(true);
    playTimer = setInterval(() => {
      // Files may have been closed since playback started.
      if (files.length < 2) {
        stopPlayback();
        return;
      }
      // Side effects (activateFile) must not run inside a store update callback.
      const next = nextPlayable(currentIdx);
      if (next < 0) return; // every other frame is unavailable: stay on this one
      timelineIndex.set(next);
      activateFile(next);
    }, intervalMs);
  }

  function stopPlayback() {
    timelinePlaybackActive.set(false);
    if (playTimer !== null) {
      clearInterval(playTimer);
      playTimer = null;
    }
  }

  function activateFile(idx: number) {
    if (idx < 0 || idx >= files.length) return;
    const file = files[idx];
    setActiveFile(file.id);

    // Frames queued for an earlier position are stale now.
    wsManager.cancelGroup(GROUP);

    // The frame on screen is needed now ...
    const v = file.selectedVariable;
    const s = file.selectedSweep;
    if (v !== null) {
      wsManager.requestSweepData(v, s, file.id, { priority: 'visible', group: GROUP });
    }

    // ... and while playing, the next few are warmed in the background.
    if (!isPlaying) return;
    const now = performance.now();
    let queued = 0;
    for (let step = 1; step < files.length && queued < LOOKAHEAD; step++) {
      const ahead = files[(idx + step) % files.length];
      if (!isPlayable(ahead)) continue;
      const key = `${ahead.id}:${ahead.selectedVariable}:${ahead.selectedSweep}`;
      queued++; // warmed frames still count towards the lookahead window
      const t = warmed.get(key);
      if (t !== undefined && now - t < WARM_TTL_MS) continue;
      warmed.set(key, now);
      wsManager.requestSweepData(ahead.selectedVariable!, ahead.selectedSweep, ahead.id, {
        priority: 'prefetch',
        group: GROUP,
        onSettled: (result) => { if (result !== 'done') warmed.delete(key); },
      });
    }
    if (warmed.size > 256) {
      for (const [k, t] of warmed) if (now - t >= WARM_TTL_MS) warmed.delete(k);
    }
  }

  function setSpeed(ms: number) {
    timelineIntervalMs.set(ms);
    if (isPlaying) {
      stopPlayback();
      startPlayback();
    }
  }

  // ── Drag / click on track ──────────────────────────────────────────────
  function handleTrackMouseDown(e: MouseEvent) {
    isDragging = true;
    updateFromMouse(e);
  }

  function handleTrackMouseMove(e: MouseEvent) {
    if (isDragging) {
      updateFromMouse(e);
    }
    // Hover
    const idx = mouseToIndex(e);
    hoverIdx = idx >= 0 && idx < files.length ? idx : null;
  }

  function handleTrackMouseUp() {
    isDragging = false;
  }

  function handleTrackMouseLeave() {
    isDragging = false;
    hoverIdx = null;
  }

  function updateFromMouse(e: MouseEvent) {
    const idx = mouseToIndex(e);
    if (idx >= 0 && idx < files.length && idx !== currentIdx) {
      timelineIndex.set(idx);
      activateFile(idx);
    }
  }

  function mouseToIndex(e: MouseEvent): number {
    if (!trackEl) return 0;
    const rect = trackEl.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const pct = Math.max(0, Math.min(1, x / rect.width));
    return Math.round(pct * (files.length - 1));
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (e.key === 'ArrowLeft' && currentIdx > 0) {
      const next = currentIdx - 1;
      timelineIndex.set(next);
      activateFile(next);
    } else if (e.key === 'ArrowRight' && currentIdx < files.length - 1) {
      const next = currentIdx + 1;
      timelineIndex.set(next);
      activateFile(next);
    }
  }

  // Closing files down to <2 hides the timeline: make sure playback does not keep cycling.
  $: if (files.length < 2 && playTimer !== null) stopPlayback();

  onDestroy(() => {
    stopPlayback();
    wsManager.cancelGroup(GROUP);
  });

  // ── Computed ───────────────────────────────────────────────────────────
  $: progressPct = files.length > 1 ? (currentIdx / (files.length - 1)) * 100 : 0;
  $: currentFile = files[currentIdx] ?? null;
  $: hoverFile = hoverIdx !== null ? files[hoverIdx] : null;
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
{#if files.length >= 2}
  <div class="timeline" on:keydown={handleKeyDown} tabindex="-1">
    <!-- Play/Pause button -->
    <button class="tl-play-btn" class:active={isPlaying} on:click={togglePlay} title={isPlaying ? 'Pause' : 'Play'}>
      {#if isPlaying}
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
          <rect x="6" y="4" width="4" height="16" rx="1" />
          <rect x="14" y="4" width="4" height="16" rx="1" />
        </svg>
      {:else}
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
          <polygon points="5,3 19,12 5,21" />
        </svg>
      {/if}
    </button>

    <!-- Speed controls -->
    <div class="tl-speed">
      <button class="tl-speed-btn" class:active={intervalMs === 2000} on:click={() => setSpeed(2000)}>0.5x</button>
      <button class="tl-speed-btn" class:active={intervalMs === 1000} on:click={() => setSpeed(1000)}>1x</button>
      <button class="tl-speed-btn" class:active={intervalMs === 500} on:click={() => setSpeed(500)}>2x</button>
    </div>

    <!-- Track -->
    <div
      class="tl-track-container"
      bind:this={trackEl}
      on:mousedown={handleTrackMouseDown}
      on:mousemove={handleTrackMouseMove}
      on:mouseup={handleTrackMouseUp}
      on:mouseleave={handleTrackMouseLeave}
    >
      <div class="tl-track">
        <!-- Progress fill -->
        <div class="tl-progress" style="width: {progressPct}%"></div>

        <!-- Markers for each file -->
        {#each files as f, i}
          <div
            class="tl-marker"
            class:active={i === currentIdx}
            class:hover={i === hoverIdx}
            style="left: {files.length > 1 ? (i / (files.length - 1)) * 100 : 50}%"
            title={f.filename}
          >
            <div class="tl-marker-dot"></div>
          </div>
        {/each}

        <!-- Playhead -->
        <div class="tl-playhead" style="left: {progressPct}%">
          <div class="tl-playhead-knob"></div>
        </div>
      </div>

      <!-- Hover tooltip -->
      {#if hoverFile && hoverIdx !== null}
        <div
          class="tl-tooltip"
          style="left: {files.length > 1 ? (hoverIdx / (files.length - 1)) * 100 : 50}%"
        >
          <div class="tl-tooltip-inner">
            <span class="tl-tooltip-name">{hoverFile.filename}</span>
            <span class="tl-tooltip-idx">#{hoverIdx + 1} / {files.length}</span>
          </div>
        </div>
      {/if}
    </div>

    <!-- Current file label -->
    <div class="tl-label">
      {#if currentFile}
        <span class="tl-label-name">{currentFile.filename}</span>
        <span class="tl-label-idx">{currentIdx + 1}/{files.length}</span>
      {:else}
        <span class="tl-label-name">---</span>
      {/if}
    </div>
  </div>
{/if}

<style>
  .timeline {
    display: flex;
    align-items: center;
    gap: 8px;
    height: 36px;
    padding: 0 12px;
    background: var(--glass-bg-heavy);
    border-top: 1px solid var(--glass-border);
    flex-shrink: 0;
    user-select: none;
    outline: none;
  }

  .tl-play-btn {
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    background: rgba(var(--c-surface-0-rgb), 0.5);
    border: 1px solid var(--glass-border);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all var(--transition-fast);
    flex-shrink: 0;
    padding: 0;
  }

  .tl-play-btn:hover {
    border-color: var(--accent-primary);
    color: var(--accent-primary);
    background: rgba(var(--c-accent-rgb), 0.08);
  }

  .tl-play-btn.active {
    border-color: var(--accent-primary);
    color: var(--accent-primary);
    background: rgba(var(--c-accent-rgb), 0.12);
    box-shadow: 0 0 8px rgba(var(--c-accent-rgb), 0.15);
  }

  .tl-speed {
    display: flex;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    overflow: hidden;
    flex-shrink: 0;
  }

  .tl-speed-btn {
    background: transparent;
    border: none;
    border-right: 1px solid var(--glass-border);
    color: var(--text-secondary);
    font-size: 9px;
    padding: 2px 6px;
    cursor: pointer;
    transition: all var(--transition-fast);
    white-space: nowrap;
    border-radius: 0;
    height: auto;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .tl-speed-btn:last-child { border-right: none; }
  .tl-speed-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
  .tl-speed-btn.active { background: var(--bg-active); color: var(--accent-hover); }

  .tl-track-container {
    flex: 1;
    position: relative;
    height: 24px;
    cursor: pointer;
    display: flex;
    align-items: center;
  }

  .tl-track {
    position: relative;
    width: 100%;
    height: 4px;
    background: rgba(var(--c-line-rgb), 0.08);
    border-radius: 2px;
    overflow: visible;
  }

  .tl-progress {
    position: absolute;
    top: 0;
    left: 0;
    height: 100%;
    background: linear-gradient(90deg, var(--accent-primary), var(--accent-hover));
    border-radius: 2px;
    transition: width 100ms ease;
    box-shadow: 0 0 6px rgba(var(--c-accent-rgb), 0.3);
  }

  .tl-marker {
    position: absolute;
    top: 50%;
    transform: translate(-50%, -50%);
    z-index: 1;
  }

  .tl-marker-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: rgba(var(--c-line-rgb), 0.15);
    border: 1px solid rgba(var(--c-line-rgb), 0.08);
    transition: all var(--transition-fast);
  }

  .tl-marker.active .tl-marker-dot {
    background: var(--accent-primary);
    border-color: var(--accent-primary);
    box-shadow: 0 0 6px rgba(var(--c-accent-rgb), 0.4);
    width: 8px;
    height: 8px;
  }

  .tl-marker.hover .tl-marker-dot {
    background: rgba(var(--c-ink-rgb), 0.4);
    border-color: rgba(var(--c-ink-rgb), 0.3);
    width: 8px;
    height: 8px;
  }

  .tl-playhead {
    position: absolute;
    top: 50%;
    transform: translate(-50%, -50%);
    z-index: 3;
    transition: left 100ms ease;
  }

  .tl-playhead-knob {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--accent-primary);
    border: 2px solid rgba(var(--c-ink-rgb), 0.9);
    box-shadow: 0 0 8px rgba(var(--c-accent-rgb), 0.4), 0 1px 3px rgba(var(--c-shadow-rgb), 0.3);
    transition: transform 100ms ease;
  }

  .tl-track-container:hover .tl-playhead-knob {
    transform: scale(1.2);
  }

  .tl-tooltip {
    position: absolute;
    bottom: 100%;
    transform: translateX(-50%);
    margin-bottom: 6px;
    pointer-events: none;
    z-index: 10;
  }

  .tl-tooltip-inner {
    background: var(--glass-bg-heavy);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    padding: 4px 8px;
    white-space: nowrap;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }

  .tl-tooltip-name {
    font-size: 10px;
    color: var(--text-primary);
    font-weight: 500;
  }

  .tl-tooltip-idx {
    font-size: 9px;
    color: var(--text-muted);
    font-variant-numeric: tabular-nums;
  }

  .tl-label {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 1px;
    min-width: 100px;
    flex-shrink: 0;
  }

  .tl-label-name {
    font-size: 10px;
    color: var(--text-secondary);
    max-width: 120px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .tl-label-idx {
    font-size: 9px;
    color: var(--text-muted);
    font-variant-numeric: tabular-nums;
  }
</style>
