<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { fade, fly } from 'svelte/transition';
  import { focusTrap } from '../utils/focusTrap';
  import LogoMark from './LogoMark.svelte';
  import RadarSweep from './RadarSweep.svelte';
  import { webUi } from '../stores/webInfo';
  import { WEBSITE_URL, XRADAR_DOCS_URL, PRODUCT_CREDIT } from '../utils/brand';
  import { openReportIssue } from '../stores/reportIssue';

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

  function reportIssue() {
    close();
    openReportIssue();
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
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div class="about-overlay" on:click={handleOverlayClick} transition:fade={{ duration: 150 }}>
    <div class="about-dialog brackets-4" role="dialog" aria-modal="true" aria-label="About radarx desktop" transition:fly={{ y: 16, duration: 250 }} use:focusTrap={{ initial: '.about-close-btn' }}>
      <!-- Logo area: the mark over a slow scope -->
      <div class="about-logo">
        <div class="logo-scope"><RadarSweep size={132} period={8} rings={2} labels={false} hub={false} /></div>
        <div class="logo-icon"><LogoMark size={68} title="Radarx" /></div>
      </div>

      <div class="about-name">
        RADAR<span class="name-x">X</span>
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
        <button class="link-btn" on:click={() => openLink(WEBSITE_URL)}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
          </svg>
          Website
        </button>
        <button class="link-btn" on:click={() => openLink(XRADAR_DOCS_URL)}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
          </svg>
          xradar library documentation
        </button>
        <button class="link-btn" on:click={reportIssue}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/>
          </svg>
          Report an issue
        </button>
      </div>

      <p class="about-built">
        Radarx is built on the open xradar and radarx libraries.
      </p>

      <!-- License -->
      <div class="about-license">
        PolyForm Noncommercial 1.0.0 &middot; &copy; {PRODUCT_CREDIT}
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

  .about-built {
    font-size: var(--fs-xs);
    color: var(--c-text-dim);
    max-width: 340px;
    line-height: 1.5;
  }

  .about-license {
    font-size: var(--fs-xs);
    color: var(--c-muted);
    user-select: text;
  }

  .about-close-btn { margin-top: var(--space-1); padding: 6px 28px; }
</style>
