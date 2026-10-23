import { api } from '@/api/client';

export interface OpenClawModelSummary {
  key: string;
  openclaw_ref: string;
  display_name: string;
  family: string;
  subscription: boolean;
  visible: boolean;
  hide_reason?: string | null;
  tool_calling: boolean;
  context?: number | null;
  max_output?: number | null;
  pricing_summary?: {
    input?: number;
    output?: number;
    cached_input?: number;
    tiered?: boolean;
  } | null;
  notional_summary?: {
    input?: number;
    output?: number;
    cached_input?: number;
    tiered?: boolean;
  } | null;
  warnings: string[];
}

export interface OpenClawSyncRunRecord {
  run_id: string;
  started_by: string;
  dry_run: boolean;
  forced: boolean;
  status: 'running' | 'succeeded' | 'failed' | 'blocked' | 'interrupted';
  started_at: string;
  finished_at?: string | null;
  counts?: Record<string, number> | null;
  diff?: Record<string, unknown> | null;
  warnings?: string[] | null;
  error?: string | null;
}

export interface OpenClawRoleWarning {
  code: string;
  role: string;
  model: string;
  message: string;
  link: string;
}

export interface OpenClawStatusResponse {
  meta: {
    generated_at?: string | null;
    source?: string | null;
    broker_version?: string | null;
    openclaw_version?: string | null;
    counts: {
      catalog: number;
      visible: number;
      hidden: number;
      text_only: number;
    };
    warnings: string[];
    price_discrepancies: Array<{
      ref: string;
      field: string;
      values: Record<string, number>;
      chosen: number;
    }>;
  };
  last_run?: OpenClawSyncRunRecord | null;
  last_succeeded_run?: OpenClawSyncRunRecord | null;
  role_warnings: OpenClawRoleWarning[];
  models: OpenClawModelSummary[];
}

export interface OpenClawHealthResponse {
  status:
    | 'ok'
    | 'degraded'
    | 'not_installed'
    | 'gateway_restarting'
    | 'auth_error'
    | 'token_missing'
    | 'unreachable'
    | 'error';
  http_status?: number | null;
  latency_ms: number;
  checked_at: string;
  base_url: string;
  broker?: {
    brokerVersion?: string;
    openclawVersion?: string;
    journal?: { pending?: number; completed?: number; failed?: number };
    inflight?: number;
    queued?: number;
  } | null;
  smoke?: {
    generated_at?: string;
    blocking_failures?: string[];
  } | null;
}

export interface OpenClawSyncResponse {
  run_id: string;
  dry_run: boolean;
  applied: boolean;
  added: Array<{ key: string; openclaw_ref: string; display_name: string }>;
  removed: Array<{ key: string; openclaw_ref: string; reason: string }>;
  changed: Array<{ key: string; fields: Record<string, [unknown, unknown]> }>;
  unchanged: number;
  warnings: string[];
  price_discrepancies: Array<{
    ref: string;
    field: string;
    values: Record<string, number>;
    chosen: number;
  }>;
  blocked: Array<{ key: string; referenced_by: string[] }>;
  synced_at?: string;
  counts: {
    allowlist: number;
    visible: number;
    hidden: number;
    text_only?: number;
    added?: number;
    removed?: number;
    changed?: number;
  };
}

export interface DaltonTaskCapEntry {
  scope_kind: 'workspace' | 'thread';
  scope_id: string;
  scope_name: string;
  cap_usd: number;
  updated_at?: string;
}

export interface DaltonBudgetSettings {
  daily_cap_usd: number | null;
  default_task_cap_usd: number | null;
  timezone: string;
  configured: boolean;
  task_caps: DaltonTaskCapEntry[];
}

export interface DaltonBudgetToday {
  day: string;
  timezone?: string;
  configured?: boolean;
  cap_usd: number;
  default_task_cap_usd?: number | null;
  settled_usd: number;
  open_reserved_usd: number;
  expired_conservative_usd: number;
  unreserved_usage_usd: number;
  spent_usd: number;
  remaining_usd: number;
  subscription_notional_usd: number;
  budget_gate_enabled?: boolean;
  by_provider?: Record<
    string,
    {
      cash_usd: number;
      notional_usd: number;
      calls: number;
    }
  >;
  runs: Array<{
    run_ref: string;
    thread_id?: string | null;
    workspace_id?: string | null;
    state: 'open' | 'released' | 'settled' | 'expired';
    reserved_usd: number;
    observed_usd: number;
    charge_usd: number;
    created_at: string;
  }>;
}

export interface DaltonMuseSubsUsage {
  window?: {
    used_percent?: number | null;
    window_duration_mins?: number | null;
    resets_at?: string | number | null;
  } | null;
  weekly?: {
    used_percent?: number | null;
    resets_at?: string | number | null;
  } | null;
  tier?: string | null;
}

export interface DaltonProviderStatusItem {
  provider: string;
  display_name: string;
  access_type: string;
  billing: 'subscription' | 'cash';
  enabled: boolean;
  configured: boolean;
  is_subs_active?: boolean | null;
  subs_tier_name?: string | null;
  subs_usage?: DaltonMuseSubsUsage | null;
  credential: {
    source?: string;
    email?: string | null;
    plan_type?: string | null;
    expires_at?: string | null;
    seconds_left?: number | null;
    last_refresh?: { at?: string; ok?: boolean } | null;
    env_key?: string;
    has_env?: boolean;
    has_byok?: boolean;
    reconnect_required?: boolean;
    status_message?: string | null;
    is_subs_active?: boolean | null;
    subs_tier_name?: string | null;
    subs_usage?: DaltonMuseSubsUsage | null;
    base_url?: string | null;
  };
  last_success?: {
    at?: string;
    model?: string;
    source?: string;
  } | null;
  recent_errors: Array<{
    at?: string;
    kind?: string;
    model?: string | null;
    message?: string;
  }>;
  errors_24h: number;
  calls_24h: number;
  used_by_roles: Array<{ role: string; model: string }>;
  warnings: Array<{ code: string; message: string }>;
  tos_risk: 'high' | 'none';
  default_probe_model?: string | null;
}

export interface DaltonProvidersStatusResponse {
  generated_at: string;
  providers: DaltonProviderStatusItem[];
  role_warnings?: OpenClawRoleWarning[];
  broker: {
    enabled: boolean;
    health?: OpenClawHealthResponse | null;
  };
}

export interface DaltonProviderProbeResponse {
  ok: boolean;
  provider: string;
  model?: string | null;
  latency_ms: number;
  reply?: string;
  usage?: Record<string, unknown>;
  error_kind?: string | null;
  message: string;
}

export interface DaltonAvailableModelsResponse {
  user_id: string;
  available_keys: string[];
  models: Record<
    string,
    {
      available: boolean;
      provider: string;
      family: string;
      reasons: string[];
    }
  >;
}

export async function fetchOpenClawStatus(): Promise<OpenClawStatusResponse> {
  const { data } = await api.get<OpenClawStatusResponse>('/api/v1/dalton/openclaw/models/status');
  return data;
}

export async function fetchOpenClawHealth(): Promise<OpenClawHealthResponse> {
  const { data } = await api.get<OpenClawHealthResponse>('/api/v1/dalton/openclaw/health');
  return data;
}

export async function syncOpenClawModels(params: {
  dry_run: boolean;
  force?: boolean;
}): Promise<OpenClawSyncResponse> {
  const { data } = await api.post<OpenClawSyncResponse>('/api/v1/dalton/openclaw/models/sync', params);
  return data;
}

export async function fetchDaltonProvidersStatus(): Promise<DaltonProvidersStatusResponse> {
  const { data } = await api.get<DaltonProvidersStatusResponse>('/api/v1/dalton/providers/status');
  return data;
}

export async function probeDaltonProvider(
  provider: string,
  model?: string | null,
): Promise<DaltonProviderProbeResponse> {
  const { data } = await api.post<DaltonProviderProbeResponse>(
    `/api/v1/dalton/providers/${encodeURIComponent(provider)}/probe`,
    model ? { model } : {},
  );
  return data;
}

export async function fetchDaltonAvailableModels(): Promise<DaltonAvailableModelsResponse> {
  const { data } = await api.get<DaltonAvailableModelsResponse>(
    '/api/v1/dalton/providers/available-models',
  );
  return data;
}

export async function fetchDaltonBudget(): Promise<DaltonBudgetSettings> {
  const { data } = await api.get<DaltonBudgetSettings>('/api/v1/dalton/budget');
  return data;
}

export async function updateDaltonBudget(payload: {
  daily_cap_usd: number;
  default_task_cap_usd: number | null;
  timezone: string;
}): Promise<DaltonBudgetSettings> {
  const { data } = await api.put<DaltonBudgetSettings>('/api/v1/dalton/budget', payload);
  return data;
}

export async function upsertDaltonTaskCap(
  scopeKind: 'workspace' | 'thread',
  scopeId: string,
  capUsd: number,
): Promise<{ scope_kind: string; scope_id: string; cap_usd: number }> {
  const { data } = await api.put(`/api/v1/dalton/budget/task-caps/${scopeKind}/${scopeId}`, {
    cap_usd: capUsd,
  });
  return data;
}

export async function deleteDaltonTaskCap(
  scopeKind: 'workspace' | 'thread',
  scopeId: string,
): Promise<{ deleted: boolean }> {
  const { data } = await api.delete(`/api/v1/dalton/budget/task-caps/${scopeKind}/${scopeId}`);
  return data;
}

export async function fetchDaltonBudgetToday(): Promise<DaltonBudgetToday> {
  const { data } = await api.get<DaltonBudgetToday>('/api/v1/dalton/budget/today');
  return data;
}
