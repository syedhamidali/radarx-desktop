import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

// Components are mounted with `mount()` (src/main.ts); the Svelte 4 `new Component({ target })` API
// is not used anywhere, so the legacy class wrapper is not compiled in. Legacy-syntax components
// (`export let`, `$:`) still work: that is the compiler's default mode, not this option.
export default {
  preprocess: vitePreprocess(),
};
