/**
 * The decision "may the app open?", kept free of any browser or Supabase API so it is unit-tested.
 *
 * A first run asks for an account. After that the app opens whenever the stored session was
 * verified with the server within the grace period, so a radar scientist on a ship with no
 * internet is never locked out of their own data. Past the grace period the app asks to reconnect
 * and sign in again.
 */
export type GateState =
  | 'needs-account' // never signed in on this install
  | 'open' // signed in and verified recently
  | 'open-offline' // signed in, server unreachable, still inside the grace period
  | 'reconnect'; // signed in once, but not verified for longer than the grace period

export interface GateInput {
  hasSession: boolean;
  /** ms since epoch of the last successful server check, or null if never */
  lastVerifiedAt: number | null;
  now: number;
  online: boolean;
  graceDays: number;
}

const DAY_MS = 86_400_000;

export function gateState(i: GateInput): GateState {
  if (!i.hasSession) return 'needs-account';
  if (i.online) return 'open';
  const last = i.lastVerifiedAt;
  if (last == null || !Number.isFinite(last)) return 'reconnect';
  // a clock set backwards must not extend the grace period indefinitely
  if (last > i.now + DAY_MS) return 'reconnect';
  return i.now - last <= i.graceDays * DAY_MS ? 'open-offline' : 'reconnect';
}

/** Whole days left of the offline grace period (0 when over). */
export function graceDaysLeft(lastVerifiedAt: number | null, now: number, graceDays: number): number {
  if (lastVerifiedAt == null) return 0;
  return Math.max(0, Math.ceil(graceDays - (now - lastVerifiedAt) / DAY_MS));
}

/** Basic address check; the server is the real authority. */
export function looksLikeEmail(s: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(s.trim());
}

/** Password rules shown to the user (the server enforces its own minimum too). */
export function passwordProblem(pw: string): string | null {
  if (pw.length < 8) return 'Use at least 8 characters.';
  if (!/[a-zA-Z]/.test(pw) || !/[0-9]/.test(pw)) return 'Mix letters and numbers.';
  return null;
}

/** Turns Supabase's terse errors into sentences a beginner can act on. */
export function friendlyAuthError(message: string | undefined | null): string {
  const m = (message ?? '').toLowerCase();
  if (!m) return 'Something went wrong. Please try again.';
  if (m.includes('invalid login credentials')) return 'That e-mail and password do not match. Check them, or use "Forgot password".';
  if (m.includes('email not confirmed')) return 'Please confirm your e-mail first: enter the code we sent you.';
  if (m.includes('already registered') || m.includes('already been registered')) return 'There is already an account with this e-mail. Try signing in instead.';
  if (m.includes('rate limit') || m.includes('too many')) return 'Too many attempts. Wait a minute and try again.';
  if (m.includes('token has expired') || m.includes('invalid') && m.includes('otp')) return 'That code is wrong or has expired. Request a new one.';
  if (m.includes('fetch') || m.includes('network') || m.includes('failed to')) return 'Cannot reach the account server. Check your internet connection.';
  if (m.includes('provider is not enabled') || m.includes('unsupported provider')) return 'This sign-in method is not switched on yet.';
  return message as string;
}

/**
 * The text shown when the provider's redirect carries an `error`. Only a fixed sentence is ever shown:
 * `error_description` is text chosen by whoever built the redirect (anything that can reach the loopback
 * port can build one), so it must not reach the screen.
 */
export function oauthFailureMessage(errorParam: string | null | undefined): string {
  return (errorParam ?? '').toLowerCase() === 'access_denied'
    ? 'Sign-in was cancelled.'
    : 'The sign-in was not completed. Please try again.';
}
