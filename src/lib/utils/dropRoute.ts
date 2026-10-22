/**
 * Where dropped paths go: the single-file opener (one or two plain files, or a Zarr store) or the
 * Autopilot (many paths, or any folder, which the engine expands; docs/AUTOPILOT.md).
 *
 * `kinds` comes from the shell's `path_kinds` command ("file" | "zarr" | "folder" | "missing");
 * when it is unknown (an older shell, the command failed) only the count decides, as before.
 */

export type PathKind = 'file' | 'zarr' | 'folder' | 'missing';
export type DropRoute = 'open' | 'autopilot' | 'none';

/** Dropping at least this many paths opens Autopilot instead of opening every file. */
export const AUTOPILOT_DROP_MIN = 3;

export function routeDrop(paths: string[], kinds?: (PathKind | string)[] | null): DropRoute {
  const list = paths.filter(Boolean);
  if (list.length === 0) return 'none';
  if (list.length >= AUTOPILOT_DROP_MIN) return 'autopilot';
  if (kinds && kinds.length === paths.length && kinds.some((k) => k === 'folder')) return 'autopilot';
  return 'open';
}
