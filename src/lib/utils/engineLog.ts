/**
 * The last things the engine told the app this session (warnings and errors), kept in memory only, so that
 * "Report an issue" can offer them as optional diagnostics. The app has no access to the engine's own log file;
 * this is what the app itself saw. Nothing here is stored on disk or sent anywhere by itself.
 */
const MAX_LINES = 200;
const MAX_LINE_CHARS = 500;
const lines: string[] = [];

const pad = (n: number) => String(n).padStart(2, '0');

export function recordEngineMessage(level: 'warning' | 'error', text: string, now: Date = new Date()): void {
  const stamp = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  lines.push(`${stamp} ${level}: ${String(text).slice(0, MAX_LINE_CHARS)}`);
  if (lines.length > MAX_LINES) lines.splice(0, lines.length - MAX_LINES);
}

/** A copy of the recorded lines, oldest first. */
export function engineLogLines(): string[] {
  return lines.slice();
}

export function clearEngineLog(): void {
  lines.length = 0;
}
