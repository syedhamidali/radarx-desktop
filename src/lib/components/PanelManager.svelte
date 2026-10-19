<script lang="ts">
  import { onMount, onDestroy, setContext } from 'svelte';
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
  import PipelinePanel from './PipelinePanel.svelte';
  import WindPanel from './WindPanel.svelte';

  setContext('embedded-panel', true); // panels below draw only their body; this component owns the header

  const PANEL_MAP: Record<string, { component: any; label: string; icon: string }> = {
    cloud: { component: CloudDataPanel, label: 'Cloud Data', icon: '☁' },
    mosaic: { component: MosaicPanel, label: 'Mosaic', icon: '\u{1F5FA}' },
    wind: { component: WindPanel, label: 'Wind (Multi-Doppler)', icon: '\u{1F32C}' },
    volume: { component: VolumePanel, label: '3D Volume', icon: '\u{1F4E6}' },
    timeseries: { component: TimeSeriesChart, label: 'Time Series', icon: '\u{1F4C8}' },
    crosssection: { component: CrossSectionPanel, label: 'Cross-Section', icon: '///' },
    qc: { component: QCPanel, label: 'Quality Control', icon: '\u2714' },
    storm: { component: StormPanel, label: 'Storm Cells', icon: '\u26C8' },
    modules: { component: ModulesPanel, label: 'Modules', icon: '\u{1F9E9}' },
    pipeline: { component: PipelinePanel, label: 'Pipelines', icon: '\u26D3' },
    processing: { component: ProcessingPanel, label: 'Processing', icon: '\u2699' },
    export: { component: ExportPanel, label: 'Export', icon: '\u21E9' },
  };

  // Line icons for the panel headers (24x24, stroke). A panel without an entry here falls back to the
  // `icon` text in PANEL_MAP, so a new panel needs no change to this table.
  const PANEL_ICONS: Record<string, string> = {
    cloud: '<path d="M7 18a4 4 0 0 1-.6-7.96A5.5 5.5 0 0 1 17 8.5a4.5 4.5 0 0 1 0 9.5H7z"/>',
    mosaic: '<rect x="3" y="3" width="8" height="8"/><rect x="13" y="3" width="8" height="8"/><rect x="3" y="13" width="8" height="8"/><path d="M13 17h8M17 13v8"/>',
    wind: '<path d="M3 8h11a3 3 0 1 0-3-3M3 12h16a3 3 0 1 1-3 3M3 16h8a2.5 2.5 0 1 1-2.5 2.5"/>',
    volume: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12L4 7.5M12 12v9"/>',
    timeseries: '<path d="M3 3v18h18"/><path d="M7 15l4-5 3 3 5-7"/>',
    crosssection: '<rect x="3" y="4" width="18" height="16"/><path d="M3 16l6-5 4 3 8-8"/>',
    qc: '<path d="M12 3l8 3v6c0 4.5-3.2 7.8-8 9-4.8-1.2-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
    storm: '<path d="M7 15a4 4 0 0 1-.6-7.96A5.5 5.5 0 0 1 17 6.5a4.5 4.5 0 0 1 0 8.5"/><path d="M12.5 11l-3 5h4l-2 5"/>',
    modules: '<rect x="3" y="3" width="8" height="8"/><rect x="13" y="3" width="8" height="8"/><rect x="3" y="13" width="8" height="8"/><rect x="14" y="14" width="6" height="6" transform="rotate(45 17 17)"/>',
    pipeline: '<circle cx="5" cy="6" r="2"/><circle cx="19" cy="12" r="2"/><circle cx="5" cy="18" r="2"/><path d="M7 6h5a3 3 0 0 1 3 3v0M7 18h5a3 3 0 0 0 3-3v0M15 12h2"/>',
    processing: '<path d="M4 7h10M18 7h2M4 17h2M10 17h10M4 12h6M14 12h6"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/><circle cx="12" cy="12" r="2"/>',
    export: '<path d="M12 15V3M7.5 7.5L12 3l4.5 4.5"/><path d="M4 14v6h16v-6"/>',
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

  /** Expand one panel and bring it into view (e.g. the command palette's "Export Current View"). */
  function showPanel(e: Event) {
    const id = (e as CustomEvent<{ id?: string }>).detail?.id;
    if (!id || !PANEL_MAP[id]) return;
    if (panelCollapsed[id]) {
      panelCollapsed = { ...panelCollapsed, [id]: false };
      workspaceStore.updateCurrentConfig({ rightPanelCollapsed: panelCollapsed });
    }
    // Wait for the panel body to render before scrolling it into view.
    setTimeout(() => document.querySelector(`[data-panel-id="${id}"]`)?.scrollIntoView({ block: 'start', behavior: 'smooth' }), 50);
  }

  onMount(() => {
    window.addEventListener('xradar:mosaic-request', expandMosaic);
    window.addEventListener('xradar:show-panel', showPanel);
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
    window.removeEventListener('xradar:show-panel', showPanel);
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
        data-panel-id={id}
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
          <button class="managed-panel-toggle" on:click={() => toggleCollapse(id)} aria-expanded={!panelCollapsed[id]} title="{panelCollapsed[id] ? 'Expand' : 'Collapse'} {panel.label}">
            <span class="managed-panel-index mono">{String(panelOrder.indexOf(id) + 1).padStart(2, '0')}</span>
            {#if PANEL_ICONS[id]}
              <svg class="managed-panel-svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{@html PANEL_ICONS[id]}</svg>
            {:else}
              <span class="managed-panel-icon">{panel.icon}</span>
            {/if}
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
    border-bottom: 1px solid var(--c-border);
    transition: opacity var(--dur-fast);
  }
  .managed-panel.dragging { opacity: 0.4; }
  .managed-panel.drag-over { box-shadow: inset 0 2px 0 var(--c-accent); }

  /* Panel header: index, icon, tracked label. An open panel gets a cyan notch on its left edge and
     a segmented hairline underneath; a collapsed one stays quiet. */
  .managed-panel-header {
    position: relative;
    display: flex;
    align-items: center;
    background: var(--c-panel-head);
    border-bottom: 1px solid var(--c-border);
  }
  .managed-panel:not(.collapsed) .managed-panel-header {
    background: linear-gradient(90deg, rgba(var(--c-accent-rgb), 0.09), var(--c-panel-head) 55%);
    border-bottom-color: transparent;
  }
  .managed-panel:not(.collapsed) .managed-panel-header::before {
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 2px;
    background: var(--c-accent);
    box-shadow: 0 0 8px rgba(var(--c-accent-rgb), 0.55);
  }
  .managed-panel:not(.collapsed) .managed-panel-header::after {
    content: '';
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    height: 1px;
    background: linear-gradient(90deg, var(--c-accent) 0 28px, transparent 28px 32px, var(--c-border-strong) 32px);
  }

  .drag-handle {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 34px;
    color: var(--c-muted);
    cursor: grab;
    opacity: 0.35;
    transition: opacity var(--dur-fast), color var(--dur-fast);
    flex-shrink: 0;
    padding-left: 5px;
  }
  .drag-handle:hover { opacity: 1; color: var(--c-accent); }
  .drag-handle:active { cursor: grabbing; }

  .managed-panel-toggle {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex: 1;
    padding: 0 var(--space-3) 0 var(--space-1);
    height: 34px;
    font-weight: 600;
    font-size: var(--fs-xs);
    text-transform: uppercase;
    letter-spacing: var(--ls-label);
    color: var(--c-text-dim);
    background: transparent;
    border: none;
    border-radius: 0;
    cursor: pointer;
  }
  .managed-panel-toggle:hover { background: var(--bg-hover); color: var(--c-text); border: none; }
  .managed-panel:not(.collapsed) .managed-panel-toggle { color: var(--c-text); }

  .managed-panel-index { font-size: 9.5px; font-weight: 400; letter-spacing: 0.04em; color: var(--c-muted); }
  .managed-panel:not(.collapsed) .managed-panel-index { color: var(--c-accent); }
  .managed-panel-svg { flex-shrink: 0; color: var(--c-muted); }
  .managed-panel:not(.collapsed) .managed-panel-svg { color: var(--c-accent); }
  .managed-panel-icon { font-size: var(--fs-sm); opacity: 0.8; }
  .managed-panel-label { flex: 1; text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  .managed-panel-chevron {
    display: flex;
    align-items: center;
    justify-content: center;
    transition: transform var(--dur-base) var(--ease-in-out);
    color: var(--c-muted);
  }
  .managed-panel-chevron.open { transform: rotate(0deg); }
  .managed-panel-chevron:not(.open) { transform: rotate(-90deg); }

  .managed-panel-content {
    max-height: 0;
    overflow: hidden;
    transition: max-height var(--dur-base) var(--ease-in-out);
  }
  .managed-panel-content.visible {
    max-height: 5000px;
    transition: max-height var(--dur-slow) var(--ease-in-out);
  }
</style>
