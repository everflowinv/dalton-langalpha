/**
 * HTTP client for standalone core local authentication (`/api/v1/dalton/auth/local/*`)
 * and first-administrator bootstrap (`/api/v1/dalton/bootstrap/*`).
 *
 * All refresh-cookie endpoints pass `credentials: 'include'` so the browser sends and
 * stores the HttpOnly `dalton_refresh_token` cookie. Access tokens live strictly in
 * memory (`lib/authToken.ts`) and are never written to Web Storage.
 */

import { bearerHeaderFor } from '@/lib/authToken';

const baseURL: string = (import.meta.env.VITE_API_BASE_URL as string) ?? '';

export interface LocalAuthUser {
  user_id: string;
  email: string;
  username: string | null;
  name?: string | null;
  tier: string;
}

export interface LocalLoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: LocalAuthUser;
}

export interface LocalRefreshResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface BootstrapStatusResponse {
  needs_bootstrap: boolean;
}

export interface BootstrapAdminPayload {
  bootstrap_token: string;
  email: string;
  password: string;
  username?: string;
  name?: string;
}

export interface LocalChangePasswordResponse {
  status: string;
  sessions_revoked: number;
}

export class LocalAuthError extends Error {
  status: number;
  code: string | null;
  retryAfter: number | null;

  constructor(
    message: string,
    status: number,
    options?: { code?: string | null; retryAfter?: number | null },
  ) {
    super(message);
    this.name = 'LocalAuthError';
    this.status = status;
    this.code = options?.code ?? null;
    this.retryAfter = options?.retryAfter ?? null;
  }
}

async function readErrorMessage(res: Response, fallback: string): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown; message?: unknown };
    if (typeof body?.detail === 'string' && body.detail.trim()) {
      return body.detail;
    }
    if (body?.detail && typeof body.detail === 'object') {
      const msg = (body.detail as { message?: unknown }).message;
      if (typeof msg === 'string' && msg.trim()) {
        return msg;
      }
    }
    if (typeof body?.message === 'string' && body.message.trim()) {
      return body.message;
    }
  } catch {
    /* ignore non-JSON body */
  }
  return fallback;
}

function errorCodeForStatus(status: number): string | null {
  if (status === 401) return 'invalid_credentials';
  if (status === 429) return 'over_request_rate_limit';
  return null;
}

/**
 * Decode the `sub` claim from a local JWT access token payload without external libraries.
 * Used only to populate `userId` in memory after a cookie refresh; signature verification
 * always happens on the backend.
 */
export function parseJwtSubject(token: string): string | null {
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  try {
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const pad = base64.length % 4 === 0 ? '' : '='.repeat(4 - (base64.length % 4));
    const json = atob(base64 + pad);
    const payload = JSON.parse(json) as { sub?: unknown };
    return typeof payload?.sub === 'string' && payload.sub.trim() ? payload.sub.trim() : null;
  } catch {
    return null;
  }
}

export async function localLogin(
  identifier: string,
  password: string,
): Promise<LocalLoginResponse> {
  const res = await fetch(`${baseURL}/api/v1/dalton/auth/local/login`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identifier: identifier.trim(), password }),
  });
  if (!res.ok) {
    const detail = await readErrorMessage(res, `Login failed (${res.status})`);
    const retryHeader = res.headers.get('Retry-After');
    const retryAfter = retryHeader ? parseInt(retryHeader, 10) || null : null;
    throw new LocalAuthError(detail, res.status, {
      code: errorCodeForStatus(res.status),
      retryAfter,
    });
  }
  return (await res.json()) as LocalLoginResponse;
}

/**
 * Rotate the HttpOnly refresh cookie and return a fresh short-lived access token.
 * Returns `null` when the backend responds 401 (no active cookie or revoked session).
 * Retries once briefly if a sibling tab hit the concurrent refresh grace window (409).
 */
export async function localRefresh(retryOnConflict = true): Promise<LocalRefreshResponse | null> {
  const res = await fetch(`${baseURL}/api/v1/dalton/auth/local/refresh`, {
    method: 'POST',
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });
  if (res.status === 401) {
    return null;
  }
  if (res.status === 409 && retryOnConflict) {
    await new Promise((resolve) => setTimeout(resolve, 150));
    return localRefresh(false);
  }
  if (!res.ok) {
    const detail = await readErrorMessage(res, `Session refresh failed (${res.status})`);
    throw new LocalAuthError(detail, res.status);
  }
  return (await res.json()) as LocalRefreshResponse;
}

export async function localLogout(): Promise<void> {
  try {
    await fetch(`${baseURL}/api/v1/dalton/auth/local/logout`, {
      method: 'POST',
      credentials: 'include',
      headers: { Accept: 'application/json' },
    });
  } catch {
    /* local state still clears even if the network call fails */
  }
}

export async function localChangePassword(
  accessToken: string,
  currentPassword: string,
  newPassword: string,
): Promise<LocalChangePasswordResponse> {
  const res = await fetch(`${baseURL}/api/v1/dalton/auth/local/change-password`, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...bearerHeaderFor(accessToken),
    },
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });
  if (!res.ok) {
    const detail = await readErrorMessage(res, `Password change failed (${res.status})`);
    throw new LocalAuthError(detail, res.status);
  }
  return (await res.json()) as LocalChangePasswordResponse;
}

export async function fetchBootstrapStatus(): Promise<BootstrapStatusResponse> {
  const res = await fetch(`${baseURL}/api/v1/dalton/bootstrap/status`, {
    method: 'GET',
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });
  if (!res.ok) {
    const detail = await readErrorMessage(res, `Bootstrap status check failed (${res.status})`);
    throw new LocalAuthError(detail, res.status);
  }
  return (await res.json()) as BootstrapStatusResponse;
}

export async function bootstrapFirstAdmin(
  payload: BootstrapAdminPayload,
): Promise<LocalLoginResponse> {
  const body: Record<string, string> = {
    bootstrap_token: payload.bootstrap_token.trim(),
    email: payload.email.trim(),
    password: payload.password,
  };
  if (payload.username && payload.username.trim()) {
    body.username = payload.username.trim();
  }
  if (payload.name && payload.name.trim()) {
    body.name = payload.name.trim();
  }
  const res = await fetch(`${baseURL}/api/v1/dalton/bootstrap/admin`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await readErrorMessage(res, `Admin setup failed (${res.status})`);
    throw new LocalAuthError(detail, res.status);
  }
  return (await res.json()) as LocalLoginResponse;
}
