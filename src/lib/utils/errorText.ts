/**
 * Tidy a server error for a toast. Some engine libraries put their internals into the exception text
 * (icechunk: "x the repository doesn't exist | context: | 0: icechunk::repository::open | at
 * icechunk/src/repository.rs:343 ..."); the person needs the sentence, not the trace. The full text
 * stays in the console.
 */
const MAX_LEN = 280;

export function tidyServerError(text: string): string {
  const lines = String(text ?? '')
    .split(/\r?\n/)
    .map((l) => l.trim())
    // trace art and source locations: "| context:", "`-> ...", "0: crate::path", "at src/file.rs:12"
    .filter((l) => l && !/^[|`]/.test(l) && !/^\d+:\s*\S+::/.test(l) && !/^at\s+\S+:\d+/.test(l) && !/^Traceback\b/.test(l));
  // miette-style "message:   x cause" -> "message: cause" (needs the wide gap: "5 x 5 grid" is left alone)
  let s = lines.join(' ').replace(/:\s{2,}x\s+(?=\S)/g, ': ').replace(/\s{2,}/g, ' ').trim();
  if (!s) s = 'Unknown error from server';
  return s.length > MAX_LEN ? s.slice(0, MAX_LEN - 1).trimEnd() + '…' : s;
}
