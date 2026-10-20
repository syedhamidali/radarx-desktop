/**
 * Sharing a result (an exported image or movie) on social networks.
 *
 * No network lets a link attach a file, and radarx never posts on anyone's behalf: the dialog opens
 * the network's own compose page with the caption filled in, and the user attaches the saved file
 * there. Instagram has no compose link at all, so it gets the caption on the clipboard and the file
 * revealed in the file manager. Pure functions only (tested in share.test.mjs).
 */
import { WEBSITE_URL } from './brand.ts';

export type ShareTarget = 'x' | 'facebook' | 'linkedin' | 'bluesky';

export const SHARE_URL = WEBSITE_URL;
const IMAGE = ['png', 'jpg', 'jpeg', 'gif', 'webp'];
const VIDEO = ['mp4', 'mov', 'webm'];

export type ShareKind = 'image' | 'video';

export function shareKind(path: string): ShareKind | null {
  const ext = /\.([a-z0-9]+)$/i.exec(path)?.[1]?.toLowerCase();
  if (!ext) return null;
  if (IMAGE.includes(ext)) return 'image';
  if (VIDEO.includes(ext)) return 'video';
  return null;
}

export function defaultCaption(kind: ShareKind): string {
  const what = kind === 'video' ? 'radar animation' : 'radar image';
  return `A ${what} made with radarx #radar #weather #meteorology`;
}

export const TARGET_LABEL: Record<ShareTarget, string> = {
  x: 'X (Twitter)',
  facebook: 'Facebook',
  linkedin: 'LinkedIn',
  bluesky: 'Bluesky',
};

/** The network's own compose page, with the caption (and the project link where it takes one). */
export function composeUrl(target: ShareTarget, caption: string, link: string = SHARE_URL): string {
  const text = encodeURIComponent(caption);
  const url = encodeURIComponent(link);
  switch (target) {
    case 'x':
      return `https://twitter.com/intent/tweet?text=${text}&url=${url}`;
    case 'facebook':
      return `https://www.facebook.com/sharer/sharer.php?u=${url}&quote=${text}`;
    case 'linkedin':
      return `https://www.linkedin.com/sharing/share-offsite/?url=${url}`;
    case 'bluesky':
      return `https://bsky.app/intent/compose?text=${encodeURIComponent(`${caption} ${link}`)}`;
  }
}

/** What Instagram needs, stated plainly. */
export function instagramAdvice(kind: ShareKind): string {
  return kind === 'video'
    ? 'Instagram takes vertical (9:16) or square video up to 90 s for Reels. Upload the saved file from the Instagram app or instagram.com.'
    : 'Instagram takes square or 4:5 images. Upload the saved file from the Instagram app or instagram.com.';
}

/** Only these hosts are ever opened from the dialog. */
export function isAllowedShareUrl(u: string): boolean {
  try {
    const { protocol, hostname } = new URL(u);
    return (
      protocol === 'https:' &&
      ['twitter.com', 'www.facebook.com', 'www.linkedin.com', 'bsky.app', 'www.instagram.com'].includes(hostname)
    );
  } catch {
    return false;
  }
}
