/**
 * Theme switching: "Mission control" (dark) and "Clean room" (light).
 *
 * The palettes live in src/lib/styles/tokens.css; this module only decides which one is active and
 * records it on <html> (data-theme, plus the legacy .theme-light / .theme-auto classes that a few
 * older components still target). The basemap tint (utils/basemaps.ts) reads the same mode.
 */

export type ThemeMode = 'dark' | 'light' | 'auto';

function osPrefersLight(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-color-scheme: light)').matches;
}

/** The theme actually shown for a setting (auto follows the OS). */
export function resolveTheme(mode: ThemeMode): 'dark' | 'light' {
  if (mode === 'light') return 'light';
  if (mode === 'auto') return osPrefersLight() ? 'light' : 'dark';
  return 'dark';
}

/** Write the theme on <html>. Safe to call repeatedly (also on every OS preference change). */
export function applyThemeMode(mode: ThemeMode): 'dark' | 'light' {
  const root = document.documentElement;
  const resolved = resolveTheme(mode);
  root.dataset.theme = resolved;
  root.classList.toggle('theme-light', mode === 'light');
  root.classList.toggle('theme-auto', mode === 'auto');
  root.style.colorScheme = resolved;
  return resolved;
}

/** Before the app mounts: apply the saved mode so the first paint already has the right theme. */
export function applySavedTheme(): void {
  let mode: ThemeMode = 'dark';
  try {
    const saved = JSON.parse(localStorage.getItem('xradar-settings') ?? 'null')?.theme?.mode;
    if (saved === 'dark' || saved === 'light' || saved === 'auto') mode = saved;
  } catch {
    /* unreadable storage: default theme */
  }
  applyThemeMode(mode);
}
