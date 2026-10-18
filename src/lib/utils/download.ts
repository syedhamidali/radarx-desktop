/**
 * Browser downloads of files the engine produced (web mode).
 *
 * In web mode an export success reply carries `download_url` ('/download/<token>/<filename>',
 * relative to the engine host) and `path` (basename only); a batch export may carry a
 * `download_urls` list instead. The client builds the absolute http(s) URL on the engine host and
 * clicks a hidden anchor (no popups, no window.open).
 */
import { resolveDownloadUrl, basename } from './env';

export interface DownloadTarget {
  url: string;
  filename: string;
}

/** Pull every download out of an export reply, resolved against the engine address. */
export function collectDownloads(engineWsUrl: string | null, msg: any): DownloadTarget[] {
  if (!msg || typeof msg !== 'object') return [];
  const raw: string[] = [];
  if (typeof msg.download_url === 'string') raw.push(msg.download_url);
  if (Array.isArray(msg.download_urls)) {
    for (const u of msg.download_urls) if (typeof u === 'string') raw.push(u);
  }
  const out: DownloadTarget[] = [];
  const seen = new Set<string>();
  for (const r of raw) {
    const url = resolveDownloadUrl(engineWsUrl, r);
    if (!url || seen.has(url)) continue;
    seen.add(url);
    let name = '';
    try {
      name = decodeURIComponent(basename(new URL(url).pathname));
    } catch {
      name = basename(r);
    }
    out.push({ url, filename: name });
  }
  // A lone download keeps the engine's preferred display name.
  if (out.length === 1 && typeof msg.path === 'string' && msg.path) out[0].filename = basename(msg.path);
  return out;
}

/** Click a hidden anchor. Cross-origin `download` is advisory; the engine sends Content-Disposition. */
export function triggerDownload(target: DownloadTarget): void {
  const a = document.createElement('a');
  a.href = target.url;
  a.download = target.filename;
  a.rel = 'noopener';
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** Start several downloads a beat apart so browsers do not drop the later ones. */
export function triggerDownloads(targets: DownloadTarget[], gapMs = 400): void {
  targets.forEach((t, i) => {
    if (i === 0) triggerDownload(t);
    else setTimeout(() => triggerDownload(t), i * gapMs);
  });
}
