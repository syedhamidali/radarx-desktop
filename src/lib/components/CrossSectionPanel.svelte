<script lang="ts">
  /**
   * CrossSectionPanel -- Tabbed panel with Cross-section and Vertical Profile tabs.
   * Controls the line tool, probe mode, variable/colormap selectors, and results.
   */
  import { onMount, onDestroy, untrack } from 'svelte';
  import { get } from 'svelte/store';
  import { connectionStatus, radarData, selectedVariable, processingProgress } from '../stores/radarData';
  import {
    crossSectionStore,
    patchSectionStyle,
    resetCrossSection,
    setCrossSectionLayout,
    type CrossSectionData,
    type VerticalProfileData,
  } from '../stores/crossSection';
  import { haversineKm, sectionPointCount } from '../utils/crossSectionGeometry';
  import { wsManager } from '../utils/websocket';
  import { beginOp, type OpHandle } from '../utils/requestScheduler';
  import { addToast } from '../stores/toasts';
  import { getDefaultCmap, getDefaultRange } from '../utils/ppiRenderer';
  import { COLORMAP_NAMES } from '../utils/colormaps';
  import CollapsiblePanel from './CollapsiblePanel.svelte';
  import CrossSectionViewer from './CrossSectionViewer.svelte';
  import CrossSectionSmoothing from './CrossSectionSmoothing.svelte';
  import ValueMaskControl from './ValueMaskControl.svelte';
  import { isMaskActive, maskOf, valueMasks } from '../stores/valueMask';
  import { smoothingStore } from '../stores/crossSectionSmoothing';
  import { isPlain } from '../utils/crossSectionRaster';
  import VerticalProfile from './VerticalProfile.svelte';

  // Derived from stores
  const isConnected = $derived($connectionStatus === 'connected');
  const hasData = $derived($radarData.variables.length > 0);
  const variables = $derived($radarData.variables);
  const filePath = $derived($radarData.filePath);
  const activeTab = $derived($crossSectionStore.activeTab);
  const lineToolActive = $derived($crossSectionStore.lineToolActive);
  const probeToolActive = $derived($crossSectionStore.probeToolActive);
  const csData = $derived($crossSectionStore.crossSectionData);
  const vpProfiles = $derived($crossSectionStore.verticalProfiles);
  const isLoading = $derived($crossSectionStore.isLoading);

  // Cross-section style: shared with the large view (dock / dialog), so it lives in the store.
  const style = $derived($crossSectionStore.style);
  const csVariable = $derived(style.variable);
  const csCmap = $derived(style.cmap);
  const csVmin = $derived(style.vmin);
  const csVmax = $derived(style.vmax);
  const layout = $derived($crossSectionStore.layout);
  const rhiToolActive = $derived($crossSectionStore.rhiToolActive);
  // An emptied number box reads as null; the viewer always gets a usable range.
  const rangeMin = $derived(Number.isFinite(csVmin) ? csVmin : 0);
  const rangeMax = $derived(Number.isFinite(csVmax) && csVmax !== rangeMin ? csVmax : rangeMin + 1);

  // Vertical profile state
  let vpVariables = $state<string[]>([]);
  /** Where the profile currently shown was taken (to tell one probe from the next). */
  let probeKey = '';
  /** Results of the current probe still to come (extra variables requested by this panel). */
  let vpPending = 0;

  function setLoading(v: boolean) {
    crossSectionStore.update((s) => (s.isLoading === v ? s : { ...s, isLoading: v }));
  }

  function updateCsDefaults(v: string) {
    const range = getDefaultRange(v);
    patchSectionStyle(
      { variable: v, cmap: getDefaultCmap(v), ...(range ? { vmin: range[0], vmax: range[1] } : {}) },
      { link: true },
    );
  }

  function onVariableChange(e: Event) {
    updateCsDefaults((e.currentTarget as HTMLSelectElement).value);
    if ($crossSectionStore.line) requestCrossSection();
  }
  function onRangeChange(which: 'vmin' | 'vmax', e: Event) {
    const v = parseFloat((e.currentTarget as HTMLInputElement).value);
    if (Number.isFinite(v)) patchSectionStyle({ [which]: v });
  }

  // While linked, the section's colormap and range follow the PPI's (same variable only).
  $effect(() => {
    const pp = $crossSectionStore.ppiStyle;
    untrack(() => {
      const st = get(crossSectionStore);
      if (!pp || !st.styleLinked || pp.variable !== st.style.variable) return;
      if (pp.cmap !== st.style.cmap || pp.vmin !== st.style.vmin || pp.vmax !== st.style.vmax) {
        patchSectionStyle({ cmap: pp.cmap, vmin: pp.vmin, vmax: pp.vmax }, { link: true });
      }
    });
  });

  // A different file: forget the line, results and selections that belonged to the old one.
  let lastFile: string | null | undefined = undefined;
  $effect(() => {
    const f = filePath;
    untrack(() => {
      if (lastFile !== undefined && f !== lastFile) {
        resetCrossSection();
        probeKey = '';
        vpPending = 0;
        patchSectionStyle({ variable: '' }, { link: true });
        vpVariables = [];
      }
      lastFile = f;
    });
  });

  // Keep the selections valid for the open file.
  $effect(() => {
    const vars = variables;
    const sel = $selectedVariable;
    untrack(() => {
      if (vars.length === 0) return;
      const cur = get(crossSectionStore).style.variable;
      if (!cur || !vars.includes(cur)) {
        updateCsDefaults(sel && vars.includes(sel) ? sel : vars[0]);
      }
      const kept = vpVariables.filter((v) => vars.includes(v));
      if (kept.length === 0) vpVariables = [sel && vars.includes(sel) ? sel : vars[0]];
      else if (kept.length !== vpVariables.length) vpVariables = kept;
    });
  });

  // --- Loading state as an operation (shown in the tray, with Cancel and a timeout) ---
  let loadOp: OpHandle | null = null;

  function endLoadOp(failure?: string) {
    const op = loadOp;
    loadOp = null;
    if (!op) return;
    if (failure) op.fail(failure);
    else op.done();
  }

  $effect(() => {
    const loading = isLoading;
    untrack(() => {
      if (loading && !loadOp) {
        const probing = $crossSectionStore.probeToolActive || $crossSectionStore.activeTab === 'vertical-profile';
        const op: OpHandle = beginOp({
          kind: 'profile',
          label: probing ? 'Extracting vertical profile' : 'Extracting cross-section',
          timeoutMs: 45000,
          onCancel: () => {
            loadOp = null;
            vpPending = 0;
            setLoading(false);
          },
          onTimeout: () => {
            loadOp = null;
            vpPending = 0;
            setLoading(false);
          },
        });
        loadOp = op;
      } else if (!loading && loadOp) {
        endLoadOp();
      }
    });
  });

  // The PPI line overlay's "Request Cross-Section" button asks for the same request as ours.
  let lastRequestSeq = $crossSectionStore.requestSeq;
  $effect(() => {
    const seq = $crossSectionStore.requestSeq;
    untrack(() => {
      if (seq !== lastRequestSeq) {
        lastRequestSeq = seq;
        requestCrossSection();
      }
    });
  });

  // WebSocket message handlers
  /** A new request came in while one was running; send it as soon as that one is done. */
  let rerunPending = false;
  let unsubCS: (() => void) | null = null;
  let unsubVP: (() => void) | null = null;
  let unsubErr: (() => void) | null = null;

  function clearProgress() {
    processingProgress.update((p) => (p && p.percent < 100 ? null : p));
  }

  onMount(() => {
    unsubCS = wsManager.onMessage('cross_section_result', (msg: any) => {
      const data: CrossSectionData = {
        distance_km: msg.distance_km ?? [],
        height_km: msg.height_km ?? [],
        values: msg.values ?? [],
        variable: msg.variable ?? '',
        units: msg.units ?? '',
        n_sweeps: msg.n_sweeps ?? 0,
        elevations_deg: msg.elevations_deg,
        ground_range_km: msg.ground_range_km,
        azimuth_deg: msg.azimuth_deg,
        start: msg.start,
        end: msg.end,
        radar_lat: msg.radar_lat,
        radar_lon: msg.radar_lon,
        radar_altitude_m: msg.radar_altitude_m,
        time: msg.time,
        rhi_azimuth: typeof msg.rhi_azimuth === 'number' ? msg.rhi_azimuth : undefined,
      };
      crossSectionStore.update((s) => {
        // An RHI-style cut: draw the line exactly where the engine put it.
        const line =
          data.rhi_azimuth !== undefined && s.line && data.start && data.end
            ? { ...s.line, startLat: data.start[0], startLon: data.start[1], endLat: data.end[0], endLon: data.end[1] }
            : s.line;
        return { ...s, line, crossSectionData: data.n_sweeps > 0 ? data : null, isLoading: false };
      });
      clearProgress();
      if (data.n_sweeps > 0) {
        if (!rerunPending) addToast('success', `Cross-section: ${data.n_sweeps} sweeps, ${data.distance_km.length} points`);
      } else {
        addToast('info', `${data.variable || 'This variable'} is not in any sweep of this file`);
      }
      // The line was moved while this one was being extracted: run it again for the latest line.
      if (rerunPending) {
        rerunPending = false;
        requestCrossSection();
      }
    });

    unsubVP = wsManager.onMessage('vertical_profile_result', (msg: any) => {
      const profile: VerticalProfileData = {
        lat: typeof msg.lat === 'number' ? msg.lat : undefined,
        lon: typeof msg.lon === 'number' ? msg.lon : undefined,
        heights_km: msg.heights_km ?? [],
        values: msg.values ?? [],
        elevations_deg: msg.elevations_deg ?? [],
        variable: msg.variable ?? '',
        units: msg.units ?? '',
      };
      const key = profile.lat !== undefined && profile.lon !== undefined
        ? `${profile.lat.toFixed(5)},${profile.lon.toFixed(5)}`
        : '';
      const newProbe = key !== probeKey;
      if (newProbe) {
        probeKey = key;
        vpPending = 0;
        // A click on the PPI asks for the viewed variable only; fetch the other chosen ones here.
        if (key) {
          for (const v of vpVariables) {
            if (v === profile.variable) continue;
            vpPending++;
            wsManager.send({ type: 'get_vertical_profile', lat: profile.lat, lon: profile.lon, variable: v });
          }
        }
      } else if (vpPending > 0) {
        vpPending--;
      }
      crossSectionStore.update((s) => {
        // Same variable replaces its previous profile; a new location replaces all of them.
        const base = newProbe ? [] : s.verticalProfiles.filter((p) => p.variable !== profile.variable);
        return {
          ...s,
          verticalProfiles: [...base, profile],
          isLoading: vpPending > 0,
        };
      });
      clearProgress();
      if (newProbe || vpPending === 0) {
        const n = profile.values.filter((v) => Number.isFinite(v)).length;
        if (n === 0) addToast('info', `${profile.variable} has no data at this point`);
        else if (vpPending === 0) addToast('success', `Vertical profile: ${profile.heights_km.length} levels`);
      }
    });

    unsubErr = wsManager.onMessage('error', (msg: any) => {
      if (!loadOp) return;
      const text = typeof msg?.message === 'string' ? msg.message : '';
      const named = /\b((?:get|run|identify|track|list)_[a-z0-9_]+)/.exec(text)?.[1];
      if (named && named !== 'get_cross_section' && named !== 'get_vertical_profile') return;
      vpPending = 0;
      rerunPending = false;
      const failure = text || 'Request failed';
      endLoadOp(failure);
      setLoading(false);
    });
  });

  onDestroy(() => {
    unsubCS?.();
    unsubVP?.();
    unsubErr?.();
    endLoadOp();
  });

  function setTab(tab: 'cross-section' | 'vertical-profile') {
    crossSectionStore.update((s) => ({ ...s, activeTab: tab }));
  }

  function toggleLineTool() {
    const next = !lineToolActive;
    crossSectionStore.update((s) => ({
      ...s,
      lineToolActive: next,
      probeToolActive: false,
      rhiToolActive: false,
    }));
  }

  function toggleRhiTool() {
    crossSectionStore.update((s) => ({
      ...s,
      rhiToolActive: !s.rhiToolActive,
      lineToolActive: false,
      probeToolActive: false,
    }));
  }

  function toggleProbeTool() {
    const next = !probeToolActive;
    crossSectionStore.update((s) => ({
      ...s,
      probeToolActive: next,
      lineToolActive: false,
      rhiToolActive: false,
    }));
  }

  function requestCrossSection() {
    const st = $crossSectionStore;
    const line = st.line;
    if (!line) {
      addToast('info', 'Draw a line on the PPI first');
      return;
    }
    if (!csVariable) {
      addToast('info', 'Select a variable');
      return;
    }
    if ($connectionStatus !== 'connected') return;
    if (st.isLoading) {
      rerunPending = true;
      return;
    }

    crossSectionStore.update((s) => ({ ...s, isLoading: true }));
    const lengthKm = st.rhi
      ? st.rhi.maxRangeKm
      : haversineKm(line.startLat, line.startLon, line.endLat, line.endLon);
    const n_points = sectionPointCount(lengthKm);
    if (st.rhi) {
      wsManager.send({
        type: 'get_cross_section',
        azimuth: st.rhi.azimuth,
        max_range_km: st.rhi.maxRangeKm,
        variable: csVariable,
        n_points,
      });
    } else {
      wsManager.send({
        type: 'get_cross_section',
        start: [line.startLat, line.startLon],
        end: [line.endLat, line.endLon],
        variable: csVariable,
        n_points,
      });
    }
  }

  function clearCrossSection() {
    crossSectionStore.update((s) => ({
      ...s,
      crossSectionData: null,
      line: null,
      rhi: null,
      lineToolActive: false,
      rhiToolActive: false,
    }));
  }

  function clearProfiles() {
    probeKey = '';
    crossSectionStore.update((s) => ({
      ...s,
      verticalProfiles: [],
    }));
  }

  function toggleVpVariable(v: string) {
    if (vpVariables.includes(v)) {
      vpVariables = vpVariables.filter((x) => x !== v);
    } else {
      vpVariables = [...vpVariables, v];
    }
  }

  function fmtLatLon(lat: number, lon: number): string {
    return `${Math.abs(lat).toFixed(2)}\u00B0${lat >= 0 ? 'N' : 'S'} ${Math.abs(lon).toFixed(2)}\u00B0${lon >= 0 ? 'E' : 'W'}`;
  }
</script>

<CollapsiblePanel title="Cross-Section / Profile" icon="///">
  <div class="csp-root">
    <!-- Tab bar -->
    <div class="csp-tabs">
      <button
        class="csp-tab"
        class:active={activeTab === 'cross-section'}
        on:click={() => setTab('cross-section')}
      >
        Cross-Section
      </button>
      <button
        class="csp-tab"
        class:active={activeTab === 'vertical-profile'}
        on:click={() => setTab('vertical-profile')}
      >
        Vertical Profile
      </button>
    </div>

    <!-- Cross-section tab -->
    {#if activeTab === 'cross-section'}
      <div class="csp-section">
        <div class="csp-controls">
          <div class="csp-row">
            <label class="csp-label" for="csp-var">Variable</label>
            <select id="csp-var" class="csp-select" value={csVariable} disabled={!hasData} on:change={onVariableChange}>
              {#each variables as v}
                <option value={v}>{v}</option>
              {/each}
            </select>
          </div>

          <div class="csp-row">
            <label class="csp-label" for="csp-cmap">Colormap</label>
            <select id="csp-cmap" class="csp-select" value={csCmap} on:change={(e) => patchSectionStyle({ cmap: e.currentTarget.value })}>
              {#each COLORMAP_NAMES as c}
                <option value={c}>{c}</option>
              {/each}
            </select>
          </div>

          <div class="csp-row">
            <label class="csp-label" for="csp-vmin">Range</label>
            <div class="csp-range-inputs">
              <input id="csp-vmin" type="number" class="csp-input" value={csVmin} on:change={(e) => onRangeChange('vmin', e)} step="1" aria-label="Colour range minimum" />
              <span class="csp-range-sep">-</span>
              <input id="csp-vmax" type="number" class="csp-input" value={csVmax} on:change={(e) => onRangeChange('vmax', e)} step="1" aria-label="Colour range maximum" />
            </div>
          </div>
        </div>

        <details class="csp-smoothing" open={isMaskActive(maskOf($valueMasks, csData?.variable || csVariable))}>
          <summary>Mask{isMaskActive(maskOf($valueMasks, csData?.variable || csVariable)) ? ': on' : ''}</summary>
          <ValueMaskControl variable={csData?.variable || csVariable} units={csData?.units ?? ''} compact />
        </details>

        <details class="csp-smoothing" open={!isPlain($smoothingStore)}>
          <summary>Smoothing{isPlain($smoothingStore) ? '' : ': on'}</summary>
          <CrossSectionSmoothing compact />
        </details>

        <div class="csp-actions">
          <button
            class="csp-btn"
            class:active={lineToolActive}
            on:click={toggleLineTool}
            disabled={!hasData || !isConnected}
            title="Draw a line on the PPI to define the cross-section"
          >
            {lineToolActive ? 'Drawing...' : 'Draw Line'}
          </button>

          <button
            class="csp-btn"
            class:active={rhiToolActive}
            on:click={toggleRhiTool}
            disabled={!hasData || !isConnected}
            title="Click the PPI to cut a section out of the radar along that azimuth (RHI-style)"
          >
            {rhiToolActive ? 'Click PPI...' : 'RHI'}
          </button>

          <button
            class="csp-btn primary"
            on:click={requestCrossSection}
            disabled={!$crossSectionStore.line || !isConnected || isLoading}
          >
            {isLoading ? 'Loading...' : 'Request'}
          </button>

          {#if csData}
            <button class="csp-btn muted" on:click={clearCrossSection}>Clear</button>
          {/if}
        </div>

        {#if csData}
          <div class="csp-caption">
            {csData.variable}: {csData.n_sweeps} sweeps, {csData.distance_km[csData.distance_km.length - 1]?.toFixed(1) ?? '0'} km long
          </div>
        {/if}

        <div class="csp-actions">
          <button class="csp-btn primary" on:click={() => setCrossSectionLayout('modal')} disabled={!hasData}
                  title="Open the cross-section in a large window" data-testid="cs-panel-expand">Expand</button>
          <button class="csp-btn" class:active={layout === 'dock'}
                  on:click={() => setCrossSectionLayout(layout === 'dock' ? 'panel' : 'dock')} disabled={!hasData}
                  title="Show a large cross-section under the PPI, with a height you can drag" data-testid="cs-panel-dock">
            {layout === 'dock' ? 'Undock' : 'Dock under PPI'}
          </button>
        </div>

        <!-- Viewer -->
        <div class="csp-viewer">
          <CrossSectionViewer
            data={csData}
            cmapName={csCmap}
            vmin={rangeMin}
            vmax={rangeMax}
          />
        </div>
      </div>
    {/if}

    <!-- Vertical profile tab -->
    {#if activeTab === 'vertical-profile'}
      <div class="csp-section">
        <div class="csp-controls">
          <div class="csp-row">
            <span class="csp-label" id="csp-vars-label">Variables</span>
            <div class="csp-var-chips" role="group" aria-labelledby="csp-vars-label">
              {#each variables as v}
                <button
                  class="csp-chip"
                  class:selected={vpVariables.includes(v)}
                  aria-pressed={vpVariables.includes(v)}
                  on:click={() => toggleVpVariable(v)}
                >
                  {v}
                </button>
              {/each}
            </div>
          </div>
        </div>

        <div class="csp-actions">
          <button
            class="csp-btn"
            class:active={probeToolActive}
            on:click={toggleProbeTool}
            disabled={!hasData || !isConnected}
            title="Click on the PPI to get a vertical profile"
          >
            {probeToolActive ? 'Click PPI...' : 'Probe Mode'}
          </button>

          {#if vpProfiles.length > 0}
            <button class="csp-btn muted" on:click={clearProfiles}>Clear</button>
          {/if}
        </div>

        {#if vpProfiles[0]?.lat !== undefined && vpProfiles[0]?.lon !== undefined}
          <div class="csp-caption">Profile at {fmtLatLon(vpProfiles[0].lat, vpProfiles[0].lon)}</div>
        {/if}

        <!-- Viewer -->
        <div class="csp-viewer">
          <VerticalProfile profiles={vpProfiles} />
        </div>
      </div>
    {/if}
  </div>
</CollapsiblePanel>

<style>
  .csp-root {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm, 6px);
  }

  .csp-tabs {
    display: flex;
    border: 1px solid var(--glass-border, rgba(255,255,255,0.08));
    border-radius: var(--radius-sm, 4px);
    overflow: hidden;
  }

  .csp-tab {
    flex: 1;
    padding: 6px 0;
    font-size: 11px;
    font-weight: 600;
    text-align: center;
    background: transparent;
    color: var(--text-secondary, rgba(255,255,255,0.6));
    border: none;
    border-radius: 0;
    cursor: pointer;
    transition: all 150ms ease;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
    height: auto;
  }

  .csp-tab:not(:last-child) {
    border-right: 1px solid var(--glass-border, rgba(255,255,255,0.08));
  }

  .csp-tab:hover {
    background: rgba(91, 108, 247, 0.06);
    color: var(--text-primary, #fff);
  }

  .csp-tab.active {
    background: rgba(91, 108, 247, 0.15);
    color: var(--accent-hover, #7b8aff);
  }

  .csp-section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm, 6px);
  }

  .csp-controls {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs, 4px);
  }

  .csp-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 6px);
  }

  .csp-label {
    font-size: 11px;
    color: var(--text-muted, rgba(255,255,255,0.4));
    text-transform: uppercase;
    letter-spacing: 0.03em;
    font-weight: 600;
    min-width: 60px;
    flex-shrink: 0;
  }

  .csp-select {
    flex: 1;
    min-width: 0;
  }

  .csp-range-inputs {
    display: flex;
    align-items: center;
    gap: 4px;
    flex: 1;
  }

  .csp-input {
    width: 60px;
    padding: 3px 6px;
    font-size: 11px;
    font-family: var(--font-mono, monospace);
    background: var(--bg-primary, #0f0f1a);
    border: 1px solid var(--border-color, rgba(255,255,255,0.1));
    border-radius: var(--radius-sm, 4px);
    color: var(--text-primary, #fff);
  }

  .csp-range-sep {
    color: var(--text-muted, rgba(255,255,255,0.3));
    font-size: 11px;
  }

  .csp-actions {
    display: flex;
    gap: var(--spacing-xs, 4px);
    flex-wrap: wrap;
  }

  .csp-btn {
    padding: 5px 12px;
    font-size: 11px;
    font-weight: 600;
    border-radius: var(--radius-sm, 4px);
    border: 1px solid var(--border-color, rgba(255,255,255,0.1));
    background: var(--bg-surface, rgba(15, 15, 26, 0.6));
    color: var(--text-secondary, rgba(255,255,255,0.7));
    cursor: pointer;
    transition: all 150ms ease;
    white-space: nowrap;
  }

  .csp-btn:hover:not(:disabled) {
    background: rgba(91, 108, 247, 0.1);
    border-color: rgba(91, 108, 247, 0.2);
    color: var(--text-primary, #fff);
  }

  .csp-btn.active {
    background: rgba(91, 108, 247, 0.2);
    border-color: rgba(91, 108, 247, 0.4);
    color: var(--accent-hover, #7b8aff);
  }

  .csp-btn.primary {
    background: rgba(91, 108, 247, 0.15);
    border-color: rgba(91, 108, 247, 0.3);
    color: var(--accent-hover, #7b8aff);
  }

  .csp-btn.primary:hover:not(:disabled) {
    background: rgba(91, 108, 247, 0.3);
    color: white;
  }

  .csp-btn.muted {
    color: var(--text-muted, rgba(255,255,255,0.35));
  }

  .csp-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .csp-var-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 3px;
    flex: 1;
  }

  .csp-chip {
    padding: 2px 8px;
    font-size: 10px;
    font-family: var(--font-mono, monospace);
    font-weight: 600;
    border: 1px solid var(--border-color, rgba(255,255,255,0.1));
    border-radius: 10px;
    background: transparent;
    color: var(--text-muted, rgba(255,255,255,0.4));
    cursor: pointer;
    transition: all 150ms ease;
    height: auto;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .csp-chip:hover {
    border-color: rgba(91, 108, 247, 0.3);
    color: var(--text-secondary, rgba(255,255,255,0.7));
  }

  .csp-chip.selected {
    background: rgba(91, 108, 247, 0.15);
    border-color: rgba(91, 108, 247, 0.4);
    color: var(--accent-hover, #7b8aff);
  }

  .csp-caption {
    font-size: 10px;
    font-family: var(--font-mono, monospace);
    color: var(--text-muted, rgba(255,255,255,0.4));
  }

  .csp-smoothing summary {
    cursor: pointer;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--text-muted, rgba(255,255,255,0.4));
    margin-bottom: 4px;
  }

  .csp-viewer {
    height: 250px;
    border: 1px solid var(--glass-border, rgba(255,255,255,0.08));
    border-radius: var(--radius-sm, 4px);
    overflow: hidden;
  }
</style>
