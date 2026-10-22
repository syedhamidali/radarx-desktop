/**
 * Plain-language lines of the Autopilot panel that are worth testing: where the memory budget
 * comes from (python/engine/autopilot/govern.py `budget_explained`) and what a cancelled run left
 * behind (the run record of python/engine/autopilot/run.py).
 */

export interface GovernorLike {
  machine: { ram_bytes: number | null; available_bytes: number | null };
  budget_bytes: number;
  budget_source?: string;
}

const gb = (b: number) => `${(b / 1e9).toFixed(1)} GB`;

const WHY: Record<string, string> = {
  given: 'as given',
  ram: '60 % of RAM',
  available: '90 % of the memory available now, smaller than 60 % of RAM',
  minimum: 'the 512 MB minimum',
};

/** `RAM 17.2 GB, available 3.0 GB -> budget 2.7 GB (90 % of the memory available now, ...)`. */
export function budgetLine(g: GovernorLike): string {
  const parts = [`RAM ${gb(g.machine.ram_bytes ?? 0)}`];
  if (g.machine.available_bytes) parts.push(`available ${gb(g.machine.available_bytes)}`);
  const why = g.budget_source ? WHY[g.budget_source] : undefined;
  return `${parts.join(', ')} -> budget ${gb(g.budget_bytes)}${why ? ` (${why})` : ''}`;
}

export interface RecordStepLike { id: string; title: string; status: string; files?: string[] }

/**
 * After Cancel: what was cancelled and what was kept, or null when nothing was cancelled.
 * `kept` counts the files of the steps finished before (this run or an earlier one) and any
 * files the cancelled step had already written.
 */
export function cancelSummary(steps: RecordStepLike[]): { text: string; kept: number } | null {
  const cancelled = steps.filter((s) => s.status === 'cancelled');
  if (cancelled.length === 0) return null;
  const kept = steps.reduce((n, s) => n + (s.files?.length ?? 0), 0);
  const finished = steps.filter((s) => s.status === 'done' || s.status.startsWith('skipped')).length;
  const names = cancelled.map((s) => s.title).join(', ');
  const before = finished
    ? `${finished} step${finished === 1 ? '' : 's'} finished before it`
    : 'no step had finished before it';
  return {
    text: `Cancelled during "${names}"; ${before}, ${kept} file${kept === 1 ? '' : 's'} kept. Run again resumes: finished steps are skipped.`,
    kept,
  };
}
