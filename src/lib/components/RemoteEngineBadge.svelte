<script lang="ts">
  /** The status-bar mark that says the app is using an engine on another computer ("REMOTE ENGINE host:port"). */
  import { remoteEngine } from '../stores/remoteEngine';
  import { remoteStatusText } from '../utils/remoteEngine';
</script>

{#if $remoteEngine.mode === 'remote'}
  <span
    class="remote-badge mono"
    class:bad={$remoteEngine.phase !== 'connected'}
    title="The app is using the engine of rxd serve on another computer, through an ssh forward. Settings > Engine returns to the built-in engine."
    data-testid="remote-engine-badge"
  >{remoteStatusText($remoteEngine.phase, $remoteEngine.hostPort)}</span>
{/if}

<style>
  .remote-badge {
    font-size: var(--fs-2xs); font-weight: 600; letter-spacing: 0.06em; padding: 1px 6px; margin-left: 2px;
    color: var(--c-caution); border: 1px solid var(--c-caution); border-radius: var(--radius-sm); white-space: nowrap;
  }
  .remote-badge.bad { color: var(--c-alarm); border-color: var(--c-alarm); }
</style>
