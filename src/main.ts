import { mount } from 'svelte';
import './app.css';

const target = document.getElementById('app')!;

// `?clip=1`: the offline clip renderer (tools/storm_clip), no sidecar needed; otherwise the app.
const app = new URLSearchParams(location.search).has('clip')
  ? import('./lib/components/StormClipView.svelte').then((m) => mount(m.default, { target }))
  : import('./App.svelte').then((m) => mount(m.default, { target }));

export default app;
