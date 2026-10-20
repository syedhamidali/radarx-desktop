/**
 * When the app may open instead of the sign-in screen, as pure functions (no imports) so Node tests them.
 * The store (stores/auth.ts) and AuthRoot use these; the terms version is passed in.
 */
export type OpenState = {
  gate: string;
  email: string | null;
  profileState: 'loading' | 'ready' | 'failed';
  profile: { affiliation: string | null; terms_version: string | null } | null;
};

/** With the account gate switched off at build time (UI tests, offline-only builds) there is no profile to wait for. */
export function initialProfileState(gateDisabled: boolean): 'loading' | 'ready' {
  return gateDisabled ? 'ready' : 'loading';
}

/** True when the profile still lacks what sign-up asks for (provider sign-ins skip the sign-up form). */
export function profileMissing(s: OpenState, termsVersion: string): boolean {
  const p = s.profile;
  // only once the profile has really been read: while it loads, or when it could not be read (offline,
  // server trouble), nobody is held at "One more step" (the Account dialog offers a retry)
  return s.gate === 'open' && !!s.email && s.profileState === 'ready' && (!p || !p.affiliation || p.terms_version !== termsVersion);
}

/** May the app open? One rule for AuthRoot and the tests. */
export function appMayOpen(s: OpenState, termsVersion: string): boolean {
  return (s.gate === 'open' || s.gate === 'open-offline') && !profileMissing(s, termsVersion) && s.profileState !== 'loading';
}
