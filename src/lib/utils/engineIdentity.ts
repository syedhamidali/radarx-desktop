/**
 * Telling "the engine restarted" from "the connection dropped and came back".
 *
 * The engine reports an `engine_id` in every `pong` that is unique to its process. After a
 * reconnect, the same id means the engine (and every file it holds) is still there; a different
 * one means the process was replaced and all file ids the UI remembers are dead.
 */
export interface EngineTrack {
  /** The id to remember from now on. */
  engineId: string | null;
  /** True when the engine we are now talking to is not the one we talked to before. */
  restarted: boolean;
}

export function trackEngine(previous: string | null, incoming: unknown): EngineTrack {
  // An engine that sends no id (older build) can never be shown to have restarted.
  if (typeof incoming !== 'string' || incoming.length === 0) {
    return { engineId: previous, restarted: false };
  }
  return { engineId: incoming, restarted: previous !== null && previous !== incoming };
}
