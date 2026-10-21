import { hasAuthEnv } from './localUser';

export type DaltonAuthMode = 'oss' | 'supabase';

export const DALTON_AUTH_MODE: DaltonAuthMode =
  import.meta.env.VITE_DALTON_AUTH === 'supabase' ? 'supabase' : 'oss';

/**
 * Dalton Supabase login switch for OSS deployments (`VITE_HOST_MODE=oss`).
 * Requires `VITE_DALTON_AUTH=supabase` alongside `VITE_SUPABASE_URL` and
 * `VITE_SUPABASE_PUBLISHABLE_KEY`.
 */
export const isDaltonAuth: boolean = DALTON_AUTH_MODE === 'supabase' && hasAuthEnv;
