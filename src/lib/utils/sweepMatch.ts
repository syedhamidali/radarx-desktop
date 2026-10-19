/**
 * Which pending request does a `sweep_data` reply answer?
 *
 * Replies come back in any order (the engine works on several at once) and two open files can
 * both be asked for the same variable and sweep. Matching on variable and sweep alone could hand
 * file A's data to file B's request. The engine now names the file in the reply header; this picks
 * the request of that file, and treats a reply for a file other than the one on screen as stale.
 */
export interface PendingSweep {
  variable: string;
  sweep: number;
  /** Normalised file id of the request ('' = whichever file the engine considers the default). */
  fileId: string;
}

export function findRequestForReply<T extends PendingSweep>(
  pending: readonly T[],
  reply: { variable: string; sweep: number; fileId?: string | null },
): T | undefined {
  const same = pending.filter((j) => j.variable === reply.variable && j.sweep === reply.sweep);
  if (!reply.fileId) return same[0]; // an engine that does not name the file: old behaviour
  return same.find((j) => j.fileId === reply.fileId) ?? same.find((j) => j.fileId === '');
}

/** True when a reply belongs to a file other than the one the viewer shows. */
export function isForOtherFile(replyFileId: string | null | undefined, shownFileId: string | null | undefined): boolean {
  return !!replyFileId && !!shownFileId && replyFileId !== shownFileId;
}
