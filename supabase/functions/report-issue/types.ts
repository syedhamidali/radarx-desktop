/** Shapes shared by the report-issue modules (type-only: nothing here exists at run time). */

export type FetchLike = (
  url: string,
  init: { method: string; headers: Record<string, string>; body?: string; signal: AbortSignal },
) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;

export type Logger = (event: Record<string, unknown>) => void;
