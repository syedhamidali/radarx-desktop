/**
 * Text cleaning for report-issue. Pure functions, no Deno or Supabase API (Node tests them).
 *
 * The rules: reject NUL; drop control characters (keep \n and \t), bidirectional-override and zero-width
 * characters and lone surrogates; normalise line endings and Unicode (NFC); trim; collapse absurd runs of
 * whitespace. Length limits are counted in code points, the way Postgres char_length counts them.
 */

const CONTROL = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F]/g;
// zero-width, directional marks and overrides/isolates, BOM, interlinear annotation anchors
const INVISIBLE = /[\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u206F\uFEFF\uFFF9-\uFFFB]/g;
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g;

/** True when the string contains a NUL character (such a value is rejected, not repaired). */
export function hasNul(s: string): boolean {
  return s.includes('\u0000');
}

/** Number of Unicode code points. */
export function cpLength(s: string): number {
  let n = 0;
  for (const _ of s) n++;
  return n;
}

/** Keeps at most `max` code points. */
export function truncate(s: string, max: number): string {
  if (s.length <= max) return s; // cannot be longer in code points than in UTF-16 units
  let out = '';
  let n = 0;
  for (const ch of s) {
    if (n >= max) break;
    out += ch;
    n++;
  }
  return out;
}

/** Text that may span lines (description, diagnostics). */
export function cleanMultiline(s: string): string {
  let t = s.replace(LONE_SURROGATE, '').normalize('NFC');
  t = t.replace(/\r\n?|[\u0085\u2028\u2029]/g, '\n');
  t = t.replace(CONTROL, '').replace(INVISIBLE, '');
  t = t.replace(/[^\S\n]+$/gm, ''); // trailing blanks on each line
  t = t.replace(/[^\S\n]{9,}/g, '    '); // absurd runs of spaces or tabs
  t = t.replace(/\n{4,}/g, '\n\n\n'); // absurd runs of blank lines
  return t.trim();
}

/** Text that must stay on one line (title, e-mail, versions). */
export function cleanSingleLine(s: string): string {
  return cleanMultiline(s).replace(/\s+/g, ' ').trim();
}

/** The part of an e-mail subject that comes from the user: one line, no control characters, shortened. */
export function subjectText(title: string, max = 100): string {
  const t = cleanSingleLine(title);
  return cpLength(t) > max ? truncate(t, max - 1).trimEnd() + '\u2026' : t;
}

const HTML_ESCAPES: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' };

/** Escapes text for HTML element content or a quoted attribute value. */
export function escapeHtml(s: string): string {
  return s.replace(/[&<>"'`]/g, (c) => HTML_ESCAPES[c]);
}
