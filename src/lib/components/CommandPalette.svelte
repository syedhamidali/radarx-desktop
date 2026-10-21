<script lang="ts">
  import { createEventDispatcher, tick } from 'svelte';
  import { radarData, selectedVariable, selectedSweep, type RadarDataState } from '../stores/radarData';
  import { focusTrap } from '../utils/focusTrap';
  import { formatKeyCombo, SHORTCUT_REGISTRY } from '../utils/shortcuts';
  import { settings } from '../stores/settings';

  export let visible = false;

  const dispatch = createEventDispatcher();

  let query = '';
  let selectedIndex = 0;
  let inputEl: HTMLInputElement;

  interface PaletteAction {
    id: string;
    label: string;
    category: string;
    shortcut?: string;
    icon: string;
    action: () => void;
  }

  /** Display string of a (possibly user-rebound) shortcut. */
  function keysFor(id: string, bindings: Record<string, { key: string }>): string {
    const def = SHORTCUT_REGISTRY.find((d) => d.id === id);
    return formatKeyCombo(bindings[id]?.key ?? def?.defaultKey ?? '');
  }

  function getActions(data: RadarDataState, bindings: Record<string, { key: string }>): PaletteAction[] {
    const actions: PaletteAction[] = [
      {
        id: 'open-file',
        label: 'Open File',
        category: 'File',
        shortcut: keysFor('open-file', bindings),
        icon: '\u{1F4C2}',
        action: () => dispatch('action', { type: 'open-file' }),
      },
      {
        id: 'toggle-left-sidebar',
        label: 'Toggle Data Inspector',
        category: 'View',
        shortcut: keysFor('toggle-left-sidebar', bindings),
        icon: '\u{1F4CB}',
        action: () => dispatch('action', { type: 'toggle-left-sidebar' }),
      },
      {
        id: 'toggle-right-sidebar',
        label: 'Toggle Tools Panel',
        category: 'View',
        shortcut: keysFor('toggle-right-sidebar', bindings),
        icon: '\u{1F527}',
        action: () => dispatch('action', { type: 'toggle-right-sidebar' }),
      },
      {
        id: 'toggle-map',
        label: 'Hide / Show Map (2D and 3D)',
        category: 'View',
        shortcut: keysFor('toggle-map', bindings),
        icon: '\u{1F5FA}',
        action: () => dispatch('action', { type: 'toggle-map' }),
      },
      {
        id: 'toggle-animation',
        label: 'Play / Pause Animation',
        category: 'Playback',
        shortcut: keysFor('play-pause', bindings),
        icon: '\u25B6',
        action: () => dispatch('action', { type: 'toggle-animation' }),
      },
      {
        id: 'view-2d',
        label: 'Switch to 2D View',
        category: 'View',
        icon: '\u{1F5FA}',
        action: () => dispatch('action', { type: 'set-view', value: '2d' }),
      },
      {
        id: 'view-3d',
        label: 'Switch to 3D View',
        category: 'View',
        icon: '\u{1F30D}',
        action: () => dispatch('action', { type: 'set-view', value: '3d' }),
      },
      {
        id: 'view-dualpol',
        label: 'Switch to Dual-Pol Dashboard',
        category: 'View',
        icon: '\u{1F4CA}',
        action: () => dispatch('action', { type: 'set-view', value: 'dualpol' }),
      },
      {
        id: 'open-settings',
        label: 'Open Settings',
        category: 'App',
        shortcut: keysFor('settings', bindings),
        icon: '\u2699',
        action: () => dispatch('action', { type: 'open-settings' }),
      },
      {
        id: 'show-shortcuts',
        label: 'Keyboard Shortcuts',
        category: 'App',
        shortcut: '?',
        icon: '\u2328',
        action: () => dispatch('action', { type: 'show-shortcuts' }),
      },
      {
        id: 'show-tour',
        label: 'Replay Welcome Tour',
        category: 'App',
        icon: '\u{1F9ED}',
        action: () => dispatch('action', { type: 'show-tour' }),
      },
      {
        id: 'command-studio',
        label: 'Command Studio: every rxd command as a form...',
        category: 'Tools',
        icon: '⌨',
        action: () => dispatch('action', { type: 'command-studio' }),
      },
      {
        id: 'report-issue',
        label: 'Report an Issue...',
        category: 'App',
        icon: '\u2691',
        action: () => dispatch('action', { type: 'report-issue' }),
      },
      {
        id: 'about',
        label: 'About Radarx Desktop',
        category: 'App',
        icon: '\u2139',
        action: () => dispatch('action', { type: 'about' }),
      },
    ];

    // Add variable selection actions
    for (const v of data.variables) {
      actions.push({
        id: `var-${v}`,
        label: `Select Variable: ${v}`,
        category: 'Variables',
        icon: 'V',
        action: () => {
          selectedVariable.set(v);
        },
      });
    }

    // Add sweep selection actions
    for (const s of data.sweeps) {
      const elev = s.elevation != null ? s.elevation.toFixed(1) : '?';
      actions.push({
        id: `sweep-${s.index}`,
        label: s.kind === 'rhi' ? `Select RHI ${s.azimuth?.toFixed(1) ?? '?'}\u00B0 (sweep ${s.index})` : `Select Sweep ${s.index} (${elev}\u00B0)`,
        category: 'Sweeps',
        icon: '\u{1F4E1}',
        action: () => {
          selectedSweep.set(s.index);
        },
      });
    }

    // (Colormap commands are intentionally absent: the viewer keeps its own colormap state and
    // does not read settings.colormap, so such a command would silently do nothing.)

    actions.push({
      id: 'data-check',
      label: 'Data Check: what is in this file',
      category: 'File',
      icon: '✓',
      action: () => dispatch('action', { type: 'data-check' }),
    });

    actions.push({
      id: 'autopilot',
      label: 'Autopilot: inspect, plan and run analyses for a set of files',
      category: 'File',
      icon: '◎',
      action: () => dispatch('action', { type: 'autopilot' }),
    });

    // Export action
    actions.push({
      id: 'export',
      label: 'Export Current View',
      category: 'File',
      icon: '\u{1F4BE}',
      action: () => dispatch('action', { type: 'export' }),
    });

    actions.push({
      id: 'movie-studio',
      label: 'Movie Studio: make a storm movie (MP4)',
      category: 'File',
      icon: '\u{1F3AC}',
      action: () => dispatch('action', { type: 'movie-studio' }),
    });

    return actions;
  }

  function fuzzyMatch(text: string, pattern: string): boolean {
    const lowerText = text.toLowerCase();
    const lowerPattern = pattern.toLowerCase();
    if (lowerPattern === '') return true;

    // Split pattern into words and check if all words appear in text
    const words = lowerPattern.split(/\s+/);
    return words.every(word => lowerText.includes(word));
  }

  // Pass the data in so the list is rebuilt when a file's variables / sweeps change.
  $: allActions = getActions($radarData, $settings.shortcuts);
  $: filtered = query === ''
    ? allActions.slice(0, 12)
    : allActions.filter(a => fuzzyMatch(`${a.label} ${a.category}`, query)).slice(0, 12);
  $: selectedIndex = Math.min(selectedIndex, Math.max(0, filtered.length - 1));

  $: if (visible) {
    focusInput();
  } else {
    // Closed from outside (the Ctrl+K toggle): the next open must start clean.
    query = '';
    selectedIndex = 0;
  }

  async function focusInput() {
    await tick();
    if (inputEl) inputEl.focus();
  }

  function close() {
    visible = false;
    query = '';
    selectedIndex = 0;
    dispatch('close');
  }

  function executeAction(action: PaletteAction) {
    action.action();
    close();
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (filtered.length) selectedIndex = (selectedIndex + 1) % filtered.length;
      scrollToSelected();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (filtered.length) selectedIndex = (selectedIndex - 1 + filtered.length) % filtered.length;
      scrollToSelected();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (filtered[selectedIndex]) {
        executeAction(filtered[selectedIndex]);
      }
    }
  }

  function scrollToSelected() {
    tick().then(() => {
      const el = document.querySelector('.cmd-item.selected');
      if (el) el.scrollIntoView({ block: 'nearest' });
    });
  }

  function handleBackdropClick(e: MouseEvent) {
    if ((e.target as HTMLElement).classList.contains('cmd-backdrop')) {
      close();
    }
  }

  // Reset selection on query change
  $: if (query !== undefined) selectedIndex = 0;
</script>

{#if visible}
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div class="cmd-backdrop" on:click={handleBackdropClick}>
    <div class="cmd-palette" role="dialog" tabindex="-1" aria-modal="true" aria-label="Command palette" on:keydown={handleKeyDown} use:focusTrap={{ initial: '.cmd-input' }}>
      <div class="cmd-input-wrap">
        <svg class="cmd-search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="8" />
          <line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          bind:this={inputEl}
          bind:value={query}
          class="cmd-input"
          placeholder="Type a command..."
          spellcheck="false"
          autocomplete="off"
          role="combobox"
          aria-expanded="true"
          aria-controls="cmd-listbox"
          aria-autocomplete="list"
          aria-activedescendant={filtered[selectedIndex] ? `cmd-opt-${filtered[selectedIndex].id}` : undefined}
          aria-label="Command"
        />
        <kbd class="cmd-esc-hint">ESC</kbd>
      </div>

      <div class="cmd-results" id="cmd-listbox" role="listbox" aria-label="Commands">
        {#if filtered.length === 0}
          <div class="cmd-empty" role="status">No matching commands</div>
        {:else}
          {#each filtered as action, i}
            <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
            <div
              class="cmd-item"
              class:selected={i === selectedIndex}
              id="cmd-opt-{action.id}"
              tabindex="-1"
              role="option"
              aria-selected={i === selectedIndex}
              on:click={() => executeAction(action)}
              on:mouseenter={() => (selectedIndex = i)}
            >
              <div class="cmd-item-body">
                <span class="cmd-item-label">{action.label}</span>
                <span class="cmd-item-category">{action.category}</span>
              </div>
              {#if action.shortcut}
                <kbd class="cmd-item-shortcut">{action.shortcut}</kbd>
              {/if}
            </div>
          {/each}
        {/if}
      </div>

      <div class="cmd-footer">
        <span class="cmd-footer-hint">
          <kbd>&uarr;</kbd><kbd>&darr;</kbd> navigate
        </span>
        <span class="cmd-footer-hint">
          <kbd>&crarr;</kbd> select
        </span>
        <span class="cmd-footer-hint">
          <kbd>esc</kbd> close
        </span>
      </div>
    </div>
  </div>
{/if}

<style>
  .cmd-backdrop {
    position: fixed;
    inset: 0;
    z-index: 9999;
    background: var(--c-scrim);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding-top: 15vh;
    animation: cmd-fade 120ms ease-out;
  }
  @keyframes cmd-fade {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  .cmd-palette {
    position: relative;
    width: 560px;
    max-width: 90vw;
    max-height: 60vh;
    display: flex;
    flex-direction: column;
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-3);
    overflow: hidden;
    animation: slide-down var(--dur-base) var(--ease-out);
  }
  /* accent hairline across the top edge */
  .cmd-palette::before {
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    width: 64px;
    height: 2px;
    background: var(--c-accent);
    box-shadow: 0 0 10px rgba(var(--c-accent-rgb), 0.7);
  }

  .cmd-input-wrap {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: 0 var(--space-4);
    border-bottom: 1px solid var(--c-border);
  }
  .cmd-search-icon { color: var(--c-accent); flex-shrink: 0; }
  .cmd-input {
    flex: 1;
    height: 48px;
    padding: 0;
    font-size: var(--fs-lg);
    color: var(--c-text);
    background: transparent;
    border: none;
    border-radius: 0;
    outline: none;
    box-shadow: none;
    user-select: text;
  }
  .cmd-input:focus { border: none; box-shadow: none; }
  .cmd-input::placeholder { color: var(--c-muted); }
  .cmd-esc-hint { flex-shrink: 0; }

  .cmd-results { flex: 1; overflow-y: auto; padding: var(--space-1) 0; }
  .cmd-empty { padding: var(--space-5); text-align: center; color: var(--c-muted); font-size: var(--fs-sm); }

  .cmd-item {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: 0 var(--space-4);
    height: 38px;
    cursor: pointer;
    border-left: 2px solid transparent;
  }
  .cmd-item.selected {
    background: rgba(var(--c-accent-rgb), 0.12);
    border-left-color: var(--c-accent);
  }
  .cmd-item-body { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); flex: 1; min-width: 0; }
  .cmd-item-label { font-size: var(--fs-md); color: var(--c-text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .cmd-item.selected .cmd-item-label { color: var(--c-accent-strong); }
  .cmd-item-category {
    font-size: var(--fs-2xs);
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: var(--ls-label);
    color: var(--c-muted);
  }
  .cmd-item-shortcut { flex-shrink: 0; }

  .cmd-footer {
    display: flex;
    gap: var(--space-4);
    padding: var(--space-2) var(--space-4);
    border-top: 1px solid var(--c-border);
    background: var(--c-panel-head);
  }
  .cmd-footer-hint { display: flex; align-items: center; gap: 4px; font-size: var(--fs-xs); color: var(--c-muted); }
</style>
