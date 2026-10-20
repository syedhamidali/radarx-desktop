/**
 * Processing pipelines ("recipes"): chain cleaning / processing steps on one variable and get ONE
 * derived variable back (see docs/PIPELINES.md).
 *
 *   -> list_pipeline_steps            <- pipeline_steps {steps}
 *   -> list_recipes                   <- recipes_list {recipes, errors, dir}
 *   -> save_recipe / delete_recipe    <- recipe_saved / recipe_deleted, then recipes_list
 *   -> export_recipe_script           <- recipe_script {filename, script}
 *   -> preview_pipeline {sweep}       <- pipeline_preview {steps, blocks, ...} + module_field frames
 *   -> run_pipeline {all_sweeps}      <- pipeline_result {...} + derived_variables
 *   -> cancel_pipeline                 (stops after the current step)
 *   any failure                       <- pipeline_error {message, cancelled?}
 *
 * The preview's Before / After / Removed-by-step grids arrive as ordinary `module_field` payloads
 * of the pseudo module "pipeline", so they are shown on the PPI by the module-field machinery.
 */

import { writable, derived, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { connectionStatus, selectedSweep } from './radarData';
import { activeFileId, getActiveFile } from './fileManager';
import { addToast } from './toasts';
import { registerJobSource } from './jobSources';
import {
  moduleResults,
  showFieldOnPpi,
  hideField,
  saveTextExport,
  type ModuleParam,
  type ModuleResult,
  type ModuleBlock,
} from './modules';
import {
  dropDerivedCaches,
  normaliseDerived,
  mergeDerivedForFile,
  refreshIfSelected,
} from './derived';

// ── Types ──────────────────────────────────────────────────────────────────

export interface StepSpec {
  id: string;
  title: string;
  description: string;
  category: string;
  kind: 'filter' | 'transform';
  needs: string[];
  optional: string[];
  transforms: string | null;
  params: ModuleParam[];
  packages: string[];
  uses_packages: string[];
}

export interface RecipeStep {
  step_id: string;
  params: Record<string, any>;
  enabled: boolean;
  /** Client-only key for keyed lists. */
  uid?: string;
}

export interface Recipe {
  name: string;
  description: string;
  target: string;
  apply_to_all: boolean;
  steps: RecipeStep[];
  builtin: boolean;
}

export interface StepStat {
  index: number;
  step_id: string;
  title: string;
  enabled: boolean;
  status: 'ok' | 'skipped' | 'disabled';
  reason: string;
  removed: number;
  modified: number;
  filled: number;
  valid: number;
  valid_pct: number;
  ms: number;
  notes: string[];
}

export interface PreviewState {
  sweep: number;
  recipe: string;
  target: string;
  steps: StepStat[];
  valid_before: number;
  valid_after: number;
  notes: string[];
  elapsed_ms: number;
  blocks: ModuleBlock[];
}

export interface PipelineRunResult {
  recipe: string;
  target: string;
  name: string | null;
  steps: StepStat[];
  skipped: { sweep: number; reason: string }[];
  sweeps: number[];
  elapsed_ms: number;
}

export type PipelineView = 'before' | 'after' | 'removed' | null;

// ── Stores ─────────────────────────────────────────────────────────────────

export const stepCatalog = writable<StepSpec[]>([]);
export const recipes = writable<Recipe[]>([]);
export const recipeErrors = writable<{ file: string; error: string }[]>([]);
export const recipesDir = writable<string>('');
export const previewResult = writable<PreviewState | null>(null);
export const runResult = writable<PipelineRunResult | null>(null);
export const pipelineBusy = writable<'preview' | 'run' | null>(null);
registerJobSource('pipeline', derived(pipelineBusy, (b) => (b ? 1 : 0)));
export const pipelineView = writable<PipelineView>(null);

let uidCounter = 0;
const nextUid = () => `s${++uidCounter}`;

function blankRecipe(): Recipe {
  return { name: 'My recipe', description: '', target: 'VRADH', apply_to_all: false, steps: [], builtin: false };
}

/** The recipe being edited (the panel's working copy). */
export const draft = writable<Recipe>(blankRecipe());

export const catalogById = derived(stepCatalog, ($c) => new Map($c.map((s) => [s.id, s])));

// ── Editing ────────────────────────────────────────────────────────────────

function defaultsFor(spec: StepSpec | undefined): Record<string, any> {
  const out: Record<string, any> = {};
  for (const p of spec?.params ?? []) out[p.name] = p.default;
  return out;
}

function withUids(r: Recipe): Recipe {
  return { ...r, steps: r.steps.map((s) => ({ ...s, uid: s.uid ?? nextUid() })) };
}

export function newRecipe(target = 'VRADH'): void {
  draft.set({ ...blankRecipe(), target });
  resetViews();
}

export function loadRecipe(r: Recipe): void {
  const spec = get(catalogById);
  draft.set(
    withUids({
      ...r,
      steps: r.steps.map((s) => ({
        ...s,
        params: { ...defaultsFor(spec.get(s.step_id)), ...(s.params ?? {}) },
      })),
    }),
  );
  resetViews();
}

export function setDraftField<K extends 'name' | 'description' | 'target' | 'apply_to_all'>(
  key: K,
  value: Recipe[K],
): void {
  draft.update((d) => ({ ...d, [key]: value }));
}

export function addStep(stepId: string): void {
  const spec = get(catalogById).get(stepId);
  if (!spec) return;
  draft.update((d) => ({
    ...d,
    steps: [...d.steps, { step_id: stepId, params: defaultsFor(spec), enabled: true, uid: nextUid() }],
  }));
}

export function removeStep(uid: string): void {
  draft.update((d) => ({ ...d, steps: d.steps.filter((s) => s.uid !== uid) }));
}

export function moveStep(uid: string, delta: -1 | 1): void {
  draft.update((d) => {
    const i = d.steps.findIndex((s) => s.uid === uid);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= d.steps.length) return d;
    const steps = d.steps.slice();
    [steps[i], steps[j]] = [steps[j], steps[i]];
    return { ...d, steps };
  });
}

export function toggleStep(uid: string, enabled: boolean): void {
  draft.update((d) => ({ ...d, steps: d.steps.map((s) => (s.uid === uid ? { ...s, enabled } : s)) }));
}

export function setStepParam(uid: string, name: string, value: any): void {
  draft.update((d) => ({
    ...d,
    steps: d.steps.map((s) => (s.uid === uid ? { ...s, params: { ...s.params, [name]: value } } : s)),
  }));
}

export function resetStepParams(uid: string): void {
  const cat = get(catalogById);
  draft.update((d) => ({
    ...d,
    steps: d.steps.map((s) => (s.uid === uid ? { ...s, params: defaultsFor(cat.get(s.step_id)) } : s)),
  }));
}

/** The draft as the plain object the sidecar validates. */
export function recipePayload(d: Recipe = get(draft)): Record<string, any> {
  return {
    name: d.name.trim(),
    description: d.description,
    target: d.target.trim(),
    apply_to_all: d.apply_to_all,
    steps: d.steps.map((s) => ({ step_id: s.step_id, params: s.params, enabled: s.enabled })),
  };
}

// ── Protocol ───────────────────────────────────────────────────────────────

export function requestCatalog(): void {
  void wsManager.send({ type: 'list_pipeline_steps' });
  void wsManager.send({ type: 'list_recipes' });
}

function fileMsg(base: Record<string, any>): Record<string, any> {
  const f = getActiveFile();
  return f ? { ...base, file_id: f.id } : base;
}

export function previewPipeline(): void {
  if (get(pipelineBusy)) return;
  const d = get(draft);
  if (d.steps.length === 0) {
    addToast('warning', 'Add at least one step first');
    return;
  }
  pipelineBusy.set('preview');
  void wsManager.send(
    fileMsg({ type: 'preview_pipeline', sweep: get(selectedSweep), recipe: recipePayload(d) }),
  );
}

export function runPipeline(allSweeps = true): void {
  if (get(pipelineBusy)) return;
  const d = get(draft);
  if (d.steps.length === 0) {
    addToast('warning', 'Add at least one step first');
    return;
  }
  pipelineBusy.set('run');
  void wsManager.send(
    fileMsg({ type: 'run_pipeline', all_sweeps: allSweeps, sweep: get(selectedSweep), recipe: recipePayload(d) }),
  );
}

export function cancelPipeline(): void {
  void wsManager.send(fileMsg({ type: 'cancel_pipeline' }));
}

export function saveCurrentRecipe(): void {
  void wsManager.send({ type: 'save_recipe', recipe: recipePayload() });
}

export function deleteRecipe(name: string): void {
  void wsManager.send({ type: 'delete_recipe', name });
}

export function requestScript(): void {
  void wsManager.send({ type: 'export_recipe_script', recipe: recipePayload() });
}

// ── Preview display ────────────────────────────────────────────────────────

const VIEW_FIELD: Record<Exclude<PipelineView, null>, string> = {
  before: 'Before',
  after: 'After',
  removed: 'Removed by step',
};

function resetViews(): void {
  previewResult.set(null);
  pipelineView.set(null);
  if (get(moduleResults)['pipeline']) {
    moduleResults.update((r) => {
      const { pipeline: _drop, ...rest } = r;
      return rest;
    });
  }
}

/** Show the Before / After / Removed grid of the last preview on the PPI (null = normal view). */
export function showPreviewView(view: PipelineView): void {
  const prev = get(previewResult);
  if (view === null || !prev) {
    pipelineView.set(null);
    hideField();
    return;
  }
  // The module list reload drops unknown module results: put the blocks back before showing.
  const res: ModuleResult = {
    module: 'pipeline',
    sweep: prev.sweep,
    params: {},
    elapsed_ms: prev.elapsed_ms,
    blocks: prev.blocks,
    features: [],
  };
  moduleResults.update((r) => ({ ...r, pipeline: res }));
  if (showFieldOnPpi('pipeline', VIEW_FIELD[view])) pipelineView.set(view);
}

// ── Handlers ───────────────────────────────────────────────────────────────

function handleSteps(msg: any): void {
  const steps: StepSpec[] = (Array.isArray(msg.steps) ? msg.steps : []).map((s: any) => ({
    id: String(s.id),
    title: String(s.title ?? s.id),
    description: String(s.description ?? ''),
    category: String(s.category ?? 'Other'),
    kind: s.kind === 'transform' ? 'transform' : 'filter',
    needs: Array.isArray(s.needs) ? s.needs.map(String) : [],
    optional: Array.isArray(s.optional) ? s.optional.map(String) : [],
    transforms: typeof s.transforms === 'string' ? s.transforms : null,
    params: Array.isArray(s.params) ? s.params : [],
    packages: Array.isArray(s.packages) ? s.packages.map(String) : [],
    uses_packages: Array.isArray(s.uses_packages) ? s.uses_packages.map(String) : [],
  }));
  stepCatalog.set(steps);
}

function normaliseRecipe(raw: any): Recipe {
  return {
    name: String(raw?.name ?? ''),
    description: String(raw?.description ?? ''),
    target: String(raw?.target ?? 'VRADH'),
    apply_to_all: raw?.apply_to_all === true,
    builtin: raw?.builtin === true,
    steps: (Array.isArray(raw?.steps) ? raw.steps : []).map((s: any) => ({
      step_id: String(s.step_id),
      params: s.params && typeof s.params === 'object' ? s.params : {},
      enabled: s.enabled !== false,
    })),
  };
}

function handleRecipes(msg: any): void {
  recipes.set((Array.isArray(msg.recipes) ? msg.recipes : []).map(normaliseRecipe));
  recipeErrors.set(
    (Array.isArray(msg.errors) ? msg.errors : []).map((e: any) => ({
      file: String(e?.file ?? '?'),
      error: String(e?.error ?? ''),
    })),
  );
  recipesDir.set(String(msg.dir ?? ''));
}

function handlePreview(msg: any): void {
  pipelineBusy.set(null);
  const prev: PreviewState = {
    sweep: Number(msg.sweep),
    recipe: String(msg.recipe ?? ''),
    target: String(msg.target ?? ''),
    steps: Array.isArray(msg.steps) ? msg.steps : [],
    valid_before: Number(msg.valid_before) || 0,
    valid_after: Number(msg.valid_after) || 0,
    notes: Array.isArray(msg.notes) ? msg.notes.map(String) : [],
    elapsed_ms: Number(msg.elapsed_ms) || 0,
    blocks: Array.isArray(msg.blocks) ? msg.blocks : [],
  };
  previewResult.set(prev);
  runResult.set(null);
  // Register the blocks now; the binary grids follow and `showFieldOnPpi` waits for them.
  moduleResults.update((r) => ({
    ...r,
    pipeline: {
      module: 'pipeline',
      sweep: prev.sweep,
      params: {},
      elapsed_ms: prev.elapsed_ms,
      blocks: prev.blocks,
      features: [],
    },
  }));
  // The grids arrive right after this message; show "After" once they have.
  setTimeout(() => {
    if (get(previewResult) === prev) showPreviewView('after');
  }, 250);
}

function handleResult(msg: any): void {
  pipelineBusy.set(null);
  const fileId: string | null =
    typeof msg.file_id === 'string' && msg.file_id ? msg.file_id : (get(activeFileId) ?? null);
  const made = (Array.isArray(msg.derived) ? msg.derived : [])
    .map((raw: any) => normaliseDerived(raw, 'pipeline'))
    .filter((d: any) => d !== null);
  const names = made.map((d: any) => d.name as string);
  if (fileId && made.length > 0) {
    dropDerivedCaches(names); // recomputed under the same name: cached sweeps are stale
    mergeDerivedForFile(fileId, made);
    refreshIfSelected(names);
  }
  const result: PipelineRunResult = {
    recipe: String(msg.recipe ?? ''),
    target: String(msg.target ?? ''),
    name: typeof msg.name === 'string' ? msg.name : null,
    steps: Array.isArray(msg.steps) ? msg.steps : [],
    skipped: (Array.isArray(msg.skipped) ? msg.skipped : [])
      .filter((x: any) => x && Number.isInteger(Number(x.sweep)))
      .map((x: any) => ({ sweep: Number(x.sweep), reason: String(x.reason ?? '') })),
    sweeps: made[0]?.sweeps ?? [],
    elapsed_ms: Number(msg.elapsed_ms) || 0,
  };
  runResult.set(result);
  const n = result.skipped.length;
  const label = made[0]?.label ?? result.recipe;
  addToast(
    n > 0 ? 'warning' : 'success',
    `Added "${label}" for ${result.sweeps.length} sweep${result.sweeps.length === 1 ? '' : 's'}` +
      (n > 0 ? ` (${n} sweep${n === 1 ? '' : 's'} skipped)` : '') +
      ' - choose it in the variable list',
    8000,
  );
}

function handleError(msg: any): void {
  pipelineBusy.set(null);
  if (msg.cancelled) addToast('info', 'Pipeline cancelled', 3000);
  else addToast('error', `Pipeline: ${msg.message ?? 'failed'}`, 8000);
}

function handleScript(msg: any): void {
  const name = String(msg.filename ?? 'pipeline.py').replace(/\.py$/, '');
  void saveTextExport(name, String(msg.script ?? ''), { label: 'Python script', ext: 'py', mime: 'text/x-python' });
}

// ── Initialisation ─────────────────────────────────────────────────────────

let initialised = false;

/** Register handlers once and load the catalog when the sidecar connects. Idempotent. */
export function initPipeline(): void {
  if (initialised) return;
  initialised = true;
  wsManager.onMessage('pipeline_steps', handleSteps);
  wsManager.onMessage('recipes_list', handleRecipes);
  wsManager.onMessage('pipeline_preview', handlePreview);
  wsManager.onMessage('pipeline_result', handleResult);
  wsManager.onMessage('pipeline_error', handleError);
  wsManager.onMessage('recipe_script', handleScript);
  wsManager.onMessage('recipe_saved', (m) => addToast('success', `Saved recipe "${m.recipe?.name ?? ''}"`, 3000));
  wsManager.onMessage('recipe_deleted', (m) => addToast('info', `Deleted recipe "${m.name ?? ''}"`, 3000));
  wsManager.onMessage('error', () => pipelineBusy.set(null));
  connectionStatus.subscribe((status) => {
    if (status === 'connected') requestCatalog();
    else pipelineBusy.set(null);
  });
  // A different file / sweep makes the previewed grids stale.
  activeFileId.subscribe(() => resetViews());
}
