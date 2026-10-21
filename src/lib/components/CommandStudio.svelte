<script lang="ts">
  /**
   * Command Studio: everything the `rxd` command line can do, as a form in the app.
   *
   * The engine describes its commands (`cli_describe`); this dialog lists them with a search box,
   * generates a form for the chosen one, shows the equivalent terminal command live, runs it in the
   * engine (`cli_run`) with progress and cancel, and shows what came out: text, pictures, files.
   */
  import { onMount } from 'svelte';
  import { fade } from 'svelte/transition';
  import { focusTrap } from '../utils/focusTrap';
  import { inTauri } from '../utils/env';
  import { openFiles } from '../stores/fileManager';
  import { addToast } from '../stores/toasts';
  import {
    buildArgv, cliSchema, cliSchemaError, closeCommandStudio, commandStudioCommand, defaultValues, loadSchema,
    missingValues, parentDir, runCommand, shellLine, type CliCommand, type CliOption, type CliResult, type RunHandle, type Values,
  } from '../stores/commandStudio';
  import CommandStudioField from './CommandStudioField.svelte';
  import CommandStudioResults from './CommandStudioResults.svelte';

  let { onopen }: { onopen: (paths: string[]) => void } = $props();

  let query = $state('');
  let selected = $state<string | null>(null);
  let valuesByCommand = $state<Record<string, Values>>({});
  let cwd = $state('');
  let cwdTouched = $state(false);
  let run = $state<RunHandle | null>(null);
  let progress = $state<{ percent: number; text: string } | null>(null);
  let result = $state<CliResult | null>(null);
  let searchEl: HTMLInputElement | undefined = $state();

  const schema = $derived($cliSchema);
  const commands = $derived(schema?.commands ?? []);
  const filtered = $derived.by(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    const words = q.split(/\s+/);
    return commands.filter((c) => {
      const hay = `${c.name} ${c.group} ${c.purpose} ${c.summary}`.toLowerCase();
      return words.every((w) => hay.includes(w));
    });
  });
  const groups = $derived.by(() => {
    const out: { title: string; items: CliCommand[] }[] = [];
    for (const c of filtered) {
      let g = out.find((x) => x.title === c.group);
      if (!g) out.push((g = { title: c.group, items: [] }));
      g.items.push(c);
    }
    return out;
  });
  const current = $derived(commands.find((c) => c.name === selected) ?? null);
  const values = $derived(current ? (valuesByCommand[current.name] ?? defaultValues(current)) : {});
  const argv = $derived(current ? buildArgv(current, values) : []);
  const line = $derived(shellLine(argv));
  const missing = $derived(current ? missingValues(current, values) : []);
  const openPaths = $derived($openFiles.map((f) => f.path).filter(Boolean));
  const mainOptions = $derived(current ? current.options.filter((o) => !o.advanced) : []);
  const advancedOptions = $derived(current ? current.options.filter((o) => o.advanced) : []);
  const desktop = inTauri();
  const canRun = $derived(!!current && current.interactive && desktop && !run && missing.length === 0);

  /** Options in the order of the parser, with a heading wherever the parser's group changes. */
  function sections(options: CliOption[]): { title: string; items: CliOption[] }[] {
    const out: { title: string; items: CliOption[] }[] = [];
    for (const o of options) {
      const title = o.group;
      let s = out[out.length - 1];
      if (!s || s.title !== title) out.push((s = { title, items: [] }));
      s.items.push(o);
    }
    return out;
  }

  function setValue(dest: string, value: unknown) {
    if (!current) return;
    const next = { ...values, [dest]: value };
    valuesByCommand = { ...valuesByCommand, [current.name]: next };
    if (!cwdTouched && dest === 'input' && Array.isArray(value) && value.length) {
      cwd = parentDir(String(value[0]));
    }
  }
  function choose(name: string) {
    selected = name;
    result = null;
    commandStudioCommand.set(name);
  }
  function reset() {
    if (!current) return;
    valuesByCommand = { ...valuesByCommand, [current.name]: defaultValues(current) };
  }
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      addToast('success', 'Command copied', 2000);
    } catch {
      addToast('warning', 'The clipboard is not available here', 4000);
    }
  }
  async function chooseCwd() {
    if (!desktop) return;
    const { open } = await import('@tauri-apps/plugin-dialog');
    const picked = await open({ directory: true, multiple: false, title: 'Working folder: where files without a full path are written' });
    const dir = Array.isArray(picked) ? picked[0] : picked;
    if (dir) {
      cwd = dir;
      cwdTouched = true;
    }
  }
  async function start() {
    if (!canRun || !current) return;
    result = null;
    progress = { percent: 0, text: 'Starting' };
    const handle = runCommand(argv, cwd || null, (percent, text) => {
      progress = { percent, text };
    });
    run = handle;
    const r = await handle.result;
    if (run === handle) {
      run = null;
      progress = null;
      result = r;
      addToast(r.ok ? 'success' : r.status === 'cancelled' ? 'info' : 'error', `rxd ${current.name}: ${r.message}`, r.ok ? 3000 : 8000);
    }
  }
  function cancel() {
    run?.cancel();
    progress = { percent: progress?.percent ?? 0, text: 'Stopping at the next step' };
  }
  function close() {
    closeCommandStudio();
  }
  function onKey(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      close();
    }
  }

  onMount(async () => {
    const s = await loadSchema();
    const wanted = $commandStudioCommand;
    if (s && !selected) selected = s.commands.some((c) => c.name === wanted) ? wanted : s.commands[0]?.name ?? null;
    searchEl?.focus();
  });
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="scrim" transition:fade={{ duration: 120 }} onkeydown={onKey} onclick={(e) => { if (e.target === e.currentTarget) close(); }}>
  <div class="dlg brackets-4" role="dialog" aria-modal="true" aria-labelledby="cs-title" use:focusTrap={{ initial: 'input[name=cs-search]' }}>
    <header>
      <h2 id="cs-title">Command Studio</h2>
      <p class="lead">Everything from the command line, as a form.{#if schema} <span class="dim">rxd {schema.version}</span>{/if}</p>
      <button type="button" class="close" aria-label="Close" onclick={close}>×</button>
    </header>

    {#if $cliSchemaError}
      <p class="fail">{$cliSchemaError} <button type="button" class="mini" onclick={() => loadSchema(true)}>Try again</button></p>
    {:else if !schema}
      <p class="dim">Asking the engine what it can do…</p>
    {:else}
      <div class="body">
        <aside>
          <input name="cs-search" type="search" bind:this={searchEl} bind:value={query} placeholder="Search commands (plot, winds, rain…)" aria-label="Search commands" />
          <nav aria-label="Commands">
            {#each groups as g (g.title)}
              <h3>{g.title}</h3>
              {#each g.items as c (c.name)}
                <button type="button" class="cmd" class:on={c.name === selected} onclick={() => choose(c.name)} title={c.summary}>
                  <span class="name">{c.name}</span>
                  <span class="purpose">{c.purpose || c.summary}</span>
                </button>
              {/each}
            {/each}
            {#if !filtered.length}<p class="dim">No command matches.</p>{/if}
          </nav>
        </aside>

        <main>
          {#if current}
            <div class="title">
              <h3>rxd {current.name}</h3>
              <p>{current.summary}</p>
            </div>

            {#if !current.interactive}
              <p class="note">{current.reason}</p>
            {:else}
              {#if current.examples.length}
                <details class="examples">
                  <summary>Examples</summary>
                  <ul>
                    {#each current.examples as ex (ex)}
                      <li><code>{ex}</code><button type="button" class="mini" onclick={() => copy(ex)}>Copy</button></li>
                    {/each}
                  </ul>
                </details>
              {/if}

              <form onsubmit={(e) => { e.preventDefault(); start(); }}>
                {#each sections(mainOptions) as s (s.title + s.items[0]?.dest)}
                  {#if s.title}<h4>{s.title}</h4>{/if}
                  <div class="grid">
                    {#each s.items as o (o.dest)}
                      <CommandStudioField option={o} value={values[o.dest]} onchange={(v) => setValue(o.dest, v)} {openPaths} />
                    {/each}
                  </div>
                {/each}
                {#if advancedOptions.length}
                  <details class="advanced">
                    <summary>Advanced ({advancedOptions.length} options)</summary>
                    {#each sections(advancedOptions) as s (s.title + s.items[0]?.dest)}
                      {#if s.title}<h4>{s.title}</h4>{/if}
                      <div class="grid">
                        {#each s.items as o (o.dest)}
                          <CommandStudioField option={o} value={values[o.dest]} onchange={(v) => setValue(o.dest, v)} {openPaths} />
                        {/each}
                      </div>
                    {/each}
                  </details>
                {/if}

                <div class="cwd">
                  <span>Working folder</span>
                  <input type="text" bind:value={cwd} oninput={() => (cwdTouched = true)} placeholder="where files without a full path are written (default: the folder of the first input)" />
                  {#if desktop}<button type="button" class="mini" onclick={chooseCwd}>Choose…</button>{/if}
                </div>

                <div class="preview">
                  <code>{line}</code>
                  <button type="button" class="mini" onclick={() => copy(line)}>Copy as command</button>
                </div>

                <div class="actions">
                  {#if !desktop}
                    <span class="dim">Commands run on your own computer: this needs the desktop app.</span>
                  {:else if missing.length}
                    <span class="dim">Still needed: {missing.join(', ')}</span>
                  {:else if progress}
                    <span class="progress" role="status"><span class="bar" style="width: {progress.percent}%"></span><span class="ptext">{progress.percent}% {progress.text}</span></span>
                  {/if}
                  <span class="spacer"></span>
                  <button type="button" class="mini" onclick={reset} disabled={!!run}>Reset</button>
                  {#if run}
                    <button type="button" class="danger" onclick={cancel}>Cancel</button>
                  {:else}
                    <button type="submit" class="primary" disabled={!canRun}>Run</button>
                  {/if}
                </div>
              </form>

              {#if result}
                <CommandStudioResults {result} {onopen} />
              {/if}
            {/if}
          {/if}
        </main>
      </div>
    {/if}
  </div>
</div>

<style>
  .scrim { position: fixed; inset: 0; z-index: 950; background: var(--c-scrim); display: grid; place-items: center; padding: 16px; }
  .dlg {
    width: 1040px; max-width: 100%; height: min(88vh, 860px); background: var(--c-overlay); border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm); box-shadow: var(--shadow-3); display: flex; flex-direction: column; overflow: hidden;
  }
  header { display: grid; grid-template-columns: auto 1fr auto; gap: var(--space-3); align-items: baseline; padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--c-border); }
  h2 { margin: 0; font-size: var(--fs-lg); }
  .lead { margin: 0; font-size: var(--fs-sm); color: var(--c-text-dim); }
  .dim { color: var(--c-muted); font-size: var(--fs-xs); }
  .close { background: none; border: none; font-size: 20px; line-height: 1; color: var(--c-muted); cursor: pointer; padding: 0 4px; }
  .close:hover { color: var(--c-text); }
  .body { display: grid; grid-template-columns: 250px 1fr; flex: 1; min-height: 0; }
  aside { border-right: 1px solid var(--c-border); display: flex; flex-direction: column; min-height: 0; }
  aside input { margin: var(--space-2); background: var(--c-field); border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm); color: var(--c-text); padding: 6px 8px; font: inherit; font-size: var(--fs-sm); }
  nav { overflow-y: auto; padding: 0 var(--space-2) var(--space-2); }
  nav h3 { margin: var(--space-2) 6px 2px; font-size: var(--fs-2xs); text-transform: uppercase; letter-spacing: 0.08em; color: var(--c-muted); }
  .cmd { display: flex; flex-direction: column; align-items: flex-start; gap: 1px; width: 100%; text-align: left; background: none; border: 1px solid transparent; border-radius: var(--radius-sm); padding: 5px 6px; cursor: pointer; }
  .cmd:hover { background: rgba(var(--c-accent-rgb), 0.06); }
  .cmd.on { background: rgba(var(--c-accent-rgb), 0.14); border-color: rgba(var(--c-accent-rgb), 0.4); }
  .cmd .name { font-family: var(--font-mono); font-size: var(--fs-sm); color: var(--c-text); }
  .cmd .purpose { font-size: var(--fs-2xs); color: var(--c-muted); line-height: 1.3; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; }
  main { overflow-y: auto; padding: var(--space-3) var(--space-4); display: flex; flex-direction: column; gap: var(--space-3); min-width: 0; }
  .title h3 { margin: 0; font-family: var(--font-mono); font-size: var(--fs-md); }
  .title p { margin: 2px 0 0; font-size: var(--fs-sm); color: var(--c-text-dim); }
  .note { margin: 0; padding: 8px 10px; font-size: var(--fs-sm); border: 1px solid rgba(var(--c-caution-rgb), 0.4); border-radius: var(--radius-sm); background: rgba(var(--c-caution-rgb), 0.08); }
  .fail { margin: var(--space-4); font-size: var(--fs-sm); color: var(--c-alarm); }
  details { border: 1px solid var(--c-border); border-radius: var(--radius-sm); padding: 4px 8px; }
  summary { cursor: pointer; font-size: var(--fs-xs); color: var(--c-accent); }
  .examples ul { list-style: none; margin: 6px 0 2px; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .examples li { display: flex; gap: 8px; align-items: center; }
  .examples code { flex: 1; font-family: var(--font-mono); font-size: var(--fs-xs); overflow-wrap: anywhere; }
  form { display: flex; flex-direction: column; gap: var(--space-3); }
  h4 { margin: var(--space-1) 0 0; font-size: var(--fs-2xs); text-transform: uppercase; letter-spacing: 0.08em; color: var(--c-muted); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: var(--space-3) var(--space-4); }
  .advanced .grid { margin: var(--space-2) 0; }
  .cwd { display: grid; grid-template-columns: auto 1fr auto; gap: 8px; align-items: center; font-size: var(--fs-xs); color: var(--c-text-dim); }
  .cwd input { background: var(--c-field); border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm); color: var(--c-text); padding: 6px 8px; font: inherit; font-size: var(--fs-sm); min-width: 0; }
  .preview { display: flex; gap: 8px; align-items: center; }
  .preview code { flex: 1; font-family: var(--font-mono); font-size: var(--fs-xs); background: var(--c-field); border: 1px solid var(--c-border); border-radius: var(--radius-sm); padding: 6px 8px; overflow-wrap: anywhere; user-select: all; }
  .actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .spacer { flex: 1; }
  .mini { padding: 4px 8px; font-size: var(--fs-xs); white-space: nowrap; }
  .progress { position: relative; display: inline-block; min-width: 260px; height: 22px; border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm); overflow: hidden; background: var(--c-field); }
  .bar { position: absolute; inset: 0 auto 0 0; background: rgba(var(--c-accent-rgb), 0.3); transition: width 200ms; }
  .ptext { position: relative; display: block; padding: 0 8px; line-height: 22px; font-size: var(--fs-xs); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 420px; }
  @media (max-width: 760px) {
    .body { grid-template-columns: 1fr; }
    aside { border-right: none; border-bottom: 1px solid var(--c-border); max-height: 36%; }
  }
</style>
