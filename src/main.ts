import { mount } from 'svelte';
import './lib/styles/fonts.css';
import './lib/styles/tokens.css';
import './lib/styles/theme.css';
import { applySavedTheme } from './lib/utils/theme';

applySavedTheme(); // first paint already in the right theme

const target = document.getElementById('app')!;

// `?clip=1`: the offline clip renderer (tools/storm_clip), no sidecar needed; otherwise the app.
// `?tornado=1`: the standalone tornado simulation preview (tools/tornado_sim), also lazy.
const params = new URLSearchParams(location.search);
const app = params.has('clip')
  ? import('./lib/components/StormClipView.svelte').then((m) => mount(m.default, { target }))
  : params.has('tornado')
    ? import('./lib/components/TornadoPreview.svelte').then((m) => mount(m.default, { target }))
    : import('./App.svelte').then((m) => mount(m.default, { target }));

export default app;
