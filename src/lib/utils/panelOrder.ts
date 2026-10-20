/**
 * Moving one right-hand panel up or down by one place (the keyboard alternative to dragging the
 * panel header; Alt+ArrowUp / Alt+ArrowDown on the panel's header button).
 */
export function movePanel(order: readonly string[], id: string, delta: -1 | 1): string[] {
  const from = order.indexOf(id);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= order.length) return [...order];
  const next = [...order];
  next.splice(from, 1);
  next.splice(to, 0, id);
  return next;
}
