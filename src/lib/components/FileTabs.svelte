<script lang="ts">
  import { openFiles, activeFileId, setActiveFile, removeFile } from '../stores/fileManager';
  import { wsManager } from '../utils/websocket';
  import { createEventDispatcher } from 'svelte';

  const dispatch = createEventDispatcher<{ openfile: void }>();

  const files = $derived($openFiles);
  const currentId = $derived($activeFileId);

  function onTabClick(id: string) {
    if (id !== currentId) {
      setActiveFile(id);
    }
  }

  /** Close a tab and release the file's reader on the server (otherwise it is held until exit). */
  function closeTab(id: string) {
    if (removeFile(id) !== null) wsManager.closeFile(id);
  }

  function onCloseTab(e: MouseEvent, id: string) {
    e.stopPropagation();
    closeTab(id);
  }

  function onAddClick() {
    dispatch('openfile');
  }

  function onMiddleClick(e: MouseEvent, id: string) {
    if (e.button === 1) {
      e.preventDefault();
      closeTab(id);
    }
  }

  function onTabKeydown(e: KeyboardEvent, id: string, idx: number) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onTabClick(id);
    } else if (e.key === 'Delete' || (e.key === 'w' && (e.ctrlKey || e.metaKey))) {
      e.preventDefault();
      closeTab(id);
    } else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault();
      const next = files[idx + (e.key === 'ArrowRight' ? 1 : -1)];
      if (next) {
        onTabClick(next.id);
        // Move DOM focus with the selection (the tab list is re-rendered by the store update).
        queueMicrotask(() =>
          (document.querySelector(`.file-tabs .tab[data-file-id="${next.id}"]`) as HTMLElement | null)?.focus(),
        );
      }
    }
  }
</script>

{#if files.length > 0}
  <div class="file-tabs">
    <div class="tabs-scroll" role="tablist" aria-label="Open files">
      {#each files as file, idx (file.id)}
        <div
          class="tab"
          class:active={file.id === currentId}
          on:click={() => onTabClick(file.id)}
          on:keydown={(e) => onTabKeydown(e, file.id, idx)}
          on:mousedown={(e) => onMiddleClick(e, file.id)}
          title={file.path}
          role="tab"
          tabindex={file.id === currentId ? 0 : -1}
          data-file-id={file.id}
          aria-selected={file.id === currentId}
        >
          <span class="tab-name">{file.filename}</span>
          <button
            class="tab-close"
            on:click={(e) => onCloseTab(e, file.id)}
            title="Close tab"
            aria-label="Close {file.filename}"
            tabindex="-1"
          >
            <svg width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5">
              <line x1="2" y1="2" x2="10" y2="10"/>
              <line x1="10" y1="2" x2="2" y2="10"/>
            </svg>
          </button>
        </div>
      {/each}
    </div>

    <button class="tab-add" on:click={onAddClick} title="Open another file (Ctrl+O)" aria-label="Open another file">
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5">
        <line x1="6" y1="1" x2="6" y2="11"/>
        <line x1="1" y1="6" x2="11" y2="6"/>
      </svg>
    </button>
  </div>
{/if}

<style>
  .file-tabs {
    display: flex;
    align-items: stretch;
    height: 32px;
    background: var(--bg-tertiary);
    border-bottom: 1px solid var(--border-color);
    flex-shrink: 0;
    overflow: hidden;
  }

  .tabs-scroll {
    display: flex;
    align-items: stretch;
    overflow-x: auto;
    overflow-y: hidden;
    flex: 1;
    min-width: 0;
    scrollbar-width: thin;
    scrollbar-color: var(--border-color) transparent;
  }

  .tabs-scroll::-webkit-scrollbar {
    height: 3px;
  }

  .tabs-scroll::-webkit-scrollbar-thumb {
    background: var(--border-color);
    border-radius: 2px;
  }

  .tab {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 0 12px;
    font-size: 11px;
    color: var(--text-secondary);
    border-right: 1px solid var(--border-color);
    cursor: pointer;
    white-space: nowrap;
    min-width: 0;
    max-width: 180px;
    flex-shrink: 0;
    user-select: none;
    transition: background 120ms ease, color 120ms ease;
    position: relative;
  }

  .tab:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .tab.active {
    background: var(--bg-primary);
    color: var(--text-primary);
  }

  .tab.active::after {
    content: '';
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    height: 2px;
    background: var(--accent-primary);
  }

  .tab-name {
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .tab-close {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    padding: 0;
    border: none;
    border-radius: 3px;
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
    flex-shrink: 0;
    opacity: 0;
    transition: opacity 100ms ease, background 100ms ease, color 100ms ease;
  }

  .tab:hover .tab-close,
  .tab:focus-visible .tab-close,
  .tab.active .tab-close {
    opacity: 1;
  }

  .tab-close:hover {
    background: var(--bg-active);
    color: var(--text-primary);
  }

  .tab-add {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    flex-shrink: 0;
    border: none;
    border-left: 1px solid var(--border-color);
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
    transition: background 100ms ease, color 100ms ease;
  }

  .tab-add:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }
</style>
