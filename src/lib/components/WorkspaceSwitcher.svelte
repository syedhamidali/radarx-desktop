<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import {
    workspaceStore,
    currentWorkspaceName,
    PRESET_WORKSPACES,
    type WorkspaceConfig,
  } from '../stores/workspace';

  const dispatch = createEventDispatcher<{
    switch: { config: WorkspaceConfig };
  }>();

  let dropdownOpen = false;
  let saveAsMode = false;
  let saveAsName = '';
  let confirmDelete: string | null = null;
  let currentName = '';
  let saveError = '';
  let confirmOverwrite = false;
  let renameFrom: string | null = null;
  let renameName = '';

  $: currentName = $currentWorkspaceName;
  // Derive from the store value so the lists follow Save as / Rename / Delete.
  $: presets = Object.keys(PRESET_WORKSPACES);
  $: userWorkspaces = Object.keys($workspaceStore.workspaces).filter((n) => !PRESET_WORKSPACES[n]);

  const PRESET_ICONS: Record<string, string> = {
    Default: '\u2302',      // house
    Research: '\u2622',     // microscope-ish
    Presentation: '\u25A3', // filled square
    Comparison: '\u2261',   // triple bar
  };

  function toggle() {
    dropdownOpen = !dropdownOpen;
    if (!dropdownOpen) resetTransient();
  }

  function resetTransient() {
    saveAsMode = false;
    confirmDelete = null;
    renameFrom = null;
    saveError = '';
  }

  function close() {
    dropdownOpen = false;
    resetTransient();
  }

  function switchWorkspace(name: string) {
    const config = workspaceStore.loadWorkspace(name);
    if (config) {
      dispatch('switch', { config });
    }
    close();
  }

  function saveCurrent() {
    workspaceStore.saveWorkspace(currentName);
    close();
  }

  function startSaveAs() {
    saveAsMode = true;
    saveAsName = '';
    saveError = '';
  }

  function confirmSaveAs() {
    const name = saveAsName.trim();
    if (!name) return;
    if (PRESET_WORKSPACES[name]) {
      saveError = `"${name}" is a built-in preset name`;
      return;
    }
    if (userWorkspaces.includes(name) && !confirmOverwrite) {
      saveError = `"${name}" already exists. Save again to overwrite it.`;
      confirmOverwrite = true;
      return;
    }
    confirmOverwrite = false;
    workspaceStore.saveWorkspace(name);
    dispatch('switch', { config: workspaceStore.getCurrentConfig() });
    saveAsMode = false;
    close();
  }

  function startDelete(name: string) {
    confirmDelete = name;
  }

  function doDelete() {
    if (confirmDelete) {
      const wasCurrent = confirmDelete === currentName;
      workspaceStore.deleteWorkspace(confirmDelete);
      confirmDelete = null;
      // The deleted workspace was the one on screen: the store fell back to Default, so apply it.
      if (wasCurrent) dispatch('switch', { config: workspaceStore.getCurrentConfig() });
    }
  }

  function startRename(name: string) {
    renameFrom = name;
    renameName = name;
    saveError = '';
  }

  function confirmRename() {
    if (!renameFrom) return;
    const name = renameName.trim();
    if (!name || name === renameFrom) {
      renameFrom = null;
      return;
    }
    if (PRESET_WORKSPACES[name] || userWorkspaces.includes(name)) {
      saveError = `"${name}" already exists`;
      return;
    }
    workspaceStore.renameWorkspace(renameFrom, name);
    renameFrom = null;
    saveError = '';
  }

  function handleKeydown(e: KeyboardEvent) {
    if (!dropdownOpen) return;
    if (e.key === 'Escape') {
      if (renameFrom || saveAsMode) {
        // First Esc backs out of the inline editor, the second closes the menu.
        resetTransient();
      } else close();
      return;
    }
    if (e.key === 'Enter') {
      if (saveAsMode) confirmSaveAs();
      else if (renameFrom) confirmRename();
    }
  }

  /** Focus + select the content of an inline editor as it appears. */
  function selectOnMount(node: HTMLInputElement) {
    node.focus();
    node.select();
  }

  function handleClickOutside(e: MouseEvent) {
    const target = e.target as HTMLElement;
    if (!target.closest('.workspace-switcher')) {
      close();
    }
  }
</script>

<svelte:window on:click={handleClickOutside} on:keydown={handleKeydown} />

<!-- svelte-ignore a11y-no-static-element-interactions -->
<div class="workspace-switcher" on:click|stopPropagation>
  <button class="ws-trigger" on:click={toggle} title="Switch workspace" aria-haspopup="menu" aria-expanded={dropdownOpen}>
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
    </svg>
    <span class="ws-name">{currentName}</span>
    <svg class="ws-chevron" class:open={dropdownOpen} width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
      <polyline points="6 9 12 15 18 9" />
    </svg>
  </button>

  {#if dropdownOpen}
    <div class="ws-dropdown">
      <!-- Presets section -->
      <div class="ws-section-label">Presets</div>
      {#each presets as name}
        <button
          class="ws-item"
          class:active={name === currentName}
          on:click={() => switchWorkspace(name)}
        >
          <span class="ws-item-icon">{PRESET_ICONS[name] ?? '\u2302'}</span>
          <span class="ws-item-name">{name}</span>
          {#if name === currentName}
            <span class="ws-active-dot"></span>
          {/if}
        </button>
      {/each}

      <!-- User workspaces -->
      {#if userWorkspaces.length > 0}
        <div class="ws-divider"></div>
        <div class="ws-section-label">Saved</div>
        {#each userWorkspaces as name}
          <div class="ws-item-row">
            {#if renameFrom === name}
              <div class="ws-save-as">
                <input
                  type="text"
                  class="ws-save-input"
                  aria-label="New name for {name}"
                  bind:value={renameName}
                  use:selectOnMount
                />
                <button class="ws-btn-sm primary" on:click={confirmRename} disabled={!renameName.trim()}>Rename</button>
              </div>
            {:else if confirmDelete === name}
              <div class="ws-confirm-delete">
                <span>Delete "{name}"?</span>
                <button class="ws-btn-sm ws-btn-danger" on:click={doDelete}>Yes</button>
                <button class="ws-btn-sm" on:click={() => (confirmDelete = null)}>No</button>
              </div>
            {:else}
              <button
                class="ws-item"
                class:active={name === currentName}
                on:click={() => switchWorkspace(name)}
              >
                <span class="ws-item-icon">{'\u2726'}</span>
                <span class="ws-item-name">{name}</span>
                {#if name === currentName}
                  <span class="ws-active-dot"></span>
                {/if}
              </button>
              <button
                class="ws-delete-btn"
                on:click|stopPropagation={() => startRename(name)}
                title="Rename workspace"
                aria-label="Rename workspace {name}"
              >
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>
                </svg>
              </button>
              <button
                class="ws-delete-btn"
                on:click|stopPropagation={() => startDelete(name)}
                title="Delete workspace"
                aria-label="Delete workspace {name}"
              >
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            {/if}
          </div>
        {/each}
      {/if}

      <!-- Actions -->
      <div class="ws-divider"></div>
      {#if saveError}
        <div class="ws-error" role="alert">{saveError}</div>
      {/if}
      {#if saveAsMode}
        <div class="ws-save-as">
          <input
            type="text"
            class="ws-save-input"
            placeholder="Workspace name..."
            aria-label="Workspace name"
            bind:value={saveAsName}
            on:input={() => { saveError = ''; confirmOverwrite = false; }}
            use:selectOnMount
          />
          <button class="ws-btn-sm primary" on:click={confirmSaveAs} disabled={!saveAsName.trim()}>
            {confirmOverwrite ? 'Overwrite' : 'Save'}
          </button>
        </div>
      {:else}
        <button class="ws-action" on:click={saveCurrent}>
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
            <polyline points="17 21 17 13 7 13 7 21" />
            <polyline points="7 3 7 8 15 8" />
          </svg>
          Save current
        </button>
        <button class="ws-action" on:click={startSaveAs}>
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
          </svg>
          Save as...
        </button>
      {/if}
    </div>
  {/if}
</div>

<style>
  .workspace-switcher {
    position: relative;
  }

  .ws-trigger {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    height: 28px;
    font-size: 11px;
    color: var(--text-secondary);
    background: var(--field-bg);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .ws-trigger:hover {
    border-color: var(--border-light);
    color: var(--text-primary);
    background: rgba(91, 108, 247, 0.06);
  }

  .ws-name {
    font-weight: 500;
    max-width: 100px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .ws-chevron {
    transition: transform var(--transition-fast);
    opacity: 0.5;
  }

  .ws-chevron.open {
    transform: rotate(180deg);
  }

  .ws-dropdown {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    min-width: 220px;
    background: var(--bg-secondary);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-lg);
    padding: var(--spacing-xs) 0;
    z-index: 100;
    animation: slide-down 150ms ease-out;
    backdrop-filter: blur(var(--glass-blur-heavy));
    -webkit-backdrop-filter: blur(var(--glass-blur-heavy));
  }

  .ws-section-label {
    font-size: 9px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--text-muted);
    padding: var(--spacing-xs) var(--spacing-md);
    margin-top: var(--spacing-xs);
  }

  .ws-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    padding: 6px var(--spacing-md);
    font-size: 12px;
    color: var(--text-secondary);
    background: transparent;
    border: none;
    border-radius: 0;
    cursor: pointer;
    transition: all var(--transition-fast);
    text-align: left;
    height: auto;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .ws-item:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
    border: none;
    box-shadow: none;
  }

  .ws-item.active {
    color: var(--accent-hover);
    background: rgba(91, 108, 247, 0.08);
  }

  .ws-item-icon {
    font-size: 13px;
    width: 18px;
    text-align: center;
    opacity: 0.7;
  }

  .ws-item-name {
    flex: 1;
  }

  .ws-active-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--accent-primary);
    box-shadow: 0 0 6px var(--accent-primary);
  }

  .ws-item-row {
    display: flex;
    align-items: center;
  }

  .ws-item-row .ws-item {
    flex: 1;
  }

  .ws-delete-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    margin-right: var(--spacing-sm);
    color: var(--text-muted);
    background: transparent;
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
    opacity: 0;
    transition: all var(--transition-fast);
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .ws-item-row:hover .ws-delete-btn,
  .ws-item-row:focus-within .ws-delete-btn {
    opacity: 1;
  }

  .ws-error {
    padding: 4px var(--spacing-md);
    font-size: 11px;
    color: var(--accent-danger);
  }

  .ws-delete-btn:hover {
    background: rgba(248, 113, 113, 0.15);
    color: var(--accent-danger);
    border: none;
    box-shadow: none;
  }

  .ws-confirm-delete {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 6px var(--spacing-md);
    font-size: 11px;
    color: var(--accent-danger);
    width: 100%;
  }

  .ws-confirm-delete span {
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .ws-btn-sm {
    font-size: 10px;
    padding: 2px 8px;
    height: 22px;
    border-radius: var(--radius-sm);
  }

  .ws-btn-danger {
    background: rgba(248, 113, 113, 0.15);
    border-color: rgba(248, 113, 113, 0.3);
    color: var(--accent-danger);
  }

  .ws-btn-danger:hover {
    background: rgba(248, 113, 113, 0.25);
  }

  .ws-divider {
    height: 1px;
    background: var(--glass-border);
    margin: var(--spacing-xs) 0;
  }

  .ws-action {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    padding: 6px var(--spacing-md);
    font-size: 12px;
    color: var(--text-secondary);
    background: transparent;
    border: none;
    border-radius: 0;
    cursor: pointer;
    transition: all var(--transition-fast);
    height: auto;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .ws-action:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
    border: none;
    box-shadow: none;
  }

  .ws-save-as {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xs) var(--spacing-sm);
  }

  .ws-save-input {
    flex: 1;
    height: 26px;
    font-size: 11px;
    padding: 2px 8px;
  }
</style>
