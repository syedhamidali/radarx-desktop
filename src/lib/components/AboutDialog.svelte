<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { fade, fly } from 'svelte/transition';
  import { focusTrap } from '../utils/focusTrap';

  export let visible = false;

  const dispatch = createEventDispatcher();
  const version = '0.1.0';

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
    <div class="about-dialog" role="dialog" aria-modal="true" aria-label="About xradar desktop" transition:fly={{ y: 16, duration: 250 }} use:focusTrap={{ initial: '.about-close-btn' }}>
      <!-- Logo area -->
      <div class="about-logo">
        <div class="logo-icon">
          <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
            <circle cx="24" cy="24" r="22" stroke="var(--accent-primary)" stroke-width="2" opacity="0.3" />
            <circle cx="24" cy="24" r="16" stroke="var(--accent-primary)" stroke-width="1.5" opacity="0.5" />
            <circle cx="24" cy="24" r="10" stroke="var(--accent-primary)" stroke-width="1.5" opacity="0.7" />
            <circle cx="24" cy="24" r="4" fill="var(--accent-primary)" />
            <line x1="24" y1="2" x2="24" y2="46" stroke="var(--accent-primary)" stroke-width="0.5" opacity="0.2" />
            <line x1="2" y1="24" x2="46" y2="24" stroke="var(--accent-primary)" stroke-width="0.5" opacity="0.2" />
          </svg>
        </div>
        <div class="logo-glow"></div>
      </div>

      <!-- App name -->
      <div class="about-name">
        <span class="name-x">x</span><span class="name-rest">radar</span>
        <span class="name-desktop">desktop</span>
      </div>

      <div class="about-version">v{version}</div>

      <p class="about-desc">
        Industry-grade weather radar visualization and research platform.
      </p>

      <!-- Tech stack -->
      <div class="about-tech">
        <span class="tech-label">Built with</span>
        <div class="tech-badges">
          <span class="tech-badge">Tauri 2.0</span>
          <span class="tech-badge">Svelte</span>
          <span class="tech-badge">xradar</span>
          <span class="tech-badge">Python</span>
        </div>
      </div>

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
        MIT License &middot; openradar
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
    background: rgba(0, 0, 0, 0.65);
    display: flex;
    align-items: center;
    justify-content: center;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
  }

  .about-dialog {
    width: 400px;
    max-width: 90vw;
    background: var(--bg-secondary);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-xl);
    box-shadow: var(--shadow-lg), 0 0 80px rgba(91, 108, 247, 0.08);
    padding: var(--spacing-2xl) var(--spacing-xl);
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    gap: var(--spacing-md);
  }

  .about-logo {
    position: relative;
    margin-bottom: var(--spacing-sm);
  }

  .logo-icon {
    position: relative;
    z-index: 1;
  }

  .logo-glow {
    position: absolute;
    inset: -20px;
    background: radial-gradient(circle, rgba(91, 108, 247, 0.2) 0%, transparent 70%);
    border-radius: 50%;
    z-index: 0;
    animation: about-glow 3s ease-in-out infinite alternate;
  }

  @keyframes about-glow {
    from { opacity: 0.5; transform: scale(0.9); }
    to { opacity: 1; transform: scale(1.1); }
  }

  .about-name {
    font-size: 24px;
    font-weight: 600;
    letter-spacing: -0.02em;
    user-select: text;
  }

  .name-x {
    color: var(--accent-primary);
    font-weight: 700;
    text-shadow: 0 0 20px rgba(91, 108, 247, 0.5);
  }

  .name-rest {
    color: var(--text-primary);
  }

  .name-desktop {
    color: var(--text-muted);
    font-weight: 400;
    margin-left: 4px;
    font-size: 18px;
  }

  .about-version {
    font-size: 12px;
    color: var(--text-muted);
    font-family: var(--font-mono);
    background: rgba(91, 108, 247, 0.08);
    padding: 2px 10px;
    border-radius: 10px;
    border: 1px solid rgba(91, 108, 247, 0.15);
  }

  .about-desc {
    font-size: 13px;
    color: var(--text-secondary);
    line-height: 1.6;
    margin: var(--spacing-sm) 0;
    max-width: 320px;
  }

  .about-tech {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-sm);
    margin: var(--spacing-sm) 0;
  }

  .tech-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
  }

  .tech-badges {
    display: flex;
    gap: var(--spacing-xs);
    flex-wrap: wrap;
    justify-content: center;
  }

  .tech-badge {
    font-size: 11px;
    padding: 3px 10px;
    background: rgba(91, 108, 247, 0.08);
    border: 1px solid var(--glass-border);
    border-radius: 12px;
    color: var(--text-accent);
    font-weight: 500;
  }

  .about-links {
    display: flex;
    gap: var(--spacing-sm);
    margin: var(--spacing-sm) 0;
  }

  .link-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    font-size: 12px;
    color: var(--text-secondary);
    background: transparent;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    cursor: pointer;
    height: auto;
    transition: all var(--transition-fast);
  }

  .link-btn:hover {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
    background: rgba(91, 108, 247, 0.06);
  }

  .about-license {
    font-size: 11px;
    color: var(--text-muted);
  }

  .about-close-btn {
    margin-top: var(--spacing-sm);
    padding: 6px 24px;
  }
</style>
