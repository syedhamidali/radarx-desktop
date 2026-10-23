/**
 * Run record and project file, from the app's side (docs/CLI.md "Cite and repeat").
 *
 *   -> {type:'get_run_record'}   <- {type:'run_record', record, methods}
 *   -> {type:'save_project', path, files, view, recipe}   <- {type:'project_saved'}
 *   -> {type:'load_project', path}                         <- {type:'project_loaded', project}
 *
 * The engine does the work (engine/runrecord.py, engine/project.py); this file asks the user
 * where to save / what to open, shows the outcome, and restores the open files.
 */
import { get } from 'svelte/store';
import { wsManager } from './websocket';
import { addToast } from '../stores/toasts';
import { openFiles, activeFileId, setActiveFile, updateFileSelection } from '../stores/fileManager';
import { draft, loadRecipe, recipePayload } from '../stores/pipeline';
import { saveTextExport } from '../stores/modules';
import { inTauri } from './env';
import { buildView, parseView, planOpen, type ResolvedFile } from './projectState';

function once<T = any>(type: string, timeoutMs = 20_000): Promise<T> {
  return new Promise((resolve, reject) => {
    const done = (fn: () => void) => {
      clearTimeout(timer);
      offOk();
      offErr();
      fn();
    };
    const offOk = wsManager.onMessage(type, (m) => done(() => resolve(m as T)));
    const offErr = wsManager.onMessage('error', (m) => done(() => reject(new Error(String(m.message ?? 'failed')))));
    const timer = setTimeout(() => done(() => reject(new Error('No response from the analysis engine'))), timeoutMs);
  });
}

export async function fetchRunRecord(): Promise<{ record: any; methods: string }> {
  const reply = once('run_record');
  void wsManager.send({ type: 'get_run_record' });
  return reply;
}

/** Copy the methods text of this session to the clipboard. */
export async function copyMethodsText(): Promise<void> {
  try {
    const { record, methods } = await fetchRunRecord();
    if (!record.steps?.length) {
      addToast('info', 'Nothing has been processed yet, so there is no methods text. Run a module, a pipeline or an export first.', 7000);
      return;
    }
    await navigator.clipboard.writeText(methods);
    addToast('success', `Methods text for ${record.steps.length} step${record.steps.length === 1 ? '' : 's'} copied.`, 5000);
  } catch (err) {
    addToast('error', `Could not copy the methods text: ${err instanceof Error ? err.message : String(err)}`, 8000);
  }
}

/** Save the run record (JSON) of this session. */
export async function saveRunRecord(): Promise<void> {
  try {
    const { record } = await fetchRunRecord();
    await saveTextExport('radarx_run_record', JSON.stringify(record, null, 2) + '\n', {
      label: 'Run record (JSON)',
      ext: 'json',
      mime: 'application/json',
    });
  } catch (err) {
    addToast('error', `Could not save the run record: ${err instanceof Error ? err.message : String(err)}`, 8000);
  }
}

export async function saveProject(): Promise<void> {
  if (!inTauri()) {
    addToast('info', 'Projects are a feature of the desktop app.', 5000);
    return;
  }
  const files = get(openFiles);
  if (files.length === 0) {
    addToast('info', 'Open a file first: a project records the files that are open.', 6000);
    return;
  }
  try {
    const { save } = await import('@tauri-apps/plugin-dialog');
    const path = await save({
      title: 'Save project',
      defaultPath: 'project.radarxproj',
      filters: [{ name: 'radarx project', extensions: ['radarxproj'] }],
    });
    if (!path) return;
    const id = get(activeFileId);
    const view = buildView(files, Math.max(0, files.findIndex((f) => f.id === id)));
    const d = get(draft);
    const reply = once('project_saved');
    void wsManager.send({
      type: 'save_project',
      path,
      files: view.files.map((f) => f.path),
      view,
      recipe: d.steps.length > 0 ? recipePayload(d) : null,
    });
    const done = await reply;
    addToast('success', `Project saved: ${done.path}`, 6000);
  } catch (err) {
    addToast('error', `Could not save the project: ${err instanceof Error ? err.message : String(err)}`, 8000);
  }
}

export async function openProject(): Promise<void> {
  if (!inTauri()) {
    addToast('info', 'Projects are a feature of the desktop app.', 5000);
    return;
  }
  try {
    const { open } = await import('@tauri-apps/plugin-dialog');
    const picked = await open({
      title: 'Open project',
      multiple: false,
      filters: [{ name: 'radarx project', extensions: ['radarxproj'] }],
    });
    const path = Array.isArray(picked) ? picked[0] : picked;
    if (!path) return;
    const reply = once('project_loaded');
    void wsManager.send({ type: 'load_project', path });
    const { project } = await reply;
    applyProject(project);
  } catch (err) {
    addToast('error', `Could not open the project: ${err instanceof Error ? err.message : String(err)}`, 8000);
  }
}

/** Open the files the project found and put back each file's variable and sweep. */
export function applyProject(project: any): void {
  const resolved: ResolvedFile[] = Array.isArray(project.resolved) ? project.resolved : [];
  const plan = planOpen(parseView(project.view), resolved);
  if (project.recipe && Array.isArray(project.recipe.steps)) loadRecipe(project.recipe);
  const already = new Set(get(openFiles).map((f) => f.path));
  const wanted = new Map(plan.open.map((o) => [o.path, o]));
  const pending = new Set(plan.open.map((o) => o.path).filter((p) => !already.has(p)));
  let off: () => void = () => {};
  let finished = false;
  const apply = () => {
    for (const f of get(openFiles)) {
      const o = wanted.get(f.path);
      if (o && pending.delete(f.path)) {
        const vars = f.variables ?? [];
        updateFileSelection(f.id, o.variable && vars.includes(o.variable) ? o.variable : f.selectedVariable, Math.min(o.sweep, Math.max((f.sweeps?.length ?? 1) - 1, 0)));
      }
    }
    if (pending.size === 0 && !finished) {
      finished = true;
      off();
      const active = get(openFiles).find((f) => f.path === plan.activePath);
      if (active) setActiveFile(active.id, { force: true });
    }
  };
  off = openFiles.subscribe(apply);
  if (finished) off();
  for (const path of pending) wsManager.openFile(path);
  if (plan.missing.length > 0) {
    addToast('warning', `Not found, so not opened: ${plan.missing.join(', ')}. The project keeps the recorded paths.`, 12000);
  }
  if (plan.changed.length > 0) {
    addToast('warning', `Changed since the project was saved (size or date differs): ${plan.changed.join(', ')}. Results may differ from the saved work.`, 12000);
  }
  if (plan.open.length === 0 && plan.missing.length === 0) addToast('info', 'The project lists no files.', 5000);
}
