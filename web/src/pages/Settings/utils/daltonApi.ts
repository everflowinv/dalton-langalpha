import { api } from '@/api/client';

export interface DaltonRoleWarning {
  code: string;
  role: string;
  model: string;
  message: string;
  link: string;
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
  role_warnings?: DaltonRoleWarning[];
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

export type DaltonSystemJobScope = 'company' | 'sector' | 'home' | 'data' | 'host' | 'ops';
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
export type DaltonInviteTier = 'admin' | 'internal' | 'external';

export interface DaltonInviteItem {
  invite_id: string;
  status: DaltonInviteStatus;
  status_label?: string;
  tier?: DaltonInviteTier;
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
  tier?: DaltonInviteTier;
}

export async function fetchDaltonInvites(): Promise<DaltonInviteListResponse> {
  const { data } = await api.get<DaltonInviteListResponse>('/api/v1/dalton/invites');
  return data;
}

export async function createDaltonInvite(
  tier?: DaltonInviteTier,
): Promise<DaltonInviteCreateResponse> {
  const payload = tier ? { tier } : {};
  const { data } = await api.post<DaltonInviteCreateResponse>('/api/v1/dalton/invites', payload);
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

export interface DaltonNotifyTestResponse {
  ok: boolean;
  detail: string | null;
  error_code: string | null;
}

/** Send one test push to a target the current user already has configured. */
export async function sendDaltonNotifyTest(target: string): Promise<DaltonNotifyTestResponse> {
  const { data } = await api.post<DaltonNotifyTestResponse>(
    '/api/v1/dalton/notify/test',
    { target },
  );
  return data;
}

export interface DaltonFeishuBotResponse {
  ok: boolean;
  app_name: string | null;
}

/** The chat-sending Feishu bot's display name (null when unavailable; falls back to generic copy). */
export async function fetchDaltonFeishuBotName(): Promise<DaltonFeishuBotResponse> {
  const { data } = await api.get<DaltonFeishuBotResponse>(
    '/api/v1/dalton/notify/feishu-bot',
  );
  return data;
}

export interface DaltonFeishuGroup {
  chat_id: string;
  name: string;
}

export interface DaltonFeishuGroupsResponse {
  ok: boolean;
  groups: DaltonFeishuGroup[];
}

/** The chats the notify bot is in (internal/admin only; server caches 5 minutes). */
export async function fetchDaltonFeishuGroups(): Promise<DaltonFeishuGroupsResponse> {
  const { data } = await api.get<DaltonFeishuGroupsResponse>(
    '/api/v1/dalton/notify/feishu-groups',
  );
  return data;
}

export interface DaltonDiscordChannelVerifyResponse {
  ok: boolean;
  verified: boolean;
  reason: string | null;
  channel_id: string;
  channel_name: string | null;
  detail: string | null;
}

/** Verify that a Discord channel ID is visible to the bot and is a guild text channel. */
export async function verifyDaltonDiscordChannel(
  channelId: string,
): Promise<DaltonDiscordChannelVerifyResponse> {
  const { data } = await api.post<DaltonDiscordChannelVerifyResponse>(
    '/api/v1/dalton/notify/discord-channels/verify',
    { channel_id: channelId },
  );
  return data;
}

export interface DaltonChannelChatConfig {
  workspace_id: string | null;
  name?: string | null;
  workspace?: string | null;
  [extra: string]: unknown;
}

export interface DaltonChannelAppSettings {
  preferred: string | null;
  chats: Record<string, DaltonChannelChatConfig>;
  automation_output: Record<string, string>;
  agent_messages: {
    enabled: boolean;
    allowed: string[];
    [extra: string]: unknown;
  };
  [extra: string]: unknown;
}

export interface DaltonChannelChatBindingEntry {
  platform: string;
  chat_address: string;
  chat_name: string | null;
  workspace_id: string | null;
  bound_by_user_id: string;
  last_used_at: number | string | null;
}

export interface DaltonDiscordChannelEntryRequest {
  action: 'add' | 'rename';
  channel_id: string;
  version: string;
  name?: string | null;
}

export async function mutateDaltonDiscordChannel(
  payload: DaltonDiscordChannelEntryRequest,
): Promise<DaltonChannelSettingsUpdateResponse> {
  const { data } = await api.post<DaltonChannelSettingsUpdateResponse>(
    '/api/v1/dalton/channels/discord-channels',
    payload,
  );
  return data;
}

export interface DaltonChannelSettingsUpdateResponse {
  version: string;
  settings: Record<string, unknown>;
  changes: string[];
}

/** Platforms supported by personal channel binding. */
export type DaltonChannelPlatform = 'feishu' | 'discord' | 'weixin';

/**
 * One identity row from `GET /channels/bindings`.
 * The route loads identities with `include_secrets=False` and then drops
 * `weixin_context_token` and `encrypted_weixin_context_token`.
 * Unlinked platforms are omitted; they are not sent as null.
 */
export interface DaltonChannelIdentityPublic {
  platform: string;
  platform_user_id: string;
  platform_username: string | null;
  platform_tenant_key: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** Normalized `bindings[platform]` from `GET /channels/bindings`. */
export interface DaltonChannelBindingState {
  linked: boolean;
  platform: string;
  platform_user_id: string | null;
  platform_username: string | null;
  updated_at: string | null;
  account_id?: string | null;
  status?: string | null;
  masked_ilink_user_id?: string | null;
  has_usable_context_token?: boolean | null;
}

/** Personal Weixin account status returned inside `GET /channels/bindings` (`weixin_account`). */
export type DaltonWeixinMemberStatus =
  | 'unconfigured'
  | 'active'
  | 'pending_verification'
  | 'expired'
  | 'paused'
  | 'disabled'
  | string;

export interface DaltonWeixinMemberAccountView {
  linked: boolean;
  platform: 'weixin' | string;
  status: DaltonWeixinMemberStatus;
  raw_status?: string | null;
  disabled_reason?: string | null;
  channel_enabled: boolean;
  allow_external?: boolean;
  member_allowed: boolean;
  deny_reason: string | null;
  account_id: string | null;
  platform_user_id: string | null;
  masked_ilink_user_id: string | null;
  platform_username: string | null;
  ilink_username: string | null;
  bot_generation?: number | null;
  active_push_ready: boolean;
  last_inbound_at: string | null;
  created_at?: string | null;
  updated_at: string | null;
}

/** @deprecated Legacy single-bot payload kept optional for older test fixtures. */
export interface DaltonWeixinBotPublicState {
  configured: boolean;
  status: 'unconfigured' | 'active' | 'expired' | 'error' | string;
  ilink_bot_id: string | null;
  owner_user_id?: string | null;
  ilink_user_id: string | null;
  ilink_username?: string | null;
  bot_generation?: number;
  updated_at?: string | null;
}

export interface DaltonChannelBindingsResponse {
  user_id: string;
  user_tier: 'admin' | 'internal' | 'external' | string;
  home_workspace_id: string;
  /** Includes enabled platforms (`weixin` omitted when `channel_weixin` is off). */
  bindings: Record<DaltonChannelPlatform, DaltonChannelBindingState>;
  identities: Partial<Record<string, DaltonChannelIdentityPublic>>;
  settings: {
    version: string;
    apps: Record<string, unknown>;
  };
  chat_bindings: unknown;
  weixin_account?: DaltonWeixinMemberAccountView | null;
  weixin_bot?: DaltonWeixinBotPublicState | null;
}

/**
 * `POST /channels/bind-codes`.
 * The body is `{ platform }` for the signed-in user (Feishu/Discord only).
 */
export interface DaltonChannelBindCodeCreateResponse {
  code: string;
  platform: DaltonChannelPlatform;
  expires_at: string;
  expires_in_seconds: number;
  command: string;
}

export interface DaltonChannelUnbindResponse {
  ok?: boolean;
  unbound?: boolean;
  platform: DaltonChannelPlatform | string;
  user_id?: string;
  account_id?: string | null;
  deleted_account?: boolean;
  superseded_login_sessions?: number;
  accounts_revision?: number;
}

/** QR statuses the login API returns after normalization. */
export type DaltonWeixinQrPublicStatus =
  | 'starting'
  | 'wait'
  | 'scaned'
  | 'scaned_but_redirect'
  | 'verify_code'
  | 'confirmed'
  | 'expired'
  | 'superseded'
  | 'failed'
  | 'error';

export interface DaltonWeixinQrPublicResponse {
  session_key: string;
  status: DaltonWeixinQrPublicStatus;
  connected?: boolean;
  already_connected?: boolean;
  pending_verification?: boolean;
  verify_code_accepted?: boolean;
  need_rescan?: boolean;
  revoked?: boolean;
  qrcode_url?: string | null;
  qrcode_img_content?: string | null;
  expires_at?: string | null;
  account_id?: string | null;
  account_status?: string | null;
  ilink_bot_id?: string | null;
  ilink_user_id?: string | null;
  masked_ilink_user_id?: string | null;
  ilink_username?: string | null;
  bot_generation?: number | null;
  accounts_revision?: number | null;
  code?: string | null;
  message?: string | null;
}

/** Preset audit reasons enforced by `validate_admin_unbind_reason` in backend. */
export const DALTON_WEIXIN_ADMIN_UNBIND_REASONS = [
  '离职交接',
  '手机遗失应急',
  '误绑微信号',
  '其他原因',
] as const;

export type DaltonWeixinAdminUnbindReason =
  (typeof DALTON_WEIXIN_ADMIN_UNBIND_REASONS)[number];

export interface DaltonWeixinGatewayLeaseInfo {
  instance_id: string;
  epoch: number;
  acquired_at: string | null;
  expires_at: string | null;
  active: boolean;
}

export interface DaltonWeixinAdminMemberItem {
  account_id: string | null;
  user_id: string;
  user_email: string | null;
  user_name: string | null;
  user_tier: 'admin' | 'internal' | 'external' | string;
  ilink_bot_id: string | null;
  ilink_username: string | null;
  masked_ilink_user_id: string | null;
  status:
    | 'active'
    | 'pending_verification'
    | 'expired'
    | 'disabled'
    | 'login_pending'
    | string;
  prev_status: string | null;
  disabled_reason: 'external_access_disabled' | 'member_not_in_roster' | string | null;
  bot_generation: number | null;
  has_pending_login: boolean;
  pending_login_status: 'starting' | 'wait' | 'scaned' | 'verify_code' | string | null;
  last_inbound_at: string | null;
  last_error_code: string | null;
  updated_at: string | null;
}

export interface DaltonWeixinAdminAuditLogItem {
  audit_id: string;
  action: string;
  operator_user_id: string;
  target_user_id: string | null;
  weixin_account_id: string | null;
  ilink_bot_id: string | null;
  masked_ilink_user_id: string | null;
  reason: string;
  metadata: Record<string, unknown>;
  created_at: string | null;
}

export interface DaltonWeixinAdminOverviewResponse {
  enabled: boolean;
  allow_external: boolean;
  db_allow_external: boolean | null;
  accounts_revision: number;
  max_concurrent_accounts: number;
  active_poller_count: number;
  connected_count: number;
  pending_verification_count: number;
  expired_count: number;
  gateway_lease: DaltonWeixinGatewayLeaseInfo | null;
  accounts: DaltonWeixinAdminMemberItem[];
  audit_logs: DaltonWeixinAdminAuditLogItem[];
}

export interface DaltonWeixinAdminConfigUpdatePayload {
  enabled?: boolean;
  allow_external?: boolean;
}

export interface DaltonWeixinAdminConfigUpdateResponse {
  ok: boolean;
  enabled: boolean;
  allow_external: boolean;
  accounts_revision: number;
  disabled_count: number;
  restored_count: number;
}

export async function fetchDaltonChannelBindings(): Promise<DaltonChannelBindingsResponse> {
  const { data } = await api.get<DaltonChannelBindingsResponse>(
    '/api/v1/dalton/channels/bindings',
  );
  return data;
}

export async function updateDaltonChannelSettings(payload: {
  version: string;
  settings: Record<string, unknown>;
}): Promise<DaltonChannelSettingsUpdateResponse> {
  const { data } = await api.put<DaltonChannelSettingsUpdateResponse>(
    '/api/v1/dalton/channels/settings',
    payload,
  );
  return data;
}

export async function createDaltonChannelBindCode(
  platform: DaltonChannelPlatform,
): Promise<DaltonChannelBindCodeCreateResponse> {
  const { data } = await api.post<DaltonChannelBindCodeCreateResponse>(
    '/api/v1/dalton/channels/bind-codes',
    { platform },
  );
  return data;
}

export async function unbindDaltonChannel(
  platform: DaltonChannelPlatform,
): Promise<DaltonChannelUnbindResponse> {
  const { data } = await api.delete<DaltonChannelUnbindResponse>(
    `/api/v1/dalton/channels/bindings/${encodeURIComponent(platform)}`,
  );
  return data;
}

export interface DaltonWeixinQrRequestOptions {
  signal?: AbortSignal;
}

export async function startDaltonWeixinQrLogin(
  options?: DaltonWeixinQrRequestOptions,
): Promise<DaltonWeixinQrPublicResponse> {
  const { data } = await api.post<DaltonWeixinQrPublicResponse>(
    '/api/v1/dalton/channels/weixin/qrcode/start',
    {},
    options?.signal ? { signal: options.signal } : undefined,
  );
  return data;
}

export async function waitDaltonWeixinQrLogin(
  sessionKey: string,
  options?: DaltonWeixinQrRequestOptions,
): Promise<DaltonWeixinQrPublicResponse> {
  const { data } = await api.get<DaltonWeixinQrPublicResponse>(
    '/api/v1/dalton/channels/weixin/qrcode/wait',
    {
      params: { session_key: sessionKey },
      timeout: 50_000,
      ...(options?.signal ? { signal: options.signal } : {}),
    },
  );
  return data;
}

export async function verifyDaltonWeixinQrCode(
  sessionKey: string,
  verifyCode: string,
  options?: DaltonWeixinQrRequestOptions,
): Promise<DaltonWeixinQrPublicResponse> {
  const { data } = await api.post<DaltonWeixinQrPublicResponse>(
    '/api/v1/dalton/channels/weixin/qrcode/verify-code',
    { session_key: sessionKey, verify_code: verifyCode },
    options?.signal ? { signal: options.signal } : undefined,
  );
  return data;
}

export async function fetchDaltonWeixinAdminOverview(): Promise<DaltonWeixinAdminOverviewResponse> {
  const { data } = await api.get<DaltonWeixinAdminOverviewResponse>(
    '/api/v1/dalton/channels/weixin/admin/overview',
  );
  return data;
}

export async function updateDaltonWeixinAdminConfig(
  payload: DaltonWeixinAdminConfigUpdatePayload,
): Promise<DaltonWeixinAdminConfigUpdateResponse> {
  const { data } = await api.put<DaltonWeixinAdminConfigUpdateResponse>(
    '/api/v1/dalton/channels/weixin/admin/config',
    payload,
  );
  return data;
}

export async function adminUnbindDaltonWeixinAccount(
  accountId: string,
  reason: DaltonWeixinAdminUnbindReason,
): Promise<DaltonChannelUnbindResponse> {
  const { data } = await api.delete<DaltonChannelUnbindResponse>(
    `/api/v1/dalton/channels/weixin/admin/accounts/${encodeURIComponent(accountId)}`,
    { data: { reason } },
  );
  return data;
}

export async function adminRevokeDaltonWeixinUser(
  targetUserId: string,
  reason: DaltonWeixinAdminUnbindReason,
): Promise<DaltonChannelUnbindResponse> {
  const { data } = await api.delete<DaltonChannelUnbindResponse>(
    `/api/v1/dalton/channels/weixin/admin/users/${encodeURIComponent(targetUserId)}/weixin`,
    { data: { reason } },
  );
  return data;
}

// ---------------------------------------------------------------------------
// Team Model Catalog (d051): admin CRUD, variants, curated setup, rollback, test-call
// ---------------------------------------------------------------------------

export type TeamCatalogPriceSourceKind = 'official' | 'estimated' | 'third_party';
export type TeamCatalogBillingMode = 'cash' | 'subscription';
export type TeamCatalogModelSource = 'team_added' | 'team_overridden' | 'factory';
export type TeamCatalogLegacyImportStatus =
  | 'pending'
  | 'not_applicable'
  | 'imported'
  | 'failed'
  | 'abandoned';

export interface TeamCatalogPricingTier {
  max_tokens: number | null;
  rate: number;
  cached_input?: number | null;
  cache_read?: number | null;
}

export interface TeamCatalogPricingCard {
  input?: number | null;
  output?: number | null;
  cached_input?: number | null;
  cache_read?: number | null;
  cache_5m?: number | null;
  cache_1h?: number | null;
  unit?: string;
  input_tiers?: TeamCatalogPricingTier[] | null;
  output_tiers?: TeamCatalogPricingTier[] | null;
  input_pricing_mode?: string | null;
  output_pricing_mode?: string | null;
  source_kind?: TeamCatalogPriceSourceKind | null;
  estimated?: boolean | null;
  source?: string | null;
  source_url?: string | null;
  verified_at?: string | null;
  note?: string | null;
}

export interface TeamCatalogPricingMeta {
  source_kind?: TeamCatalogPriceSourceKind;
  estimated?: boolean | null;
  source?: string | null;
  source_url?: string | null;
  verified_at?: string | null;
  note?: string | null;
}

export interface TeamCatalogDaltonMeta {
  family?: string | null;
  billing?: TeamCatalogBillingMode | null;
  tool_calling?: boolean | null;
  forbidden_for_roles?: string[] | null;
  notional_pricing?: TeamCatalogPricingCard | null;
  notional_ref?: string | null;
  series?: string | null;
  awaiting_release?: boolean | null;
}

export interface TeamCatalogReasoningConfig {
  efforts?: string[];
  default?: string;
  write?: string | null;
  on?: Record<string, unknown>;
  off?: Record<string, unknown>;
  [extra: string]: unknown;
}

export interface TeamCatalogModelItem {
  model_key: string;
  model_id: string;
  provider: string;
  provider_sdk?: string | null;
  provider_display_name: string;
  provider_connected: boolean;
  catalog_defined_provider: boolean;
  display_name: string;
  visible: boolean;
  source: TeamCatalogModelSource;
  status: 'visible' | 'hidden';
  intelligence: number | null;
  speed: number | null;
  plain_language: number | null;
  context: number | null;
  compaction_profile: string | null;
  input_modalities: string[];
  reasoning: TeamCatalogReasoningConfig | null;
  pricing: TeamCatalogPricingCard | null;
  pricing_meta: TeamCatalogPricingMeta | null;
  dalton_meta: TeamCatalogDaltonMeta;
  /** Read-only model fields returned by GET /api/v1/dalton/team-models for edit-mode echo (credentials stripped). */
  parameters?: Record<string, unknown> | null;
  extra_body?: Record<string, unknown> | null;
  prompt_guidance?: string | null;
  alias?: string[] | null;
  task_costs_bound?: { model?: string; times?: number } | null;
}

export interface TeamCatalogRetiredItem {
  key: string;
  provider: string;
  retired_at: string | null;
  retired_by: string | null;
  scope: 'team' | 'readonly_legacy';
  restorable: boolean;
}

export interface TeamCatalogProviderVariant {
  provider_slug: string;
  display_name: string;
  sdk: string;
  parent_provider: string;
  access_type: 'api_key' | 'coding_plan';
  billing: TeamCatalogBillingMode;
  base_url: string;
  credential_configured: boolean;
  key_fingerprint: string | null;
  bound_base_url: string | null;
  updated_at: string | null;
}

export interface TeamCatalogOverviewResponse {
  revision: number;
  payload_sha256: string;
  legacy_sync_import_status: TeamCatalogLegacyImportStatus;
  legacy_sync_import_error: string | null;
  models: TeamCatalogModelItem[];
  retired: TeamCatalogRetiredItem[];
  provider_variants: TeamCatalogProviderVariant[];
  parent_provider_groups: string[];
  supported_variant_sdks: string[];
  curated_setup: string[] | null;
  dalton_setup_candidates: string[];
  dalton_curated_providers: string[];
}

export interface TeamCatalogRevisionRecord {
  revision: number;
  action: string;
  target_keys: string[];
  payload_sha256: string;
  diff_summary: Record<string, unknown>;
  side_effects: Array<Record<string, unknown>>;
  comment: string | null;
  restored_from: number | null;
  actor_user_id: string;
  actor_label: string | null;
  created_at: string;
}

export interface TeamCatalogRevisionsResponse {
  revision: number;
  revisions: TeamCatalogRevisionRecord[];
}

export interface TeamCatalogRevisionDetailResponse {
  current_revision: number;
  revision_record: TeamCatalogRevisionRecord;
}

export interface TeamCatalogInlineVariantUpdatePayload {
  provider_slug: string;
  display_name: string;
  sdk: string;
  parent_provider: string;
  access_type: 'api_key' | 'coding_plan';
  billing: TeamCatalogBillingMode;
  base_url: string;
  api_key?: string;
}

export interface TeamCatalogModelUpsertPayload {
  model_key?: string;
  model_id?: string;
  provider?: string;
  display_name?: string;
  visible?: boolean;
  prompt_guidance?: string | null;
  intelligence?: number | null;
  speed?: number | null;
  plain_language?: number | null;
  context?: number | null;
  compaction_profile?: string | null;
  input_modalities?: string[];
  parameters?: Record<string, unknown> | null;
  extra_body?: Record<string, unknown> | null;
  reasoning?: TeamCatalogReasoningConfig | null;
  pricing?: TeamCatalogPricingCard | null;
  pricing_meta?: TeamCatalogPricingMeta | null;
  alias?: string[] | null;
  dalton_meta?: TeamCatalogDaltonMeta | null;
  task_costs_bound?: { model?: string | null; times?: number | null } | null;
  provider_variant_update?: TeamCatalogInlineVariantUpdatePayload | null;
  curated_setup_member?: boolean | null;
  expected_revision?: number;
  comment?: string | null;
}

export interface TeamCatalogProviderVariantUpsertPayload {
  provider_slug: string;
  display_name: string;
  sdk: string;
  parent_provider: string;
  access_type: 'api_key' | 'coding_plan';
  billing: TeamCatalogBillingMode;
  base_url: string;
  api_key?: string;
  expected_revision?: number;
  comment?: string | null;
}

export interface TeamCatalogRetirePayload {
  successor_model?: string | null;
  migrate_user_defaults?: boolean;
  clear_subagent_model?: boolean;
  expected_revision?: number;
  comment?: string | null;
}

export interface TeamCatalogRollbackPayload {
  target_revision: number;
  expected_revision?: number;
  variant_credentials?: Record<string, { base_url: string; api_key: string }>;
  successor_for_dropped?: Record<string, string>;
  comment?: string | null;
}

export interface TeamCatalogRollbackResponse {
  revision: number;
  restored_from: number;
  restored_tasks: Array<Record<string, unknown>>;
  skipped_user_modified_tasks: Array<Record<string, unknown>>;
  revision_record: TeamCatalogRevisionRecord;
}

export interface TeamCatalogTestCallPayload {
  model_key?: string | null;
  provider: string;
  model_id: string;
  base_url?: string | null;
  api_key?: string | null;
  sdk?: string | null;
  parameters?: Record<string, unknown> | null;
  extra_body?: Record<string, unknown> | null;
}

export interface TeamCatalogTestCallResponse {
  ok: boolean;
  tool_calling_verified?: boolean;
  provider?: string;
  model_id?: string;
  error?: string;
  message?: string;
}

export interface TeamCatalogLegacyResolvePayload {
  action: 'retry_import' | 'abandon_legacy_file';
  persist_known_retired?: boolean;
  expected_revision?: number;
  comment?: string | null;
}

export interface TeamCatalogMutationResult {
  revision: number;
  model_key?: string;
  provider_slug?: string;
  side_effects?: Array<Record<string, unknown>>;
  legacy_sync_import_status?: TeamCatalogLegacyImportStatus;
  revision_record?: TeamCatalogRevisionRecord;
}

export async function fetchTeamModelCatalog(): Promise<TeamCatalogOverviewResponse> {
  const { data } = await api.get<TeamCatalogOverviewResponse>('/api/v1/dalton/team-models');
  return data;
}

export async function createTeamCatalogModel(
  payload: TeamCatalogModelUpsertPayload,
): Promise<TeamCatalogMutationResult> {
  const { data } = await api.post<TeamCatalogMutationResult>('/api/v1/dalton/team-models', payload);
  return data;
}

export async function updateTeamCatalogModel(
  modelKey: string,
  payload: TeamCatalogModelUpsertPayload,
): Promise<TeamCatalogMutationResult> {
  const { data } = await api.put<TeamCatalogMutationResult>(
    `/api/v1/dalton/team-models/${encodeURIComponent(modelKey)}`,
    payload,
  );
  return data;
}

export async function setTeamCatalogModelVisibility(
  modelKey: string,
  payload: { visible: boolean; expected_revision?: number; comment?: string | null },
): Promise<TeamCatalogMutationResult> {
  const { data } = await api.post<TeamCatalogMutationResult>(
    `/api/v1/dalton/team-models/${encodeURIComponent(modelKey)}/visibility`,
    payload,
  );
  return data;
}

export async function retireTeamCatalogModel(
  modelKey: string,
  payload: TeamCatalogRetirePayload,
): Promise<TeamCatalogMutationResult> {
  const { data } = await api.post<TeamCatalogMutationResult>(
    `/api/v1/dalton/team-models/${encodeURIComponent(modelKey)}/retire`,
    payload,
  );
  return data;
}

export async function restoreTeamCatalogModel(
  modelKey: string,
  payload: { expected_revision?: number; comment?: string | null },
): Promise<TeamCatalogMutationResult> {
  const { data } = await api.post<TeamCatalogMutationResult>(
    `/api/v1/dalton/team-models/${encodeURIComponent(modelKey)}/restore`,
    payload,
  );
  return data;
}

export async function restoreTeamCatalogModelDefault(
  modelKey: string,
  payload: { expected_revision?: number; comment?: string | null },
): Promise<TeamCatalogMutationResult> {
  const { data } = await api.post<TeamCatalogMutationResult>(
    `/api/v1/dalton/team-models/${encodeURIComponent(modelKey)}/restore-default`,
    payload,
  );
  return data;
}

export async function createTeamCatalogProviderVariant(
  payload: TeamCatalogProviderVariantUpsertPayload,
): Promise<TeamCatalogMutationResult> {
  const { data } = await api.post<TeamCatalogMutationResult>(
    '/api/v1/dalton/team-models/provider-variants',
    payload,
  );
  return data;
}

export async function updateTeamCatalogProviderVariant(
  providerSlug: string,
  payload: TeamCatalogProviderVariantUpsertPayload,
): Promise<TeamCatalogMutationResult> {
  const { data } = await api.put<TeamCatalogMutationResult>(
    `/api/v1/dalton/team-models/provider-variants/${encodeURIComponent(providerSlug)}`,
    payload,
  );
  return data;
}

export async function updateTeamCatalogCuratedSetup(payload: {
  curated_setup: string[] | null;
  expected_revision?: number;
  comment?: string | null;
}): Promise<TeamCatalogMutationResult> {
  const { data } = await api.put<TeamCatalogMutationResult>(
    '/api/v1/dalton/team-models/curated',
    payload,
  );
  return data;
}

export async function fetchTeamCatalogRevisions(
  limit = 50,
  offset = 0,
): Promise<TeamCatalogRevisionsResponse> {
  const { data } = await api.get<TeamCatalogRevisionsResponse>(
    '/api/v1/dalton/team-models/revisions',
    { params: { limit, offset } },
  );
  return data;
}

export async function fetchTeamCatalogRevisionDetail(
  revision: number,
): Promise<TeamCatalogRevisionDetailResponse> {
  const { data } = await api.get<TeamCatalogRevisionDetailResponse>(
    `/api/v1/dalton/team-models/revisions/${revision}`,
  );
  return data;
}

export async function rollbackTeamCatalog(
  payload: TeamCatalogRollbackPayload,
): Promise<TeamCatalogRollbackResponse> {
  const { data } = await api.post<TeamCatalogRollbackResponse>(
    '/api/v1/dalton/team-models/rollback',
    payload,
  );
  return data;
}

export async function testCallTeamCatalogModel(
  payload: TeamCatalogTestCallPayload,
): Promise<TeamCatalogTestCallResponse> {
  const { data } = await api.post<TeamCatalogTestCallResponse>(
    '/api/v1/dalton/team-models/test-call',
    payload,
  );
  return data;
}

export async function resolveTeamCatalogLegacyImport(
  payload: TeamCatalogLegacyResolvePayload,
): Promise<TeamCatalogMutationResult> {
  const { data } = await api.post<TeamCatalogMutationResult>(
    '/api/v1/dalton/team-models/legacy-import/resolve',
    payload,
  );
  return data;
}
