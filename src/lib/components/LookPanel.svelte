<script lang="ts">
  /**
   * The "Look" panel of the realistic storm: every control of lookConfig.ts (slider + number box + a
   * reset per control, colour pickers with an "auto" button), presets, saving your own looks (kept in
   * this browser) and exporting / importing a look file (JSON; TOML can be imported). Changes are live.
   */
  import {
    CONTROLS, GROUPS, defaultLook, validateLook, changedLook, lookToJson, lookToToml, parseLookText, PRESETS, applyPreset,
    matchingPreset, loadUserPresets, saveUserPreset, deleteUserPreset, LookError, type LookConfig, type Preset,
  } from '../utils/lookConfig';

  interface Props {
    look: LookConfig;
    onchange: (next: LookConfig) => void;
  }
  let { look, onchange }: Props = $props();

  let userPresets = $state<Preset[]>(loadUserPresets(localStorage));
  let saveName = $state('');
  let message = $state('');
  let open = $state<Record<string, boolean>>({ Scene: true, 'Exposure and colour': true });
  const matched = $derived(matchingPreset(look, userPresets));
  const defaults = defaultLook();

  function set(key: string, value: number | string) {
    try {
      onchange(validateLook({ [key]: value }, look));
      message = '';
    } catch (e) {
      message = e instanceof LookError ? e.message : String(e);
    }
  }
  function reset(key: string) { set(key, defaults[key] as number | string); }
  function resetAll() { onchange(defaultLook()); message = ''; }

  function pick(name: string) {
    if (!name) return;
    try { onchange(applyPreset(name, userPresets)); message = ''; } catch (e) { message = String(e); }
  }
  function save() {
    try {
      userPresets = saveUserPreset(localStorage, saveName, look);
      message = `saved "${saveName.trim()}"`;
    } catch (e) { message = e instanceof LookError ? e.message : String(e); }
  }
  function remove() {
    if (matched && userPresets.some((p) => p.name === matched)) {
      userPresets = deleteUserPreset(localStorage, matched);
      message = `deleted "${matched}"`;
    }
  }
  function download(text: string, name: string, type: string) {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function exportJson() { download(lookToJson(look, matched ?? (saveName.trim() || 'my look')), 'xradar-look.json', 'application/json'); }
  function exportToml() { download(lookToToml(look), 'xradar-look.toml', 'text/plain'); }
  async function importFile(e: Event) {
    const f = (e.target as HTMLInputElement).files?.[0];
    if (!f) return;
    try {
      const text = await f.text();
      const fmt = f.name.toLowerCase().endsWith('.toml') ? 'toml' : 'json';
      onchange(validateLook(parseLookText(text, fmt), defaultLook()));
      message = `loaded ${f.name}`;
    } catch (err) {
      message = err instanceof LookError ? err.message : String(err);
    }
    (e.target as HTMLInputElement).value = '';
  }
  const num = (k: string) => look[k] as number;
  const fmtVal = (q: (typeof CONTROLS)[number], v: number) => (q.step && q.step < 0.01 ? v.toFixed(3) : q.step && q.step < 1 ? v.toFixed(2) : String(v));
</script>

<div class="look-panel" aria-label="Look">
  <div class="lp-top">
    <label>Preset
      <select value={matched ?? ''} onchange={(e) => pick((e.target as HTMLSelectElement).value)}>
        <option value="" disabled>{matched ? '' : 'custom look'}</option>
        {#each PRESETS as p}<option value={p.name} title={p.about}>{p.name}</option>{/each}
        {#if userPresets.length}<optgroup label="Saved">{#each userPresets as p}<option value={p.name}>{p.name}</option>{/each}</optgroup>{/if}
      </select>
    </label>
    <button class="lp-btn" onclick={resetAll} title="Back to the default look">Reset all</button>
    <input class="lp-name" placeholder="name to save as" bind:value={saveName} aria-label="Name of the look to save" />
    <button class="lp-btn" onclick={save} disabled={!saveName.trim()} title="Keep this look in this browser">Save</button>
    {#if matched && userPresets.some((p) => p.name === matched)}<button class="lp-btn" onclick={remove}>Delete</button>{/if}
    <button class="lp-btn" onclick={exportJson} title="Download this look as a file to share">Export</button>
    <button class="lp-btn" onclick={exportToml} title="Download as TOML">TOML</button>
    <label class="lp-btn lp-file">Import<input type="file" accept=".json,.toml,application/json" onchange={importFile} /></label>
  </div>
  {#if message}<div class="lp-msg" role="status">{message}</div>{/if}
  <div class="lp-note">Stylised look: it never changes where echo is or how strong it is. {Object.keys(changedLook(look)).length} setting(s) changed.</div>

  {#each GROUPS as g}
    <details open={open[g] ?? false} ontoggle={(e) => (open[g] = (e.currentTarget as HTMLDetailsElement).open)}>
      <summary>{g}</summary>
      <div class="lp-grid">
        {#each CONTROLS.filter((q) => q.group === g) as q (q.key)}
          <div class="lp-row" title={q.help}>
            <label for={'lk-' + q.key}>{q.label}{q.unit ? ' (' + q.unit + ')' : ''}</label>
            {#if q.kind === 'number'}
              <input id={'lk-' + q.key} type="range" min={q.min} max={q.max} step={q.step} value={num(q.key)} oninput={(e) => set(q.key, Number((e.target as HTMLInputElement).value))} />
              <input class="lp-num" type="number" min={q.min} max={q.max} step={q.step} value={fmtVal(q, num(q.key))} onchange={(e) => set(q.key, Number((e.target as HTMLInputElement).value))} aria-label={q.label + ' value'} />
            {:else if q.kind === 'choice'}
              <select id={'lk-' + q.key} value={look[q.key]} onchange={(e) => set(q.key, (e.target as HTMLSelectElement).value)}>
                {#each q.choices ?? [] as c}<option value={c}>{c}</option>{/each}
              </select>
            {:else}
              <input id={'lk-' + q.key} type="color" value={(look[q.key] as string) || '#808080'} oninput={(e) => set(q.key, (e.target as HTMLInputElement).value)} class:auto={!look[q.key]} />
              <span class="lp-auto">{look[q.key] || 'auto'}</span>
            {/if}
            <button class="lp-reset" onclick={() => reset(q.key)} disabled={look[q.key] === defaults[q.key]} aria-label={'Reset ' + q.label} title="Reset">↺</button>
          </div>
        {/each}
      </div>
    </details>
  {/each}
</div>

<style>
  .look-panel { padding: 6px 10px; background: var(--bg-secondary); border-bottom: 1px solid var(--border-color); max-height: 46vh; overflow: auto; font-size: 11px; }
  .lp-top { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 4px; }
  .lp-top select, .lp-name { font-size: 11px; background: var(--bg-tertiary); color: var(--text-primary); border: 1px solid var(--border-color); border-radius: 3px; padding: 2px 4px; }
  .lp-name { width: 120px; }
  .lp-btn { font-size: 11px; padding: 2px 7px; border: 1px solid var(--border-color); background: var(--bg-tertiary); color: var(--text-primary); border-radius: 3px; cursor: pointer; }
  .lp-btn:disabled { opacity: 0.45; cursor: default; }
  .lp-file input { display: none; }
  .lp-msg { color: var(--accent-warning); margin: 2px 0; }
  .lp-note { color: var(--text-muted); margin-bottom: 4px; }
  summary { cursor: pointer; font-weight: 700; color: var(--accent-primary); padding: 2px 0; }
  .lp-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 2px 14px; padding: 2px 0 6px; }
  .lp-row { display: grid; grid-template-columns: 138px 1fr 62px 18px; align-items: center; gap: 6px; }
  .lp-row label { color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .lp-num { width: 62px; font-family: var(--font-mono); font-size: 10px; }
  .lp-reset { border: none; background: transparent; color: var(--text-muted); cursor: pointer; padding: 0; }
  .lp-reset:disabled { opacity: 0.25; cursor: default; }
  .lp-auto { font-family: var(--font-mono); color: var(--text-muted); font-size: 10px; }
  input[type='color'].auto { opacity: 0.4; }
</style>
