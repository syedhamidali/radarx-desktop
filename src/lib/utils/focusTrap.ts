/**
 * Svelte action for modal dialogs: moves focus into the node when it appears, keeps Tab /
 * Shift+Tab cycling inside it, and gives focus back to the previously focused element when the
 * node is removed.
 *
 *   <div role="dialog" aria-modal="true" use:focusTrap>…</div>
 *
 * Pass `{ initial: '<selector>' }` to focus a specific control first (default: the first
 * focusable element, falling back to the node itself).
 */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusables(node: HTMLElement): HTMLElement[] {
  return Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => el.offsetParent !== null || el === document.activeElement,
  );
}

export function focusTrap(node: HTMLElement, opts: { initial?: string } = {}) {
  const previous = document.activeElement as HTMLElement | null;
  if (!node.hasAttribute('tabindex')) node.tabIndex = -1;

  // Let the dialog render (and its transition start) before moving focus.
  const raf = requestAnimationFrame(() => {
    if (node.contains(document.activeElement) && document.activeElement !== node) return;
    const target =
      (opts.initial ? node.querySelector<HTMLElement>(opts.initial) : null) ?? focusables(node)[0] ?? node;
    target.focus({ preventScroll: true });
  });

  function onKeydown(e: KeyboardEvent) {
    if (e.key !== 'Tab') return;
    const items = focusables(node);
    if (items.length === 0) {
      e.preventDefault();
      node.focus();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement as HTMLElement | null;
    if (e.shiftKey && (active === first || active === node || !node.contains(active))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || !node.contains(active))) {
      e.preventDefault();
      first.focus();
    }
  }
  node.addEventListener('keydown', onKeydown);

  return {
    destroy() {
      cancelAnimationFrame(raf);
      node.removeEventListener('keydown', onKeydown);
      // Return focus only if nothing else has claimed it meanwhile.
      const a = document.activeElement;
      if (previous && previous.isConnected && (!a || a === document.body || node.contains(a))) {
        previous.focus({ preventScroll: true });
      }
    },
  };
}
