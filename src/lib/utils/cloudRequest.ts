/**
 * Hand-over from the Radar sites card to the Cloud data panel.
 *
 * The card never talks to the cloud itself: it asks the existing Cloud panel to list a station's
 * archive or to open one exact file, so progress, cancel/retry, the operations tray and the
 * web-mode limits behave exactly as for a manual open. The request waits in `pending` until the
 * panel (which may still be lazy-loading) takes it.
 */

export type CloudSiteRequest =
  | { action: 'archive'; station: string }
  | { action: 'open'; station: string; path: string; name: string; size?: number };

let pending: CloudSiteRequest | null = null;

/** Queue a request and tell the shell to bring the Cloud panel into view. */
export function requestCloud(req: CloudSiteRequest): void {
  pending = req;
  window.dispatchEvent(new CustomEvent('xradar:cloud-site', { detail: req }));
}

/** The queued request, once (null when none or already taken). */
export function takePendingCloudRequest(): CloudSiteRequest | null {
  const r = pending;
  pending = null;
  return r;
}
