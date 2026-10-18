import { api } from '@/api/client';

export interface OAuthStatusResponse {
  connected: boolean;
  account_id?: string | null;
  email?: string | null;
  plan_type?: string | null;
}

export interface DeviceCodeResponse {
  user_code: string;
  verification_url: string;
  interval?: number;
}

export async function initiateAntigravityOAuth(): Promise<{
  authorize_url: string;
  port?: number;
}> {
  const { data } = await api.post('/api/v1/oauth/antigravity/initiate');
  return data;
}

export async function pollAntigravityOAuth(): Promise<{
  success?: boolean;
  pending?: boolean;
  email?: string | null;
  plan_type?: string | null;
}> {
  const { data } = await api.post('/api/v1/oauth/antigravity/poll');
  return data;
}

export async function submitAntigravityCallback(
  callbackInput: string,
): Promise<{
  success?: boolean;
  connected?: boolean;
  email?: string | null;
  plan_type?: string | null;
}> {
  const { data } = await api.post('/api/v1/oauth/antigravity/callback', {
    callback_input: callbackInput,
  });
  return data;
}

export async function getAntigravityOAuthStatus(): Promise<OAuthStatusResponse> {
  const { data } = await api.get('/api/v1/oauth/antigravity/status');
  return data;
}

export async function disconnectAntigravityOAuth(): Promise<{ success: boolean }> {
  const { data } = await api.delete('/api/v1/oauth/antigravity');
  return data;
}

export async function initiateMuseDevice(): Promise<DeviceCodeResponse> {
  const { data } = await api.post('/api/v1/oauth/muse/device/initiate');
  return data;
}

export async function pollMuseDevice(): Promise<{
  success?: boolean;
  pending?: boolean;
}> {
  const { data } = await api.post('/api/v1/oauth/muse/device/poll');
  return data;
}

export async function getMuseOAuthStatus(): Promise<OAuthStatusResponse> {
  const { data } = await api.get('/api/v1/oauth/muse/status');
  return data;
}

export async function disconnectMuseOAuth(): Promise<{ success: boolean }> {
  const { data } = await api.delete('/api/v1/oauth/muse');
  return data;
}

export async function disconnectDaltonOAuth(
  provider: string,
): Promise<boolean> {
  if (provider === 'antigravity-oauth' || provider === 'antigravity') {
    await disconnectAntigravityOAuth();
    return true;
  }
  if (provider === 'muse-oauth' || provider === 'meta' || provider === 'muse') {
    await disconnectMuseOAuth();
    return true;
  }
  return false;
}
