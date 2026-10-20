import { getClient } from './client';

/**
 * The current access token of the signed-in account, for calls to our own Edge Functions (docs/REPORTING.md).
 * supabase-js renews an expired token when it can; offline it cannot, and then this returns null (a caller reports
 * "sign in again / try later"). Gives up after `timeoutMs` so a dead network never leaves a dialog waiting.
 */
export async function accessToken(timeoutMs = 6000): Promise<string | null> {
  try {
    const sb = await getClient();
    const answer = await Promise.race([sb.auth.getSession(), new Promise<null>((resolve) => setTimeout(() => resolve(null), timeoutMs))]);
    const s = answer?.data.session;
    if (!s) return null;
    // a token that has already expired is no use to the server
    if (typeof s.expires_at === 'number' && s.expires_at * 1000 < Date.now()) return null;
    return s.access_token;
  } catch {
    return null;
  }
}
