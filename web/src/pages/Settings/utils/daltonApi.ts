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
    token_expired?: boolean;
    quota?: {
      credit_usage_percent?: number | null;
      used?: number | null;
      monthly_limit?: number | null;
      current_period?: unknown;
      subscription_tier?: string | null;
      token_expired?: boolean;
    } | null;
    reconnect_required?: boolean;
    status_message?: string | null;
    is_subs_active?: boolean | null;
    subs_tier_name?: string | null;
    subs_usage?: DaltonMuseSubsUsage | null;
    base_url?: string | null;
    billing_mode?: string;
    status_text?: string;
    hint?: string;
  };
  quota?: {
    credit_usage_percent?: number | null;
    used?: number | null;
    monthly_limit?: number | null;
    current_period?: unknown;
    subscription_tier?: string | null;
    token_expired?: boolean;
  } | null;
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

export interface NativeModelAddedItem {
  provider: string;
  upstream_id: string;
  display_name: string;
  suggested_key: string;
  conflict: boolean;
  conflict_reason?: string | null;
  duplicate_display_name?: boolean;
  context?: number | null;
  max_output?: number | null;
  supports_reasoning_effort?: boolean | null;
  reasoning_efforts?: string[] | null;
  reasoning_default?: string | null;
  visibility?: string | null;
  /** Upstream re-lists a model the PM retired; shown as a note, never auto-restored. */
  previously_retired?: boolean;
}

export interface NativeModelDeprecatedItem {
  provider: string;
  key: string;
  model_id?: string | null;
  display_name?: string | null;
  in_use_by?: string[];
}

export interface NativeModelRetiredItem {
  key: string;
  provider?: string | null;
  retired_at?: string | null;
  retired_by?: string | null;
}

export interface NativeProviderListingRow {
  provider: string;
  display_name: string;
  status: 'ok' | 'unsupported' | 'error' | 'never';
  model_count: number;
  message?: string | null;
  error_kind?: string | null;
}

export interface NativeModelsRunRecord {
  run_id: string;
  started_by: string;
  dry_run: boolean;
  forced?: boolean;
  status: 'running' | 'succeeded' | 'failed' | 'blocked' | 'interrupted';
  started_at: string;
  finished_at?: string | null;
  counts?: Record<string, number> | null;
  diff?: Record<string, unknown> | null;
  warnings?: string[] | null;
  error?: string | null;
}

export interface NativeModelsStatusResponse {
  providers: NativeProviderListingRow[];
  last_check?: NativeModelsRunRecord | null;
  last_succeeded_check?: NativeModelsRunRecord | null;
  last_sync?: NativeModelsRunRecord | null;
  diff: {
    added: NativeModelAddedItem[];
    deprecated: NativeModelDeprecatedItem[];
    unchanged: number;
  };
  retired: NativeModelRetiredItem[];
  overlay: {
    exists: boolean;
    generated_at?: string | null;
    model_count: number;
    path: string;
  };
}

export interface NativeModelsCheckResponse {
  run_id: string;
  checked_at: string;
  providers: NativeProviderListingRow[];
  diff: {
    added: NativeModelAddedItem[];
    deprecated: NativeModelDeprecatedItem[];
    unchanged: number;
  };
  counts: Record<string, number>;
  warnings: string[];
}

export interface NativeModelsSyncResponse {
  run_id: string;
  applied: boolean;
  added_keys: string[];
  skipped_keys: string[];
  synced_at: string;
}

export interface NativeModelRetireResultItem {
  key: string;
  status: 'retired' | 'in_use' | 'not_deprecated';
  in_use_by?: string[];
}

export interface NativeModelsRetireResponse {
  run_id: string;
  results: NativeModelRetireResultItem[];
  retired_keys: string[];
  retired_at: string;
}

export interface NativeModelsRestoreResponse {
  run_id: string;
  results: { key: string; status: 'restored' | 'not_retired' }[];
  restored_keys: string[];
  restored_at: string;
}

export async function fetchNativeModelsStatus(): Promise<NativeModelsStatusResponse> {
  const { data } = await api.get<NativeModelsStatusResponse>('/api/v1/dalton/native-models/status');
  return data;
}

export async function checkNativeModels(): Promise<NativeModelsCheckResponse> {
  const { data } = await api.post<NativeModelsCheckResponse>('/api/v1/dalton/native-models/check');
  return data;
}

export async function syncNativeModels(params: { models: string[] }): Promise<NativeModelsSyncResponse> {
  const { data } = await api.post<NativeModelsSyncResponse>('/api/v1/dalton/native-models/sync', params);
  return data;
}

export async function retireNativeModels(params: { keys: string[] }): Promise<NativeModelsRetireResponse> {
  const { data } = await api.post<NativeModelsRetireResponse>('/api/v1/dalton/native-models/retire', params);
  return data;
}

export async function restoreNativeModels(params: { keys: string[] }): Promise<NativeModelsRestoreResponse> {
  const { data } = await api.post<NativeModelsRestoreResponse>('/api/v1/dalton/native-models/restore', params);
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

export type DaltonSystemJobScope = 'company' | 'sector' | 'home' | 'host' | 'ops';
export type DaltonSystemJobStatus = 'on' | 'off' | 'partial' | 'unknown';

export interface DaltonSystemJobItem {
  key: string;
  kind: string;
  scope: DaltonSystemJobScope;
  title?: string;
  description?: string;
  schedule?: string | null;
  timezone?: string;
  workspace_id?: string;
  ticker?: string;
  tier?: string;
  status: DaltonSystemJobStatus;
  enabled?: boolean;
  paused_by_user?: boolean;
  last_run_at: string | null;
  last_result: string | null;
  next_run_at: string | null;
  can_toggle: boolean;
  can_run_now: boolean;
  config?: {
    extractor_model?: string;
    model?: string;
    prompt_version?: string;
    extract_concurrency?: number;
    concurrency?: number;
  };
  exit_code?: number | null;
  log_tail?: string;
  running?: boolean;
  missing_tasks?: string[];
  // sector scope（行业层 P3 起后端返回；其余 scope 无这些字段）
  unit_id?: string | null;
  sector_slug?: string;
  subject_kind?: string;
  tracking_status?: string;
  paused_tasks?: string[];
  active_tasks?: string[];
  tasks?: unknown[];
}

export interface DaltonSystemJobsResponse {
  ok: boolean;
  is_pm?: boolean;
  jobs: DaltonSystemJobItem[];
  groups?: {
    company: DaltonSystemJobItem[];
    sector?: DaltonSystemJobItem[];
    home: DaltonSystemJobItem[];
    host: DaltonSystemJobItem[];
    ops: DaltonSystemJobItem[];
  };
}

export interface DaltonSystemJobToggleResponse {
  ok: boolean;
  job?: DaltonSystemJobItem;
  jobs?: DaltonSystemJobItem[];
  error?: string;
  message?: string;
}

export interface DaltonSystemJobRunResponse {
  ok: boolean;
  key: string;
  scope: DaltonSystemJobScope;
  async: boolean;
  status: string;
  already_running?: boolean;
  execution_id?: string;
  automation_id?: string;
  where_to_check?: string;
  message?: string;
  error?: string;
}

export async function fetchDaltonSystemJobs(): Promise<DaltonSystemJobsResponse> {
  const { data } = await api.get<DaltonSystemJobsResponse>('/api/v1/dalton/system-jobs');
  return data;
}

export async function toggleDaltonSystemJob(
  key: string,
  enabled: boolean,
): Promise<DaltonSystemJobToggleResponse> {
  const { data } = await api.post<DaltonSystemJobToggleResponse>(
    `/api/v1/dalton/system-jobs/${encodeURIComponent(key)}`,
    { enabled },
  );
  return data;
}

export async function runDaltonSystemJobNow(
  key: string,
): Promise<DaltonSystemJobRunResponse> {
  const { data } = await api.post<DaltonSystemJobRunResponse>(
    `/api/v1/dalton/system-jobs/${encodeURIComponent(key)}/run`,
    {},
  );
  return data;
}

export type DaltonInviteStatus = 'pending' | 'used' | 'expired' | 'revoked';

export interface DaltonInviteItem {
  invite_id: string;
  status: DaltonInviteStatus;
  status_label?: string;
  created_at: string | null;
  expires_at: string | null;
  used_at?: string | null;
  revoked_at?: string | null;
  used_by_username?: string | null;
  username?: string | null;
}

export interface DaltonInviteCreateResponse extends DaltonInviteItem {
  token: string;
  url: string;
  invite_url?: string;
}

export interface DaltonInviteListResponse {
  invites: DaltonInviteItem[];
  active_count?: number;
  max_active?: number;
}

export interface DaltonInviteCheckResponse {
  valid: boolean;
  reason: string | null;
}

export interface DaltonInviteRedeemResponse {
  ok: boolean;
  username: string;
  email: string;
}

export async function fetchDaltonInvites(): Promise<DaltonInviteListResponse> {
  const { data } = await api.get<DaltonInviteListResponse>('/api/v1/dalton/invites');
  return data;
}

export async function createDaltonInvite(): Promise<DaltonInviteCreateResponse> {
  const { data } = await api.post<DaltonInviteCreateResponse>('/api/v1/dalton/invites', {});
  return data;
}

export async function revokeDaltonInvite(inviteId: string): Promise<DaltonInviteItem> {
  const { data } = await api.delete<DaltonInviteItem>(
    `/api/v1/dalton/invites/${encodeURIComponent(inviteId)}`,
  );
  return data;
}

export async function checkDaltonInviteToken(token: string): Promise<DaltonInviteCheckResponse> {
  const { data } = await api.post<DaltonInviteCheckResponse>(
    '/api/v1/dalton/invites/check',
    { token },
  );
  return data;
}

export async function redeemDaltonInviteToken(payload: {
  token: string;
  username: string;
  password: string;
}): Promise<DaltonInviteRedeemResponse> {
  const { data } = await api.post<DaltonInviteRedeemResponse>(
    '/api/v1/dalton/invites/redeem',
    payload,
  );
  return data;
}


export interface DaltonFeishuReadScope {
  space_id: string;
  name: string;
  added_by: string;
  added_at: string | null;
}

export interface DaltonFeishuReadScopesResponse {
  scopes: DaltonFeishuReadScope[];
}

export interface DaltonFeishuSpace {
  space_id: string;
  name: string;
  description?: string;
}

export async function fetchDaltonFeishuReadScopes(): Promise<DaltonFeishuReadScopesResponse> {
  const { data } = await api.get<DaltonFeishuReadScopesResponse>(
    '/api/v1/dalton/feishu/read-scopes',
  );
  return data;
}

export async function updateDaltonFeishuReadScopes(
  scopes: Array<{ space_id: string; name?: string }>,
): Promise<DaltonFeishuReadScopesResponse> {
  const { data } = await api.put<DaltonFeishuReadScopesResponse>(
    '/api/v1/dalton/feishu/read-scopes',
    { scopes },
  );
  return data;
}

export async function fetchDaltonFeishuSpaces(): Promise<{ spaces: DaltonFeishuSpace[] }> {
  const { data } = await api.get<{ spaces: DaltonFeishuSpace[] }>(
    '/api/v1/dalton/feishu/spaces',
  );
  return data;
}
