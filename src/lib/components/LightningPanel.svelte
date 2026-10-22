<script lang="ts">
  /**
   * Lightning: observed flashes from files you open (GOES GLM Level 2 netCDF, Lightning Mapping Array
   * .dat), drawn in the 2D and 3D views over the radar volume. The time window follows the volume time.
   * Class OBSERVED. Files are read from your disk, nothing is downloaded. The panel states what the
   * data cannot say: GLM has no height and a parallax; detection efficiency falls with range and is
   * not drawn; a time with no file coverage is a gap, not "no lightning".
   */
  import { lightning, lightningSettings, lightningWindow, volumeMs, openLightningFiles, rereadLightning, clearLightning, resetLightningSettings } from '../stores/lightning';
  import { connectionStatus } from '../stores/radarData';
  import {
    coverageNote,
    flashRate,
    flashesText,
    pointRate,
    windowCoverageText,
    windowStats,
    kindLabel,
  } from '../utils/lightningLayer';
  import { formatUtc } from '../utils/provenance';
  import CollapsiblePanel from './CollapsiblePanel.svelte';
  import ProvenanceChip from './ProvenanceChip.svelte';
  import LightningSeries from './LightningSeries.svelte';

  const st = $derived($lightning);
  const data = $derived(st.data);
  const cfg = $derived($lightningSettings);
  const win = $derived($lightningWindow);
  const connected = $derived($connectionStatus === 'connected');

  const stats = $derived(data && win ? windowStats(data, win.start, win.end) : null);
  const covText = $derived(data && win ? windowCoverageText(data, win.start, win.end) : '');
  const rate = $derived(data ? flashRate(data) : null);
  const prate = $derived(data ? pointRate(data) : null);
  const volS = $derived(data && $volumeMs !== null ? ($volumeMs - data.tRefMs) / 1000 : null);
  const par = $derived(data?.meta?.parallax as
    | { assumed_product_height_km: number; uncertainty_cloud_top_error_km: number; uncertainty_shift_km: number; applied_to_km: number | null; assumption: string }
    | undefined);

  async function chooseFiles() {
    let paths: string[] = [];
    try {
      const { open } = await import('@tauri-apps/plugin-dialog');
      const picked = await open({
        multiple: true,
        title: 'Open lightning files (GOES GLM .nc or LMA .dat)',
        filters: [{ name: 'Lightning', extensions: ['nc', 'nc4', 'dat', 'gz', 'bz2'] }],
      });
      paths = Array.isArray(picked) ? (picked as string[]) : picked ? [picked as string] : [];
    } catch {
      const typed = prompt('Full path of a lightning file (GOES GLM .nc or LMA .dat). Several: separate with a semicolon.');
      paths = typed ? typed.split(';').map((p) => p.trim()).filter(Boolean) : [];
    }
    if (paths.length) openLightningFiles(paths);
  }

  function setWindow(key: 'startMin' | 'lengthMin' | 'decayMin', v: number) {
    if (!Number.isFinite(v)) return;
    lightningSettings.update((s) => ({ ...s, window: { ...s.window, [key]: v } }));
  }
  function setOpen(fn: (o: typeof cfg.open) => void) {
    lightningSettings.update((s) => {
      const o = structuredClone(s.open);
      fn(o);
      return { ...s, open: o };
    });
  }

  const utc = (s: number) => (data ? new Date(data.tRefMs + s * 1000).toISOString().slice(11, 19) : '');
  const fmtRate = (v: number) => (Number.isFinite(v) ? `${Number(v.toPrecision(3))} per minute` : 'not given: part of the window has no data');
</script>

<CollapsiblePanel title="Lightning" badge={data ? (data.kind === 'glm' ? 'GLM' : 'LMA') : null}>
  <div class="ltg" data-testid="lightning-panel">
    <div class="actions">
      <button onclick={chooseFiles} disabled={!connected || st.loading} title="GOES-R GLM Level 2 (LCFA) netCDF files, or LMA LYLOUT .dat files, from your disk">Open files...</button>
      {#if data}<button class="quiet" onclick={clearLightning}>Clear</button>{/if}
    </div>

    {#if st.loading}<p class="hint" role="status">Reading the lightning files...</p>{/if}
    {#if st.error}<p class="err" role="alert">{st.error}</p>{/if}

    {#if !data && !st.loading && !st.error}
      <p class="hint">
        Open GOES GLM Level 2 files (<span class="mono">OR_GLM-L2-LCFA_G16_s...nc</span>) or Lightning Mapping Array source files
        (<span class="mono">LYLOUT_...dat</span>) to see observed flashes over the radar, in 2D and in 3D. They are read from your disk; nothing is
        downloaded. With a radar volume open, only the 450 km around the radar are read, and the time window follows the volume time.
      </p>
    {/if}

    {#if data}
      <div class="head">
        <ProvenanceChip provenance={(data.provenance as any) ?? { kind: 'observed', source: data.instrument }} title={kindLabel(data.kind)} />
        <div class="id">
          <strong>{data.instrument}</strong>
          <div class="mono dim">{data.files.length} {data.files.length === 1 ? 'file' : 'files'} · {flashesText(data.nf)}, {data.n} {data.kind === 'glm' ? 'groups' : 'sources'} loaded</div>
        </div>
      </div>
      {#each data.notes as n (n)}<p class="warn">{n}</p>{/each}

      <div class="toggles">
        <label class="chk"><input type="checkbox" checked={cfg.show2d} onchange={(e) => lightningSettings.update((s) => ({ ...s, show2d: e.currentTarget.checked }))} /> Show in 2D</label>
        <label class="chk"><input type="checkbox" checked={cfg.show3d} onchange={(e) => lightningSettings.update((s) => ({ ...s, show3d: e.currentTarget.checked }))} /> Show in 3D</label>
      </div>

      <fieldset>
        <legend>Time window (follows the volume time)</legend>
        <label>Starts {cfg.window.startMin >= 0 ? '+' : ''}{cfg.window.startMin} min from the volume time
          <input type="range" min="-30" max="30" step="0.5" value={cfg.window.startMin} oninput={(e) => setWindow('startMin', +e.currentTarget.value)} />
        </label>
        <label>Lasts {cfg.window.lengthMin} min
          <input type="range" min="0.5" max="30" step="0.5" value={cfg.window.lengthMin} oninput={(e) => setWindow('lengthMin', +e.currentTarget.value)} />
        </label>
        <label>Fades with a time constant of {cfg.window.decayMin} min
          <input type="range" min="0.1" max="15" step="0.1" value={cfg.window.decayMin} oninput={(e) => setWindow('decayMin', +e.currentTarget.value)} />
        </label>
        {#if win}
          <p class="mono dim" data-testid="lightning-window">{utc(win.start)} to {utc(win.end)} UTC</p>
        {:else}
          <p class="hint">Open a radar volume: the window is placed from its valid time.</p>
        {/if}
      </fieldset>

      {#if stats}
        <table class="nums" data-testid="lightning-stats">
          <caption>In the window</caption>
          <tbody>
            <tr><th scope="row">Flashes (by start time)</th><td class="mono">{stats.flashes}</td></tr>
            <tr><th scope="row">{data.kind === 'glm' ? 'Groups' : 'Sources'}</th><td class="mono">{stats.points}</td></tr>
            <tr><th scope="row">Flash rate</th><td class="mono">{fmtRate(stats.ratePerMin)}</td></tr>
            {#if stats.flashes}<tr><th scope="row">{data.kind === 'glm' ? 'Groups' : 'Sources'} per flash</th><td class="mono">{Number(stats.meanPointsPerFlash.toPrecision(3))}</td></tr>{/if}
          </tbody>
        </table>
        {#if covText}<p class="warn">{covText}</p>{/if}
      {/if}

      <LightningSeries series={rate} title="Flash rate" unit="flashes per minute" windowS={win} volumeS={volS} tRefMs={data.tRefMs} />
      <LightningSeries series={prate} title={data.kind === 'glm' ? 'Group rate' : 'Source density in time'} unit={data.kind === 'glm' ? 'groups per minute' : 'sources per minute'} windowS={win} volumeS={volS} tRefMs={data.tRefMs} color="var(--c-text-dim)" />

      {#if data.kind === 'glm' && par}
        <fieldset data-testid="lightning-parallax">
          <legend>Parallax (GLM looks from 35 786 km)</legend>
          <p class="hint">
            A flash high in the cloud is seen displaced away from the satellite. Here each kilometre of cloud-top height error moves a flash about
            {Number((par.uncertainty_shift_km / par.uncertainty_cloud_top_error_km).toPrecision(2))} km; {par.uncertainty_cloud_top_error_km} km of error is a shift of
            about <strong>{Number(par.uncertainty_shift_km.toPrecision(2))} km</strong> at the centre of these data. Positions are drawn as the product gives them
            unless you correct them below.
          </p>
          <label class="chk">
            <input type="checkbox" checked={cfg.open.parallaxOn} onchange={(e) => setOpen((o) => (o.parallaxOn = e.currentTarget.checked))} />
            Move flashes to a cloud-top height of
            <input class="num" type="number" min="0" max="25" step="0.5" value={cfg.open.parallaxKm} disabled={!cfg.open.parallaxOn}
                   onchange={(e) => setOpen((o) => (o.parallaxKm = Math.min(25, Math.max(0, +e.currentTarget.value || 0))))} /> km
          </label>
          <p class="hint">{par.assumption} The correction moves each position along the line of sight from the assumed product height to the height you choose (WGS84 Earth, satellite at the nominal position in the file).
            {#if par.applied_to_km !== null}Applied now: cloud top {par.applied_to_km} km.{:else}Not applied now.{/if}</p>
          <button onclick={rereadLightning} disabled={st.loading || !connected}>Apply</button>
        </fieldset>
      {/if}

      {#if data.kind === 'lma' && data.meta?.link}
        {@const link = cfg.open.link}
        <fieldset data-testid="lightning-clustering">
          <legend>Flash clustering and quality filter</legend>
          <p class="hint">
            Sources are linked into a flash when (distance / D)<sup>2</sup> + (time / T)<sup>2</sup> is at most 1 (single linkage), the space-time metric of Fuchs et al. (2016).
            The defaults are theirs for the Colorado network. Sources are kept when the reduced chi-square is at most the limit and at least the
            minimum number of stations located them.
          </p>
          <div class="grid">
            <label>D, distance (m) <input class="num" type="number" min="100" max="50000" step="100" value={link.distanceM} onchange={(e) => setOpen((o) => (o.link.distanceM = +e.currentTarget.value || 3000))} /></label>
            <label>T, time (s) <input class="num" type="number" min="0.005" max="10" step="0.01" value={link.timeS} onchange={(e) => setOpen((o) => (o.link.timeS = +e.currentTarget.value || 0.15))} /></label>
            <label>Sources needed (min points) <input class="num" type="number" min="1" max="50" step="1" value={link.minPoints} onchange={(e) => setOpen((o) => (o.link.minPoints = Math.max(1, Math.round(+e.currentTarget.value || 1))))} /></label>
            <label>Longest flash (s) <input class="num" type="number" min="0.5" max="60" step="0.5" value={link.maxDurationS ?? ''} placeholder="no limit" onchange={(e) => setOpen((o) => (o.link.maxDurationS = e.currentTarget.value === '' ? null : +e.currentTarget.value))} /></label>
            <label>Chi-square at most <input class="num" type="number" min="0.1" max="50" step="0.1" value={cfg.open.qc.maxChi2} onchange={(e) => setOpen((o) => (o.qc.maxChi2 = +e.currentTarget.value || 1))} /></label>
            <label>Stations at least <input class="num" type="number" min="0" max="30" step="1" value={cfg.open.qc.minStations} onchange={(e) => setOpen((o) => (o.qc.minStations = Math.max(0, Math.round(+e.currentTarget.value || 0))))} /></label>
          </div>
          <label>Metric
            <select value={link.metric} onchange={(e) => setOpen((o) => (o.link.metric = e.currentTarget.value === 'box' ? 'box' : 'ellipsoid'))}>
              <option value="ellipsoid">Space-time ellipsoid (Fuchs et al. 2016)</option>
              <option value="box">Separate limits: distance and time</option>
            </select>
          </label>
          <button onclick={rereadLightning} disabled={st.loading || !connected}>Apply</button>
        </fieldset>
      {/if}

      <details class="about" open>
        <summary>What this shows, and its limits</summary>
        <p class="hint">
          {data.kind === 'glm'
            ? 'Each dot is a GLM group (the centroid of lit 8 km pixels in one 2 ms frame) at the time it was measured; dots of one flash are joined in time order. Flashes and groups are as the GLM Level 2 product grouped them (time threshold in the file: ' + (data.meta?.flash_time_threshold ?? 'not given') + ' s). GLM has no height, so in 3D the dots lie on the ground. Brightness is the radiant energy of the group (log scale), fading with age.'
            : 'Each dot is a located VHF source at its measured time and height (km above sea level, shown above the radar ground in 3D); sources of one flash are joined in time order. Brightness is the source power in dBW, fading with age. Heights come from the array and are not terrain-corrected.'}
        </p>
        <p class="hint">{coverageNote(data)}</p>
        <p class="hint">Brightness scale: {data.strengthInfo.brightnessScale}. Times are UTC. Files: <span class="mono">{data.files.slice(0, 2).join(', ')}{data.files.length > 2 ? ` and ${data.files.length - 2} more` : ''}</span>.</p>
        {#if data.kind === 'lma' && data.meta?.link}
          <p class="hint">Clustering used D = {data.meta.link.distance_m} m, T = {data.meta.link.time_s} s, minimum points {data.meta.link.min_points}, longest flash {data.meta.link.max_duration_s ?? 'no limit'} s, metric {data.meta.link.metric}; quality filter chi-square at most {data.meta.qc?.max_chi2 ?? 'off'}, at least {data.meta.qc?.min_stations ?? 'off'} stations.</p>
        {/if}
        <p class="hint">Valid from {formatUtc(new Date(data.tRefMs + (data.coverage[0]?.[0] ?? 0) * 1000).toISOString())}.</p>
        <p class="hint">Modelled or proxy lightning is a separate layer and is never mixed with these observed flashes.</p>
      </details>
      <button class="quiet-link" onclick={resetLightningSettings}>Reset display settings</button>
    {/if}
  </div>
</CollapsiblePanel>

<style>
  .ltg { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-3); }
  .actions { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .actions .quiet { margin-left: auto; background: transparent; border-color: transparent; color: var(--c-muted); }
  .hint { margin: 0; font-size: var(--fs-xs); color: var(--c-text-dim); line-height: 1.5; }
  .err { margin: 0; font-size: var(--fs-xs); color: var(--c-alarm); }
  .warn { margin: 0; font-size: var(--fs-xs); color: var(--c-caution); line-height: 1.5; }
  .dim { color: var(--c-muted); }
  .mono { font-family: var(--font-mono); font-size: 11px; }
  .head { display: flex; align-items: flex-start; gap: var(--space-2); }
  .id { font-size: var(--fs-sm); line-height: 1.3; }
  .toggles { display: flex; gap: var(--space-4); }
  label { display: flex; flex-direction: column; gap: 2px; font-size: var(--fs-xs); color: var(--c-text-dim); }
  label.chk { flex-direction: row; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
  fieldset { display: flex; flex-direction: column; gap: var(--space-2); margin: 0; padding: var(--space-2) var(--space-3); border: 1px solid var(--c-border); border-radius: var(--radius-sm); }
  legend { font-size: 10.5px; text-transform: uppercase; letter-spacing: var(--ls-label); color: var(--c-muted); padding: 0 4px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-2); }
  input.num { width: 72px; }
  table.nums { width: 100%; border-collapse: collapse; font-size: var(--fs-xs); }
  caption { text-align: left; color: var(--c-muted); text-transform: uppercase; letter-spacing: var(--ls-label); font-size: 10px; padding: 0 0 2px; }
  th { text-align: left; font-weight: 400; color: var(--c-text-dim); padding: 2px 0; }
  td { text-align: right; padding: 2px 0; color: var(--c-text); }
  .about summary { cursor: pointer; color: var(--c-text-dim); text-transform: uppercase; letter-spacing: var(--ls-label); font-size: 10.5px; margin-bottom: var(--space-2); }
  .about .hint + .hint { margin-top: var(--space-2); }
  .quiet-link { align-self: flex-start; background: none; border: none; padding: 0; height: auto; color: var(--c-muted); text-decoration: underline; cursor: pointer; font-size: var(--fs-xs); }
</style>
