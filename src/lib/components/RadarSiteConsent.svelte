<script lang="ts">
  /**
   * RadarSiteConsent - the dialog that stands in front of every network request of the Radar
   * sites layer. It says what will be fetched, from where and how big it is, and offers "once",
   * "for this session" (memory only) or "cancel". Escape and a click outside cancel.
   */
  import { focusTrap } from '../utils/focusTrap';
  import { portal } from '../utils/portal';
  import type { ConsentPrompt } from '../stores/radarSites';

  let { prompt }: { prompt: ConsentPrompt } = $props();

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      prompt.answer('cancel');
    }
  }
</script>

<!-- capture: the dialog owns Escape while it is open, whatever has the focus behind it -->
<svelte:window onkeydowncapture={onKey} />

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="rsc-scrim" use:portal onclick={(e) => { if (e.target === e.currentTarget) prompt.answer('cancel'); }}>
  <div
    class="rsc brackets-4"
    role="dialog"
    aria-modal="true"
    aria-labelledby="rsc-title"
    aria-describedby="rsc-body"
    use:focusTrap={{ initial: '.rsc-once' }}
  >
    <h3 id="rsc-title">{prompt.text.title}</h3>
    <div id="rsc-body">
      <p class="lead">This contacts the internet. Nothing is sent or fetched until you agree.</p>
      <dl>
        <dt>What</dt>
        <dd>
          {#each prompt.text.lines as line}<span class="ln">{line}</span>{/each}
        </dd>
        <dt>From</dt>
        <dd class="mono">{prompt.text.host}</dd>
        <dt>Size</dt>
        <dd>{prompt.text.size}</dd>
      </dl>
    </div>
    <div class="row">
      <button type="button" class="rsc-once primary" onclick={() => prompt.answer('once')}>Allow once</button>
      <button type="button" onclick={() => prompt.answer('session')}>Allow for this session</button>
      <button type="button" onclick={() => prompt.answer('cancel')}>Cancel</button>
    </div>
    <p class="fine">"This session" is remembered in memory only and is forgotten when you quit or reload.</p>
  </div>
</div>

<style>
  .rsc-scrim {
    position: fixed;
    inset: 0;
    z-index: 2000;
    display: grid;
    place-items: center;
    background: var(--c-scrim);
    padding: var(--space-4);
  }
  .rsc {
    width: min(460px, 100%);
    padding: var(--space-5);
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-3);
    color: var(--c-text);
  }
  h3 { margin: 0 0 var(--space-2); font-size: var(--fs-lg); }
  .lead { margin: 0 0 var(--space-3); color: var(--c-text-dim); font-size: var(--fs-sm); }
  dl { display: grid; grid-template-columns: auto 1fr; gap: var(--space-2) var(--space-3); margin: 0 0 var(--space-4); font-size: var(--fs-sm); }
  dt { color: var(--c-muted); }
  dd { margin: 0; overflow-wrap: anywhere; }
  .ln { display: block; }
  .mono { font-family: var(--font-mono); font-size: var(--fs-xs); }
  .row { display: flex; gap: var(--space-2); flex-wrap: wrap; }
  .fine { margin: var(--space-3) 0 0; color: var(--c-muted); font-size: var(--fs-2xs); }
</style>
