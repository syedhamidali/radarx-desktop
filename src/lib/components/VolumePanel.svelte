<script lang="ts">
  /**
   * VolumePanel — ties together box selection, transfer function editing,
   * and 3D volume rendering. Placed in the right sidebar.
   *
   * Communicates with the PPI via events (box-select toggle) and listens
   * to the WebSocket for volume_data messages.
   */
  import { onDestroy, untrack } from 'svelte';
  import { selectedVariable, radarData, connectionStatus } from '../stores/radarData';
  import { addToast } from '../stores/toasts';
  import { runFeatureRequest, type FeatureRequestHandle } from '../utils/featureRequest';
  import { boxSelectActive, selectedBox as selectedBoxStore } from '../stores/volumeSelect';
  import CollapsiblePanel from './CollapsiblePanel.svelte';
  import VolumeViewer from './VolumeViewer.svelte';
  import TransferFunctionEditor from './TransferFunctionEditor.svelte';

  // Volume data state
  let volumeData: Float32Array | null = $state(null);
  let nx = $state(0);
  let ny = $state(0);
  let nz = $state(0);
  let bounds = $state({ xMin: 0, xMax: 1, yMin: 0, yMax: 1, zMin: 0, zMax: 15000 });
  let vmin = $state(0);
  let vmax = $state(60);
  let isLoading = $state(false);

  // Settings
  let resolution: number = $state(100);
  let maxHeight: number | null = $state(15); // km (null while the box is empty)

  // Transfer function
  let transferFunction = $state(createDefaultTransferFunction());

  // Store subscriptions
  const variable = $derived($selectedVariable ?? '');
  const filePath = $derived($radarData.filePath);
  const isConnected = $derived($connectionStatus === 'connected');
  const heightProblem = $derived(
    maxHeight === null || !Number.isFinite(maxHeight) || maxHeight < 1 || maxHeight > 25
      ? 'Max height must be between 1 and 25 km'
      : '',
  );

  // Selected box (in meters from radar center)
  let selectedBox: { xMin: number; xMax: number; yMin: number; yMax: number } | null = $state(null);

  // Volume viewer ref
  let volumeViewer: VolumeViewer;

  let handle: FeatureRequestHandle | null = null;

  onDestroy(() => {
    handle?.cancel();
    unsubBox();
  });

  // A different file: the box and the volume belong to the old one.
  let lastFile: string | null | undefined = undefined;
  $effect(() => {
    const f = filePath;
    untrack(() => {
      if (lastFile !== undefined && f !== lastFile) {
        handle?.cancel();
        volumeData = null;
        selectedBox = null;
        selectedBoxStore.set(null);
        isLoading = false;
      }
      lastFile = f;
    });
  });

  function toggleBoxSelect() {
    boxSelectActive.update(v => !v);
  }

  // React to box selections from the PPI
  const unsubBox = selectedBoxStore.subscribe((box) => {
    if (box) {
      selectedBox = box;
      boxSelectActive.set(false);
      requestVolume();
    }
  });

  function requestVolume() {
    if (!selectedBox || !variable) {
      addToast('info', 'Select a region on the PPI first');
      return;
    }
    if (heightProblem) {
      addToast('info', heightProblem);
      return;
    }
    if (!isConnected) return;

    handle?.cancel();
    isLoading = true;
    volumeData = null;

    const box = selectedBox;
    const v = variable;
    const h = runFeatureRequest({
      kind: 'volume',
      label: `Extracting 3D volume (${v})`,
      message: {
        type: 'get_volume',
        variable: v,
        box: {
          x_min: box.xMin,
          x_max: box.xMax,
          y_min: box.yMin,
          y_max: box.yMax,
        },
        resolution,
        z_max: (maxHeight ?? 15) * 1000,
      },
      resultTypes: ['volume_data'],
      timeoutMs: 120000,
      onResult: (msg: any) => {
        if (msg.error) {
          addToast('error', `Volume error: ${msg.error}`);
          return;
        }

        // Decode volume data from the message
        const dataArray = msg.data;
        if (dataArray && Array.isArray(dataArray)) {
          volumeData = new Float32Array(dataArray);
        } else if (dataArray instanceof Float32Array) {
          volumeData = dataArray;
        } else if (dataArray instanceof ArrayBuffer) {
          volumeData = new Float32Array(dataArray);
        } else {
          volumeData = null;
        }

        nx = msg.nx ?? 0;
        ny = msg.ny ?? 0;
        nz = msg.nz ?? 0;
        bounds = {
          xMin: msg.bounds?.xMin ?? 0,
          xMax: msg.bounds?.xMax ?? 1,
          yMin: msg.bounds?.yMin ?? 0,
          yMax: msg.bounds?.yMax ?? 1,
          zMin: msg.bounds?.zMin ?? 0,
          zMax: msg.bounds?.zMax ?? (maxHeight ?? 15) * 1000,
        };
        vmin = msg.vmin ?? 0;
        vmax = msg.vmax ?? 60;

        if (volumeData) {
          addToast('success', `Volume loaded: ${nx}x${ny}x${nz} (${v})`);
        }
      },
      onSettled: () => {
        if (handle === h) {
          handle = null;
          isLoading = false;
        }
      },
      retry: requestVolume,
    });
    handle = h;
  }

  function onTransferFunctionChange(detail: { transferFunction: Uint8Array }) {
    transferFunction = detail.transferFunction;
    // If a renderer is active, it would pick up the new transfer function
    // through the VolumeViewer component binding
  }

  function createDefaultTransferFunction(): Uint8Array {
    const tf = new Uint8Array(256 * 4);
    // Default: grayscale ramp with linear alpha
    for (let i = 0; i < 256; i++) {
      tf[i * 4 + 0] = i;
      tf[i * 4 + 1] = i;
      tf[i * 4 + 2] = i;
      tf[i * 4 + 3] = i;
    }
    return tf;
  }

  const hasBox = $derived(selectedBox !== null);
  const hasVolume = $derived(volumeData !== null);
  const boxLabel = $derived.by(() => {
    const b = selectedBox;
    if (!b) return '';
    return `${(Math.abs(b.xMax - b.xMin) / 1000).toFixed(0)} x ${(Math.abs(b.yMax - b.yMin) / 1000).toFixed(0)} km`;
  });
</script>

<CollapsiblePanel title="3D Volume" icon="&#9634;">
  <div class="vp-root">
    <!-- Box select toggle -->
    <div class="vp-row">
      <button
        class="vp-btn"
        class:active={$boxSelectActive}
        on:click={toggleBoxSelect}
        title={$boxSelectActive ? 'Cancel box selection' : 'Draw a selection box on the PPI'}
      >
        {$boxSelectActive ? 'Drawing...' : 'Select Region'}
      </button>
      {#if hasBox}
        <span class="vp-box-label">{boxLabel}</span>
      {/if}
    </div>

    <!-- Settings row -->
    <div class="vp-settings">
      <div class="vp-field">
        <label class="vp-label" for="vp-resolution">Resolution</label>
        <select id="vp-resolution" class="vp-select" bind:value={resolution}>
          <option value={50}>50</option>
          <option value={75}>75</option>
          <option value={100}>100</option>
          <option value={150}>150</option>
        </select>
      </div>

      <div class="vp-field">
        <label class="vp-label" for="vp-max-height">Max Height (km)</label>
        <input
          id="vp-max-height"
          type="number"
          class="vp-input"
          class:invalid={!!heightProblem}
          aria-invalid={!!heightProblem}
          title={heightProblem || '1 to 25 km'}
          bind:value={maxHeight}
          min="1"
          max="25"
          step="1"
        />
      </div>
    </div>

    <!-- Re-request button -->
    {#if hasBox}
      <button
        class="vp-btn primary"
        on:click={requestVolume}
        disabled={isLoading || !variable || !!heightProblem || !isConnected}
        title={heightProblem || undefined}
      >
        {#if isLoading}
          <span class="vp-spinner"></span>
          Loading...
        {:else}
          Render Volume
        {/if}
      </button>
      {#if isLoading}
        <button class="vp-btn" on:click={() => handle?.cancel()} title="Stop waiting for the volume">Cancel</button>
      {/if}
    {/if}

    <!-- Transfer function editor -->
    <div class="vp-section">
      <span class="vp-section-title">Transfer Function</span>
      <TransferFunctionEditor
        bind:transferFunction={transferFunction}
        onchange={onTransferFunctionChange}
      />
    </div>

    <!-- Volume viewer -->
    <div class="vp-viewer">
      <VolumeViewer
        bind:this={volumeViewer}
        {volumeData}
        {nx} {ny} {nz}
        {bounds}
        {vmin} {vmax}
        {variable}
      />
    </div>
  </div>
</CollapsiblePanel>

<style>
  .vp-root {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm, 6px);
  }

  .vp-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 6px);
  }

  .vp-btn {
    padding: 5px 12px;
    font-size: 11px;
    font-weight: 600;
    border-radius: var(--radius-sm, 4px);
    border: 1px solid var(--border-color);
    background: var(--bg-surface);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 150ms ease;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .vp-btn:hover:not(:disabled) {
    background: rgba(var(--c-accent-rgb), 0.1);
    border-color: rgba(var(--c-accent-rgb), 0.2);
    color: var(--text-primary);
  }

  .vp-btn.active {
    background: rgba(var(--c-accent-rgb), 0.2);
    border-color: rgba(var(--c-accent-rgb), 0.4);
    color: var(--accent-hover);
  }

  .vp-btn.primary {
    background: rgba(var(--c-accent-rgb), 0.15);
    border-color: rgba(var(--c-accent-rgb), 0.3);
    color: var(--accent-hover);
    width: 100%;
    justify-content: center;
  }

  .vp-btn.primary:hover:not(:disabled) {
    background: rgba(var(--c-accent-rgb), 0.3);
    color: var(--c-text);
  }

  .vp-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .vp-box-label {
    font-size: 10px;
    font-family: var(--font-mono, monospace);
    color: var(--text-muted);
  }

  /* Settings */
  .vp-settings {
    display: flex;
    gap: var(--spacing-sm, 6px);
  }

  .vp-field {
    display: flex;
    flex-direction: column;
    gap: 2px;
    flex: 1;
  }

  .vp-label {
    font-size: 10px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.03em;
    white-space: nowrap;
  }

  .vp-select {
    width: 100%;
    padding: 3px 6px;
    font-size: 11px;
    font-family: var(--font-mono, monospace);
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm, 4px);
    color: var(--text-primary);
  }

  .vp-input.invalid {
    border-color: var(--accent-danger);
    box-shadow: 0 0 0 1px var(--accent-danger);
  }

  .vp-input {
    width: 100%;
    padding: 3px 6px;
    font-size: 11px;
    font-family: var(--font-mono, monospace);
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm, 4px);
    color: var(--text-primary);
  }

  .vp-spinner {
    width: 12px;
    height: 12px;
    border: 2px solid rgba(var(--c-ink-rgb), 0.3);
    border-top-color: var(--c-text);
    border-radius: 50%;
    animation: vp-spin 600ms linear infinite;
    display: inline-block;
  }

  @keyframes vp-spin {
    to { transform: rotate(360deg); }
  }

  /* Section */
  .vp-section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs, 4px);
    margin-top: var(--spacing-xs, 4px);
  }

  .vp-section-title {
    font-size: 10px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  /* Viewer */
  .vp-viewer {
    height: 300px;
    margin-top: var(--spacing-xs, 4px);
    border-radius: var(--radius-sm, 4px);
    overflow: hidden;
  }
</style>
