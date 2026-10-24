<script lang="ts">
  /**
   * LightningSound - listen to the lightning of the current time window. Off until you press Listen.
   *
   * The sound is made from the data and says so ("Synthesised from the data, not a recording"): the time
   * of each flash, its distance, its direction and its strength are data; the thunder arrives after
   * distance / 343 m/s, divided by the replay speed; the timbre is made up. The listener is at the radar or
   * follows the 3D camera. A small mixer (level and mute per layer, master level, a limiter ceiling) is
   * remembered. There is no wind, rain, hail or tornado sound: nothing in lightning data says anything
   * about them.
   */
  import { onDestroy } from 'svelte';
  import { lightning, lightningWindow, soundSettings, currentListener, flashPlacer, radarPosition } from '../stores/lightning';
  import { LightningSound, MAX_VOICES } from '../utils/liveSoundEngine';
  import { SOUND_LABEL, flashStrength01, layerReport, planReplay, thunderDelayS, type LayerId } from '../utils/liveSound';

  const data = $derived($lightning.data);
  const win = $derived($lightningWindow);
  const cfg = $derived($soundSettings);
  const supported = LightningSound.supported();
  const player = new LightningSound();

  let playing = $state(false);
  let status = $state('');

  const report = $derived(data ? layerReport(data) : []);

  function setLayer(id: LayerId, patch: Partial<{ levelDb: number; mute: boolean }>) {
    soundSettings.update((s) => ({ ...s, mixer: { ...s.mixer, layers: { ...s.mixer.layers, [id]: { ...s.mixer.layers[id], ...patch } } } }));
  }

  // the mixer works while playing
  $effect(() => {
    player.setMixer(cfg.mixer);
  });

  async function listen() {
    if (playing) {
      player.stop();
      playing = false;
      status = 'Stopped.';
      return;
    }
    const radar = radarPosition();
    if (!data || !win || !radar) {
      status = 'Open a radar volume and lightning files first: the window and the listener come from them.';
      return;
    }
    const place = flashPlacer(data, radar.lat, radar.lon);
    const events = planReplay(data, win.start, win.end, currentListener(), place, { speed: cfg.speed });
    if (!events.length) {
      status = 'No flashes in this window: nothing to play (this may be a time the files do not cover).';
      return;
    }
    const strength = flashStrength01(data);
    try {
      const r = await player.play(events, {
        getPose: currentListener,
        mixer: cfg.mixer,
        seed: 1,
        strengths: (i) => strength[i],
        heightKm: (i) => (Number.isFinite(data.f.altKm[i]) ? data.f.altKm[i] : 0),
      });
      playing = true;
      status = `Playing ${r.voiced} of ${events.length} flashes${r.skipped ? ` (the ${r.skipped} quietest are left out: at most ${MAX_VOICES} at once)` : ''}; ${Math.round(r.durationS)} s${r.firstThunderS !== null ? `, the first thunder after ${r.firstThunderS.toFixed(1)} s` : ''}.`;
      setTimeout(() => {
        if (!player.playing) {
          playing = false;
          status = 'Finished.';
        }
      }, (r.durationS + 0.8) * 1000);
    } catch (e) {
      status = `The sound could not start: ${e instanceof Error ? e.message : String(e)}`;
    }
  }

  onDestroy(() => player.dispose());

  // one real second of delay for how far away at the chosen speed (the physics, shown to the user)
  const delayExample = $derived(thunderDelayS(10, cfg.speed));
</script>

{#if data && supported}
  <details class="snd" data-testid="lightning-sound">
    <summary>Sound</summary>
    <p class="label" data-testid="sound-label">{SOUND_LABEL}</p>
    <div class="row">
      <button class="primary" onclick={listen} aria-pressed={playing} disabled={!win}>{playing ? 'Stop' : 'Listen to this window'}</button>
      <label class="inline">Speed
        <select value={cfg.speed} onchange={(e) => soundSettings.update((s) => ({ ...s, speed: +e.currentTarget.value }))}>
          {#each [1, 10, 30, 60, 120, 300] as v (v)}<option value={v}>{v}x</option>{/each}
        </select>
      </label>
    </div>
    <p class="hint" role="status" aria-live="polite">{status}</p>
    <p class="hint">
      Replays the flashes of the window in time order, {cfg.speed} data seconds per second. Thunder arrives after distance / 343 m/s divided by the speed:
      a flash 10 km away is heard {delayExample.toFixed(1)} s after its tick. At 1x that is the real delay (29 s).
    </p>

    <fieldset>
      <legend>Listener</legend>
      <label class="chk"><input type="radio" name="ltg-listener" checked={cfg.listener === 'radar'} onchange={() => soundSettings.update((s) => ({ ...s, listener: 'radar' }))} /> At the radar, facing north</label>
      <label class="chk"><input type="radio" name="ltg-listener" checked={cfg.listener === 'camera'} onchange={() => soundSettings.update((s) => ({ ...s, listener: 'camera' }))} /> Follows the 3D camera (open the 3D view)</label>
    </fieldset>

    <fieldset>
      <legend>Mixer</legend>
      <label>Master {cfg.mixer.masterDb} dB
        <input type="range" min="-40" max="6" step="1" value={cfg.mixer.masterDb} oninput={(e) => soundSettings.update((s) => ({ ...s, mixer: { ...s.mixer, masterDb: +e.currentTarget.value } }))} />
      </label>
      {#each report as l (l.id)}
        <div class="layer">
          <label>{l.name} {cfg.mixer.layers[l.id].levelDb} dB
            <input type="range" min="-40" max="12" step="1" value={cfg.mixer.layers[l.id].levelDb} oninput={(e) => setLayer(l.id, { levelDb: +e.currentTarget.value })} />
          </label>
          <label class="chk"><input type="checkbox" checked={cfg.mixer.layers[l.id].mute} onchange={(e) => setLayer(l.id, { mute: e.currentTarget.checked })} /> Mute</label>
        </div>
      {/each}
      <label>Limiter ceiling {cfg.mixer.ceilingDb} dBFS
        <input type="range" min="-24" max="0" step="1" value={cfg.mixer.ceilingDb} oninput={(e) => soundSettings.update((s) => ({ ...s, mixer: { ...s.mixer, ceilingDb: +e.currentTarget.value } }))} />
      </label>
      <p class="hint">The limiter keeps the sound under the ceiling; it never makes it louder. Levels are not a perceptual loudness (no K-weighting).</p>
    </fieldset>

    <div class="report" data-testid="sound-report">
      <h5>What drives each layer</h5>
      <ul>
        {#each report as l (l.id)}<li><strong>{l.name}</strong>: {l.driver}. <span class="dim">({l.kind})</span></li>{/each}
      </ul>
      <p class="hint">Real: when each flash happened, where, how strong, and the physics of the delay and the loudness falling with distance. Made up: the timbre (shaped noise). No wind, rain, hail or tornado sound is made: nothing in lightning data says anything about them.</p>
    </div>
  </details>
{:else if data && !supported}
  <p class="hint">This browser cannot play Web Audio, so the lightning sound is not available.</p>
{/if}

<style>
  .snd { font-size: var(--fs-xs); color: var(--c-text-dim); display: flex; flex-direction: column; gap: var(--space-2); }
  summary { cursor: pointer; color: var(--c-text-dim); text-transform: uppercase; letter-spacing: var(--ls-label); font-size: 10.5px; margin-bottom: var(--space-2); }
  .label { margin: 0; font-weight: 600; color: var(--c-text); }
  .row { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
  .hint { margin: 0; line-height: 1.5; }
  .dim { color: var(--c-muted); }
  fieldset { display: flex; flex-direction: column; gap: var(--space-2); margin: 0; padding: var(--space-2) var(--space-3); border: 1px solid var(--c-border); border-radius: var(--radius-sm); }
  legend { font-size: 10.5px; text-transform: uppercase; letter-spacing: var(--ls-label); color: var(--c-muted); padding: 0 4px; }
  label { display: flex; flex-direction: column; align-items: stretch; text-align: left; gap: 2px; }
  label.chk, label.inline { flex-direction: row; align-items: center; gap: var(--space-2); }
  .layer { display: grid; grid-template-columns: 1fr auto; gap: var(--space-2); align-items: end; }
  h5 { margin: 0 0 2px; font-size: 10.5px; color: var(--c-muted); text-transform: uppercase; letter-spacing: var(--ls-label); font-weight: 500; }
  ul { margin: 0; padding-left: 16px; line-height: 1.5; }
</style>
