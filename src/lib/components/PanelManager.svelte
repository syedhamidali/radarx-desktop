<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { workspaceStore, currentWorkspace } from '../stores/workspace';
  import ProcessingPanel from './ProcessingPanel.svelte';
  import ExportPanel from './ExportPanel.svelte';
  import StormPanel from './StormPanel.svelte';
  import QCPanel from './QCPanel.svelte';
  import TimeSeriesChart from './TimeSeriesChart.svelte';
  import CrossSectionPanel from './CrossSectionPanel.svelte';
  import VolumePanel from './VolumePanel.svelte';
  import CloudDataPanel from './CloudDataPanel.svelte';
  import ModulesPanel from './ModulesPanel.svelte';
  import MosaicPanel from './MosaicPanel.svelte';

  const PANEL_MAP: Record<string, { component: any; label: string; icon: string }> = {
    cloud: { component: CloudDataPanel, label: 'Cloud Data', icon: '☁' },
    mosaic: { component: MosaicPanel, label: 'Mosaic', icon: '\u{1F5FA}' },
    volume: { component: VolumePanel, label: '3D Volume', icon: '\u{1F4E6}' },
    timeseries: { component: TimeSeriesChart, label: 'Time Series', icon: '\u{1F4C8}' },
    crosssection: { component: CrossSectionPanel, label: 'Cross-Section', icon: '///' },
    qc: { component: QCPanel, label: 'Quality Control', icon: '\u2714' },
    storm: { component: StormPanel, label: 'Storm Cells', icon: '\u26C8' },
    modules: { component: ModulesPanel, label: 'Modules', icon: '\u{1F9E9}' },
    processing: { component: ProcessingPanel, label: 'Processing', icon: '\u2699' },
    export: { component: ExportPanel, label: 'Export', icon: '\u21E9' },
  };

  let panelOrder = $state<string[]>(Object.keys(PANEL_MAP));
  let panelCollapsed = $state<Record<string, boolean>>({});
  let dragTarget = $state<string | null>(null);
  let dragOverTarget = $state<string | null>(null);

  let unsubWorkspace: (() => void) | null = null;

  /** A mosaic request (e.g. from the Cloud Data panel) opens the Mosaic panel. */
  function expandMosaic() {
    if (!panelCollapsed.mosaic) return;
    panelCollapsed = { ...panelCollapsed, mosaic: false };
    workspaceStore.updateCurrentConfig({ rightPanelCollapsed: panelCollapsed });
  }

  onMount(() => {
    window.addEventListener('xradar:mosaic-request', expandMosaic);
    unsubWorkspace = currentWorkspace.subscribe((ws) => {
      const known = Object.keys(PANEL_MAP);
      const saved = (ws.rightPanelOrder ?? []).filter((id: string) => PANEL_MAP[id]);
      // Append any newly added panels not yet in saved order
      const missing = known.filter((id) => !saved.includes(id));
      panelOrder = saved.length ? [...saved, ...missing] : known;
      panelCollapsed = ws.rightPanelCollapsed ?? {};
    });
  });

  onDestroy(() => {
    window.removeEventListener('xradar:mosaic-request', expandMosaic);
    unsubWorkspace?.();
  });

  function toggleCollapse(id: string) {
    panelCollapsed = { ...panelCollapsed, [id]: !panelCollapsed[id] };
    workspaceStore.updateCurrentConfig({ rightPanelCollapsed: panelCollapsed });
  }

  function handleDragStart(e: DragEvent, id: string) {
    dragTarget = id;
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', id);
    }
  }

  function handleDragOver(e: DragEvent, id: string) {
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
    dragOverTarget = id;
  }

  function handleDragLeave() {
    dragOverTarget = null;
  }

  function handleDrop(e: DragEvent, targetId: string) {
    e.preventDefault();
    dragOverTarget = null;
    if (!dragTarget || dragTarget === targetId) {
      dragTarget = null;
      return;
    }

    const fromIdx = panelOrder.indexOf(dragTarget);
    const toIdx = panelOrder.indexOf(targetId);
    if (fromIdx < 0 || toIdx < 0) return;

    const newOrder = [...panelOrder];
    newOrder.splice(fromIdx, 1);
    newOrder.splice(toIdx, 0, dragTarget);
    panelOrder = newOrder;
    dragTarget = null;

    workspaceStore.updateCurrentConfig({ rightPanelOrder: panelOrder });
  }

  function handleDragEnd() {
    dragTarget = null;
    dragOverTarget = null;
  }
</script>

<div class="panel-manager">
  {#each panelOrder as id (id)}
    {@const panel = PANEL_MAP[id]}
    {#if panel}
      <div
        class="managed-panel"
        class:collapsed={panelCollapsed[id]}
        class:drag-over={dragOverTarget === id}
        class:dragging={dragTarget === id}
        on:dragover={(e) => handleDragOver(e, id)}
        on:dragleave={handleDragLeave}
        on:drop={(e) => handleDrop(e, id)}
      >
        <!-- svelte-ignore a11y-no-static-element-interactions -->
        <div class="managed-panel-header">
          <div
            class="drag-handle"
            draggable="true"
            on:dragstart={(e) => handleDragStart(e, id)}
            on:dragend={handleDragEnd}
            title="Drag to reorder"
          >
            <svg width="8" height="12" viewBox="0 0 8 12" fill="currentColor">
              <circle cx="2" cy="2" r="1.2" /><circle cx="6" cy="2" r="1.2" />
              <circle cx="2" cy="6" r="1.2" /><circle cx="6" cy="6" r="1.2" />
              <circle cx="2" cy="10" r="1.2" /><circle cx="6" cy="10" r="1.2" />
            </svg>
          </div>
          <button class="managed-panel-toggle" on:click={() => toggleCollapse(id)}>
            <span class="managed-panel-icon">{panel.icon}</span>
            <span class="managed-panel-label">{panel.label}</span>
            <span class="managed-panel-chevron" class:open={!panelCollapsed[id]}>
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                <polyline points="6 9 12 15 18 9" />
              </svg>
            </span>
          </button>
        </div>
        <div class="managed-panel-content" class:visible={!panelCollapsed[id]}>
          <svelte:component this={panel.component} />
        </div>
      </div>
    {/if}
  {/each}
</div>

<style>
  .panel-manager {
    display: flex;
    flex-direction: column;
    height: 100%;
    overflow-y: auto;
  }

  .managed-panel {
    border-bottom: 1px solid var(--glass-border);
    transition: opacity var(--transition-fast);
  }

  .managed-panel.dragging {
    opacity: 0.4;
  }

  .managed-panel.drag-over {
    border-top: 2px solid var(--accent-primary);
  }

  .managed-panel-header {
    display: flex;
    align-items: center;
    background: rgba(17, 22, 40, 0.5);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--glass-border);
  }

  .drag-handle {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 36px;
    color: var(--text-muted);
    cursor: grab;
    opacity: 0.4;
    transition: opacity var(--transition-fast), color var(--transition-fast);
    flex-shrink: 0;
    padding-left: 4px;
  }

  .drag-handle:hover {
    opacity: 1;
    color: var(--accent-hover);
  }

  .drag-handle:active {
    cursor: grabbing;
  }

  .managed-panel-toggle {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex: 1;
    padding: 0 var(--spacing-sm) 0 var(--spacing-xs);
    height: 36px;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-secondary);
    background: transparent;
    border: none;
    border-radius: 0;
    cursor: pointer;
    transition: all var(--transition-fast);
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .managed-panel-toggle:hover {
    background: rgba(91, 108, 247, 0.06);
    color: var(--text-primary);
    border: none;
    box-shadow: none;
  }

  .managed-panel-icon {
    font-size: 12px;
    opacity: 0.7;
  }

  .managed-panel-label {
    flex: 1;
    text-align: left;
    white-space: nowrap;
  }

  .managed-panel-chevron {
    display: flex;
    align-items: center;
    justify-content: center;
    transition: transform 250ms cubic-bezier(0.4, 0, 0.2, 1);
    color: var(--text-muted);
  }

  .managed-panel-chevron.open {
    transform: rotate(0deg);
  }

  .managed-panel-chevron:not(.open) {
    transform: rotate(-90deg);
  }

  .managed-panel-content {
    max-height: 0;
    overflow: hidden;
    transition: max-height 250ms cubic-bezier(0.4, 0, 0.2, 1);
  }

  .managed-panel-content.visible {
    max-height: 5000px;
    transition: max-height 350ms cubic-bezier(0.4, 0, 0.2, 1);
  }
</style>
