/**
 * Sign-in with Google / Apple / Facebook.
 *
 * Desktop: the system browser does the sign-in, then Supabase redirects to a one-shot listener the
 * Rust shell opens on 127.0.0.1 (src-tauri/src/oauth.rs). No custom URL scheme is needed, so it
 * works the same on macOS, Windows and Linux. PKCE keeps the code useless to anything that merely
 * sees the redirect. Web: the page itself is redirected and the library completes the exchange.
 */
import { getClient } from './client';
import { inTauri } from '../utils/env';
import type { OAuthProvider } from './config';

async function openExternal(url: string): Promise<void> {
  const { open } = await import('@tauri-apps/plugin-shell');
  await open(url);
}

export async function signInWithProvider(provider: OAuthProvider): Promise<void> {
  const sb = await getClient();
  if (!inTauri()) {
    const { error } = await sb.auth.signInWithOAuth({
      provider,
      options: { redirectTo: location.origin + location.pathname },
    });
    if (error) throw error;
    return; // the page navigates away
  }
  const { invoke } = await import('@tauri-apps/api/core');
  const port = await invoke<number>('oauth_listen_start');
  try {
    const { data, error } = await sb.auth.signInWithOAuth({
      provider,
      options: { redirectTo: `http://127.0.0.1:${port}/auth/callback`, skipBrowserRedirect: true },
    });
    if (error || !data.url) throw error ?? new Error('No sign-in address was returned.');
    await openExternal(data.url);
    const query = await invoke<string>('oauth_listen_wait');
    const params = new URLSearchParams(query);
    const failure = params.get('error_description') || params.get('error');
    if (failure) throw new Error(failure);
    const code = params.get('code');
    if (!code) throw new Error('The sign-in did not return a code.');
    const { error: exErr } = await sb.auth.exchangeCodeForSession(code);
    if (exErr) throw exErr;
  } catch (e) {
    await invoke('oauth_listen_cancel').catch(() => {});
    throw e;
  }
}

export async function cancelOAuth(): Promise<void> {
  if (!inTauri()) return;
  const { invoke } = await import('@tauri-apps/api/core');
  await invoke('oauth_listen_cancel').catch(() => {});
}
