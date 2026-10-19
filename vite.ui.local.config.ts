// local helper (untracked): same config as vite.config.ts but with a private dep cache, so that several dev servers
// sharing one node_modules (git worktrees) do not corrupt each other's pre-bundled Svelte
import base from "./vite.config";
export default (env: any) => ({ ...(base as any)(env), cacheDir: "/private/tmp/claude-502/vite-cache-ui" });
