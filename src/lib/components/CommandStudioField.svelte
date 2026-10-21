<script lang="ts">
  /**
   * One option of an `rxd` command as a form control, chosen from the engine's description of the
   * option (its kind): toggle, count, dropdown, number with unit, file / folder / output pickers,
   * a KEY=VALUE list, or plain text. Knows no command by name.
   */
  import { inTauri } from '../utils/env';
  import { optionLabel, type CliOption } from '../stores/commandStudio';

  let {
    option,
    value,
    onchange,
    openPaths = [],
  }: {
    option: CliOption;
    value: unknown;
    onchange: (value: unknown) => void;
    /** Files already open in the app: one-click choices for input options. */
    openPaths?: string[];
  } = $props();

  const label = $derived(optionLabel(option));
  const flagText = $derived(option.positional ? option.metavar ?? option.dest : option.flags.join(', '));
  const list = $derived(Array.isArray(value) ? (value as string[]) : []);
  let draft = $state('');

  function setList(next: string[]) {
    onchange(next);
  }
  function addPath(p: string) {
    const t = p.trim();
    if (!t || list.includes(t)) return;
    setList([...list, t]);
  }
  function addDraft() {
    addPath(draft);
    draft = '';
  }

  async function pick(opts: { directory?: boolean; multiple?: boolean; title: string }): Promise<string[]> {
    if (!inTauri()) return [];
    const { open } = await import('@tauri-apps/plugin-dialog');
    const picked = await open({ multiple: opts.multiple ?? false, directory: opts.directory ?? false, title: opts.title });
    if (!picked) return [];
    return Array.isArray(picked) ? picked : [picked];
  }

  async function pickInputs(directory: boolean) {
    const paths = await pick({ directory, multiple: !directory, title: directory ? 'Choose a folder of radar files' : 'Choose radar files' });
    if (paths.length) setList([...list, ...paths.filter((p) => !list.includes(p))]);
  }
  async function pickSingle() {
    const [p] = await pick({ title: `Choose ${label.toLowerCase()}` });
    if (p) onchange(p);
  }
  async function pickOutput() {
    if (!inTauri()) return;
    const { save } = await import('@tauri-apps/plugin-dialog');
    const current = typeof value === 'string' ? value : '';
    const p = await save({ title: `${label}: where to write`, defaultPath: current || undefined });
    if (p) onchange(p);
  }

  const numberStep = $derived(option.kind === 'int' ? 1 : 'any');
</script>

<div class="field" class:wide={option.kind === 'input' || option.kind === 'params'}>
  <div class="head">
    <span class="label">{label}{#if option.required}<span class="req" title="required"> *</span>{/if}</span>
    <code class="flag">{flagText}</code>
  </div>

  {#if option.kind === 'flag'}
    <label class="toggle">
      <input type="checkbox" checked={value === true} onchange={(e) => onchange((e.currentTarget as HTMLInputElement).checked)} />
      <span>{option.help || 'on'}</span>
    </label>
  {:else if option.kind === 'count'}
    <select value={String(Number(value) || 0)} onchange={(e) => onchange(Number((e.currentTarget as HTMLSelectElement).value))}>
      <option value="0">off</option>
      <option value="1">on ({option.flags[0]})</option>
      <option value="2">more ({option.flags[0]}{option.flags[0].replace(/^-+/, '')})</option>
    </select>
  {:else if option.kind === 'choice' && option.choices}
    <select value={String(value ?? '')} onchange={(e) => onchange((e.currentTarget as HTMLSelectElement).value)}>
      {#if !option.required}<option value="">{option.default != null ? `default (${option.default})` : '(not set)'}</option>{/if}
      {#each option.choices as c (c)}<option value={c}>{c}</option>{/each}
    </select>
  {:else if option.kind === 'int' || option.kind === 'float'}
    <div class="num">
      <input
        type="number"
        step={numberStep}
        value={value === '' || value == null ? '' : String(value)}
        placeholder={option.default != null ? String(option.default) : ''}
        oninput={(e) => onchange((e.currentTarget as HTMLInputElement).value)}
      />
      {#if option.unit}<span class="unit">{option.unit}</span>{/if}
    </div>
  {:else if option.kind === 'input'}
    {#if list.length}
      <ul class="chips">
        {#each list as p, i (p)}
          <li><span title={p}>{p.split(/[\\/]/).pop()}</span><button class="x" type="button" aria-label="Remove {p}" onclick={() => setList(list.filter((_, j) => j !== i))}>×</button></li>
        {/each}
      </ul>
    {/if}
    {#if openPaths.some((p) => !list.includes(p))}
      <div class="quick">
        <span>Open in the app:</span>
        {#each openPaths.filter((p) => !list.includes(p)) as p (p)}
          <button type="button" class="mini" title={p} onclick={() => addPath(p)}>+ {p.split(/[\\/]/).pop()}</button>
        {/each}
      </div>
    {/if}
    <div class="row">
      <input type="text" bind:value={draft} placeholder="a path, a glob like data/*_V06, or a folder" onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addDraft(); } }} />
      <button type="button" class="mini" onclick={addDraft} disabled={!draft.trim()}>Add</button>
      {#if inTauri()}
        <button type="button" class="mini" onclick={() => pickInputs(false)}>Files…</button>
        <button type="button" class="mini" onclick={() => pickInputs(true)}>Folder…</button>
      {/if}
    </div>
  {:else if option.kind === 'output' || option.kind === 'file'}
    <div class="row">
      <input type="text" value={String(value ?? '')} placeholder={option.metavar ?? ''} oninput={(e) => onchange((e.currentTarget as HTMLInputElement).value)} />
      {#if inTauri()}
        <button type="button" class="mini" onclick={() => (option.kind === 'output' ? pickOutput() : pickSingle())}>Choose…</button>
      {/if}
    </div>
  {:else if option.kind === 'params'}
    <textarea rows="2" placeholder="one KEY=VALUE per line" value={list.join('\n')} oninput={(e) => onchange((e.currentTarget as HTMLTextAreaElement).value.split('\n').map((s) => s.trim()).filter(Boolean))}></textarea>
  {:else}
    <input type="text" value={String(value ?? '')} placeholder={option.default != null ? String(option.default) : option.metavar ?? ''} oninput={(e) => onchange((e.currentTarget as HTMLInputElement).value)} />
  {/if}

  {#if option.kind !== 'flag' && option.help}<p class="help">{option.help}</p>{/if}
</div>

<style>
  .field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .field.wide { grid-column: 1 / -1; }
  .head { display: flex; justify-content: space-between; align-items: baseline; gap: var(--space-2); }
  .label { font-size: var(--fs-sm); color: var(--c-text); }
  .req { color: var(--c-caution); }
  .flag { font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-muted); white-space: nowrap; }
  input[type='text'], input[type='number'], select, textarea {
    background: var(--c-field); border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm); color: var(--c-text);
    padding: 6px 8px; font: inherit; font-size: var(--fs-sm); width: 100%; box-sizing: border-box; min-width: 0;
  }
  textarea { font-family: var(--font-mono); resize: vertical; }
  .num { display: flex; align-items: center; gap: 6px; }
  .unit { font-size: var(--fs-xs); color: var(--c-muted); white-space: nowrap; }
  .row { display: flex; gap: 6px; align-items: center; }
  .row input { flex: 1; }
  .toggle { display: flex; gap: 8px; align-items: flex-start; font-size: var(--fs-sm); color: var(--c-text-dim); cursor: pointer; line-height: 1.4; }
  .toggle input { margin-top: 3px; accent-color: var(--c-accent); }
  .help { margin: 0; font-size: var(--fs-2xs); color: var(--c-muted); line-height: 1.4; }
  .chips { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 4px; }
  .chips li { display: inline-flex; align-items: center; gap: 4px; max-width: 100%; padding: 2px 4px 2px 8px; border: 1px solid var(--c-border-strong); border-radius: 999px; font-size: var(--fs-xs); background: rgba(var(--c-accent-rgb), 0.08); }
  .chips span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 260px; }
  .x { background: none; border: none; padding: 0 4px; color: var(--c-muted); cursor: pointer; font-size: var(--fs-sm); line-height: 1; }
  .x:hover { color: var(--c-alarm); }
  .quick { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; font-size: var(--fs-2xs); color: var(--c-muted); }
  .mini { padding: 4px 8px; font-size: var(--fs-xs); white-space: nowrap; }
</style>
