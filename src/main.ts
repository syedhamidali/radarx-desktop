import { mount } from 'svelte';
import './lib/styles/fonts.css';
import './lib/styles/tokens.css';
import './lib/styles/theme.css';
import { applySavedTheme } from './lib/utils/theme';

applySavedTheme(); // first paint already in the right theme

const target = document.getElementById('app')!;

// `?clip=1`: the offline clip renderer (tools/storm_clip), no sidecar needed; `?tornado=1` is the tornado
// simulation preview (tools/tornado_sim), which StormClipView hosts so that this entry chunk does not grow;
// otherwise the app.
const app = /clip|tornado/.test(location.search)
  ? import('./lib/components/StormClipView.svelte').then((m) => mount(m.default, { target }))
  : import('./App.svelte').then((m) => mount(m.default, { target }));

export default app;
