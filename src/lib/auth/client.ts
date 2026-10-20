import type { SupabaseClient } from '@supabase/supabase-js';
import { SUPABASE_URL, SUPABASE_KEY } from './config';
import { isWebMode } from '../utils/env';

let client: SupabaseClient | null = null;

/** The one Supabase client (the library is fetched on first use so it never slows the first paint). */
export async function getClient(): Promise<SupabaseClient> {
  if (client) return client;
  const { createClient } = await import('@supabase/supabase-js');
  client = createClient(SUPABASE_URL, SUPABASE_KEY, {
    auth: {
      flowType: 'pkce',
      persistSession: true,
      autoRefreshToken: true,
      // The desktop app finishes OAuth itself (see oauth.ts); a browser tab lets the library do it.
      detectSessionInUrl: isWebMode(),
      storageKey: 'radarx.auth',
    },
  });
  return client;
}
