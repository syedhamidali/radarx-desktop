<script lang="ts">
  /** What a Command Studio run produced: the verdict, text output, files, thumbnails. */
  import { revealFile, type CliResult } from '../stores/commandStudio';
  import { inTauri } from '../utils/env';
  import { addToast } from '../stores/toasts';

  let { result, onopen }: { result: CliResult; onopen: (paths: string[]) => void } = $props();

  let preview = $state<string | null>(null);

  const images = $derived(result.files.filter((f) => f.thumbnail));

  function size(n: number | null): string {
    if (n == null) return '';
    if (n < 1000) return `${n} B`;
    if (n < 1e6) return `${(n / 1e3).toFixed(1)} kB`;
    return `${(n / 1e6).toFixed(1)} MB`;
  }
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      addToast('success', 'Copied', 2000);
    } catch {
      addToast('warning', 'The clipboard is not available here', 4000);
    }
  }
</script>

<section class="results" aria-live="polite">
  <p class="verdict" class:ok={result.status === 'ok'} class:bad={result.status === 'failed'} class:cancelled={result.status === 'cancelled'}>
    <strong>{result.status === 'ok' ? 'Done' : result.status === 'cancelled' ? 'Cancelled' : 'Failed'}</strong>
    <span>{result.message}</span>
    <span class="meta">exit {result.code} · {(result.elapsed_ms / 1000).toFixed(1)} s</span>
  </p>

  {#if result.command}
    <p class="cmd"><code>{result.command}</code><button type="button" class="mini" onclick={() => copy(result.command ?? '')}>Copy</button></p>
  {/if}

  {#if result.files.length}
    <ul class="files">
      {#each result.files as f (f.path)}
        <li>
          <span class="kind">{f.kind}</span>
          <span class="name" title={f.path}>{f.name}</span>
          <span class="size">{size(f.size)}</span>
          <span class="acts">
            {#if inTauri()}<button type="button" class="mini" onclick={() => revealFile(f.path)}>Show in folder</button>{/if}
            {#if f.kind === 'radar'}<button type="button" class="mini" onclick={() => onopen([f.path])}>Open in the app</button>{/if}
            {#if f.thumbnail}<button type="button" class="mini" onclick={() => (preview = f.thumbnail ?? null)}>View</button>{/if}
            <button type="button" class="mini" onclick={() => copy(f.path)}>Copy path</button>
          </span>
        </li>
      {/each}
    </ul>
  {/if}

  {#if images.length}
    <div class="thumbs">
      {#each images as f (f.path)}
        <button type="button" class="thumb" title={f.name} onclick={() => (preview = f.thumbnail ?? null)}>
          <img src={f.thumbnail} alt={f.name} />
        </button>
      {/each}
    </div>
  {/if}

  {#if result.stdout.trim()}
    <details open>
      <summary>Output</summary>
      <pre>{result.stdout}</pre>
    </details>
  {/if}
  {#if result.stderr.trim()}
    <details open={!result.ok}>
      <summary>Messages</summary>
      <pre>{result.stderr}</pre>
    </details>
  {/if}
</section>

{#if preview}
  <button type="button" class="lightbox" aria-label="Close the picture" onclick={() => (preview = null)}>
    <img src={preview} alt="The file the command wrote, at full size" />
  </button>
{/if}

<style>
  .results { display: flex; flex-direction: column; gap: var(--space-2); }
  .verdict { margin: 0; display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; padding: 8px 10px; border-radius: var(--radius-sm); border: 1px solid var(--c-border); font-size: var(--fs-sm); }
  .verdict.ok { border-color: rgba(var(--c-nominal-rgb), 0.5); background: rgba(var(--c-nominal-rgb), 0.08); }
  .verdict.bad { border-color: rgba(var(--c-alarm-rgb), 0.5); background: rgba(var(--c-alarm-rgb), 0.08); }
  .verdict.cancelled { border-color: rgba(var(--c-caution-rgb), 0.5); background: rgba(var(--c-caution-rgb), 0.08); }
  .meta { margin-left: auto; color: var(--c-muted); font-size: var(--fs-xs); font-variant-numeric: tabular-nums; }
  .cmd { margin: 0; display: flex; gap: 8px; align-items: center; font-size: var(--fs-xs); }
  .cmd code { flex: 1; font-family: var(--font-mono); background: var(--c-field); border: 1px solid var(--c-border); border-radius: var(--radius-sm); padding: 4px 8px; overflow-wrap: anywhere; }
  .files { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .files li { display: grid; grid-template-columns: auto 1fr auto auto; gap: 8px; align-items: center; font-size: var(--fs-sm); padding: 4px 6px; border: 1px solid var(--c-border); border-radius: var(--radius-sm); }
  .kind { font-size: var(--fs-2xs); text-transform: uppercase; letter-spacing: 0.04em; color: var(--c-derived); }
  .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .size { color: var(--c-muted); font-size: var(--fs-xs); font-variant-numeric: tabular-nums; }
  .acts { display: flex; gap: 4px; flex-wrap: wrap; }
  .mini { padding: 3px 8px; font-size: var(--fs-xs); white-space: nowrap; }
  .thumbs { display: flex; flex-wrap: wrap; gap: 8px; }
  .thumb { padding: 0; border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm); background: var(--c-field); overflow: hidden; width: 160px; height: 120px; cursor: zoom-in; }
  .thumb img { width: 100%; height: 100%; object-fit: contain; display: block; }
  details { border: 1px solid var(--c-border); border-radius: var(--radius-sm); padding: 4px 8px; }
  summary { cursor: pointer; font-size: var(--fs-xs); color: var(--c-accent); }
  pre { margin: 6px 0 2px; max-height: 320px; overflow: auto; font-family: var(--font-mono); font-size: var(--fs-xs); line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--c-text); }
  .lightbox { position: fixed; inset: 0; z-index: 1200; background: var(--c-scrim); border: none; display: grid; place-items: center; cursor: zoom-out; padding: 24px; }
  .lightbox img { max-width: 100%; max-height: 100%; box-shadow: var(--shadow-3); background: #fff; }
</style>
