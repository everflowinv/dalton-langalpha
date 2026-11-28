import { useContext } from 'react';
import { QueryClient, QueryClientContext, useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';
import { queryKeys } from '@/lib/queryKeys';

export type PersonalIntegrationProvider = 'google' | 'feishu';
export type PersonalIntegrationService = 'calendar' | 'mail';
export type PersonalIntegrationStatus =
  | 'connected'
  | 'needs_reauth'
  | 'refresh_ambiguous'
  | 'disconnect_pending'
  | 'revoked_purging';

export interface PersonalIntegrationConnectorInfo {
  provider: PersonalIntegrationProvider;
  service: PersonalIntegrationService;
  capability_id: string;
  batch: 1 | 2 | 3;
  enabled: boolean;
  ready: boolean;
  credentials_ready: boolean;
  langsmith_tracing_enabled?: boolean;
  callback_url: string;
  events_url: string | null;
}

export interface PersonalIntegrationConnection {
  provider: PersonalIntegrationProvider;
  service: PersonalIntegrationService;
  provider_account_id: string;
  provider_email: string | null;
  granted_scopes: string[];
  status: PersonalIntegrationStatus;
  auth_epoch: number;
  authorized_at: string | null;
  expires_at: string | null;
  last_refresh_at: string | null;
  needs_reauth_at: string | null;
  last_error_code: string | null;
  client_rotated: boolean;
  preferences: {
    primary_calendar_id?: string | null;
    tracker_calendar_id?: string | null;
    allow_create_draft?: boolean;
    allow_send_mail?: boolean;
    [key: string]: unknown;
  };
  updated_at: string | null;
}

export interface PersonalIntegrationPurgeLedger {
  purge_id: string;
  provider: PersonalIntegrationProvider;
  service: PersonalIntegrationService | null;
  provider_account_id: string;
  trigger_reason: string;
  local_purge_status: 'running' | 'completed' | 'not_required';
  remote_revoke_status:
    | 'pending'
    | 'succeeded'
    | 'failed_retryable'
    | 'superseded'
    | 'not_applicable';
  retry_count: number;
  next_retry_at: string | null;
  last_remote_error: string | null;
  disconnected_at: string | null;
  local_completed_at: string | null;
  remote_completed_at: string | null;
}

export interface PersonalIntegrationsListResponse {
  connectors: PersonalIntegrationConnectorInfo[];
  connections: PersonalIntegrationConnection[];
  active_purges: PersonalIntegrationPurgeLedger[];
  recent_purges?: PersonalIntegrationPurgeLedger[];
  has_personal_integrations: boolean;
}

export interface AuthorizePersonalIntegrationRequest {
  switch_account?: boolean;
  allow_create_draft?: boolean;
  allow_send_mail?: boolean;
  return_to?: string;
}

export interface AuthorizePersonalIntegrationResponse {
  authorize_url: string;
  provider: PersonalIntegrationProvider;
  service: PersonalIntegrationService;
  intent: string;
  expires_in: number;
}

export interface ConfirmPersonalIntegrationRequest {
  confirmation_token: string;
  confirm_account_switch?: boolean;
  cancel?: boolean;
}

export interface ConfirmPersonalIntegrationResponse {
  status: 'connected' | 'cancelled' | 'account_switch_confirmation_required';
  code?: string;
  message?: string;
  provider: PersonalIntegrationProvider;
  service: PersonalIntegrationService;
  connection?: PersonalIntegrationConnection | null;
  purge_id?: string | null;
  previous_account_email?: string | null;
  new_account_email?: string | null;
}

export interface DisconnectPersonalIntegrationResponse {
  local_disconnected: boolean;
  local_tokens_destroyed: boolean;
  provider: PersonalIntegrationProvider;
  service?: PersonalIntegrationService;
  purge_id?: string;
  local_purge_status?: string;
  remote_revoke_status?: string;
  remote_revocation_mode?: string;
  remote_access_token_max_ttl_seconds?: number;
}

export interface RetryRemoteRevokeResponse {
  purge: PersonalIntegrationPurgeLedger;
}

export async function fetchPersonalIntegrations(): Promise<PersonalIntegrationsListResponse> {
  const { data } = await api.get<PersonalIntegrationsListResponse>('/api/v1/dalton/integrations');
  return data;
}

export async function authorizePersonalIntegration(
  provider: PersonalIntegrationProvider,
  service: PersonalIntegrationService,
  body: AuthorizePersonalIntegrationRequest = {},
): Promise<AuthorizePersonalIntegrationResponse> {
  const { data } = await api.post<AuthorizePersonalIntegrationResponse>(
    `/api/v1/dalton/integrations/${provider}/${service}/authorize`,
    body,
  );
  return data;
}

export async function confirmPersonalIntegration(
  body: ConfirmPersonalIntegrationRequest,
): Promise<ConfirmPersonalIntegrationResponse> {
  const { data } = await api.post<ConfirmPersonalIntegrationResponse>(
    '/api/v1/dalton/integrations/confirm',
    body,
  );
  return data;
}

export async function updatePersonalIntegrationPreferences(
  provider: PersonalIntegrationProvider,
  service: PersonalIntegrationService,
  body: {
    primary_calendar_id?: string;
    tracker_calendar_id?: string;
    allow_create_draft?: boolean;
    allow_send_mail?: boolean;
  },
): Promise<{ connection: PersonalIntegrationConnection }> {
  const { data } = await api.patch<{ connection: PersonalIntegrationConnection }>(
    `/api/v1/dalton/integrations/${provider}/${service}`,
    body,
  );
  return data;
}

export async function disconnectPersonalIntegrationService(
  provider: PersonalIntegrationProvider,
  service: PersonalIntegrationService,
  mode: 'remote_if_sole' | 'local_only' = 'remote_if_sole',
): Promise<DisconnectPersonalIntegrationResponse> {
  const { data } = await api.delete<DisconnectPersonalIntegrationResponse>(
    `/api/v1/dalton/integrations/${provider}/${service}?mode=${mode}`,
  );
  return data;
}

export interface DisconnectPersonalIntegrationProviderPurgeItem {
  purge_id: string;
  local_purge_status?: 'running' | 'completed' | 'not_required' | string;
  remote_revoke_status?:
    | 'pending'
    | 'succeeded'
    | 'failed_retryable'
    | 'superseded'
    | 'not_applicable'
    | string;
}

export interface DisconnectPersonalIntegrationProviderResponse {
  local_disconnected?: boolean;
  local_tokens_destroyed?: boolean;
  provider: PersonalIntegrationProvider;
  purges: DisconnectPersonalIntegrationProviderPurgeItem[];
}

export async function disconnectPersonalIntegrationProvider(
  provider: PersonalIntegrationProvider,
  mode: 'remote_if_sole' | 'local_only' = 'remote_if_sole',
): Promise<DisconnectPersonalIntegrationProviderResponse> {
  const { data } = await api.delete<DisconnectPersonalIntegrationProviderResponse>(
    `/api/v1/dalton/integrations/${provider}?mode=${mode}`,
  );
  return data;
}

export async function retryPersonalIntegrationRevoke(
  purgeId: string,
): Promise<RetryRemoteRevokeResponse> {
  const { data } = await api.post<RetryRemoteRevokeResponse>(
    `/api/v1/dalton/integrations/purges/${encodeURIComponent(purgeId)}/retry-revoke`,
  );
  return data;
}

const FALLBACK_QUERY_CLIENT = new QueryClient({
  defaultOptions: {
    queries: { enabled: false, retry: false },
  },
});

export function hasPendingPersonalIntegrationWork(
  current: PersonalIntegrationsListResponse | undefined,
): boolean {
  if (!current) return false;
  return (
    (current.active_purges ?? []).some(
      (p) => p.local_purge_status === 'running' || p.remote_revoke_status === 'pending',
    ) ||
    (current.connections ?? []).some(
      (c) => c.status === 'disconnect_pending' || c.status === 'revoked_purging',
    )
  );
}

export function usePersonalIntegrationsQuery(enabled = true) {
  const contextClient = useContext(QueryClientContext);
  const queryEnabled = enabled && contextClient !== undefined;
  return useQuery<PersonalIntegrationsListResponse>(
    {
      queryKey: queryKeys.dalton.personalIntegrations(),
      queryFn: fetchPersonalIntegrations,
      enabled: queryEnabled,
      retry: false,
      staleTime: 60_000,
      refetchInterval: (query) =>
        hasPendingPersonalIntegrationWork(query.state.data) ? 3_000 : false,
    },
    contextClient ?? FALLBACK_QUERY_CLIENT,
  );
}

/**
 * Lightweight visibility helper: returns true when the current user has any
 * existing personal integration connection or active purge ledger (even if
 * deployment capabilities were later turned off, §6 item 3).
 */
export function useHasPersonalIntegrations(enabled = true): boolean {
  const { data } = usePersonalIntegrationsQuery(enabled);
  return Boolean(data?.has_personal_integrations);
}
