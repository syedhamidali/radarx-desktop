<script lang="ts">
  // The xradar mark: a radar scope with a sweep wedge, one live echo (cyan) and one derived echo
  // (violet). Colours come from the theme tokens, so it follows the active theme. The standalone
  // file with fixed colours is public/logo.svg (also the source of the app icons).
  export let size = 28;
  /** Rotate the sweep (only the wedge moves, ~6 s per turn; off under prefers-reduced-motion). */
  export let animate = false;
  export let title = '';

  const uid = Math.random().toString(36).slice(2, 8);
</script>

<svg
  class="logo-mark"
  width={size}
  height={size}
  viewBox="0 0 64 64"
  fill="none"
  role={title ? 'img' : undefined}
  aria-label={title || undefined}
  aria-hidden={title ? undefined : 'true'}
>
  <defs>
    <linearGradient id="wedge-{uid}" x1="52" y1="15" x2="22" y2="7" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="var(--c-accent)" stop-opacity="0.85" />
      <stop offset="1" stop-color="var(--c-accent)" stop-opacity="0" />
    </linearGradient>
  </defs>
  <circle cx="32" cy="32" r="27" stroke="var(--c-accent)" stroke-width="2.5" />
  <circle cx="32" cy="32" r="16" stroke="var(--c-accent)" stroke-width="1.5" opacity="0.5" />
  <path d="M32 3v6M32 55v6M3 32h6M55 32h6" stroke="var(--c-accent)" stroke-width="2" stroke-linecap="round" />
  <g class="sweep" class:animate style="transform-origin: 32px 32px">
    <path d="M32 32 L22.8 6.6 A27 27 0 0 1 52.7 14.6 Z" fill="url(#wedge-{uid})" />
    <path d="M32 32 L52.7 14.6" stroke="var(--c-accent-strong)" stroke-width="2" stroke-linecap="round" />
  </g>
  <circle cx="44" cy="21" r="3" fill="var(--c-accent-strong)" />
  <circle cx="24" cy="42" r="2.4" fill="var(--c-derived)" />
  <circle cx="32" cy="32" r="2.6" fill="var(--c-accent-strong)" />
</svg>

<style>
  .logo-mark { flex-shrink: 0; display: block; }
  .sweep.animate { animation: logo-spin 6s linear infinite; }
  @keyframes logo-spin { to { transform: rotate(360deg); } }
</style>
