/**
 * Keyboard rules of the sidebar resize handles (role="separator"). The mouse drag lives in App.svelte;
 * this is what a keyboard (or screen-reader) user gets: the arrows move the edge, Shift moves it in
 * bigger steps, Home or Enter resets to the default width, End gives the widest.
 */
export const SIDEBAR_MIN = 200;
export const SIDEBAR_MAX = 500;
export const SIDEBAR_DEFAULT = 300;
export const SIDEBAR_KEY_STEP = 16;
export const SIDEBAR_KEY_STEP_BIG = 64;

export function clampSidebarWidth(w: number): number {
  if (!Number.isFinite(w)) return SIDEBAR_DEFAULT;
  return Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, Math.round(w)));
}

/**
 * The width after a key press on the handle of `side`, or null when the key is not for the handle
 * (so the caller does not swallow it). The handle sits on the sidebar's inner edge: ArrowRight moves
 * that edge right, which widens the left sidebar and narrows the right one.
 */
export function sidebarWidthAfterKey(
  side: 'left' | 'right',
  key: string,
  shift: boolean,
  width: number,
): number | null {
  if (key === 'Home' || key === 'Enter') return SIDEBAR_DEFAULT;
  if (key === 'End') return SIDEBAR_MAX;
  if (key !== 'ArrowLeft' && key !== 'ArrowRight') return null;
  const step = shift ? SIDEBAR_KEY_STEP_BIG : SIDEBAR_KEY_STEP;
  const edgeMove = key === 'ArrowRight' ? step : -step;
  return clampSidebarWidth(width + (side === 'left' ? edgeMove : -edgeMove));
}
