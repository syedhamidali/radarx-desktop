import { derived } from 'svelte/store';
import { settings, type RecentFile } from './settings';
import { basename } from '../utils/env';

const MAX_RECENT = 20;

function createRecentFilesStore() {
  const recentFiles = derived(settings, ($s) => $s.data.recentFiles);

  return {
    subscribe: recentFiles.subscribe,

    /** Add a file to recent list (moves to top if already present) */
    add(path: string, name?: string, size?: number): void {
      settings.update((s) => {
        const filtered = s.data.recentFiles.filter((f) => f.path !== path);
        const entry: RecentFile = {
          path,
          name: name ?? basename(path),
          openedAt: Date.now(),
          size,
        };
        const updated = [entry, ...filtered].slice(0, MAX_RECENT);
        return {
          ...s,
          data: { ...s.data, recentFiles: updated },
        };
      });
    },

    /** Remove a specific file from recent list */
    remove(path: string): void {
      settings.update((s) => ({
        ...s,
        data: {
          ...s.data,
          recentFiles: s.data.recentFiles.filter((f) => f.path !== path),
        },
      }));
    },

    /** Clear all recent files */
    clear(): void {
      settings.update((s) => ({
        ...s,
        data: { ...s.data, recentFiles: [] },
      }));
    },
  };
}

export const recentFiles = createRecentFilesStore();
