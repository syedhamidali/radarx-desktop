/**
 * The part of a project file the app owns: what was on screen (`view`), and how to put it back.
 *
 * Pure functions so they can be unit tested (projectState.test.mjs). The engine stores `view`
 * as it is given (engine/project.py); nothing here copies data.
 */

export interface ViewFile {
  /** Absolute path as opened (also sent in the project's file list). */
  path: string;
  filename: string;
  variable: string | null;
  sweep: number;
}

export interface ProjectView {
  version: 1;
  files: ViewFile[];
  /** Index into `files` of the active tab. */
  active: number;
}

export interface OpenFileLike {
  path: string;
  filename: string;
  selectedVariable: string | null;
  selectedSweep: number;
}

/** What to store: one entry per open file with its own variable and sweep. */
export function buildView(files: OpenFileLike[], activeIndex: number): ProjectView {
  return {
    version: 1,
    files: files
      .filter((f) => !!f.path)
      .map((f) => ({
        path: f.path,
        filename: f.filename,
        variable: f.selectedVariable,
        sweep: f.selectedSweep,
      })),
    active: Math.max(0, Math.min(activeIndex, files.length - 1)),
  };
}

/** Read a stored view defensively (older or damaged data must never break opening a project). */
export function parseView(raw: unknown): ProjectView {
  const empty: ProjectView = { version: 1, files: [], active: 0 };
  if (!raw || typeof raw !== 'object') return empty;
  const r = raw as Record<string, unknown>;
  const list = Array.isArray(r.files) ? r.files : [];
  const files: ViewFile[] = [];
  for (const f of list) {
    if (!f || typeof f !== 'object') continue;
    const o = f as Record<string, unknown>;
    if (typeof o.path !== 'string' || !o.path) continue;
    files.push({
      path: o.path,
      filename: typeof o.filename === 'string' ? o.filename : o.path.split(/[\\/]/).pop() ?? o.path,
      variable: typeof o.variable === 'string' ? o.variable : null,
      sweep: Number.isInteger(o.sweep) && (o.sweep as number) >= 0 ? (o.sweep as number) : 0,
    });
  }
  const active = Number.isInteger(r.active) ? (r.active as number) : 0;
  return { version: 1, files, active: Math.max(0, Math.min(active, Math.max(files.length - 1, 0))) };
}

export interface ResolvedFile {
  name: string;
  status: 'found' | 'changed' | 'missing';
  path: string | null;
}

export interface OpenPlan {
  /** Files to open, in the order of the project, with the selection to restore. */
  open: { path: string; variable: string | null; sweep: number; changed: boolean }[];
  /** Names of recorded files that could not be found. */
  missing: string[];
  /** Names of files found but changed since the project was saved. */
  changed: string[];
  /** Path (as it will be opened) of the file that becomes the active tab, or null. */
  activePath: string | null;
}

/**
 * Turn the engine's answer (`resolved`: where each file is now) and the stored view into what to
 * open. A file the engine could not find is never replaced by another one.
 */
export function planOpen(view: ProjectView, resolved: ResolvedFile[]): OpenPlan {
  const byName = new Map(resolved.map((r) => [r.name, r]));
  const plan: OpenPlan = { open: [], missing: [], changed: [], activePath: null };
  const stored = view.files.length > 0 ? view.files : resolved.map((r) => ({ path: r.path ?? '', filename: r.name, variable: null, sweep: 0 }));
  stored.forEach((f, i) => {
    const r = byName.get(f.filename) ?? resolved[i];
    if (!r || r.status === 'missing' || !r.path) {
      plan.missing.push(f.filename);
      return;
    }
    if (r.status === 'changed') plan.changed.push(f.filename);
    plan.open.push({ path: r.path, variable: f.variable, sweep: f.sweep, changed: r.status === 'changed' });
    if (i === view.active) plan.activePath = r.path;
  });
  if (!plan.activePath && plan.open.length > 0) plan.activePath = plan.open[0].path;
  return plan;
}
