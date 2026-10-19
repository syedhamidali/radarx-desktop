import { mount } from 'svelte';
import App from './App.svelte';
import './lib/styles/fonts.css';
import './lib/styles/tokens.css';
import './lib/styles/theme.css';
import { applySavedTheme } from './lib/utils/theme';

applySavedTheme(); // first paint already in the right theme

const app = mount(App, {
  target: document.getElementById('app')!,
});

export default app;
