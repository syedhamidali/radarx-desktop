<script lang="ts">
  import { getContext } from 'svelte';

  // PanelManager already renders a header (title, collapse, drag handle) around each panel; inside it
  // this component only provides the body (plus the badge, if any) so titles are not shown twice.
  const embedded: boolean = getContext<boolean>('embedded-panel') === true;

  export let title: string;
  export let badge: string | number | null = null;
  export let collapsed: boolean = false;
  export let icon: string = '';

  function toggle() {
    collapsed = !collapsed;
  }
</script>

<div class="collapsible-panel" class:collapsed={collapsed && !embedded} class:embedded>
  {#if embedded}
    {#if badge !== null}
      <div class="embedded-badge"><span class="panel-badge" title={title}>{badge}</span></div>
    {/if}
  {:else}
  <button class="panel-header" on:click={toggle} title="{collapsed ? 'Expand' : 'Collapse'} {title}">
    <div class="panel-header-left">
      {#if icon}
        <span class="panel-icon">{icon}</span>
      {/if}
      <span class="panel-title">{title}</span>
    </div>
    <div class="panel-header-right">
      {#if badge !== null}
        <span class="panel-badge">{badge}</span>
      {/if}
      <span class="panel-chevron" class:open={!collapsed}>
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </span>
    </div>
  </button>
  {/if}
  <div class="panel-content" class:visible={embedded || !collapsed}>
    <div class="panel-body">
      <slot />
    </div>
  </div>
</div>

<style>
  .embedded-badge {
    display: flex;
    justify-content: flex-end;
    padding: 6px var(--space-3) 0;
  }

  .collapsible-panel.embedded { border-bottom: none; }
  .collapsible-panel { border-bottom: 1px solid var(--c-border); }

  .panel-header {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    padding: var(--space-2) var(--space-3);
    font-weight: 600;
    font-size: var(--fs-xs);
    text-transform: uppercase;
    letter-spacing: var(--ls-label);
    color: var(--c-text-dim);
    background: var(--c-panel-head);
    border: none;
    border-bottom: 1px solid var(--c-border);
    border-radius: 0;
    cursor: pointer;
    height: 34px;
  }
  .panel-header:hover { background: var(--bg-hover); color: var(--c-text); border-color: var(--c-border); }
  .collapsible-panel:not(.collapsed) > .panel-header::before {
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 2px;
    background: var(--c-accent);
  }

  .panel-header-left,
  .panel-header-right { display: flex; align-items: center; gap: var(--space-2); }
  .panel-icon { font-size: var(--fs-sm); opacity: 0.8; }
  .panel-title { white-space: nowrap; }

  .panel-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 20px;
    height: 18px;
    padding: 0 6px;
    font-family: var(--font-mono);
    font-size: var(--fs-2xs);
    font-weight: 500;
    color: var(--c-accent);
    background: rgba(var(--c-accent-rgb), 0.1);
    border: 1px solid rgba(var(--c-accent-rgb), 0.28);
    border-radius: var(--radius-sm);
  }

  .panel-chevron {
    display: flex;
    align-items: center;
    justify-content: center;
    transition: transform var(--dur-base) var(--ease-in-out);
    color: var(--c-muted);
  }
  .panel-chevron.open { transform: rotate(0deg); }
  .panel-chevron:not(.open) { transform: rotate(-90deg); }

  .panel-content {
    max-height: 0;
    overflow: hidden;
    transition: max-height var(--dur-base) var(--ease-in-out);
  }
  .panel-content.visible {
    max-height: 2000px;
    transition: max-height var(--dur-slow) var(--ease-in-out);
  }
  .panel-body { padding: var(--space-3); }

  /* Inside PanelManager the outer panel owns collapsing and height. */
  .embedded .panel-content,
  .embedded .panel-content.visible {
    max-height: none;
    overflow: visible;
    transition: none;
  }
</style>
