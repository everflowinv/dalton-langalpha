import { api, ApiError } from '@/api/client';
import type { DaltonProfile } from '@/config/brand';

export type CredentialSource = 'env' | 'saved' | 'vault_readonly' | 'vault_error' | 'unset';

export type DeploymentFieldInputKind =
  | 'secret'
  | 'text'
  | 'url'
  | 'boolean'
  | 'boolean_one'
  | 'select'
  | 'textarea';

export interface DeploymentSettingField {
  name: string;
  section: string;
  label_zh: string;
  label_en: string;
  secret: boolean;
  credential_group: string | null;
  pair_with: string | null;
  input_kind: DeploymentFieldInputKind;
  requires_restart: string;
  test_kind: string | null;
  batch: number;
  configured: boolean;
  source: CredentialSource;
  readonly: boolean;
  updated_at: string | null;
  value?: string | null;
}

export interface DeploymentSettingsResponse {
  profile: DaltonProfile;
  revision: number;
  encryption_configured: boolean;
  fields: DeploymentSettingField[];
  redirect_uris: {
    google: string;
    feishu: string;
  };
  feishu_events_url: string;
  google_redirect_uri_eligible: boolean;
  google_redirect_uri_reason: string | null;
}

export interface DeploymentSettingsUpdatePayload {
  expected_revision: number;
  values: Record<string, string | boolean | null>;
}

export interface DeploymentSettingsUpdateResponse {
  revision: number;
  changed: string[];
  pending_apply: boolean;
  warnings: string[];
}

export type ProbeTarget = 'google_oauth' | 'feishu_app';

export type ProbeVerdict =
  | 'valid'
  | 'invalid_credentials'
  | 'network_error'
  | 'rate_limited'
  | 'format_invalid';

export interface DeploymentCredentialProbeResponse {
  ok: boolean;
  network_reachable: boolean;
  credentials_valid: boolean | null;
  verdict: ProbeVerdict;
  detail_code: string;
}

export interface ParsedDeploymentSettingsError {
  status: number | null;
  code: string | null;
  currentRevision?: number;
  settingName?: string;
  reasons: string[];
}

export const DEPLOYMENT_SETTINGS_ENDPOINT = '/api/v1/dalton/deployment-settings';
export const ADMIN_INTEGRATIONS_HELP_URL = '/help/get-started/admin-integrations/';

export async function fetchDeploymentSettings(): Promise<DeploymentSettingsResponse> {
  const { data } = await api.get<DeploymentSettingsResponse>(DEPLOYMENT_SETTINGS_ENDPOINT);
  return data;
}

export async function updateDeploymentSettings(
  payload: DeploymentSettingsUpdatePayload,
): Promise<DeploymentSettingsUpdateResponse> {
  const { data } = await api.put<DeploymentSettingsUpdateResponse>(
    DEPLOYMENT_SETTINGS_ENDPOINT,
    payload,
  );
  return data;
}

export async function testDeploymentCredentials(
  target: ProbeTarget,
  values: Record<string, string | null> = {},
): Promise<DeploymentCredentialProbeResponse> {
  const { data } = await api.post<DeploymentCredentialProbeResponse>(
    `${DEPLOYMENT_SETTINGS_ENDPOINT}/test`,
    { target, values },
  );
  return data;
}

/** Extract structured error codes from a failed deployment settings API call without leaking input values. */
export function parseDeploymentSettingsError(err: unknown): ParsedDeploymentSettingsError {
  if (!(err instanceof ApiError) || !err.response) {
    return { status: null, code: null, reasons: [] };
  }
  const { status, data } = err.response;
  const body = (data && typeof data === 'object' ? data : {}) as Record<string, unknown>;
  const detail =
    body.detail && typeof body.detail === 'object'
      ? (body.detail as Record<string, unknown>)
      : null;

  const code =
    (typeof body.code === 'string' && body.code) ||
    (detail && typeof detail.code === 'string' && detail.code) ||
    null;

  const currentRevision =
    typeof body.current_revision === 'number' ? body.current_revision : undefined;
  const settingName = typeof body.setting_name === 'string' ? body.setting_name : undefined;

  const rawErrors = Array.isArray(body.errors) ? body.errors : [];
  const reasons: string[] = [];
  for (const item of rawErrors) {
    if (item && typeof item === 'object' && typeof (item as Record<string, unknown>).reason === 'string') {
      reasons.push(String((item as Record<string, unknown>).reason));
    }
  }

  return {
    status,
    code,
    currentRevision,
    settingName,
    reasons,
  };
}
