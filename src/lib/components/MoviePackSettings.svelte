<script lang="ts">
  /**
   * Settings > Data > Movie components: the optional download of the installed app (Playwright,
   * which the Movie Studio needs to drive the Chrome that draws and encodes a film). Shows what is installed, its size
   * and where it lives, with Remove. Nothing is downloaded here: the Movie Studio asks first.
   */
  import { onMount } from 'svelte';
  import { moviePack, refreshMoviePack, removeMoviePack } from '../stores/movieStudio';

  const p = $derived($moviePack);
  const mb = (n: number) => `${(n / 1e6).toFixed(0)} MB`;

  onMount(() => {
    refreshMoviePack();
  });
</script>

<div class="section-header">
  <h3>Movie components</h3>
  {#if p?.installed}
    <button class="reset-btn small" on:click={removeMoviePack}>Remove</button>
  {/if}
</div>
{#if !p}
  <p class="empty-message">Checking...</p>
{:else if p.installed}
  <p class="empty-message">
    Installed for Radarx {p.pack_version}: {p.components.map((c) => `${c.name} ${c.version}`).join(', ')}, {mb(p.size)} on disk.
    {#if p.problem}<br /><span class="mp-warn">{p.problem}</span>{/if}
  </p>
  <p class="empty-message mono small">{p.path}</p>
{:else}
  <p class="empty-message">
    Not installed. The Movie Studio needs them to render a film (Playwright drives your Google Chrome, which draws and encodes the MP4) and offers the download (about 45 MB) when you open it;
    {#if !p.needed}this engine has its own copies or gets them with the Python package.{:else}they are kept in <span class="mono">{p.root}</span>.{/if}
  </p>
{/if}

<style>
  .mp-warn { color: var(--c-caution); }
  .mono { font-family: var(--font-mono); overflow-wrap: anywhere; }
  .small { font-size: var(--fs-xs); }
</style>
