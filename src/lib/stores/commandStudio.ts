/**
 * Command Studio: every `rxd` command as a form in the app.
 *
 * The engine describes its own command line (`cli_describe`), the form is generated from that
 * description, the values become the same words a terminal would get (`buildArgv`), and the
 * engine runs them (`cli_run`) with progress and cancel. Nothing here knows a command by name.
 */
import { writable, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';

export type OptionKind =
  | 'flag' | 'count' | 'input' | 'output' | 'choice' | 'int' | 'float' | 'file' | 'params' | 'text';

export interface CliOption {
  dest: string;
  flags: string[];
  positional: boolean;
  metavar: string | null;
  kind: OptionKind;
  choices: string[] | null;
  default: unknown;
  help: string;
  required: boolean;
  repeatable: boolean;
  nargs: number | string | null;
  group: string;
  advanced: boolean;
  unit: string | null;
  exclusive: string[];
}

export interface CliCommand {
  name: string;
  group: string;
  purpose: string;
  summary: string;
  epilog: string;
  examples: string[];
  options: CliOption[];
  interactive: boolean;
  reason: string | null;
}

export interface CliSchema {
  prog: string;
  version: string;
  groups: string[];
  commands: CliCommand[];
  exit_codes: Record<string, string>;
}

export interface CliFile {
  path: string;
  name: string;
  kind: 'image' | 'radar' | 'table' | 'video' | 'other';
  size: number | null;
  thumbnail?: string;
}

export interface CliResult {
  code: number;
  status: 'ok' | 'failed' | 'cancelled';
  ok: boolean;
  message: string;
  stdout: string;
  stderr: string;
  files: CliFile[];
  command: string | null;
  elapsed_ms: number;
}

/** A form's values: option dest -> value (string, number, boolean, string[]). */
export type Values = Record<string, unknown>;

// ── Dialog state ───────────────────────────────────────────────────────────

export const commandStudioOpen = writable(false);
/** The command the dialog should show when it opens (null: the last one, or the first). */
export const commandStudioCommand = writable<string | null>(null);

export function openCommandStudio(command?: string): void {
  if (command) commandStudioCommand.set(command);
  commandStudioOpen.set(true);
}
export function closeCommandStudio(): void {
  commandStudioOpen.set(false);
}

// ── Schema ─────────────────────────────────────────────────────────────────

export const cliSchema = writable<CliSchema | null>(null);
export const cliSchemaError = writable<string | null>(null);

let schemaPromise: Promise<CliSchema | null> | null = null;

function newRequestId(prefix: string): string {
  const rnd = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `${prefix}-${rnd}`;
}

/** Ask the engine for the description of its commands (cached; `force` asks again). */
export function loadSchema(force = false): Promise<CliSchema | null> {
  const have = get(cliSchema);
  if (have && !force) return Promise.resolve(have);
  if (schemaPromise && !force) return schemaPromise;
  cliSchemaError.set(null);
  const requestId = newRequestId('cli-describe');
  schemaPromise = new Promise<CliSchema | null>((resolve) => {
    let done = false;
    const finish = (schema: CliSchema | null, error: string | null) => {
      if (done) return;
      done = true;
      off();
      clearTimeout(timer);
      if (schema) cliSchema.set(schema);
      if (error) cliSchemaError.set(error);
      schemaPromise = null;
      resolve(schema);
    };
    const off = wsManager.onMessage('cli_schema', (msg: any) => {
      if (msg.request_id !== requestId) return;
      const { type: _t, request_id: _r, ...schema } = msg;
      finish(schema as CliSchema, null);
    });
    const timer = setTimeout(() => finish(null, 'The engine did not answer. Is it running?'), 20000);
    void wsManager.send({ type: 'cli_describe', request_id: requestId });
  });
  return schemaPromise;
}

// ── Values and command lines ───────────────────────────────────────────────

/** The form's starting values: the parser defaults, nothing else. */
export function defaultValues(cmd: CliCommand): Values {
  const v: Values = {};
  for (const o of cmd.options) {
    if (o.kind === 'flag') v[o.dest] = false;
    else if (o.kind === 'count') v[o.dest] = 0;
    else if (o.repeatable || o.kind === 'input' || o.kind === 'params') v[o.dest] = [];
    else v[o.dest] = o.default ?? '';
  }
  return v;
}

function isEmpty(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === 'string') return value.trim() === '';
  if (Array.isArray(value)) return value.filter((x) => String(x).trim() !== '').length === 0;
  return false;
}

function sameAsDefault(o: CliOption, value: unknown): boolean {
  if (o.default === null || o.default === undefined) return false;
  if (typeof value === 'number' || typeof o.default === 'number') return Number(value) === Number(o.default);
  return String(value) === String(o.default);
}

/** The words a terminal would get for *cmd* with these values (positionals first). */
export function buildArgv(cmd: CliCommand, values: Values): string[] {
  const argv: string[] = [cmd.name];
  const positional: string[] = [];
  const optional: string[] = [];
  for (const o of cmd.options) {
    const value = values[o.dest];
    if (o.kind === 'flag') {
      if (value === true) optional.push(o.flags[0]);
      continue;
    }
    if (o.kind === 'count') {
      const n = Number(value) || 0;
      if (n > 0) optional.push('-' + o.flags[0].replace(/^-+/, '')[0].repeat(n));
      continue;
    }
    if (isEmpty(value)) continue;
    if (o.positional) {
      const many = o.nargs === '*' || o.nargs === '+';
      const list = Array.isArray(value) ? value.map(String) : many ? splitWords(String(value)) : [String(value)];
      positional.push(...list.filter((s) => s.trim() !== ''));
      continue;
    }
    if (sameAsDefault(o, value) && !o.required) continue;
    const list = (Array.isArray(value) ? value : [value]).map(String).filter((s) => s.trim() !== '');
    if (o.kind === 'input' || o.nargs === '+' || o.nargs === '*') {
      optional.push(o.flags[0], ...list);
    } else {
      for (const item of list) optional.push(o.flags[0], item);
    }
  }
  return [...argv, ...positional, ...optional];
}

/** Words of a line, with "double" and 'single' quotes keeping spaces (`show "Reflectivity QC"`). */
export function splitWords(line: string): string[] {
  const words: string[] = [];
  let cur = '';
  let quote: string | null = null;
  let has = false;
  for (const ch of line) {
    if (quote) {
      if (ch === quote) quote = null;
      else cur += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      has = true;
    } else if (/\s/.test(ch)) {
      if (has || cur) words.push(cur);
      cur = '';
      has = false;
    } else {
      cur += ch;
    }
  }
  if (has || cur) words.push(cur);
  return words;
}

/** One word as it would be typed in a POSIX shell. */
export function shellQuote(word: string): string {
  if (word === '') return "''";
  if (/^[A-Za-z0-9@%+=:,./_-]+$/.test(word)) return word;
  return "'" + word.replace(/'/g, "'\\''") + "'";
}

export function shellLine(argv: string[]): string {
  return ['rxd', ...argv].map(shellQuote).join(' ');
}

/** What is still missing before the command can run (empty list: it can). */
export function missingValues(cmd: CliCommand, values: Values): string[] {
  const missing: string[] = [];
  for (const o of cmd.options) {
    if (!o.required) continue;
    if (isEmpty(values[o.dest])) missing.push(o.metavar ?? o.flags[0] ?? o.dest);
  }
  return missing;
}

// ── Running ────────────────────────────────────────────────────────────────

export interface RunHandle {
  requestId: string;
  result: Promise<CliResult>;
  cancel: () => void;
}

/** Run *argv* in the engine; `onProgress` gets the engine's progress lines while it runs. */
export function runCommand(argv: string[], cwd: string | null, onProgress?: (percent: number, text: string) => void): RunHandle {
  const requestId = newRequestId('cli-run');
  let offResult = () => {};
  let offProgress = () => {};
  let offError = () => {};
  const result = new Promise<CliResult>((resolve) => {
    const finish = (r: CliResult) => {
      offResult();
      offProgress();
      offError();
      resolve(r);
    };
    offResult = wsManager.onMessage('cli_result', (msg: any) => {
      if (msg.request_id !== requestId) return;
      const { type: _t, request_id: _r, ...rest } = msg;
      finish(rest as CliResult);
    });
    offProgress = wsManager.onMessage('progress', (msg: any) => {
      if (onProgress && typeof msg.percent === 'number') onProgress(msg.percent, String(msg.message ?? ''));
    });
    offError = wsManager.onMessage('error', (msg: any) => {
      // The engine never answers cli_run with an error frame; a lost connection does.
      const text = String(msg.message ?? '');
      if (/connection lost/i.test(text)) finish(failedResult(text));
    });
    void wsManager.send({ type: 'cli_run', request_id: requestId, argv, cwd: cwd ?? undefined });
  });
  return {
    requestId,
    result,
    cancel: () => void wsManager.send({ type: 'cli_cancel', request_id: requestId }),
  };
}

function failedResult(message: string): CliResult {
  return { code: 70, status: 'failed', ok: false, message, stdout: '', stderr: '', files: [], command: null, elapsed_ms: 0 };
}

export function revealFile(path: string): void {
  void wsManager.send({ type: 'cli_reveal', path });
}

/** The folder of a path (without the file name), or '' when there is none. */
export function parentDir(path: string): string {
  const i = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
  return i > 0 ? path.slice(0, i) : '';
}

/** A short plain-language label for an option (`--threshold-dbz` -> `Threshold dbz`). */
export function optionLabel(o: CliOption): string {
  const long = o.flags.find((f) => f.startsWith('--')) ?? o.flags[0] ?? o.metavar ?? o.dest;
  const words = long.replace(/^-+/, '').replace(/[-_]+/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}
