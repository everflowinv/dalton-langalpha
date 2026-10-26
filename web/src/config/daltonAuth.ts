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

