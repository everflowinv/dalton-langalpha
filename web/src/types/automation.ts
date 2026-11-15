/** Automation types: the wire shapes of `/api/v1/automations*` (src/server/models/automation.py). */

export interface DeliveryConfig {
  methods: string[];
}

export interface PriceCondition {
  type: 'price_above' | 'price_below' | 'pct_change_above' | 'pct_change_below';
  value: number;
  reference?: 'previous_close' | 'day_open';
}

export interface PriceTriggerConfig {
  symbol: string;
  market?: 'stock' | 'index';
  conditions: PriceCondition[];
  retrigger: {
    mode: 'one_shot' | 'recurring';
    cooldown_seconds?: number;
  };
}

/** `executing` is a price automation mid-run; cron and once automations stay
 *  `active` while they run, and only their execution row says `running`. */
export type AutomationStatus = 'active' | 'paused' | 'completed' | 'disabled' | 'executing';

/** `waiting` is a firing held until the turn running in its thread ends;
 *  `skipped` is one that never ran, and is not a failure. */
export type ExecutionStatus = 'pending' | 'waiting' | 'running' | 'completed' | 'failed' | 'timeout' | 'skipped';

/** Why a run was skipped: the reader chose to, its thread stayed busy (or an
 *  earlier firing was already waiting), or the server stopped mid-wait. */
export type SkipReason = 'user' | 'thread_busy' | 'interrupted' | 'precheck'; // DALTON-HOOK(precheck-label)

/** A failed run's cause, where the server knows it: a usage limit refused
 *  the firing or paused its run (`error_message` is then the quota service's
 *  own words), a Claude plan's or another subscription's usage limit ended
 *  it, the model provider rejected the user's own key, the run failed
 *  on the server's side, or the server cut it off. Only the first two are the
 *  reader's to act on. */
export type FailureReason = 'usage_limit' | 'claude_plan' | 'plan_limit' | 'provider_auth' | 'server_error' | 'interrupted';

/** Why the server switched an automation off. Cleared by a resume. */
export type DisableReason = 'provider_auth' | 'max_failures';

export type TriggerType = 'cron' | 'once' | 'price';

/** How a run's result reached a chat: the agent sent it, its final answer
 *  was posted for it, or a notice said the run didn't finish. */
export type DeliveryVia = 'agent' | 'fallback' | 'notice';

/** One delivery entry's outcome for a run. A run delivered through the
 *  messaging service names the chat it landed in, how, and why it failed;
 *  an older run carries only `method` and `success`. */
export interface DeliveryAttempt {
  /** The entry as the automation stores it. */
  method: string;
  success: boolean;
  address?: string | null;
  name?: string | null;
  via?: DeliveryVia | null;
  error?: string | null;
}

/** A chat an app offers as a delivery target. `kind` is e.g. `dm` or `channel`. */
export interface DeliveryChat {
  address: string;
  name: string;
  kind: string;
}

/** Where an app's bare entry lands now: this workspace's default output,
 *  the app's preferred chat, or the DM. */
export interface DeliveryDefault {
  address: string;
  name: string;
  via: 'workspace' | 'preferred' | 'dm';
}

export interface DeliveryApp {
  chats: DeliveryChat[];
  default: DeliveryDefault | null;
  error: string | null;
}

/** `GET /api/v1/automations/delivery-options`. `enabled` is false where no
 *  messaging service is connected, and then `apps` says nothing. */
export interface DeliveryOptions {
  enabled: boolean;
  apps: Record<string, DeliveryApp>;
  /** The workspace whose default chat an app-only entry follows. Null when
   *  the run has none: no workspace, or Home. */
  workspace_id: string | null;
}

/** One run as the server sends it: a list row's newest execution, an
 *  automation's history and the cross-automation feed all read the same
 *  columns. */
export interface AutomationExecution {
  automation_execution_id: string;
  automation_id: string;
  status: ExecutionStatus;
  conversation_thread_id: string | null;
  scheduled_at: string;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
  skip_reason: SkipReason | null;
  failure_reason: FailureReason | null;
  delivery_result: DeliveryAttempt[] | null;
  created_at: string;
  /** The start of the run's final answer, as plain text, when it gave one. */
  excerpt: string | null;
  /** When the reader dismissed this run's failure. Only a failed or timed
   *  out run can carry one. */
  dismissed_at: string | null;
}

export interface Automation {
  automation_id: string;
  user_id: string;
  name: string;
  description: string | null;

  trigger_type: TriggerType;
  cron_expression: string | null;
  timezone: string;
  trigger_config: PriceTriggerConfig | null;

  next_run_at: string | null;
  last_run_at: string | null;

  instruction: string;
  /** Where it runs: Home's id for the Chief of Staff, else that workspace's. */
  workspace_id: string | null;
  llm_model: string | null;

  thread_strategy: 'new' | 'continue';
  conversation_thread_id: string | null;

  status: AutomationStatus;
  max_failures: number;
  failure_count: number;
  disable_reason?: DisableReason | null;

  delivery_config: DeliveryConfig | null;
  metadata?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  /** Set while the task is in the recycle bin. Absent on a live row. */
  deleted_at?: string | null;
  /** Server config version; required by soft delete, restore and rollback. */
  config_version?: number;
  deleted_by?: string | null;

  last_execution: AutomationExecution | null;
  // DALTON-HOOK(dalton-notify): pushable category, effective value, source, and custom route targets returned by backend
  dalton_notify_category?: string | null;
  dalton_notify_effective?: boolean | null;
  dalton_notify_source?: string | null;
  dalton_notify_route_targets?: string[] | null;
}

/** The settings the form writes: every field on a create, and the whole set
 *  again on an edit, which the server applies as a patch. */
export interface AutomationPayload {
  name: string;
  description?: string;
  trigger_type: TriggerType;
  cron_expression?: string;
  timezone: string;
  trigger_config?: PriceTriggerConfig;
  next_run_at?: string;
  instruction: string;
  /** Home's id for the Chief of Staff, else the workspace's. */
  workspace_id: string;
  // DALTON-HOOK(automation-model): optional per-automation model override (null clears override)
  llm_model?: string | null;
  thread_strategy: Automation['thread_strategy'];
  max_failures: number;
  delivery_config: DeliveryConfig;
}

/** What an edit sends, applied by the server as a patch: only the fields
 *  the edit changed need to be there. An automation keeps its kind of
 *  trigger for life, so there is no `trigger_type`. */
export type AutomationUpdatePayload = Partial<Omit<AutomationPayload, 'trigger_type'>>;

/** Schedule change for one company script. Unknown fields are refused.
 *  `expected_config_version` is the detail's current `config_version`. */
export interface CompanyScriptUpdatePayload {
  cron_expression?: string;
  timezone?: string;
  expected_config_version: number;
}

/** One entry of the cross-automation run feed: a run and the identity of
 *  the automation it belongs to. */
export interface AutomationRun extends AutomationExecution {
  automation_name: string;
  trigger_type: TriggerType;
  workspace_id: string | null;
}

/** A list or feed filter. Omitted on the request, the list stays the caller's
 *  own personal and research tasks and does not ask for company tasks. */
export type AutomationScope = 'personal' | 'research_tracking' | 'company';

/** Normalized list key. `null` is the omitted filter, not a fourth scope and
 *  not an empty string. */
export interface AutomationReadListQuery {
  scope: AutomationScope | null;
  status: string | null;
  limit: number;
  offset: number;
  /** Present only on a recycle-bin key. Live list keys omit it. */
  includeDeleted?: true;
}

/** Normalized feed key. `thread_id` null means the request left it off. */
export interface AutomationRunReadQuery {
  scope: AutomationScope | null;
  status: string | null;
  thread_id: string | null;
  limit: number;
  offset: number;
}

/** Normalized page key for one task's runs or its modification history. */
export interface AutomationReadPageQuery {
  limit: number;
  offset: number;
}

// DALTON-HOOK(company-automations): agent and script read models. Write payloads stay agent-only.

/** An agent task as the read API returns it. `last_execution` is the execution
 *  row from the list: it has no `engine_kind`. */
export interface AgentAutomationRead extends Automation {
  engine_kind: 'agent';
  scope: AutomationScope;
  notify_storage_mode: string;
  config_version: number;
  active_version: number;
  created_by: string | null;
  updated_by: string | null;
  additional_context: Record<string, unknown>[] | null;
}

/** One script run nested on a script task. Status stays the stored word
 *  (`succeeded`, `timed_out`, …). There is no conversation to open. */
export interface ScriptExecutionRead {
  automation_execution_id: string;
  automation_id: string;
  status: string;
  scheduled_at: string;
  created_at: string;
  engine_kind: 'script';
  conversation_thread_id: null;
  started_at: string | null;
  completed_at: string | null;
  exit_code: number | null;
  failure_reason: string | null;
  notify_status: string | null;
  triggered_by_user_id: string | null;
  log_tail: string | null;
  trigger_source: string | null;
}

/** A company manifest script. Agent fields (instruction, trigger, user,
 *  workspace, model) are absent. `description` is always null. */
export interface ScriptAutomationRead {
  automation_id: string;
  name: string;
  status: string;
  enabled: boolean;
  paused_by_user: boolean;
  cron_expression: string;
  timezone: string;
  config_version: number;
  active_version: number;
  updated_at: string;
  notify_storage_mode: string;
  engine_kind: 'script';
  scope: 'company';
  description: null;
  next_run_at: string | null;
  last_run_at: string | null;
  updated_by: string | null;
  /** Set while the script is in the recycle bin. Absent on a live row. */
  deleted_at?: string | null;
  deleted_by?: string | null;
  dalton_notify_category: string | null;
  dalton_notify_effective: boolean | null;
  dalton_notify_source: string | null;
  dalton_notify_route_targets: string[] | null;
  last_execution: ScriptExecutionRead | null;
}

/** One task from the scoped list or the detail route. */
export type AutomationRead = AgentAutomationRead | ScriptAutomationRead;

/** An agent run in the feed or in one task's run page. */
export interface AgentAutomationRunRead extends AutomationRun {
  engine_kind: 'agent';
}

/** A script run in the feed or in one task's run page. `trigger_type` is
 *  absent. `workspace_id` and `excerpt` are null. */
export interface ScriptAutomationRunRead extends ScriptExecutionRead {
  automation_name: string;
  workspace_id: null;
  excerpt: null;
}

/** One row of the shared run page. */
export type AutomationRunRead = AgentAutomationRunRead | ScriptAutomationRunRead;

/** `{automations, total}` from `GET /api/v1/automations`. */
export interface AutomationReadList {
  automations: AutomationRead[];
  /** Server total for the filter, not `automations.length`. */
  total: number;
}

/** `{executions, has_more}` from the feed and from one task's run page. */
export interface AutomationRunReadList {
  executions: AutomationRunRead[];
  /** Server flag. A short page can still have more, and a full page can be the last. */
  has_more: boolean;
}

/** One field's before and after on a modification-history entry. */
export interface AutomationFieldChange {
  old: unknown;
  new: unknown;
}

/** One stored version. Nullable columns stay nullable. */
export interface AutomationHistoryEntry {
  version: number;
  prev_version: number | null;
  restored_from_version: number | null;
  actor_user_id: string | null;
  changed_at: string | null;
  action: string;
  changes: Record<string, AutomationFieldChange>;
  snapshot: Record<string, unknown>;
}

/** `GET /api/v1/automations/{id}/history`. */
export interface AutomationHistory {
  automation_id: string;
  scope: string;
  engine_kind: 'agent' | 'script';
  config_version: number;
  active_version: number;
  entries: AutomationHistoryEntry[];
  total: number;
  limit: number;
  offset: number;
}
