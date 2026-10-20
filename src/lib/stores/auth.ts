import { writable, get } from 'svelte/store';
import type { Session } from '@supabase/supabase-js';
import { getClient, STORAGE_KEY } from '../auth/client';
import { signInWithProvider, cancelOAuth } from '../auth/oauth';
import { AUTH_DISABLED, OFFLINE_GRACE_DAYS, type OAuthProvider } from '../auth/config';
import { initialProfileState, profileMissing, appMayOpen as mayOpen } from '../auth/openRule';
import { friendlyAuthError, type GateState } from '../auth/gate';
import { parseStoredSession, planStartup, tokenIssuedAt, isDefiniteAuthFailure, isValidDeviceId, type StoredSession } from '../auth/startup';
import { startEngineLink, engineSignedIn, engineSignedOut, engineTokenRefreshed, hasPendingSignOut } from '../auth/engineLink';

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
  /** 'loading' until the profile has been asked for; 'failed' (offline, server trouble) never traps the user. */
  profileState: 'loading' | 'ready' | 'failed';
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
  profileState: initialProfileState(AUTH_DISABLED),
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

/** The session in storage (possibly expired), read without asking Supabase to renew it. */
function storedSession(): StoredSession | null {
  return parseStoredSession(readLocal(STORAGE_KEY));
}

/** A random id for this install; it never changes and carries no personal data. */
export function deviceId(): string {
  let id = readLocal(DEVICE_KEY);
  if (!isValidDeviceId(id)) {
    id = (globalThis.crypto?.randomUUID?.() ?? `d${Date.now()}${Math.random().toString(36).slice(2)}`).replace(/[^A-Za-z0-9]/g, '');
    if (!isValidDeviceId(id)) id = `d${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`.replace(/[^A-Za-z0-9]/g, '').padEnd(8, '0');
    writeLocal(DEVICE_KEY, id);
  }
  return id;
}

function deviceName(): string {
  const p = typeof navigator !== 'undefined' ? navigator.platform || 'device' : 'device';
  return p.includes('Mac') ? 'Mac' : p.includes('Win') ? 'Windows PC' : p.includes('Linux') ? 'Linux PC' : p;
}

/** The profile row; `ok` is false when it could not be read (offline, server trouble): that is not "no profile". */
async function loadProfile(): Promise<{ profile: Profile | null; ok: boolean }> {
  try {
    const sb = await getClient();
    const query = sb.from('profiles').select('display_name, avatar_url, affiliation, use_case, terms_version').maybeSingle();
    const timeout = new Promise<never>((_, reject) => setTimeout(() => reject(new Error('timeout')), 8000));
    const { data, error } = await Promise.race([query, timeout]);
    if (error) return { profile: null, ok: false };
    return { profile: (data as Profile | null) ?? null, ok: true };
  } catch {
    return { profile: null, ok: false };
  }
}

async function refreshProfile(): Promise<void> {
  patch({ profileState: 'loading' });
  const r = await loadProfile();
  patch(r.ok ? { profile: r.profile, profileState: 'ready' } : { profileState: 'failed' });
}

/** Try the profile again (the Account dialog offers it after a failed load). */
export async function retryProfile(): Promise<void> {
  await refreshProfile();
}

/** True from a click on "Sign in" until the session arrives (a silent restore is not a sign-in). */
let explicitSignIn = false;

/** Called with a verified session: remember it, load the profile, record this device. */
async function onSignedIn(session: Session, how: 'start' | 'event' = 'event'): Promise<void> {
  const now = Date.now();
  // the server vouched for the account when it issued this token (a signed claim), not "now"
  const iat = tokenIssuedAt(session.access_token);
  writeLocal(VERIFIED_KEY, String(iat ? iat * 1000 : now));
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
  patch({ email: session.user.email ?? null, lastVerifiedAt: iat ? iat * 1000 : now, gate: 'open', pendingEmail: null, error: null, profileState: 'loading' });
  await refreshProfile();
  try {
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

/** How long start-up waits for the account server before it uses the stored session instead. supabase-js
 * retries a failed renewal with growing pauses (minutes in all), which must not hold the window shut. */
const SERVER_WAIT_MS = 8000;

async function withinTime<T>(work: Promise<T>): Promise<{ value: T } | null> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<null>((resolve) => {
    timer = setTimeout(() => resolve(null), SERVER_WAIT_MS);
  });
  try {
    const r = await Promise.race([work.then((value) => ({ value })), timeout]);
    return r;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function engineHooks() {
  return {
    deviceId,
    onRequired: (_reason: string, message: string) => patch({ gate: 'needs-account', error: message, info: null }),
  };
}

/**
 * Open the app on the strength of the STORED session (no network, or the server answered with
 * something that is not a verdict about the account): the engine is given the stored token, which it
 * accepts inside the 30-day allowance, and the app is marked "offline".
 */
async function openWithStoredSession(stored: StoredSession, gate: 'open-offline' | 'reconnect'): Promise<void> {
  const iat = tokenIssuedAt(stored.access_token);
  patch({ email: stored.user?.email ?? null, lastVerifiedAt: iat ? iat * 1000 : null, gate });
  if (gate !== 'open-offline') return;
  try {
    await startEngineLink(engineHooks());
  } catch {
    /* retried when the connection comes back */
  }
  patch({ profileState: 'failed' }); // the profile needs the server; the app does not wait for it
}

/** Decide the first screen: a stored session opens the app (online or inside the offline allowance). */
export async function initAuth(): Promise<void> {
  if (AUTH_DISABLED) {
    patch({ ready: true, gate: 'open', profileState: 'ready' });
    return;
  }
  if (started) return;
  started = true;
  try {
    if (hasPendingSignOut()) void startEngineLink(engineHooks()).catch(() => {}); // delivers an undelivered sign-out
    const sb = await getClient();
    sb.auth.onAuthStateChange((event, session) => {
      if (event === 'SIGNED_OUT') {
        engineSignedOut();
        patch({ gate: 'needs-account', email: null, profile: null });
      } else if (session && (event === 'SIGNED_IN' || event === 'TOKEN_REFRESHED')) {
        void onSignedIn(session);
      }
    });
    if (typeof window !== 'undefined') {
      // back online while the app runs on a stored session: renew it (this also leaves "offline")
      window.addEventListener('online', () => {
        if (get(auth).gate === 'open-offline') void sb.auth.getSession();
      });
    }
    const stored = storedSession();
    const plan = planStartup({ stored, online: online(), nowMs: Date.now(), graceDays: OFFLINE_GRACE_DAYS });
    if (plan.kind === 'needs-account') {
      patch({ gate: 'needs-account' });
    } else if (plan.kind === 'offline') {
      await openWithStoredSession(stored!, plan.gate);
    } else {
      // online: renew if needed and ask the server (getUser notices a deleted or disabled account)
      const first = await withinTime(sb.auth.getSession());
      const { data, error } = first?.value ?? { data: { session: null }, error: { message: 'timeout', status: 0 } as any };
      if (data.session) {
        const second = await withinTime(sb.auth.getUser());
        const { data: u, error: uerr } = second?.value ?? { data: { user: null }, error: { message: 'timeout', status: 0 } as any };
        if (!uerr && u.user) await onSignedIn(data.session, 'start');
        else if (uerr && isDefiniteAuthFailure(uerr)) {
          await sb.auth.signOut({ scope: 'local' });
          patch({ gate: 'needs-account' });
        } else {
          const fallback = planStartup({ stored: storedSession() ?? stored, online: false, nowMs: Date.now(), graceDays: OFFLINE_GRACE_DAYS });
          if (fallback.kind === 'offline') await openWithStoredSession((storedSession() ?? stored)!, fallback.gate);
          else patch({ gate: 'needs-account' });
        }
      } else if (isDefiniteAuthFailure(error) || !storedSession()) {
        patch({ gate: 'needs-account' }); // the server refused the session (supabase-js dropped it)
      } else {
        // the renewal failed for another reason (5xx, 429, a portal page): that is not a verdict
        const again = storedSession()!;
        const fallback = planStartup({ stored: again, online: false, nowMs: Date.now(), graceDays: OFFLINE_GRACE_DAYS });
        if (fallback.kind === 'offline') await openWithStoredSession(again, fallback.gate);
        else patch({ gate: 'needs-account' });
      }
    }
  } catch {
    const again = storedSession();
    const fallback = planStartup({ stored: again, online: false, nowMs: Date.now(), graceDays: OFFLINE_GRACE_DAYS });
    if (again && fallback.kind === 'offline') await openWithStoredSession(again, fallback.gate);
    else patch({ gate: 'needs-account' });
  } finally {
    patch({ ready: true });
  }
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
    await refreshProfile();
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
  return profileMissing(s, TERMS_VERSION);
}

/** May the app open (instead of the sign-in screen)? */
export function appMayOpen(s: AuthState): boolean {
  return mayOpen(s, TERMS_VERSION);
}

export const authSnapshot = () => get(auth);
