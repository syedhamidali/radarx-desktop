/**
 * Svelte action: move the element to <body>.
 *
 * A `position: fixed` modal inside an ancestor that has `backdrop-filter`, `filter` or `transform`
 * is positioned (and clipped) relative to that ancestor instead of the window. Mounting the modal
 * on <body> avoids that.
 */
export function portal(node: HTMLElement) {
  node.ownerDocument.body.appendChild(node);
  return {
    destroy() {
      node.remove();
    },
  };
}
