<script lang="ts">
  /** Shown instead of a view that needs PPI sweeps when the open file only holds RHI scans. */
  interface Props {
    title: string;
    detail?: string;
  }
  let {
    title,
    detail = 'This file only holds range-height (RHI) scans. Pick the 2D view to see them as a height-versus-range plot.',
  }: Props = $props();
</script>

<div class="ppi-only" role="status" data-testid="ppi-only-notice">
  <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true">
    <path d="M3 20h18" />
    <path d="M3 20L17 6" />
    <path d="M3 20c4 0 7-3 8-7" stroke-dasharray="2 2" />
  </svg>
  <h2>{title}</h2>
  <p>{detail}</p>
</div>

<style>
  .ppi-only {
    flex: 1;
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-md, 12px);
    padding: var(--spacing-xl, 24px);
    text-align: center;
    color: var(--text-muted);
    background: var(--bg-primary);
  }
  h2 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-secondary);
  }
  p {
    margin: 0;
    max-width: 440px;
    font-size: 13px;
    line-height: 1.5;
  }
</style>
