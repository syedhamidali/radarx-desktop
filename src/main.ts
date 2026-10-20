import { mount } from 'svelte';
import './lib/styles/fonts.css';
import './lib/styles/tokens.css';
import './lib/styles/theme.css';
import { applySavedTheme } from './lib/utils/theme';

applySavedTheme(); // first paint already in the right theme

const target = document.getElementById('app')!;

// `?clip=1`: the offline clip renderer (tools/storm_clip), no sidecar needed; otherwise the account
// gate, which opens the app (see AuthRoot.svelte).
const app = new URLSearchParams(location.search).has('clip')
  ? import('./lib/components/StormClipView.svelte').then((m) => mount(m.default, { target }))
  : import('./lib/components/AuthRoot.svelte').then((m) => mount(m.default, { target }));

export default app;
