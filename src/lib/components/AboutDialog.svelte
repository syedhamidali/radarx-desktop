<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { fade, fly } from 'svelte/transition';
  import { focusTrap } from '../utils/focusTrap';
  import LogoMark from './LogoMark.svelte';
  import RadarSweep from './RadarSweep.svelte';
  import { webUi } from '../stores/webInfo';

  export let visible = false;

  const dispatch = createEventDispatcher();
  const version = __APP_VERSION__;

  function close() {
    visible = false;
    dispatch('close');
  }

  function handleOverlayClick(e: MouseEvent) {
    if ((e.target as HTMLElement).classList.contains('about-overlay')) {
      close();
    }
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (!visible) return;
    if (e.key === 'Escape') {
      close();
      e.preventDefault();
      e.stopPropagation();
    }
  }

  async function openLink(url: string) {
    try {
      const { open } = await import('@tauri-apps/plugin-shell');
      await open(url);
    } catch {
      window.open(url, '_blank');
    }
  }
</script>

<svelte:window on:keydown={handleKeyDown} />

{#if visible}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="about-overlay" on:click={handleOverlayClick} transition:fade={{ duration: 150 }}>
    <div class="about-dialog brackets-4" role="dialog" aria-modal="true" aria-label="About radarx desktop" transition:fly={{ y: 16, duration: 250 }} use:focusTrap={{ initial: '.about-close-btn' }}>
      <!-- Logo area: the mark over a slow scope -->
      <div class="about-logo">
        <div class="logo-scope"><RadarSweep size={132} period={8} rings={2} labels={false} hub={false} /></div>
        <div class="logo-icon"><LogoMark size={68} title="xradar" /></div>
      </div>

      <div class="about-name">
        <span class="name-x">X</span>RADAR
      </div>
      <div class="about-sub">{$webUi ? 'web' : 'desktop'} &middot; radar research workstation</div>

      <p class="about-desc">
        Weather radar visualization and research platform: open, inspect, process and export volumes from
        NEXRAD, ODIM, CfRadial, IRIS and more.
      </p>

      <dl class="about-specs">
        <div><dt>VERSION</dt><dd>v{version}</dd></div>
        <div><dt>SHELL</dt><dd>Tauri 2 / Svelte</dd></div>
        <div><dt>ENGINE</dt><dd>Python / xradar</dd></div>
      </dl>

      <!-- Links -->
      <div class="about-links">
        <button class="link-btn" on:click={() => openLink('https://github.com/openradar/xradar')}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
            <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/>
          </svg>
          GitHub
        </button>
        <button class="link-btn" on:click={() => openLink('https://docs.openradarscience.org/projects/xradar/')}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
          </svg>
          Documentation
        </button>
      </div>

      <!-- License -->
      <div class="about-license">
        PolyForm Noncommercial 1.0.0 &middot; &copy; Hamid Ali Syed
      </div>

      <!-- Close -->
      <button class="about-close-btn primary" on:click={close}>Close</button>
    </div>
  </div>
{/if}

<style>
  .about-overlay {
    position: fixed;
    inset: 0;
    z-index: 950;
    background: var(--c-scrim);
    display: flex;
    align-items: center;
    justify-content: center;
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
  }

  .about-dialog {
    width: 420px;
    max-width: 90vw;
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-3);
    padding: var(--space-6) var(--space-5) var(--space-5);
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    gap: var(--space-3);
  }

  .about-logo {
    position: relative;
    width: 132px;
    height: 132px;
    margin-bottom: var(--space-1);
  }
  .logo-scope { position: absolute; inset: 0; opacity: 0.7; }
  .logo-icon {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    filter: drop-shadow(0 0 10px rgba(var(--c-accent-rgb), 0.5));
  }

  .about-name {
    font-size: var(--fs-2xl);
    font-weight: 600;
    letter-spacing: 0.3em;
    padding-left: 0.3em;
    user-select: text;
  }
  .name-x { color: var(--c-accent); }
  .about-sub {
    margin-top: -6px;
    font-size: var(--fs-2xs);
    letter-spacing: var(--ls-wide);
    text-transform: uppercase;
    color: var(--c-muted);
  }

  .about-desc {
    font-size: var(--fs-sm);
    color: var(--c-text-dim);
    line-height: 1.6;
    max-width: 340px;
  }

  .about-specs {
    align-self: stretch;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    margin: var(--space-1) 0;
    border: 1px solid var(--c-border);
    background: var(--c-field);
  }
  .about-specs > div { padding: var(--space-2) var(--space-2); border-right: 1px solid var(--c-border); }
  .about-specs > div:last-child { border-right: 0; }
  .about-specs dt { font-size: 9.5px; letter-spacing: var(--ls-label); color: var(--c-muted); }
  .about-specs dd { margin-top: 2px; font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--c-text); user-select: text; }

  .about-links { display: flex; gap: var(--space-2); }

  .link-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    font-size: var(--fs-sm);
    color: var(--c-text-dim);
    background: transparent;
    height: auto;
  }
  .link-btn:hover { color: var(--c-accent); border-color: var(--c-accent); background: rgba(var(--c-accent-rgb), 0.06); }

  .about-license {
    font-size: var(--fs-xs);
    color: var(--c-muted);
    user-select: text;
  }

  .about-close-btn { margin-top: var(--space-1); padding: 6px 28px; }
</style>
