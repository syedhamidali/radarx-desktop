<script lang="ts">
  /**
   * The very first thing the app mounts. A new install asks for an account; a returning one opens
   * straight into the app (also with no network, for up to the offline grace period). The app itself
   * is only fetched once the gate is open.
   */
  import { onMount } from 'svelte';
  import type { Component } from 'svelte';
  import { auth, initAuth, profileIncomplete } from '../stores/auth';
  import AuthGate from './AuthGate.svelte';

  let App: Component | null = $state(null);

  const open = $derived(($auth.gate === 'open' || $auth.gate === 'open-offline') && !profileIncomplete($auth));

  onMount(() => {
    void initAuth();
  });

  $effect(() => {
    if (open && !App) void import('../../App.svelte').then((m) => (App = m.default));
  });
</script>

{#if open && App}
  <App />
{:else if !open}
  <AuthGate />
{/if}
