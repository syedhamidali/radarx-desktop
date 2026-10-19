<script lang="ts">
  // The xradar app icon (src/lib/assets/icon.png is a copy of src-tauri/icons/icon-256.png),
  // shown unchanged. It is only framed: a hairline ring, and while `animate` is on a faint sweep
  // that turns around it.
  import iconUrl from '../assets/icon.png';

  export let size = 28;
  /** Turn a faint sweep around the icon (off under prefers-reduced-motion). */
  export let animate = false;
  export let title = '';
</script>

<span class="logo-mark" style="--s: {size}px" class:animate>
  <img
    src={iconUrl}
    width={size}
    height={size}
    alt={title}
    aria-hidden={title ? undefined : 'true'}
    draggable="false"
  />
</span>

<style>
  .logo-mark {
    position: relative;
    display: inline-block;
    width: var(--s);
    height: var(--s);
    flex-shrink: 0;
    border-radius: 22%;
    box-shadow: 0 0 0 1px var(--c-border-strong);
  }
  .logo-mark img { display: block; width: 100%; height: 100%; border-radius: 22%; }
  /* a bright arc turning around the rounded square (border-gradient mask trick) */
  .logo-mark.animate::after {
    content: '';
    position: absolute;
    inset: -3px;
    padding: 2px;
    border-radius: 28%;
    background: conic-gradient(from var(--a, 0deg), transparent 0 270deg, rgba(var(--c-accent-rgb), 0.9) 360deg);
    -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite: xor;
    mask: linear-gradient(#000 0 0) content-box exclude, linear-gradient(#000 0 0);
    animation: logo-spin 2.4s linear infinite;
  }
  @property --a { syntax: '<angle>'; inherits: false; initial-value: 0deg; }
  @keyframes logo-spin { to { --a: 360deg; } }
</style>
