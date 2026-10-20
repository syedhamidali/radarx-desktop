/**
 * Where accounts live. Both values are PUBLIC by design: the project URL and the publishable
 * (anon) key only identify the project, and every table is protected by row-level security (see
 * supabase/migrations). Never put the database password or a service-role key in this repo.
 * A build can point at another project with VITE_SUPABASE_URL / VITE_SUPABASE_KEY.
 */
const env = (import.meta as any).env ?? {};

export const SUPABASE_URL: string = env.VITE_SUPABASE_URL || 'https://bfyimkgvnqiqedcwprpi.supabase.co';
export const SUPABASE_KEY: string = env.VITE_SUPABASE_KEY || 'sb_publishable_PiB4d1KKf-BEMEIXS5t6AQ_WfLZXlbA';

/** Build-time switch for automated UI tests and offline-only builds (never set in releases). */
declare const __AUTH_DISABLED__: boolean | undefined;
export const AUTH_DISABLED: boolean = typeof __AUTH_DISABLED__ !== 'undefined' && !!__AUTH_DISABLED__;

/**
 * Sign-in providers besides e-mail. A button is shown only for a provider that is BOTH switched on in
 * the Supabase dashboard AND listed in VITE_OAUTH_PROVIDERS (comma separated, e.g. "google,apple").
 * The default is none: e-mail sign-up is the way in until a provider's app has been created
 * (docs/ACCOUNTS.md).
 */
const ALL_PROVIDERS = [
  { id: 'google', label: 'Google' },
  { id: 'apple', label: 'Apple' },
  { id: 'facebook', label: 'Facebook' },
] as const;

export function enabledProviders(list: string | undefined) {
  const want = new Set((list ?? '').split(',').map((x) => x.trim().toLowerCase()).filter(Boolean));
  return ALL_PROVIDERS.filter((p) => want.has(p.id));
}

export const OAUTH_PROVIDERS = enabledProviders(env.VITE_OAUTH_PROVIDERS);
export type OAuthProvider = (typeof ALL_PROVIDERS)[number]['id'];

/** How long a signed-in install keeps working with no network (field laptops, ships, aircraft). */
export const OFFLINE_GRACE_DAYS = 30;
