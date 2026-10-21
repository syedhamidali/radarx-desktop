<script lang="ts">
  /**
   * Movie Studio: make a storm movie (MP4) without a terminal. Six steps on one page: what to film,
   * the plan (nothing is drawn), the focus and camera (with a preview still), the look, the sound
   * (synthesised, labelled), and the render with its result. Every step calls the same engine as
   * `rxd animate`; the record written next to the film says how it was made.
   */
  import { onMount } from 'svelte';
  import { fade, fly } from 'svelte/transition';
  import { focusTrap } from '../utils/focusTrap';
  import { isWebMode } from '../utils/env';
  import { openFiles } from '../stores/fileManager';
  import { PRESETS } from '../utils/lookConfig';
  import {
    movieStudioOpen, movieInputs, movieOptions, capabilities, capabilitiesError,
    planJob, plan, stillJob, still, renderJob, result, videoUrl, anyRunning,
    closeMovieStudio, loadCapabilities, addInputs, removeInput, pickFolder, pickFiles, pickGlmFolder,
    requestPlan, requestStill, startRender, cancel, cancelRender, exportCopy, showInFolder, clearResults,
    type CameraPreset,
  } from '../stores/movieStudio';

  const web = isWebMode();
  const caps = $derived($capabilities);
  const o = $derived($movieOptions);
  const p = $derived($plan);
  const r = $derived($result);
  const canRender = $derived(!!caps?.ready && $movieInputs.length > 0 && !$anyRunning);
  const canPlan = $derived(!!caps?.plan_ready && $movieInputs.length > 0 && !$anyRunning);
  let stillAt = $state(12);
  let showReport = $state(false);
  let showRecord = $state(false);
  let root: HTMLElement | undefined = $state();

  const CAMERAS: { id: CameraPreset; label: string; about: string }[] = [
    { id: 'orbit', label: 'Orbit', about: 'a wide oblique view, a slow orbit, a low pass along the storm motion, a top-down view and back' },
    { id: 'top-down', label: 'Top-down', about: 'straight down, north up, slowly closing in and out' },
    { id: 'window', label: 'Window (low)', about: 'a camera on the ground beside the storm track, true scale' },
    { id: 'wide', label: 'Wide', about: 'a slower, wider orbit for a system a few hundred km across' },
    { id: 'flight', label: 'Flight', about: 'the cyclone flight: in along a rainband, through the eyewall, into the eye (use with the eye tracker)' },
  ];
  const realisticPresets = PRESETS.filter((x) => x.look.style === 'realistic');

  onMount(() => {
    if (!caps) loadCapabilities();
    // The files already open are the natural thing to film.
    if ($movieInputs.length === 0) addInputs($openFiles.map((f) => f.path).filter(Boolean));
    root?.focus();
  });

  function set<K extends keyof typeof o>(key: K, value: (typeof o)[K]) {
    movieOptions.update((x) => ({ ...x, [key]: value }));
  }
  function onKey(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      closeMovieStudio();
    }
  }
  function useOpenFiles() {
    addInputs($openFiles.map((f) => f.path).filter(Boolean));
  }
  function base(path: string): string {
    return path.split(/[\\/]/).pop() ?? path;
  }
  function mins(s: number): string {
    return `${(s / 60).toFixed(1)} min`;
  }
  function hhmm(iso: string): string {
    return iso.slice(11, 19);
  }
  function mb(n: number | null | undefined): string {
    return n ? `${(n / 1e6).toFixed(1)} MB` : '';
  }
  function previewAt(t: number) {
    stillAt = t;
    requestStill(t);
  }
  const pct = (j: { percent: number }) => Math.max(0, Math.min(100, j.percent));
</script>

{#if $movieStudioOpen}
  <div class="ms-overlay" transition:fade={{ duration: 150 }}>
    <div
      class="ms-dialog brackets-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="ms-title"
      tabindex="-1"
      bind:this={root}
      onkeydown={onKey}
      transition:fly={{ y: 16, duration: 220 }}
      use:focusTrap={{ initial: '.ms-dialog' }}
    >
      <header class="head">
        <div>
          <h2 id="ms-title">Movie Studio</h2>
          <div class="sub">A storm in 3D over time, from radar files on disk. The film is an MP4; the record of how it was made is written next to it.</div>
        </div>
        <button class="icon" onclick={closeMovieStudio} aria-label="Close" title="Close (Esc)">&times;</button>
      </header>

      <div class="body">
        <!-- 0. What this needs -->
        {#if web}
          <p class="err">The Movie Studio renders on this computer with Chrome: it is available in the desktop app only.</p>
        {:else if $capabilitiesError}
          <p class="err">{$capabilitiesError} <button class="link" onclick={loadCapabilities}>Check again</button></p>
        {:else if !caps}
          <p class="dim">Checking what the renderer needs...</p>
        {:else if !caps.ready}
          <div class="needs">
            <div class="needs-title">Before a film can be rendered</div>
            <ul>
              {#each caps.problems as pr}
                <li><span class="what">{pr.what}</span><br /><span class="hint mono">{pr.hint}</span></li>
              {/each}
            </ul>
            <div class="row">
              <button onclick={loadCapabilities}>Check again</button>
              {#if caps.plan_ready}<span class="dim small">The plan (step 2) works without these.</span>{/if}
            </div>
          </div>
        {/if}

        <!-- 1. What to film -->
        <section>
          <h3><span class="n">1</span> What to film</h3>
          <p class="lede">Several volumes of one radar, or several radars over time: the files already open, more files, or a folder (a case folder with one sub-folder per radar works too).</p>
          <div class="row">
            <button onclick={useOpenFiles} disabled={$openFiles.length === 0}>Use the {$openFiles.length} open file{$openFiles.length === 1 ? '' : 's'}</button>
            {#if !web}
              <button onclick={pickFolder}>Add a folder...</button>
              <button onclick={pickFiles}>Add files...</button>
            {/if}
          </div>
          {#if $movieInputs.length}
            <ul class="inputs">
              {#each $movieInputs as path (path)}
                <li><span class="mono" title={path}>{base(path)}</span><button class="link" onclick={() => removeInput(path)}>remove</button></li>
              {/each}
            </ul>
          {:else}
            <p class="dim small">Nothing chosen yet.</p>
          {/if}
          <div class="grid2">
            <label>From (UTC) <input type="text" placeholder="2021-12-11T02:00:00Z" value={o.start} oninput={(e) => set('start', e.currentTarget.value)} /></label>
            <label>To (UTC) <input type="text" placeholder="leave empty for the last volume" value={o.end} oninput={(e) => set('end', e.currentTarget.value)} /></label>
          </div>
        </section>

        <!-- 2. The plan -->
        <section>
          <h3><span class="n">2</span> The plan</h3>
          <p class="lede">Nothing is drawn: the time steps, the storm's track, how many frames are observed and how many interpolated, and the camera.</p>
          <div class="row">
            <button class="primary" onclick={requestPlan} disabled={!canPlan}>Plan the film</button>
            {#if $planJob.running}<button onclick={() => cancel($planJob.requestId)}>Cancel</button>{/if}
          </div>
          {#if $planJob.running}
            <div class="progress"><div class="bar"><div class="fill" style="width:{pct($planJob)}%"></div></div><div class="msg">{$planJob.message}</div></div>
          {/if}
          {#if $planJob.error}<p class="err">{$planJob.error}</p>{/if}
          {#if p}
            <dl class="facts">
              <dt>Radars</dt><dd>{p.inputs.used_radars.join(', ')} (reference {p.inputs.reference})</dd>
              <dt>Time steps</dt><dd>{p.steps.length} over {mins(p.storm.span_s)}: {p.steps.map((s) => hhmm(s.time)).join(', ')} UTC</dd>
              <dt>Storm track</dt><dd>moved {p.storm.displacement_km.toFixed(1)} km toward {p.storm.heading_deg.toFixed(0)} deg (mean {p.storm.mean_speed_ms.toFixed(1)} m/s), tracker "{p.storm.tracker}"; up to {Math.max(...p.storm.track.map((t) => t.max_dbz)).toFixed(0)} dBZ, tops to {Math.max(...p.storm.track.map((t) => t.top_km)).toFixed(0)} km</dd>
              <dt>Frames</dt><dd>{p.frames.frames} ({p.frames.duration_s} s at {p.frames.fps} fps, {p.frames.size[0]}x{p.frames.size[1]}): {p.frames.observed_frames} observed, {p.frames.interpolated_frames} interpolated ({(100 * p.frames.interpolated_share).toFixed(0)}%); time-lapse x{p.frames.timelapse.toFixed(0)}</dd>
              <dt>Camera</dt><dd>{p.camera.mode} ({p.camera.source}), follows {(100 * p.camera.target_lag).toFixed(0)}% of the storm's move</dd>
              {#if p.inputs.skipped.length}<dt>Not used</dt><dd>{p.inputs.skipped.length} file{p.inputs.skipped.length === 1 ? '' : 's'}: {p.inputs.skipped.slice(0, 3).map((s) => `${s.what} (${s.reason})`).join('; ')}{p.inputs.skipped.length > 3 ? '; ...' : ''}</dd>{/if}
            </dl>
            {#each p.warnings as w}<p class="warn">{w}</p>{/each}
            <p class="note">Frames between two volumes are interpolated (the echo carried along the storm motion), not observed; the film says so on every such frame.</p>
            <button class="link" onclick={() => (showReport = !showReport)}>{showReport ? 'Hide' : 'Show'} the full report</button>
            {#if showReport}<pre class="report">{p.report}</pre>{/if}
          {/if}
        </section>

        <!-- 3. Focus and camera -->
        <section>
          <h3><span class="n">3</span> Focus and camera</h3>
          <div class="choices">
            <label class="choice" class:on={o.focus === 'storm'}><input type="radio" name="focus" checked={o.focus === 'storm'} onchange={() => set('focus', 'storm')} /> Zoom on the strongest storm</label>
            <label class="choice" class:on={o.focus === 'scene'}><input type="radio" name="focus" checked={o.focus === 'scene'} onchange={() => set('focus', 'scene')} /> The whole scene</label>
          </div>
          <div class="grid3">
            <label>Box half-width (km) <input type="number" min="10" max="400" step="5" value={o.cropKm} oninput={(e) => set('cropKm', Number(e.currentTarget.value))} /></label>
            <label>Storm tracker
              <select value={o.track} onchange={(e) => set('track', e.currentTarget.value as any)}>
                <option value="cells">cells (default)</option><option value="linear">linear fit</option><option value="eye">eye of a cyclone</option>
              </select>
            </label>
            <label>Cell threshold (dBZ) <input type="number" min="20" max="70" step="1" value={o.cellDbz} oninput={(e) => set('cellDbz', Number(e.currentTarget.value))} /></label>
          </div>
          <div class="cams">
            {#each CAMERAS as c}
              <button class="cam" class:on={o.camera === c.id} onclick={() => set('camera', c.id)} title={c.about}>{c.label}</button>
            {/each}
          </div>
          <p class="dim small">{CAMERAS.find((c) => c.id === o.camera)?.about}</p>
          <div class="grid3">
            <label>Length (s) <input type="number" min="5" max="600" step="1" value={o.duration} oninput={(e) => set('duration', Number(e.currentTarget.value))} /></label>
            <label>Frames per second <input type="number" min="1" max="60" step="1" value={o.fps} oninput={(e) => set('fps', Number(e.currentTarget.value))} /></label>
            <label>Size
              <select value={`${o.width}x${o.height}`} onchange={(e) => { const [w, h] = e.currentTarget.value.split('x').map(Number); set('width', w); set('height', h); }}>
                <option value="1280x720">1280 x 720</option><option value="1920x1080">1920 x 1080</option><option value="2560x1440">2560 x 1440</option><option value="3840x2160">3840 x 2160</option>
              </select>
            </label>
          </div>
          <div class="row">
            <label class="inline">Preview at <input type="number" min="0" max={o.duration} step="1" bind:value={stillAt} /> s</label>
            <button onclick={() => previewAt(stillAt)} disabled={!canRender}>Preview this frame</button>
            {#if $stillJob.running}<button onclick={() => cancel($stillJob.requestId)}>Cancel</button>{/if}
          </div>
          {#if $stillJob.running}
            <div class="progress"><div class="bar"><div class="fill" style="width:{pct($stillJob)}%"></div></div><div class="msg">{$stillJob.message}</div></div>
          {/if}
          {#if $stillJob.error}<p class="err">{$stillJob.error}</p>{/if}
          {#if $still}
            <figure class="still"><img src={$still.url} alt="Preview frame at {$still.at.toFixed(0)} s" /><figcaption>Frame at {$still.at.toFixed(0)} s, drawn by the app's renderer (small size; the film is {o.width}x{o.height}).</figcaption></figure>
          {/if}
        </section>

        <!-- 4. The look -->
        <section>
          <h3><span class="n">4</span> The look</h3>
          <div class="choices">
            <label class="choice" class:on={o.stormStyle === 'scientific'}><input type="radio" name="style" checked={o.stormStyle === 'scientific'} onchange={() => set('stormStyle', 'scientific')} /> Scientific colour scale (dBZ)</label>
            <label class="choice" class:on={o.stormStyle === 'realistic'}><input type="radio" name="style" checked={o.stormStyle === 'realistic'} onchange={() => set('stormStyle', 'realistic')} /> Realistic <span class="tag">STYLISED</span></label>
          </div>
          <div class="grid3">
            <label>Ground
              <select value={o.ground} onchange={(e) => set('ground', e.currentTarget.value as any)}>
                <option value="map">flat map</option><option value="realistic">terrain with satellite imagery</option>
              </select>
            </label>
            <label>Map theme
              <select value={o.mapTheme} onchange={(e) => set('mapTheme', e.currentTarget.value as any)}>
                <option value="dark">dark</option><option value="light">light</option>
              </select>
            </label>
            {#if o.stormStyle === 'realistic'}
              <label>Light
                <select value={o.light} onchange={(e) => set('light', e.currentTarget.value as any)}>
                  <option value="daylight">daylight</option><option value="dusk">dusk</option><option value="overcast">overcast</option><option value="night">night</option>
                </select>
              </label>
              <label>Palette
                <select value={o.palette} onchange={(e) => set('palette', e.currentTarget.value as any)}>
                  <option value="white">white</option><option value="soft">soft</option><option value="stormy">stormy</option><option value="cyclone">cyclone</option>
                </select>
              </label>
              <label>Look preset
                <select value={o.lookPreset} onchange={(e) => set('lookPreset', e.currentTarget.value)}>
                  <option value="">(none)</option>
                  {#each realisticPresets as pr}<option value={pr.name}>{pr.name}</option>{/each}
                </select>
              </label>
            {/if}
          </div>
          {#if o.stormStyle === 'realistic'}
            <p class="note">The realistic look is stylised: the light is chosen, not observed, and the radar sees rain and ice, not cloud. Echo position and strength are the radar data; nothing is added where the radar saw nothing. The film says so on screen.</p>
          {/if}
          {#if o.ground === 'realistic'}
            <p class="note">Terrain and imagery tiles are fetched once into the tile cache (with their attribution on screen); see the realistic 3D page of the docs.</p>
          {/if}
          <div class="grid2">
            <label>Title <input type="text" placeholder="the radars and date" value={o.title} oninput={(e) => set('title', e.currentTarget.value)} /></label>
            <label>Subtitle <input type="text" value={o.subtitle} oninput={(e) => set('subtitle', e.currentTarget.value)} /></label>
          </div>
        </section>

        <!-- 5. Sound -->
        {#if caps?.sound}
          <section>
            <h3><span class="n">5</span> Sound <span class="tag">SYNTHESISED</span></h3>
            <label class="choice" class:on={o.sound}><input type="checkbox" checked={o.sound} onchange={(e) => set('sound', e.currentTarget.checked)} /> Add a synthesised sound track</label>
            <p class="note">Not a recording: wind from the retrieved winds, rain from the reflectivity and thunder timed to flashes you name, heard by a virtual listener beside the storm. The film, its tags and its record say so.</p>
            {#if o.sound}
              <div class="grid2">
                <label>Thunder level (dB, 0 = default mix) <input type="number" min="-30" max="20" step="1" value={o.soundThunderDb} oninput={(e) => set('soundThunderDb', Number(e.currentTarget.value))} /></label>
                <label>OBSERVED GOES GLM flashes (local folder)
                  <span class="row">
                    <input type="text" placeholder="none: no thunder" value={o.flashesFromGlm} oninput={(e) => set('flashesFromGlm', e.currentTarget.value)} />
                    <button onclick={pickGlmFolder}>Choose...</button>
                  </span>
                </label>
              </div>
              <label class="choice"><input type="checkbox" checked={o.lightningModel} onchange={(e) => set('lightningModel', e.currentTarget.checked)} /> Use MODELLED flashes instead (labelled "not observed")</label>
              <label class="choice"><input type="checkbox" checked={o.drawFlashes} onchange={(e) => set('drawFlashes', e.currentTarget.checked)} /> Draw the flashes the thunder follows</label>
            {/if}
          </section>
        {:else if caps}
          <section>
            <h3><span class="n">5</span> Sound</h3>
            <p class="dim small">This engine has no sound synthesis (or no MP4 writer); the film is silent.</p>
          </section>
        {/if}

        <!-- 6. Render -->
        <section>
          <h3><span class="n">{caps?.sound || caps ? 6 : 5}</span> Render</h3>
          <div class="grid2">
            <label>File name <input type="text" placeholder="animate_<radars>_<time>.mp4" value={o.outputName} oninput={(e) => set('outputName', e.currentTarget.value)} /></label>
            <label>Frames on disk while rendering
              <select value={o.frameFormat} onchange={(e) => set('frameFormat', e.currentTarget.value as any)}>
                <option value="png">PNG (lossless)</option><option value="jpg">JPEG (about 3x smaller)</option>
              </select>
            </label>
          </div>
          {#if caps?.default_output_dir}<p class="dim small">Written to <span class="mono">{caps.default_output_dir}</span> unless the name is a full path. A stopped film resumes where it stopped.</p>{/if}
          <div class="row">
            <button class="primary" onclick={startRender} disabled={!canRender}>Render the film</button>
            {#if $renderJob.running}<button onclick={cancelRender}>Cancel</button>{/if}
            {#if r || $still || p}<button class="quiet" onclick={clearResults} disabled={$anyRunning}>Clear results</button>{/if}
          </div>
          {#if $renderJob.running}
            <div class="progress"><div class="bar"><div class="fill" style="width:{pct($renderJob)}%"></div></div><div class="msg">{pct($renderJob)}% {$renderJob.message}</div></div>
          {/if}
          {#if $renderJob.error}<p class="err">{$renderJob.error}</p>{/if}
          {#if !$renderJob.running && !$renderJob.error && $renderJob.message && !r}<p class="dim small">{$renderJob.message}</p>{/if}

          {#if r}
            <div class="done">
              {#if r.video}
                {#if $videoUrl}
                  <!-- svelte-ignore a11y_media_has_caption -->
                  <video class="player" src={$videoUrl} controls playsinline></video>
                {:else}
                  <p class="dim small">Loading the film...</p>
                {/if}
                <div class="row">
                  {#if !web}<button onclick={showInFolder}>Show in folder</button>{/if}
                  <button onclick={exportCopy}>Export a copy...</button>
                  <span class="dim small mono">{base(r.video)} {mb(r.video_bytes)}, {r.seconds.toFixed(0)} s</span>
                </div>
              {:else if r.frames_written}
                <p class="warn">No MP4 could be written (PyAV is missing). The frames are in <span class="mono">{r.frames_written}</span>.{#if r.ffmpeg} To encode them: <span class="mono">{r.ffmpeg}</span>{/if}</p>
              {/if}
              {#each r.warnings as w}<p class="warn">{w}</p>{/each}
              <button class="link" onclick={() => (showRecord = !showRecord)}>{showRecord ? 'Hide' : 'Show'} the record (sidecar)</button>
              {#if showRecord}
                <dl class="facts">
                  <dt>Record</dt><dd class="mono">{r.provenance ?? '(none)'}</dd>
                  <dt>Equivalent command</dt><dd class="mono">{r.plan.command}</dd>
                  <dt>Observed</dt><dd>{r.plan.frames.observed_frames} of {r.plan.frames.frames} frames sit on a real volume; the rest are interpolated and labelled so</dd>
                  {#if r.sound}<dt>Sound</dt><dd>synthesised (see the record for what each sound is made of)</dd>{/if}
                </dl>
              {/if}
            </div>
          {/if}
        </section>
      </div>

      <footer class="foot">
        <span class="dim small">Everything here is also <span class="mono">rxd animate</span> on the command line; the record next to the film holds the equivalent command.</span>
        <button class="quiet" onclick={closeMovieStudio}>Close</button>
      </footer>
    </div>
  </div>
{/if}

<style>
  .ms-overlay {
    position: fixed; inset: 0; z-index: 950;
    background: var(--c-scrim);
    display: flex; align-items: center; justify-content: center;
    backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
  }
  .ms-dialog {
    width: 760px; max-width: 94vw; max-height: 90vh;
    display: flex; flex-direction: column;
    background: var(--c-overlay); border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm); box-shadow: var(--shadow-3);
    color: var(--c-text); font-size: var(--fs-sm); outline: none;
  }
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--c-border); background: var(--c-panel-head); }
  h2 { margin: 0; font-size: var(--fs-lg); font-weight: 600; }
  .sub { font-size: var(--fs-xs); color: var(--c-muted); margin-top: 2px; line-height: 1.4; }
  .icon { background: transparent; border: none; font-size: 22px; line-height: 1; color: var(--c-muted); padding: 0 var(--space-1); cursor: pointer; }
  .icon:hover { color: var(--c-text); }
  .body { overflow-y: auto; padding: var(--space-3) var(--space-4); display: flex; flex-direction: column; gap: var(--space-3); }
  section { border: 1px solid var(--c-border); border-radius: var(--radius-sm); padding: var(--space-3); display: flex; flex-direction: column; gap: var(--space-2); background: var(--c-field); }
  h3 { margin: 0; font-size: var(--fs-sm); font-weight: 600; display: flex; align-items: center; gap: var(--space-2); text-transform: uppercase; letter-spacing: var(--ls-label); }
  .n { display: inline-flex; width: 20px; height: 20px; align-items: center; justify-content: center; border-radius: 50%; background: rgba(var(--c-accent-rgb), 0.18); color: var(--c-accent); font-size: var(--fs-xs); font-family: var(--font-mono); }
  .lede, .note { margin: 0; color: var(--c-text-dim); font-size: var(--fs-xs); line-height: 1.5; }
  .note { border-left: 2px solid rgba(var(--c-accent-rgb), 0.6); padding-left: var(--space-2); }
  .dim { color: var(--c-muted); }
  .small { font-size: var(--fs-xs); }
  .mono { font-family: var(--font-mono); font-size: 12px; overflow-wrap: anywhere; }
  .row { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-2) var(--space-3); }
  .grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space-2) var(--space-3); }
  label { display: flex; flex-direction: column; gap: 3px; font-size: var(--fs-xs); color: var(--c-text-dim); min-width: 0; }
  label.inline { flex-direction: row; align-items: center; gap: var(--space-1); }
  label.inline input { width: 64px; }
  input[type='text'], input[type='number'], select { width: 100%; box-sizing: border-box; }
  .choices { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .choice { flex-direction: row; align-items: center; gap: var(--space-2); padding: var(--space-1) var(--space-2); border: 1px solid var(--c-border); border-radius: var(--radius-sm); color: var(--c-text); cursor: pointer; }
  .choice.on { border-color: rgba(var(--c-accent-rgb), 0.6); background: rgba(var(--c-accent-rgb), 0.1); }
  .cams { display: flex; flex-wrap: wrap; gap: var(--space-1); }
  .cam.on { background: rgba(var(--c-accent-rgb), 0.14); border-color: rgba(var(--c-accent-rgb), 0.55); color: var(--c-accent); }
  .tag { font-size: 9.5px; padding: 0 5px; border-radius: 3px; border: 1px solid; letter-spacing: 0.06em; color: var(--c-derived); border-color: rgba(var(--c-derived-rgb), 0.55); text-transform: uppercase; }
  .inputs { margin: 0; padding: 0; list-style: none; max-height: 120px; overflow-y: auto; font-size: var(--fs-xs); display: flex; flex-direction: column; gap: 2px; }
  .inputs li { display: flex; justify-content: space-between; gap: var(--space-2); }
  button.link { background: transparent; border: none; padding: 0; color: var(--c-accent); text-decoration: underline; cursor: pointer; font-size: var(--fs-xs); align-self: flex-start; }
  button.primary { background: var(--c-accent); color: var(--c-on-accent); border-color: var(--c-accent); }
  button.primary:disabled { opacity: 0.5; }
  .quiet { background: transparent; border-color: transparent; color: var(--c-muted); }
  .quiet:hover { color: var(--c-text); border-color: var(--c-border); }
  .err { margin: 0; padding: var(--space-2) var(--space-3); font-size: var(--fs-xs); color: var(--c-alarm); background: rgba(var(--c-alarm-rgb), 0.1); border: 1px solid rgba(var(--c-alarm-rgb), 0.4); border-radius: var(--radius-sm); overflow-wrap: anywhere; }
  .warn { margin: 0; font-size: var(--fs-xs); color: var(--c-caution); }
  .needs { border: 1px solid rgba(var(--c-caution-rgb), 0.5); border-radius: var(--radius-sm); padding: var(--space-2) var(--space-3); font-size: var(--fs-xs); }
  .needs-title { color: var(--c-caution); font-weight: 600; margin-bottom: var(--space-1); }
  .needs ul { margin: 0 0 var(--space-2); padding-left: 1.2em; line-height: 1.5; }
  .needs .what { color: var(--c-text); }
  .needs .hint { color: var(--c-text-dim); white-space: pre-wrap; }
  .progress .bar { height: 6px; background: rgba(var(--c-accent-rgb), 0.12); border-radius: 3px; overflow: hidden; }
  .progress .fill { height: 100%; background: var(--c-accent); transition: width var(--dur-base) var(--ease-in-out); }
  .progress .msg { margin-top: 4px; font-size: var(--fs-xs); color: var(--c-text-dim); }
  dl.facts { display: grid; grid-template-columns: max-content 1fr; gap: 4px var(--space-3); margin: 0; font-size: var(--fs-xs); }
  dl.facts dt { color: var(--c-muted); text-transform: uppercase; letter-spacing: var(--ls-label); font-size: 10.5px; padding-top: 2px; }
  dl.facts dd { margin: 0; color: var(--c-text); overflow-wrap: anywhere; }
  pre.report { margin: 0; font-size: 11px; line-height: 1.4; max-height: 260px; overflow: auto; padding: var(--space-2); background: var(--c-overlay); border: 1px solid var(--c-border); border-radius: var(--radius-sm); }
  figure.still { margin: 0; display: flex; flex-direction: column; gap: 4px; }
  figure.still img { width: 100%; border: 1px solid var(--c-border); border-radius: var(--radius-sm); }
  figcaption { font-size: var(--fs-xs); color: var(--c-muted); }
  .done { display: flex; flex-direction: column; gap: var(--space-2); }
  .player { width: 100%; max-height: 50vh; background: var(--c-surface-0); border-radius: var(--radius-sm); }
  .foot { display: flex; justify-content: space-between; align-items: center; gap: var(--space-2); padding: var(--space-2) var(--space-4); border-top: 1px solid var(--c-border); }
  @media (max-width: 640px) { .grid2, .grid3 { grid-template-columns: 1fr; } }
  @media (prefers-reduced-motion: reduce) { .progress .fill { transition: none; } }
</style>
