<script lang="ts">
  import { onMount, onDestroy, createEventDispatcher } from 'svelte';
  import { settings, DEFAULT_SETTINGS, type AppSettings } from '../stores/settings';
  import { recentFiles } from '../stores/recentFiles';
  import { webUi } from '../stores/webInfo';
  import { focusTrap } from '../utils/focusTrap';
  import { isWebMode } from '../utils/env';
  import UpdatesSettings from './UpdatesSettings.svelte';
  import MoviePackSettings from './MoviePackSettings.svelte';
  import { lazy } from '../utils/lazy.svelte';
  import {
    SHORTCUT_REGISTRY,
    formatKeyCombo,
    detectConflict,
    updateShortcut,
    resetShortcut,
    resetAllShortcuts,
    type ShortcutDef,
  } from '../utils/shortcuts';

  let { open = false } = $props<{ open?: boolean }>();

  const dispatch = createEventDispatcher();

  // The radar-sites section brings the site catalogue with it: fetch it when the Data tab is first shown.
  const radarSitesSettings = lazy(() => import('./RadarSitesSettings.svelte'));
  const liveSettings = lazy(() => import('./LiveSettings.svelte'));
  type TabId = 'general' | 'display' | 'data' | 'export' | 'updates' | 'shortcuts';
  let activeTab: TabId = $state('general');
  $effect(() => { if (activeTab === 'data') { void radarSitesSettings.load(); void liveSettings.load(); } });

  // Local copy for live editing
  const s = $derived($settings);

  // Shortcut recording state
  let recordingShortcutId: string | null = $state(null);
  let conflictWarning = $state('');

  const tabs: { id: TabId; label: string }[] = [
    { id: 'general', label: 'General' },
    { id: 'display', label: 'Display' },
    { id: 'data', label: 'Data' },
    { id: 'export', label: 'Export' },
    // Self-update exists only in the desktop app, never in a browser.
    ...(isWebMode() ? [] : [{ id: 'updates' as TabId, label: 'Updates' }]),
    { id: 'shortcuts', label: 'Shortcuts' },
  ];

  function close() {
    open = false;
    recordingShortcutId = null;
    conflictWarning = '';
    dispatch('close');
  }

  function handleOverlayClick(e: MouseEvent) {
    if ((e.target as HTMLElement).classList.contains('settings-overlay')) {
      close();
    }
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (!open) return;

    // If recording a shortcut, capture the key combo
    if (recordingShortcutId) {
      e.preventDefault();
      e.stopPropagation();

      if (e.key === 'Escape') {
        recordingShortcutId = null;
        conflictWarning = '';
        return;
      }

      // Ignore bare modifier presses
      if (['Control', 'Meta', 'Alt', 'Shift'].includes(e.key)) return;

      const parts: string[] = [];
      if (e.ctrlKey || e.metaKey) parts.push('mod');
      if (e.shiftKey) parts.push('shift');
      if (e.altKey) parts.push('alt');

      let keyName = e.key.toLowerCase();
      if (keyName === ' ') keyName = 'space';
      parts.push(keyName);

      const combo = parts.join('+');
      const conflict = detectConflict(combo, recordingShortcutId);
      if (conflict) {
        conflictWarning = `Conflicts with "${conflict.description}"`;
        return;
      }

      updateShortcut(recordingShortcutId, combo);
      recordingShortcutId = null;
      conflictWarning = '';
      return;
    }

    if (e.key === 'Escape') {
      close();
    }
  }

  // ─── Update helpers ──────────────────────────────────────────────────────

  function setThemeMode(mode: 'dark' | 'light' | 'auto') {
    settings.updateSetting('theme', { mode });
  }

  function setAccentColor(color: string) {
    settings.updateSetting('theme', { accentColor: color });
  }

  function setFontSize(size: number) {
    settings.updateSetting('theme', { fontSize: size });
  }

  function setBgColor(color: string) {
    settings.updateSetting('display', { bgColor: color });
  }

  function toggleRangeRings() {
    settings.updateSetting('display', { showRangeRings: !s.display.showRangeRings });
  }

  function toggleCrosshair() {
    settings.updateSetting('display', { showCrosshair: !s.display.showCrosshair });
  }

  function setRangeRingColor(color: string) {
    settings.updateSetting('display', { rangeRingColor: color });
  }

  function toggleAntialias() {
    settings.updateSetting('rendering', { antialias: !s.rendering.antialias });
  }

  function setInterpolation(v: 'nearest' | 'bilinear') {
    settings.updateSetting('rendering', { interpolation: v });
  }

  function setAutoLoadLast() {
    settings.updateSetting('data', { autoLoadLast: !s.data.autoLoadLast });
  }

  function setDefaultDirectory(dir: string) {
    settings.updateSetting('data', { defaultDirectory: dir });
  }

  function setExportFormat(fmt: 'png' | 'svg' | 'pdf') {
    settings.updateSetting('export_', { defaultFormat: fmt });
  }

  function setExportDpi(dpi: number) {
    // Clamp to the control's own range: typing 5 or 99999 must not reach the exporter.
    settings.updateSetting('export_', { dpi: Math.min(600, Math.max(72, Math.round(dpi) || 150)) });
  }

  function setExportDir(dir: string) {
    settings.updateSetting('export_', { outputDirectory: dir });
  }

  function startRecording(id: string) {
    recordingShortcutId = id;
    conflictWarning = '';
  }

  function getBoundKeyDisplay(id: string): string {
    const binding = s.shortcuts[id];
    const def = SHORTCUT_REGISTRY.find((d) => d.id === id);
    const key = binding?.key ?? def?.defaultKey ?? '';
    return formatKeyCombo(key);
  }

  function formatRelativeTime(ts: number): string {
    const diff = Date.now() - ts;
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  }

  const accentPresets = [
    '#2bd9f5', '#3b82f6', '#8b5cf6', '#ec4899',
    '#ef4444', '#f59e0b', '#a3e635', '#10b981',
  ];
</script>

<svelte:window on:keydown={handleKeyDown} />

{#if open}
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div class="settings-overlay" on:click={handleOverlayClick}>
    <div class="settings-dialog" role="dialog" aria-modal="true" aria-label="Settings" use:focusTrap={{ initial: '.tab-btn.active' }}>
      <!-- Header -->
      <div class="settings-header">
        <h2>Settings</h2>
        <button class="close-btn" on:click={close} title="Close (Esc)" aria-label="Close settings">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>

      <div class="settings-body">
        <!-- Tab bar -->
        <div class="settings-tabs" role="tablist" aria-label="Settings sections">
          {#each tabs as tab (tab.id)}
            <button
              class="tab-btn"
              role="tab"
              aria-selected={activeTab === tab.id}
              class:active={activeTab === tab.id}
              on:click={() => (activeTab = tab.id)}
            >
              {tab.label}
            </button>
          {/each}
        </div>

        <!-- Tab content -->
        <div class="tab-content">
          <!-- ═══ General ═══ -->
          {#if activeTab === 'general'}
            <div class="settings-section">
              <h3>Theme</h3>
              <div class="form-row">
                <span class="form-label" id="st-appearance">Appearance</span>
                <div class="btn-group" role="group" aria-labelledby="st-appearance">
                  <button
                    class:active={s.theme.mode === 'dark'}
                    on:click={() => setThemeMode('dark')}
                   title="Mission control: dark, low glare for long sessions">Mission control</button>
                  <button
                    class:active={s.theme.mode === 'light'}
                    on:click={() => setThemeMode('light')}
                   title="Clean room: light, for print, projectors and bright rooms">Clean room</button>
                  <button
                    class:active={s.theme.mode === 'auto'}
                    on:click={() => setThemeMode('auto')}
                   title="Follow the operating system setting">System</button>
                </div>
              </div>

              <div class="form-row">
                <span class="form-label" id="st-accent">Accent color</span>
                <div class="accent-row" role="group" aria-labelledby="st-accent">
                  {#each accentPresets as color}
                    <button
                      class="color-swatch"
                      class:selected={s.theme.accentColor === color}
                      style="background: {color}"
                      on:click={() => setAccentColor(color)}
                      title={color}
                      aria-label="Accent colour {color}"
                      aria-pressed={s.theme.accentColor === color}
                    ></button>
                  {/each}
                  <input
                    type="color"
                    value={s.theme.accentColor}
                    on:input={(e) => setAccentColor(e.currentTarget.value)}
                    class="color-input"
                    title="Custom color"
                    aria-label="Custom accent colour"
                  />
                </div>
              </div>

              <div class="form-row">
                <label class="form-label" for="st-font-size">Font size</label>
                <div class="inline-control">
                  <input
                    type="range"
                    min="11"
                    max="18"
                    step="1"
                    id="st-font-size"
                    value={s.theme.fontSize}
                    on:input={(e) => setFontSize(parseInt(e.currentTarget.value))}
                    class="range-input"
                  />
                  <span class="range-value">{s.theme.fontSize}px</span>
                </div>
              </div>
            </div>

            <div class="settings-section">
              <div class="section-header">
                <h3>Rendering</h3>
              </div>
              <div class="form-row">
                <label class="form-label" for="st-antialias">Antialiasing</label>
                <label class="toggle-label">
                  <input id="st-antialias" type="checkbox" checked={s.rendering.antialias} on:change={toggleAntialias} />
                  <span>{s.rendering.antialias ? 'On' : 'Off'}</span>
                </label>
              </div>
              <div class="form-row">
                <label class="form-label" for="st-interp">Interpolation</label>
                <select
                  id="st-interp"
                  value={s.rendering.interpolation}
                  on:change={(e) => setInterpolation(e.currentTarget.value as 'nearest' | 'bilinear')}
                >
                  <option value="nearest">Nearest neighbor</option>
                  <option value="bilinear">Bilinear</option>
                </select>
              </div>
            </div>

            <div class="section-footer">
              <button class="reset-btn" on:click={() => { settings.resetSection('theme'); settings.resetSection('rendering'); }}>
                Reset General
              </button>
            </div>

          <!-- ═══ Display ═══ -->
          {:else if activeTab === 'display'}
            <div class="settings-section">
              <h3>Map Display</h3>
              <div class="form-row">
                <label class="form-label" for="st-bg">Background color</label>
                <div class="inline-control">
                  <input
                    id="st-bg"
                    type="color"
                    value={s.display.bgColor}
                    on:input={(e) => setBgColor(e.currentTarget.value)}
                    class="color-input"
                  />
                  <span class="mono text-muted" style="font-size:11px">{s.display.bgColor}</span>
                </div>
              </div>

              <div class="form-row">
                <label class="form-label" for="st-rings">Range rings</label>
                <label class="toggle-label">
                  <input id="st-rings" type="checkbox" checked={s.display.showRangeRings} on:change={toggleRangeRings} />
                  <span>{s.display.showRangeRings ? 'Visible' : 'Hidden'}</span>
                </label>
              </div>

              {#if s.display.showRangeRings}
                <div class="form-row">
                  <label class="form-label" for="st-ring-color">Ring color</label>
                  <div class="inline-control">
                    <input
                      id="st-ring-color"
                      type="color"
                      value={s.display.rangeRingColor}
                      on:input={(e) => setRangeRingColor(e.currentTarget.value)}
                      class="color-input"
                    />
                  </div>
                </div>
              {/if}

              <div class="form-row">
                <label class="form-label" for="st-crosshair">Crosshair</label>
                <label class="toggle-label">
                  <input id="st-crosshair" type="checkbox" checked={s.display.showCrosshair} on:change={toggleCrosshair} />
                  <span>{s.display.showCrosshair ? 'Visible' : 'Hidden'}</span>
                </label>
              </div>
            </div>

            <div class="settings-section">
              <h3>Default Colormaps</h3>
              <p class="section-hint">Set default colormap per radar variable</p>
              <div class="cmap-grid">
                {#each Object.entries(s.display.defaultCmapPerVariable) as [variable, cmap]}
                  <div class="form-row compact">
                    <label class="form-label mono" for="st-cmap-{variable}">{variable}</label>
                    <input
                      id="st-cmap-{variable}"
                      type="text"
                      value={cmap}
                      on:change={(e) => {
                        const updated = { ...s.display.defaultCmapPerVariable, [variable]: e.currentTarget.value };
                        settings.updateSetting('display', { defaultCmapPerVariable: updated });
                      }}
                      placeholder="colormap name"
                    />
                  </div>
                {/each}
              </div>
            </div>

            <div class="section-footer">
              <button class="reset-btn" on:click={() => settings.resetSection('display')}>
                Reset Display
              </button>
            </div>

          <!-- ═══ Data ═══ -->
          {:else if activeTab === 'data'}
            {#if $webUi}
            <div class="settings-section">
              <h3>File Handling</h3>
              <p class="empty-message">
                Files you upload are processed on a shared server and deleted when you close the tab.
                The default directory, auto-load and recent-files settings only apply to the desktop app.
              </p>
            </div>
            {:else}
            <div class="settings-section">
              <h3>File Handling</h3>
              <div class="form-row">
                <label class="form-label" for="st-default-dir">Default directory</label>
                <input
                  id="st-default-dir"
                  type="text"
                  value={s.data.defaultDirectory}
                  on:change={(e) => setDefaultDirectory(e.currentTarget.value)}
                  placeholder="Leave empty for system default"
                  style="flex:1"
                />
              </div>
              <div class="form-row">
                <label class="form-label" for="st-autoload">Auto-load last file</label>
                <label class="toggle-label">
                  <input id="st-autoload" type="checkbox" checked={s.data.autoLoadLast} on:change={setAutoLoadLast} />
                  <span>{s.data.autoLoadLast ? 'Yes' : 'No'}</span>
                </label>
              </div>
            </div>

            <div class="settings-section">
              <div class="section-header">
                <h3>Recent Files</h3>
                {#if s.data.recentFiles.length > 0}
                  <button class="reset-btn small" on:click={() => recentFiles.clear()}>
                    Clear All
                  </button>
                {/if}
              </div>
              {#if s.data.recentFiles.length === 0}
                <p class="empty-message">No recent files</p>
              {:else}
                <div class="recent-list">
                  {#each s.data.recentFiles as file}
                    <div class="recent-item">
                      <div class="recent-info">
                        <span class="recent-name">{file.name}</span>
                        <span class="recent-path mono">{file.path}</span>
                      </div>
                      <div class="recent-meta">
                        <span class="text-muted">{formatRelativeTime(file.openedAt)}</span>
                        <button
                          class="icon-btn-sm"
                          on:click={() => recentFiles.remove(file.path)}
                          title="Remove"
                          aria-label="Remove {file.name} from recent files"
                        >
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
                          </svg>
                        </button>
                      </div>
                    </div>
                  {/each}
                </div>
              {/if}
            </div>

            {/if}

            {#if radarSitesSettings.component}
              <div class="settings-section"><radarSitesSettings.component /></div>
            {/if}

            {#if !$webUi && liveSettings.component}
              <div class="settings-section"><liveSettings.component /></div>
            {/if}

            {#if !$webUi}
              <div class="settings-section"><MoviePackSettings /></div>
            {/if}

            <div class="section-footer">
              <button class="reset-btn" on:click={() => settings.resetSection('data')}>
                Reset Data Settings
              </button>
            </div>

          <!-- ═══ Export ═══ -->
          {:else if activeTab === 'export'}
            <div class="settings-section">
              <h3>Export Defaults</h3>
              <div class="form-row">
                <label class="form-label" for="st-export-format">Format</label>
                <select
                  id="st-export-format"
                  value={s.export_.defaultFormat}
                  on:change={(e) => setExportFormat(e.currentTarget.value as 'png' | 'svg' | 'pdf')}
                >
                  <option value="png">PNG</option>
                  <option value="svg">SVG</option>
                  <option value="pdf">PDF</option>
                </select>
              </div>

              <div class="form-row">
                <label class="form-label" for="st-export-dpi">DPI</label>
                <div class="inline-control">
                  <input
                    id="st-export-dpi"
                    type="number"
                    min="72"
                    max="600"
                    step="1"
                    value={s.export_.dpi}
                    on:change={(e) => { setExportDpi(parseInt(e.currentTarget.value)); e.currentTarget.value = String($settings.export_.dpi); }}
                    style="width:80px"
                  />
                </div>
              </div>

              <div class="form-row">
                <label class="form-label" for="st-export-dir">Output directory</label>
                <input
                  id="st-export-dir"
                  type="text"
                  value={s.export_.outputDirectory}
                  on:change={(e) => setExportDir(e.currentTarget.value)}
                  placeholder="Leave empty for system default"
                  style="flex:1"
                />
              </div>
            </div>

            <div class="section-footer">
              <button class="reset-btn" on:click={() => settings.resetSection('export_')}>
                Reset Export
              </button>
            </div>

          <!-- ═══ Updates (desktop only) ═══ -->
          {:else if activeTab === 'updates' && !isWebMode()}
            <div class="settings-section">
              <UpdatesSettings />
            </div>

          <!-- ═══ Shortcuts ═══ -->
          {:else if activeTab === 'shortcuts'}
            <div class="settings-section">
              <div class="section-header">
                <h3>Keyboard Shortcuts</h3>
                <button class="reset-btn small" on:click={resetAllShortcuts}>
                  Reset All
                </button>
              </div>

              {#if conflictWarning}
                <div class="conflict-warning">{conflictWarning}</div>
              {/if}

              <div class="shortcuts-list">
                {#each SHORTCUT_REGISTRY as def}
                  <div class="shortcut-row">
                    <span class="shortcut-desc">{def.description}</span>
                    <span class="shortcut-category">{def.category}</span>
                    <div class="shortcut-key-area">
                      {#if recordingShortcutId === def.id}
                        <span class="recording-badge">Press keys...</span>
                      {:else}
                        <button
                          class="shortcut-key-btn"
                          on:click={() => startRecording(def.id)}
                          title="Click to rebind"
                        >
                          <kbd>{getBoundKeyDisplay(def.id)}</kbd>
                        </button>
                      {/if}
                      <button
                        class="icon-btn-sm"
                        on:click={() => resetShortcut(def.id)}
                        title="Reset to default"
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                          <polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>
                        </svg>
                      </button>
                    </div>
                  </div>
                {/each}
              </div>
            </div>
          {/if}
        </div>
      </div>

      <!-- Footer -->
      <div class="settings-footer">
        <button class="reset-btn" on:click={() => { if (confirm('Reset every setting (including recent files and shortcuts) to its default?')) settings.resetToDefaults(); }}>
          Reset All Settings
        </button>
        <button class="primary" on:click={close}>Done</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .settings-overlay {
    position: fixed;
    inset: 0;
    z-index: 900;
    background: var(--c-scrim);
    display: flex;
    align-items: center;
    justify-content: center;
    backdrop-filter: blur(4px);
  }

  .settings-dialog {
    width: 640px;
    max-width: 90vw;
    max-height: 80vh;
    position: relative;
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-3);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .settings-dialog::before {
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    width: 64px;
    height: 2px;
    background: var(--c-accent);
    box-shadow: 0 0 10px rgba(var(--c-accent-rgb), 0.7);
  }

  .settings-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-md) var(--spacing-lg);
    border-bottom: 1px solid var(--border-color);
  }

  .settings-header h2 {
    font-size: var(--fs-md);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: var(--ls-wide);
    color: var(--text-primary);
    margin: 0;
  }

  .close-btn {
    background: transparent;
    border: none;
    color: var(--text-muted);
    padding: 4px;
    height: auto;
    cursor: pointer;
    border-radius: var(--radius-sm);
  }

  .close-btn:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .settings-body {
    display: flex;
    flex: 1;
    overflow: hidden;
    min-height: 0;
  }

  /* ─── Tabs ─── */
  .settings-tabs {
    display: flex;
    flex-direction: column;
    width: 140px;
    flex-shrink: 0;
    border-right: 1px solid var(--border-color);
    padding: var(--spacing-sm) 0;
    background: var(--bg-tertiary);
  }

  .tab-btn {
    display: block;
    width: 100%;
    text-align: left;
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: 12px;
    font-weight: 500;
    color: var(--text-secondary);
    background: transparent;
    border: none;
    border-radius: 0;
    cursor: pointer;
    height: auto;
    transition: background var(--transition-fast), color var(--transition-fast);
  }

  .tab-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .tab-btn.active {
    background: var(--bg-active);
    color: var(--accent-hover);
    border-right: 2px solid var(--accent-primary);
  }

  /* ─── Tab content ─── */
  .tab-content {
    flex: 1;
    overflow-y: auto;
    padding: var(--spacing-lg);
    min-height: 360px;
  }

  .settings-section {
    margin-bottom: var(--spacing-xl);
  }

  .settings-section h3 {
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: var(--ls-label);
    color: var(--c-accent);
    margin-bottom: var(--spacing-md);
  }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--spacing-md);
  }

  .section-header h3 {
    margin-bottom: 0;
  }

  .section-hint {
    font-size: 11px;
    color: var(--text-muted);
    margin-bottom: var(--spacing-sm);
  }

  /* ─── Form rows ─── */
  .form-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding: var(--spacing-xs) 0;
    min-height: 32px;
  }

  .form-row.compact {
    padding: 2px 0;
  }

  .form-label {
    font-size: 12px;
    color: var(--text-secondary);
    white-space: nowrap;
    cursor: default;
    flex-shrink: 0;
    min-width: 120px;
  }

  .inline-control {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    font-size: 12px;
    color: var(--text-primary);
    cursor: pointer;
  }

  /* ─── Button group ─── */
  .btn-group {
    display: flex;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }

  .btn-group button {
    background: transparent;
    border: none;
    border-right: 1px solid var(--border-color);
    border-radius: 0;
    color: var(--text-secondary);
    font-size: 11px;
    padding: 4px 12px;
    cursor: pointer;
    height: auto;
    transition: background var(--transition-fast), color var(--transition-fast);
  }

  .btn-group button:last-child {
    border-right: none;
  }

  .btn-group button:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .btn-group button.active {
    background: var(--accent-primary);
    color: var(--c-on-accent);
  }

  /* ─── Color controls ─── */
  .accent-row {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }

  .color-swatch {
    width: 22px;
    height: 22px;
    border-radius: 50%;
    border: 2px solid transparent;
    cursor: pointer;
    padding: 0;
    transition: border-color var(--transition-fast), transform var(--transition-fast);
  }

  .color-swatch:hover {
    transform: scale(1.15);
  }

  .color-swatch.selected {
    border-color: var(--text-primary);
    box-shadow: 0 0 0 2px var(--bg-secondary);
  }

  .color-input {
    width: 28px;
    height: 22px;
    padding: 0;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    cursor: pointer;
    background: transparent;
  }

  /* ─── Range ─── */
  .range-input {
    width: 120px;
    accent-color: var(--accent-primary);
  }

  .range-value {
    font-size: 11px;
    font-variant-numeric: tabular-nums;
    color: var(--text-secondary);
    min-width: 32px;
  }

  /* ─── Colormap grid ─── */
  .cmap-grid {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .cmap-grid input[type="text"] {
    width: 140px;
  }

  /* ─── Recent files ─── */
  .empty-message {
    font-size: 12px;
    color: var(--text-muted);
    font-style: italic;
    padding: var(--spacing-sm) 0;
  }

  .recent-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
    max-height: 240px;
    overflow-y: auto;
  }

  .recent-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
  }

  .recent-item:hover {
    background: var(--bg-hover);
  }

  .recent-info {
    display: flex;
    flex-direction: column;
    min-width: 0;
    flex: 1;
  }

  .recent-name {
    font-size: 12px;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .recent-path {
    font-size: 10px;
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .recent-meta {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex-shrink: 0;
    font-size: 11px;
  }

  .icon-btn-sm {
    background: transparent;
    border: none;
    color: var(--text-muted);
    padding: 2px;
    height: auto;
    cursor: pointer;
    border-radius: var(--radius-sm);
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }

  .icon-btn-sm:hover {
    color: var(--text-primary);
    background: var(--bg-active);
  }

  /* ─── Shortcuts ─── */
  .shortcuts-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .shortcut-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
  }

  .shortcut-row:hover {
    background: var(--bg-hover);
  }

  .shortcut-desc {
    flex: 1;
    font-size: 12px;
    color: var(--text-primary);
  }

  .shortcut-category {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    min-width: 60px;
  }

  .shortcut-key-area {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  .shortcut-key-btn {
    background: transparent;
    border: none;
    padding: 2px 4px;
    height: auto;
    cursor: pointer;
    border-radius: var(--radius-sm);
  }

  .shortcut-key-btn:hover {
    background: var(--bg-active);
  }

  .shortcut-key-btn kbd {
    font-size: 11px;
    pointer-events: none;
  }

  .recording-badge {
    font-size: 11px;
    color: var(--accent-warning);
    background: rgba(var(--c-caution-rgb), 0.15);
    padding: 2px 8px;
    border-radius: var(--radius-sm);
    animation: pulse 1s ease-in-out infinite alternate;
  }

  @keyframes pulse {
    from { opacity: 0.7; }
    to { opacity: 1; }
  }

  .conflict-warning {
    background: rgba(var(--c-alarm-rgb), 0.15);
    color: var(--accent-danger);
    font-size: 11px;
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
    margin-bottom: var(--spacing-sm);
  }

  /* ─── Footer ─── */
  .settings-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-lg);
    border-top: 1px solid var(--border-color);
    background: var(--bg-tertiary);
  }

  .section-footer {
    padding-top: var(--spacing-sm);
    border-top: 1px solid var(--border-color);
  }

  .reset-btn {
    background: transparent;
    border: 1px solid var(--border-color);
    color: var(--text-muted);
    font-size: 11px;
    padding: 4px 10px;
    cursor: pointer;
    border-radius: var(--radius-sm);
    height: auto;
  }

  .reset-btn:hover {
    color: var(--accent-danger);
    border-color: var(--accent-danger);
    background: rgba(var(--c-alarm-rgb), 0.1);
  }

  .reset-btn.small {
    font-size: 10px;
    padding: 2px 8px;
  }
</style>
