/**
 * Validation of the report-issue request body. Pure: returns either the cleaned report or the first problem
 * (with the name of the field), never throws, never echoes the offending value back.
 */
import { cleanMultiline, cleanSingleLine, cpLength, hasNul } from './sanitise.ts';

export type Source = 'app' | 'web';
export type Kind = 'bug' | 'idea' | 'question';
export const SOURCES: readonly string[] = ['app', 'web'];
export const KINDS: readonly string[] = ['bug', 'idea', 'question'];

export const LIMITS = {
  title: { min: 3, max: 140 },
  description: { min: 10, max: 5000 },
  contact_email: { max: 200 },
  app_version: { max: 40 },
  os: { max: 60 },
  engine_version: { max: 40 },
  diagnostics: { max: 20000 },
  turnstile_token: { max: 2048 },
};

export type Report = {
  source: Source;
  kind: Kind;
  title: string;
  description: string;
  contact_email: string | null;
  app_version: string | null;
  os: string | null;
  engine_version: string | null;
  diagnostics: string | null;
  turnstile_token: string | null;
};

export type Validation = { ok: true; value: Report } | { ok: false; field: string; message: string };

const LOCAL = /^[\p{L}\p{N}.!#$%&'*+/=?^_`{|}~-]+$/u;
const LABEL = /^[\p{L}\p{N}](?:[\p{L}\p{N}-]*[\p{L}\p{N}])?$/u;

/** Does this look like an e-mail address? (A shape check, not a deliverability check.) */
export function isEmailShape(s: string): boolean {
  if (cpLength(s) > LIMITS.contact_email.max) return false;
  const at = s.lastIndexOf('@');
  if (at < 1 || s.indexOf('@') !== at) return false;
  const local = s.slice(0, at);
  const domain = s.slice(at + 1);
  if (local.length > 64 || !LOCAL.test(local) || local.startsWith('.') || local.endsWith('.') || local.includes('..')) return false;
  const labels = domain.split('.');
  if (labels.length < 2 || domain.length > 253) return false;
  if (!labels.every((l) => l.length >= 1 && l.length <= 63 && LABEL.test(l))) return false;
  return cpLength(labels[labels.length - 1]) >= 2;
}

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x);
}

/** True when the honeypot field `website` has anything in it. Bots fill it; people never see it. */
export function honeypotFilled(body: unknown): boolean {
  if (!isObject(body)) return false;
  const w = body.website;
  if (w === undefined || w === null) return false;
  if (typeof w !== 'string') return true;
  return w.trim() !== '';
}

const bad = (field: string, message: string): Validation => ({ ok: false, field, message });

type Text = { ok: true; value: string | null } | { ok: false; field: string; message: string };

function text(body: Record<string, unknown>, field: string, opts: { required: boolean; min?: number; max: number; multiline?: boolean; label: string }): Text {
  const raw = body[field];
  if (raw === undefined || raw === null) {
    return opts.required ? { ok: false, field, message: `${opts.label} is required.` } : { ok: true, value: null };
  }
  if (typeof raw !== 'string') return { ok: false, field, message: `${opts.label} must be text.` };
  if (hasNul(raw)) return { ok: false, field, message: `${opts.label} contains a character that is not allowed.` };
  const v = opts.multiline ? cleanMultiline(raw) : cleanSingleLine(raw);
  if (v === '') {
    return opts.required ? { ok: false, field, message: `${opts.label} is required.` } : { ok: true, value: null };
  }
  const n = cpLength(v);
  if (opts.min !== undefined && n < opts.min) {
    return { ok: false, field, message: `${opts.label} must be at least ${opts.min} characters.` };
  }
  if (n > opts.max) return { ok: false, field, message: `${opts.label} must be at most ${opts.max} characters.` };
  return { ok: true, value: v };
}

export function validateReport(body: unknown): Validation {
  if (!isObject(body)) return bad('body', 'The request must be a JSON object.');

  const source = body.source;
  if (typeof source !== 'string' || !SOURCES.includes(source)) return bad('source', 'Source must be "app" or "web".');
  const kind = body.kind;
  if (typeof kind !== 'string' || !KINDS.includes(kind)) return bad('kind', 'Kind must be "bug", "idea" or "question".');

  const title = text(body, 'title', { required: true, min: LIMITS.title.min, max: LIMITS.title.max, label: 'The title' });
  if (!title.ok) return bad(title.field, title.message);
  const description = text(body, 'description', { required: true, min: LIMITS.description.min, max: LIMITS.description.max, multiline: true, label: 'The description' });
  if (!description.ok) return bad(description.field, description.message);

  const email = text(body, 'contact_email', { required: false, max: LIMITS.contact_email.max, label: 'The e-mail address' });
  if (!email.ok) return bad(email.field, email.message);
  if (email.value !== null && !isEmailShape(email.value)) return bad('contact_email', 'That does not look like an e-mail address.');

  const appVersion = text(body, 'app_version', { required: false, max: LIMITS.app_version.max, label: 'The app version' });
  if (!appVersion.ok) return bad(appVersion.field, appVersion.message);
  const os = text(body, 'os', { required: false, max: LIMITS.os.max, label: 'The system name' });
  if (!os.ok) return bad(os.field, os.message);
  const engine = text(body, 'engine_version', { required: false, max: LIMITS.engine_version.max, label: 'The engine version' });
  if (!engine.ok) return bad(engine.field, engine.message);
  const diagnostics = text(body, 'diagnostics', { required: false, max: LIMITS.diagnostics.max, multiline: true, label: 'The diagnostics' });
  if (!diagnostics.ok) return bad(diagnostics.field, diagnostics.message);

  // The Turnstile token is opaque: only its type and size are checked, it is never cleaned or stored.
  let token: string | null = null;
  const t = body.turnstile_token;
  if (t !== undefined && t !== null) {
    if (typeof t !== 'string' || hasNul(t) || t.length > LIMITS.turnstile_token.max) return bad('turnstile_token', 'The human check is not valid.');
    token = t.trim() === '' ? null : t.trim();
  }

  return {
    ok: true,
    value: {
      source: source as Source,
      kind: kind as Kind,
      title: title.value as string,
      description: description.value as string,
      contact_email: email.value,
      app_version: appVersion.value,
      os: os.value,
      engine_version: engine.value,
      diagnostics: diagnostics.value,
      turnstile_token: token,
    },
  };
}
