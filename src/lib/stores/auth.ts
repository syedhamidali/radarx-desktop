import { writable, get } from 'svelte/store';
import type { Session } from '@supabase/supabase-js';
import { getClient } from '../auth/client';
import { signInWithProvider, cancelOAuth } from '../auth/oauth';
import { AUTH_DISABLED, OFFLINE_GRACE_DAYS, type OAuthProvider } from '../auth/config';
import { gateState, friendlyAuthError, type GateState } from '../auth/gate';
import { startEngineLink, engineSignedIn, engineSignedOut, engineTokenRefreshed } from '../auth/engineLink';

/**
 * Version of the terms and privacy notice the user agreed to (bump when they change; everyone is then
 * asked once more, see profileIncomplete). 2026-10-21: an account is now required to use radarx
 * everywhere, and the feature-level activity log is disclosed (docs/PRIVACY.md).
 */
export const TERMS_VERSION = '2026-10-21';

export const USE_CASES = [
  { id: 'research', label: 'Research' },
  { id: 'operations', label: 'Operations / forecasting' },
  { id: 'teaching', label: 'Teaching' },
  { id: 'hydrology', label: 'Hydrology / flood work' },
  { id: 'hobby', label: 'Storm chasing / hobby' },
  { id: 'other', label: 'Something else' },
] as const;

export interface Profile {
  display_name: string | null;
  avatar_url: string | null;
  affiliation: string | null;
  use_case: string | null;
  terms_version: string | null;
}

export interface DeviceRow {
  id: string;
  device_id: string;
  name: string | null;
  platform: string | null;
  last_seen: string;
}

export interface AuthState {
  /** The first check (stored session, server) has finished. */
  ready: boolean;
  gate: GateState;
  email: string | null;
  profile: Profile | null;
  lastVerifiedAt: number | null;
  /** A request is running (disable the buttons). */
  busy: boolean;
  error: string | null;
  /** A sign-up waits for the code from the confirmation e-mail. */
  pendingEmail: string | null;
  info: string | null;
}

const VERIFIED_KEY = 'radarx.auth.verifiedAt';
const DEVICE_KEY = 'radarx.deviceId';

function readLocal(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function writeLocal(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode / blocked storage: the session simply is not remembered */
  }
}

export const auth = writable<AuthState>({
  ready: false,
  gate: AUTH_DISABLED ? 'open' : 'needs-account',
  email: null,
  profile: null,
  lastVerifiedAt: null,
  busy: false,
  error: null,
  pendingEmail: null,
  info: null,
});

function patch(p: Partial<AuthState>): void {
  auth.update((s) => ({ ...s, ...p }));
}

const online = () => (typeof navigator === 'undefined' ? true : navigator.onLine);

function lastVerified(): number | null {
  const v = Number(readLocal(VERIFIED_KEY));
  return Number.isFinite(v) && v > 0 ? v : null;
}

/** A random id for this install; it never changes and carries no personal data. */
export function deviceId(): string {
  let id = readLocal(DEVICE_KEY);
  if (!id) {
    id = (globalThis.crypto?.randomUUID?.() ?? `d${Date.now()}${Math.random().toString(36).slice(2)}`).replace(/-/g, '');
    writeLocal(DEVICE_KEY, id);
  }
  return id;
}

function deviceName(): string {
  const p = typeof navigator !== 'undefined' ? navigator.platform || 'device' : 'device';
  return p.includes('Mac') ? 'Mac' : p.includes('Win') ? 'Windows PC' : p.includes('Linux') ? 'Linux PC' : p;
}

async function loadProfile(): Promise<Profile | null> {
  const sb = await getClient();
  const { data } = await sb.from('profiles').select('display_name, avatar_url, affiliation, use_case, terms_version').maybeSingle();
  return (data as Profile | null) ?? null;
}

/** True from a click on "Sign in" until the session arrives (a silent restore is not a sign-in). */
let explicitSignIn = false;

/** Called with a verified session: remember it, load the profile, record this device. */
async function onSignedIn(session: Session, how: 'start' | 'event' = 'event'): Promise<void> {
  const now = Date.now();
  writeLocal(VERIFIED_KEY, String(now));
  // hand the account to the engine (it refuses all work without one); see auth/engineLink.ts
  try {
    await startEngineLink({
      deviceId,
      onRequired: (_reason, message) => patch({ gate: 'needs-account', error: message, info: null }),
    });
  } catch {
    /* the engine link is retried at the next token refresh; signing in itself succeeded */
  }
  if (explicitSignIn) {
    explicitSignIn = false;
    engineSignedIn();
  } else if (how === 'event') {
    engineTokenRefreshed();
  }
  patch({ email: session.user.email ?? null, lastVerifiedAt: now, gate: 'open', pendingEmail: null, error: null });
  try {
    patch({ profile: await loadProfile() });
    const sb = await getClient();
    await sb.rpc('register_device', {
      p_device_id: deviceId(),
      p_name: deviceName(),
      p_platform: typeof navigator !== 'undefined' ? navigator.platform : null,
      p_app_version: typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : null,
    });
  } catch {
    /* the profile and device record are conveniences; signing in already succeeded */
  }
}

let started = false;

/** Decide the first screen: a stored session opens the app (online or inside the offline grace). */
export async function initAuth(): Promise<void> {
  if (AUTH_DISABLED) {
    patch({ ready: true, gate: 'open' });
    return;
  }
  if (started) return;
  started = true;
  try {
    const sb = await getClient();
    sb.auth.onAuthStateChange((event, session) => {
      if (event === 'SIGNED_OUT') {
        engineSignedOut();
        patch({ gate: 'needs-account', email: null, profile: null });
      } else if (session && (event === 'SIGNED_IN' || event === 'TOKEN_REFRESHED')) {
        void onSignedIn(session);
      }
    });
    const { data } = await sb.auth.getSession();
    if (data.session && online()) {
      // getUser asks the server, so a deleted or disabled account is noticed
      const { data: u, error } = await sb.auth.getUser();
      if (!error && u.user) await onSignedIn(data.session, 'start');
      else if (error && /fetch|network/i.test(error.message)) offlineOrReconnect(true);
      else {
        await sb.auth.signOut({ scope: 'local' });
        patch({ gate: 'needs-account' });
      }
    } else if (data.session) {
      offlineOrReconnect(true, data.session.user.email ?? null);
    } else {
      patch({ gate: 'needs-account' });
    }
  } catch {
    offlineOrReconnect(false);
  } finally {
    patch({ ready: true });
  }
}

function offlineOrReconnect(hasSession: boolean, email: string | null = null): void {
  const last = lastVerified();
  patch({
    gate: gateState({ hasSession, lastVerifiedAt: last, now: Date.now(), online: false, graceDays: OFFLINE_GRACE_DAYS }),
    lastVerifiedAt: last,
    email,
  });
}

async function run<T>(work: () => Promise<T>): Promise<T | undefined> {
  patch({ busy: true, error: null, info: null });
  try {
    return await work();
  } catch (e: any) {
    explicitSignIn = false; // a failed attempt is not a sign-in
    patch({ error: friendlyAuthError(e?.message ?? String(e)) });
    return undefined;
  } finally {
    patch({ busy: false });
  }
}

export interface SignUpInput {
  email: string;
  password: string;
  name: string;
  affiliation: string;
  useCase: string;
}

export async function signUp(i: SignUpInput): Promise<void> {
  await run(async () => {
    const sb = await getClient();
    const { data, error } = await sb.auth.signUp({
      email: i.email.trim(),
      password: i.password,
      options: {
        data: {
          full_name: i.name.trim(),
          affiliation: i.affiliation.trim(),
          use_case: i.useCase,
          terms_version: TERMS_VERSION,
        },
      },
    });
    if (error) throw error;
    // With "Confirm email" on there is no session yet: ask for the code from the e-mail.
    if (!data.session) patch({ pendingEmail: i.email.trim(), info: 'We sent a code to your e-mail. Enter it below.' });
  });
}

export async function confirmCode(email: string, code: string): Promise<void> {
  await run(async () => {
    explicitSignIn = true;
    const sb = await getClient();
    const { error } = await sb.auth.verifyOtp({ email, token: code.replace(/\s/g, ''), type: 'signup' });
    if (error) throw error;
  });
}

export async function resendCode(email: string): Promise<void> {
  await run(async () => {
    const sb = await getClient();
    const { error } = await sb.auth.resend({ type: 'signup', email });
    if (error) throw error;
    patch({ info: 'A new code is on its way.' });
  });
}

export async function signIn(email: string, password: string): Promise<void> {
  await run(async () => {
    explicitSignIn = true;
    const sb = await getClient();
    const { error } = await sb.auth.signInWithPassword({ email: email.trim(), password });
    if (error) {
      if (/not confirmed/i.test(error.message)) patch({ pendingEmail: email.trim() });
      throw error;
    }
  });
}

export async function signInWith(provider: OAuthProvider): Promise<void> {
  explicitSignIn = true;
  await run(() => signInWithProvider(provider));
}

export async function abortOAuth(): Promise<void> {
  explicitSignIn = false;
  await cancelOAuth();
  patch({ busy: false });
}

/** Sends a reset code to the e-mail (the same answer whether or not an account exists). */
export async function resetPassword(email: string): Promise<boolean> {
  const ok = await run(async () => {
    const sb = await getClient();
    const { error } = await sb.auth.resetPasswordForEmail(email.trim());
    if (error) throw error;
    patch({ info: 'If an account exists for that e-mail, a code is on its way.' });
    return true;
  });
  return !!ok;
}

/** Checks the code from the reset e-mail, then sets the new password (which also signs in). */
export async function completeReset(email: string, code: string, newPassword: string): Promise<boolean> {
  const ok = await run(async () => {
    explicitSignIn = true;
    const sb = await getClient();
    const { error } = await sb.auth.verifyOtp({ email: email.trim(), token: code.replace(/\s/g, ''), type: 'recovery' });
    if (error) throw error;
    const { error: upErr } = await sb.auth.updateUser({ password: newPassword });
    if (upErr) throw upErr;
    return true;
  });
  return !!ok;
}

export async function signOut(): Promise<void> {
  await run(async () => {
    engineSignedOut(); // before the session is gone: the engine records the sign-out and clears the shared file
    const sb = await getClient();
    await sb.auth.signOut();
    patch({ gate: 'needs-account', email: null, profile: null, pendingEmail: null });
  });
}

export async function saveProfile(p: { display_name: string; affiliation: string; use_case: string }): Promise<boolean> {
  const ok = await run(async () => {
    const sb = await getClient();
    const { data: u } = await sb.auth.getUser();
    if (!u.user) throw new Error('not signed in');
    const { error } = await sb
      .from('profiles')
      .update({
        display_name: p.display_name.trim() || null,
        affiliation: p.affiliation.trim() || null,
        use_case: p.use_case || null,
        terms_version: TERMS_VERSION,
        terms_accepted_at: new Date().toISOString(),
      })
      .eq('id', u.user.id);
    if (error) throw error;
    patch({ profile: await loadProfile() });
    return true;
  });
  return !!ok;
}

export async function listDevices(): Promise<DeviceRow[]> {
  const sb = await getClient();
  const { data } = await sb.from('devices').select('id, device_id, name, platform, last_seen').order('last_seen', { ascending: false });
  return (data as DeviceRow[]) ?? [];
}

export interface ActivityRow {
  id: number;
  occurred_at: string;
  surface: string;
  event: string;
  detail: Record<string, string>;
}

/** The last 100 events this account has sent (the log is readable by its owner only; docs/PRIVACY.md). */
export async function listActivity(): Promise<ActivityRow[]> {
  const sb = await getClient();
  const { data, error } = await sb
    .from('activity_events')
    .select('id, occurred_at, surface, event, detail')
    .order('occurred_at', { ascending: false })
    .limit(100);
  if (error) throw error;
  return (data as ActivityRow[]) ?? [];
}

export async function removeDevice(id: string): Promise<void> {
  const sb = await getClient();
  await sb.from('devices').delete().eq('id', id);
}

export async function deleteAccount(): Promise<boolean> {
  const ok = await run(async () => {
    const sb = await getClient();
    const { error } = await sb.rpc('delete_my_account');
    if (error) throw error;
    engineSignedOut();
    await sb.auth.signOut({ scope: 'local' });
    patch({ gate: 'needs-account', email: null, profile: null, pendingEmail: null });
    return true;
  });
  return !!ok;
}

/** True when the profile still lacks what sign-up asks for (OAuth users skip the sign-up form). */
export function profileIncomplete(s: AuthState): boolean {
  const p = s.profile;
  return s.gate === 'open' && !!s.email && (!p || !p.affiliation || p.terms_version !== TERMS_VERSION);
}

export const authSnapshot = () => get(auth);
