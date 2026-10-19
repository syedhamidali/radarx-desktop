/**
 * Tile image loader with a persistent browser cache.
 *
 * A tile is looked up in the Cache API first (it survives reloads and works offline), then fetched
 * (CORS) and stored. Where the Cache API is not available (insecure context, some webviews) it is a
 * plain fetch. A failed or slow fetch resolves null: callers fall back to a flat ground.
 *
 * Tile servers are shared services: callers keep the concurrency low (the ground and terrain
 * builders use at most 4 requests at once, nearest tiles first) and every tile is fetched only once.
 */

const CACHE_NAME = 'xradar-tiles-v1';
const TIMEOUT_MS = 12000;

let cachePromise: Promise<Cache | null> | null = null;

function openCache(): Promise<Cache | null> {
  if (!cachePromise) {
    cachePromise = (async () => {
      try {
        if (typeof caches === 'undefined') return null;
        return await caches.open(CACHE_NAME);
      } catch {
        return null;
      }
    })();
  }
  return cachePromise;
}

/** Fetch a tile image (cached); null when it could not be loaded. */
export async function fetchTileBitmap(url: string): Promise<ImageBitmap | null> {
  if (!url) return null;
  const cache = await openCache();
  try {
    if (cache) {
      const hit = await cache.match(url);
      if (hit) return await createImageBitmap(await hit.blob());
    }
  } catch {
    /* fall through to the network */
  }
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), TIMEOUT_MS) : null;
  try {
    const resp = await fetch(url, { mode: 'cors', signal: ctl?.signal });
    if (!resp.ok) return null;
    if (cache) {
      try {
        await cache.put(url, resp.clone());
      } catch {
        /* quota or unsupported: keep going without the cache */
      }
    }
    return await createImageBitmap(await resp.blob());
  } catch {
    // fetch blocked (a page policy that allows images but not requests): decode it as an image
    return await viaImage(url);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function viaImage(url: string): Promise<ImageBitmap | null> {
  return new Promise((resolve) => {
    if (typeof Image === 'undefined') { resolve(null); return; }
    const img = new Image();
    img.crossOrigin = 'anonymous';
    const timer = setTimeout(() => { img.src = ''; resolve(null); }, TIMEOUT_MS);
    img.onload = () => { clearTimeout(timer); createImageBitmap(img).then(resolve, () => resolve(null)); };
    img.onerror = () => { clearTimeout(timer); resolve(null); };
    img.src = url;
  });
}

/** Run `fn` over `items` with at most `limit` in flight. */
export async function mapLimit<T>(items: T[], limit: number, fn: (item: T) => Promise<void>, stop: () => boolean = () => false): Promise<void> {
  let cursor = 0;
  const worker = async () => {
    while (cursor < items.length && !stop()) {
      const it = items[cursor++];
      await fn(it);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
}
