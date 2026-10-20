<script lang="ts">
  /** Status-bar chip with the signed-in name; opens the Account dialog. Shows when working offline. */
  import { auth } from '../stores/auth';
  import { AUTH_DISABLED, OFFLINE_GRACE_DAYS } from '../auth/config';
  import { graceDaysLeft } from '../auth/gate';
  import { lazy } from '../utils/lazy.svelte';

  const dlg = lazy(() => import('./AccountDialog.svelte'));
  let open = $state(false);

  const a = $derived($auth);
  const label = $derived(a.profile?.display_name || a.email || 'Account');
  const offline = $derived(a.gate === 'open-offline');
  const left = $derived(graceDaysLeft(a.lastVerifiedAt, Date.now(), OFFLINE_GRACE_DAYS));

  function show() {
    open = true;
    dlg.load();
  }
</script>

{#if !AUTH_DISABLED}
  <button class="chip" onclick={show} title={offline ? `Offline: radarx keeps working for ${left} more day(s) without a connection check.` : 'Your account'}>
    <span class="dot" class:off={offline} aria-hidden="true"></span>
    <span class="who">{label}</span>{#if offline}<span class="off-text">offline</span>{/if}
  </button>
  {#if dlg.component && open}<dlg.component onclose={() => (open = false)} />{/if}
{/if}

<style>
  .chip {
    display: inline-flex; align-items: center; gap: 6px; background: none; border: none;
    color: var(--c-text-dim); font-size: var(--fs-xs); cursor: pointer; padding: 0 8px; height: 100%;
  }
  .chip:hover { color: var(--c-text); }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--c-nominal); }
  .dot.off { background: var(--c-caution); }
  .who { max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .off-text { color: var(--c-caution); }
</style>
