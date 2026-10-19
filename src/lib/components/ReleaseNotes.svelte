<script lang="ts">
  /**
   * Release notes from the update server. They are untrusted, so they are never inserted as HTML:
   * parseReleaseNotes() turns the markdown into a tree of plain nodes and this renders them as text
   * (Svelte escapes every string). Links are http(s) only and open in the system browser.
   */
  import { parseReleaseNotes, type Inline } from '../utils/updateLogic';
  import { openExternal } from '../utils/updater';

  let { markdown = '' } = $props<{ markdown?: string }>();

  const blocks = $derived(parseReleaseNotes(markdown));

  function go(e: MouseEvent, href: string) {
    e.preventDefault(); // never navigate the app window itself
    void openExternal(href);
  }
</script>

{#snippet inl(nodes: Inline[])}
  {#each nodes as n}
    {#if n.t === 'text'}{n.s}
    {:else if n.t === 'code'}<code>{n.s}</code>
    {:else if n.t === 'strong'}<strong>{@render inl(n.c)}</strong>
    {:else if n.t === 'em'}<em>{@render inl(n.c)}</em>
    {:else if n.t === 'link'}<a href={n.href} rel="noopener noreferrer" onclick={(e) => go(e, n.href)}>{@render inl(n.c)}</a>
    {/if}
  {/each}
{/snippet}

<div class="notes">
  {#each blocks as b}
    {#if b.t === 'h'}
      <p class="h h{b.level}">{@render inl(b.c)}</p>
    {:else if b.t === 'p'}
      <p>{@render inl(b.c)}</p>
    {:else if b.t === 'ul'}
      <ul>{#each b.items as item}<li>{@render inl(item)}</li>{/each}</ul>
    {:else if b.t === 'ol'}
      <ol>{#each b.items as item}<li>{@render inl(item)}</li>{/each}</ol>
    {:else if b.t === 'code'}
      <pre>{b.s}</pre>
    {:else if b.t === 'hr'}
      <hr />
    {/if}
  {/each}
</div>

<style>
  .notes {
    font-size: var(--fs-sm);
    line-height: 1.55;
    color: var(--c-text-dim);
    overflow-wrap: anywhere;
    user-select: text;
  }
  .notes p { margin: 0 0 var(--space-2); }
  .h { color: var(--c-text); font-weight: 600; margin-top: var(--space-3); }
  .h1 { font-size: var(--fs-md); }
  .h2 { font-size: var(--fs-sm); text-transform: uppercase; letter-spacing: var(--ls-label); color: var(--c-accent); }
  .h3 { font-size: var(--fs-sm); }
  ul, ol { margin: 0 0 var(--space-2); padding-left: 1.3em; }
  li { margin: 2px 0; }
  code {
    font-family: var(--font-mono);
    font-size: 0.92em;
    padding: 0 4px;
    background: var(--c-field);
    border: 1px solid var(--c-border);
    border-radius: 3px;
    color: var(--c-text);
  }
  pre {
    margin: 0 0 var(--space-2);
    padding: var(--space-2);
    overflow-x: auto;
    font-family: var(--font-mono);
    font-size: var(--fs-xs);
    background: var(--c-field);
    border: 1px solid var(--c-border);
    border-radius: var(--radius-sm);
    color: var(--c-text);
  }
  hr { border: 0; border-top: 1px solid var(--c-border); margin: var(--space-3) 0; }
  a { color: var(--c-accent); text-decoration: underline; text-underline-offset: 2px; }
  a:hover { color: var(--c-accent-strong); }
  strong { color: var(--c-text); }
</style>
