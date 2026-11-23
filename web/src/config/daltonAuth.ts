import { hasAuthEnv } from './localUser';

export type DaltonAuthMode = 'oss' | 'supabase' | 'local';

export const DALTON_AUTH_MODE: DaltonAuthMode =
  import.meta.env.VITE_DALTON_AUTH === 'supabase'
    ? 'supabase'
    : import.meta.env.VITE_DALTON_AUTH === 'local'
      ? 'local'
      : 'oss';

/**
 * Dalton Supabase login switch for OSS deployments (`VITE_HOST_MODE=oss`).
 * Requires `VITE_DALTON_AUTH=supabase` alongside `VITE_SUPABASE_URL` and
 * `VITE_SUPABASE_PUBLISHABLE_KEY`.
 */
export const isDaltonSupabaseAuth: boolean = DALTON_AUTH_MODE === 'supabase' && hasAuthEnv;

/**
 * Standalone core local account login switch (`VITE_DALTON_AUTH=local`).
 * Uses backend `/api/v1/dalton/auth/local/*` and `/api/v1/dalton/bootstrap/*`.
 */
export const isDaltonLocalAuth: boolean = DALTON_AUTH_MODE === 'local';

/**
 * True whenever Dalton multi-user auth (Supabase or local accounts) is active.
 */
export const isDaltonAuth: boolean = isDaltonSupabaseAuth || isDaltonLocalAuth;

export const DEFAULT_DALTON_INVITE_EMAIL_DOMAIN = 'team.dalton.local';

export function getDaltonInviteEmailDomain(override?: string): string {
  const raw = (override ?? import.meta.env.VITE_DALTON_INVITE_EMAIL_DOMAIN ?? '')
    .trim()
    .toLowerCase()
    .replace(/^@+/, '');
  return raw || DEFAULT_DALTON_INVITE_EMAIL_DOMAIN;
}

export const DALTON_INVITE_EMAIL_DOMAIN: string = getDaltonInviteEmailDomain();

/**
 * Resolve a login input ("email or username") into an email address for Supabase login.
 * If the trimmed identifier contains no `@`, appends `@{VITE_DALTON_INVITE_EMAIL_DOMAIN}`.
 */
export function resolveDaltonLoginEmail(identifier: string, domainOverride?: string): string {
  const trimmed = identifier.trim();
  if (!trimmed || trimmed.includes('@')) {
    return trimmed;
  }
  return `${trimmed.toLowerCase()}@${getDaltonInviteEmailDomain(domainOverride)}`;
}

