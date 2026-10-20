<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { fade, fly } from 'svelte/transition';
  import { focusTrap } from '../utils/focusTrap';

  export let visible = false;

  interface TourStep {
    title: string;
    description: string;
    selector: string;
    position: 'top' | 'bottom' | 'left' | 'right';
  }

  const steps: TourStep[] = [
    {
      title: 'Welcome to Radarx Desktop',
      description: 'Your weather radar research workstation. Let us show you around.',
      selector: '.app-title',
      position: 'bottom',
    },
    {
      title: 'Open a Radar File',
      description: 'Click here or press Ctrl+O to open radar data files. You can also drag and drop files onto the window.',
      selector: '.toolbar-center .toolbar-btn',
      position: 'bottom',
    },
    {
      title: 'Variable Selector',
      description: 'Once a file is loaded, use the data inspector on the left to select radar variables like reflectivity, velocity, and more.',
      selector: '.sidebar-left',
      position: 'right',
    },
    {
      title: 'Sweep Controls',
      description: 'Play through elevation sweeps, adjust animation speed, and navigate between different scan angles.',
      selector: '.anim-controls',
      position: 'bottom',
    },
    {
      title: 'Command Palette',
      description: 'Press Ctrl+K (or Cmd+K on Mac) to open the command palette for quick access to any action.',
      selector: '.cmd-trigger',
      position: 'bottom',
    },
    {
      title: 'Settings',
      description: 'Customize themes, accent colour, rendering, keyboard shortcuts, and more from the settings panel (Ctrl+,).',
      selector: '.settings-btn',
      position: 'bottom',
    },
    {
      title: 'Export & Processing',
      description: 'Use the right panel for processing pipelines and exporting radar plots in PNG, SVG, or PDF formats.',
      selector: '.sidebar-right',
      position: 'left',
    },
  ];

  let currentStep = 0;
  // Finishing or skipping the tour is a decision: remember it unless the user un-ticks the box.
  let dontShowAgain = true;
  let spotlight = { top: 0, left: 0, width: 0, height: 0 };
  let tooltipStyle = '';
  let vw = typeof window !== 'undefined' ? window.innerWidth : 1280;
  let vh = typeof window !== 'undefined' ? window.innerHeight : 800;

  $: step = steps[currentStep];
  $: progress = ((currentStep + 1) / steps.length) * 100;

  function updateSpotlight() {
    if (!visible) return;
    vw = window.innerWidth;
    vh = window.innerHeight;
    const el = document.querySelector(step.selector);
    if (!el) {
      spotlight = { top: window.innerHeight / 2 - 40, left: window.innerWidth / 2 - 100, width: 200, height: 80 };
    } else {
      const rect = el.getBoundingClientRect();
      const pad = 8;
      spotlight = {
        top: rect.top - pad,
        left: rect.left - pad,
        width: rect.width + pad * 2,
        height: rect.height + pad * 2,
      };
    }
    positionTooltip();
  }

  function positionTooltip() {
    const ttWidth = 340;
    const ttHeight = 200;
    const gap = 16;
    let top = 0;
    let left = 0;

    switch (step.position) {
      case 'bottom':
        top = spotlight.top + spotlight.height + gap;
        left = spotlight.left + spotlight.width / 2 - ttWidth / 2;
        break;
      case 'top':
        top = spotlight.top - ttHeight - gap;
        left = spotlight.left + spotlight.width / 2 - ttWidth / 2;
        break;
      case 'right':
        top = spotlight.top + spotlight.height / 2 - ttHeight / 2;
        left = spotlight.left + spotlight.width + gap;
        break;
      case 'left':
        top = spotlight.top + spotlight.height / 2 - ttHeight / 2;
        left = spotlight.left - ttWidth - gap;
        break;
    }

    // Clamp to viewport
    left = Math.max(16, Math.min(left, window.innerWidth - ttWidth - 16));
    top = Math.max(16, Math.min(top, window.innerHeight - ttHeight - 16));

    tooltipStyle = `top: ${top}px; left: ${left}px; width: ${ttWidth}px;`;
  }

  function next() {
    if (currentStep < steps.length - 1) {
      currentStep++;
      updateSpotlight();
    } else {
      finish();
    }
  }

  function prev() {
    if (currentStep > 0) {
      currentStep--;
      updateSpotlight();
    }
  }

  function skip() {
    finish();
  }

  function finish() {
    if (dontShowAgain) {
      try {
        localStorage.setItem('xradar-onboarding-done', 'true');
      } catch {
        /* storage unavailable: the tour simply shows again next launch */
      }
    }
    visible = false;
    currentStep = 0;
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (!visible) return;
    if (e.key === 'Escape') { skip(); e.preventDefault(); }
    else if (e.key === 'ArrowRight' || (e.key === 'Enter' && !['BUTTON', 'INPUT'].includes((e.target as HTMLElement)?.tagName))) { next(); e.preventDefault(); }
    else if (e.key === 'ArrowLeft') { prev(); e.preventDefault(); }
  }

  onMount(() => {
    window.addEventListener('keydown', handleKeyDown, true);
    window.addEventListener('resize', updateSpotlight);
  });

  onDestroy(() => {
    window.removeEventListener('keydown', handleKeyDown, true);
    window.removeEventListener('resize', updateSpotlight);
  });

  $: if (visible) {
    // Small delay to let DOM settle
    setTimeout(updateSpotlight, 50);
  }
</script>

{#if visible}
  <div class="onboarding-overlay" role="dialog" aria-modal="true" aria-label="Welcome tour" transition:fade={{ duration: 200 }}>
    <!-- SVG mask for spotlight cutout -->
    <svg class="spotlight-mask" viewBox="0 0 {vw} {vh}">
      <defs>
        <mask id="spotlight-mask">
          <rect x="0" y="0" width="100%" height="100%" fill="white" />
          <rect
            x={spotlight.left}
            y={spotlight.top}
            width={spotlight.width}
            height={spotlight.height}
            rx="8"
            fill="black"
          />
        </mask>
      </defs>
      <rect
        x="0" y="0" width="100%" height="100%"
        fill="rgba(var(--c-shadow-rgb), 0.75)"
        mask="url(#spotlight-mask)"
      />
    </svg>

    <!-- Spotlight ring -->
    <div
      class="spotlight-ring"
      style="top: {spotlight.top}px; left: {spotlight.left}px; width: {spotlight.width}px; height: {spotlight.height}px;"
    ></div>

    <!-- Tooltip -->
    <div class="tour-tooltip brackets-4" style={tooltipStyle} transition:fly={{ y: 8, duration: 200 }} use:focusTrap={{ initial: '.tour-btn-primary' }}>
      <div class="tour-step-badge">Step {currentStep + 1} of {steps.length}</div>
      <h3 class="tour-title">{step.title}</h3>
      <p class="tour-desc">{step.description}</p>

      <div class="tour-actions">
        <div class="tour-dots">
          {#each steps as _, i}
            <button
              class="tour-dot"
              class:active={i === currentStep}
              class:completed={i < currentStep}
              on:click={() => { currentStep = i; updateSpotlight(); }}
              aria-label="Go to step {i + 1}"
            ></button>
          {/each}
        </div>
        <div class="tour-buttons">
          <button class="tour-btn tour-btn-secondary" on:click={skip}>Skip</button>
          {#if currentStep > 0}
            <button class="tour-btn tour-btn-secondary" on:click={prev}>Back</button>
          {/if}
          <button class="tour-btn tour-btn-primary" on:click={next}>
            {currentStep === steps.length - 1 ? 'Done' : 'Next'}
          </button>
        </div>
      </div>

      <label class="tour-dismiss">
        <input type="checkbox" bind:checked={dontShowAgain} />
        <span>Don't show again</span>
      </label>
    </div>
  </div>
{/if}

<style>
  .onboarding-overlay {
    position: fixed;
    inset: 0;
    z-index: 2000;
    pointer-events: auto;
  }

  .spotlight-mask {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
  }

  .spotlight-ring {
    position: absolute;
    border: 2px solid var(--accent-primary);
    border-radius: var(--radius-md);
    box-shadow: 0 0 0 4px rgba(var(--c-accent-rgb), 0.2), 0 0 30px rgba(var(--c-accent-rgb), 0.15);
    pointer-events: none;
    transition: all 400ms cubic-bezier(0.4, 0, 0.2, 1);
  }

  .tour-tooltip {
    position: absolute;
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    padding: var(--spacing-lg);
    box-shadow: var(--shadow-3);
    z-index: 2001;
    transition: top 400ms cubic-bezier(0.4, 0, 0.2, 1),
                left 400ms cubic-bezier(0.4, 0, 0.2, 1);
  }

  .tour-step-badge {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: var(--ls-label);
    color: var(--accent-primary);
    margin-bottom: var(--spacing-xs);
  }

  .tour-title {
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 var(--spacing-sm) 0;
  }

  .tour-desc {
    font-size: 13px;
    color: var(--text-secondary);
    line-height: 1.6;
    margin: 0 0 var(--spacing-lg) 0;
  }

  .tour-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
  }

  .tour-dots {
    display: flex;
    gap: 6px;
  }

  .tour-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    border: none;
    background: var(--glass-border);
    cursor: pointer;
    padding: 0;
    transition: all var(--transition-fast);
  }

  .tour-dot:hover {
    background: var(--text-muted);
    transform: scale(1.2);
  }

  .tour-dot.active {
    background: var(--accent-primary);
    box-shadow: 0 0 6px var(--accent-primary);
    width: 20px;
    border-radius: 4px;
  }

  .tour-dot.completed {
    background: var(--accent-success);
  }

  .tour-buttons {
    display: flex;
    gap: var(--spacing-sm);
  }

  .tour-btn {
    padding: 6px 16px;
    font-size: 12px;
    font-weight: 500;
    border-radius: var(--radius-sm);
    cursor: pointer;
    height: auto;
    transition: all var(--transition-fast);
  }

  .tour-btn-secondary {
    background: transparent;
    border: 1px solid var(--glass-border);
    color: var(--text-secondary);
  }

  .tour-btn-secondary:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .tour-btn-primary {
    background: var(--accent-primary);
    border: 1px solid var(--accent-primary);
    color: var(--c-on-accent);
    box-shadow: 0 0 12px rgba(var(--c-accent-rgb), 0.2);
  }

  .tour-btn-primary:hover {
    background: var(--accent-hover);
    box-shadow: 0 0 20px rgba(var(--c-accent-rgb), 0.3);
  }

  .tour-dismiss {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: var(--spacing-md);
    font-size: 11px;
    color: var(--text-muted);
    cursor: pointer;
  }

  .tour-dismiss input {
    accent-color: var(--accent-primary);
  }
</style>
