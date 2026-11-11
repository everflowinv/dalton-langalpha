// DALTON-HOOK(company-automations): scoped agent and script read types.
import { api, type ApiResponse, type QueryParams } from '@/api/client';
import type {
  Automation,
  AutomationExecution,
  AutomationHistory,
  AutomationPayload,
  AutomationRead,
  AutomationReadList,
  AutomationReadListQuery,
  AutomationReadPageQuery,
  AutomationRun,
  AutomationRunReadList,
  AutomationRunReadQuery,
  AutomationScope,
  AutomationUpdatePayload,
  CompanyScriptUpdatePayload,
  DeliveryOptions,
  ScriptAutomationRead,
} from '@/types/automation';

export interface AutomationList {
  automations: Automation[];
  total: number;
}

export interface ExecutionList {
  executions: AutomationExecution[];
  has_more: boolean;
}

export interface RunFeedPage {
  executions: AutomationRun[];
  has_more: boolean;
}

export const listAutomations = (params: QueryParams): Promise<ApiResponse<AutomationList>> =>
  api.get('/api/v1/automations', { params });

export const createAutomation = (data: AutomationPayload): Promise<ApiResponse<Automation>> =>
  api.post('/api/v1/automations', data);

export const updateAutomation = (
  id: string,
  data: AutomationUpdatePayload & { expected_config_version?: number },
): Promise<ApiResponse<Automation>> => api.patch(automationItemPath(id), data);

// DALTON-HOOK(company-automations): script schedule patch. The body is only cron, timezone, and version.
export function updateCompanyScript(
  id: string,
  data: CompanyScriptUpdatePayload,
): Promise<ApiResponse<ScriptAutomationRead>> {
  const body: CompanyScriptUpdatePayload = { expected_config_version: data.expected_config_version };
  if (typeof data.cron_expression === 'string') body.cron_expression = data.cron_expression;
  if (typeof data.timezone === 'string') body.timezone = data.timezone;
  return api.patch(automationItemPath(id), body);
}

export const deleteAutomation = (id: string): Promise<ApiResponse> => api.delete(automationItemPath(id));

/** Personal and research callers omit the version and send no body. Company sends the version. */
export const pauseAutomation = (id: string, expectedConfigVersion?: number): Promise<ApiResponse> =>
  postVersion(automationItemPath(id, '/pause'), expectedConfigVersion);

export const resumeAutomation = (id: string, expectedConfigVersion?: number): Promise<ApiResponse> =>
  postVersion(automationItemPath(id, '/resume'), expectedConfigVersion);

export const triggerAutomation = (id: string): Promise<ApiResponse> => api.post(automationItemPath(id, '/trigger'));

function postVersion(url: string, expectedConfigVersion?: number): Promise<ApiResponse> {
  if (expectedConfigVersion == null) return api.post(url);
  return api.post(url, { expected_config_version: expectedConfigVersion });
}

export const listExecutions = (id: string, params: QueryParams): Promise<ApiResponse<ExecutionList>> =>
  api.get(`/api/v1/automations/${id}/executions`, { params });

/** Every automation's runs, newest first: the feed. `thread_id` and
 *  `status` narrow it, e.g. to what waits on one thread. */
export const listRecentRuns = (params: QueryParams): Promise<ApiResponse<RunFeedPage>> =>
  api.get('/api/v1/automations/executions', { params });

/** Skip a run waiting for the turn in its thread to end. */
export const skipRun = (automationId: string, executionId: string): Promise<ApiResponse> =>
  api.post(`/api/v1/automations/${automationId}/executions/${executionId}/skip`);

/** Acknowledge a failed run, which takes its automation out of Needs
 *  attention until a newer run fails. */
export const dismissRun = (automationId: string, executionId: string): Promise<ApiResponse<Automation>> =>
  api.post(`/api/v1/automations/${automationId}/executions/${executionId}/dismiss`);

/** The chats each linked app offers an automation, and where its bare entry
 *  lands now. An automation in no workspace asks with none. */
export const getDeliveryOptions = (workspaceId: string | null): Promise<ApiResponse<DeliveryOptions>> =>
  api.get('/api/v1/automations/delivery-options', { params: workspaceId ? { workspace_id: workspaceId } : undefined });

export interface DeliveryDefaultPayload {
  workspace_id: string;
  platform: string;
  /** null clears the workspace's default for the app. */
  address: string | null;
}

/** Set or clear the chat an app's bare entry lands in for one workspace. */
export const setDeliveryDefault = (
  data: DeliveryDefaultPayload,
): Promise<ApiResponse<{ address: string | null; name: string | null }>> =>
  api.put('/api/v1/automations/delivery-default', data);

// DALTON-HOOK(company-automations): scoped agent and script reads. listAutomations,
// listExecutions, and listRecentRuns stay the agent reads the current pages use.

const READ_LIST_LIMIT = 50;
const READ_LIST_OFFSET = 0;
const RUN_READ_LIMIT = 20;
const RUN_READ_OFFSET = 0;
const HISTORY_LIMIT = 50;
const HISTORY_OFFSET = 0;

/** Filters `GET /api/v1/automations` actually accepts. */
export interface AutomationReadListParams {
  scope?: AutomationScope;
  status?: string;
  limit?: number;
  offset?: number;
}

/** Filters `GET /api/v1/automations/executions` actually accepts. */
export interface AutomationRunReadParams {
  scope?: AutomationScope;
  status?: string;
  thread_id?: string;
  limit?: number;
  offset?: number;
}

/** `limit` and `offset` for one task's runs or its modification history. */
export interface AutomationReadPageParams {
  limit?: number;
  offset?: number;
}

/** List key. An omitted scope or status is null here and left off the request. */
export function automationReadListQuery(params: AutomationReadListParams = {}): AutomationReadListQuery {
  return {
    scope: params.scope ?? null,
    status: params.status ?? null,
    limit: params.limit ?? READ_LIST_LIMIT,
    offset: params.offset ?? READ_LIST_OFFSET,
  };
}

/** Feed key. Defaults match `listAutomationRunReads`. */
export function automationRunReadQuery(params: AutomationRunReadParams = {}): AutomationRunReadQuery {
  return {
    scope: params.scope ?? null,
    status: params.status ?? null,
    thread_id: params.thread_id ?? null,
    limit: params.limit ?? RUN_READ_LIMIT,
    offset: params.offset ?? RUN_READ_OFFSET,
  };
}

/** One task's run page. Defaults match `listAutomationExecutionReads`. */
export function automationExecutionReadQuery(params: AutomationReadPageParams = {}): AutomationReadPageQuery {
  return {
    limit: params.limit ?? RUN_READ_LIMIT,
    offset: params.offset ?? RUN_READ_OFFSET,
  };
}

/** Modification-history page. Defaults match `listAutomationHistory`. */
export function automationHistoryQuery(params: AutomationReadPageParams = {}): AutomationReadPageQuery {
  return {
    limit: params.limit ?? HISTORY_LIMIT,
    offset: params.offset ?? HISTORY_OFFSET,
  };
}

/** Null and undefined stay off the query string. An empty status is sent as given. */
function readParams(
  page: AutomationReadPageQuery,
  filters: { scope?: AutomationScope | null; status?: string | null; thread_id?: string | null } = {},
): QueryParams {
  const params: QueryParams = { limit: page.limit, offset: page.offset };
  if (filters.scope != null) params.scope = filters.scope;
  if (filters.status != null) params.status = filters.status;
  if (filters.thread_id != null) params.thread_id = filters.thread_id;
  return params;
}

/** A UUID or a full `data:<job_key>`. The path is encoded, not checked. */
function automationItemPath(id: string, suffix = ''): string {
  if (!id) throw new Error('Missing automation id');
  return `/api/v1/automations/${encodeURIComponent(id)}${suffix}`;
}

/** Scoped task list. Omitted scope keeps the caller's own agent rows. */
export function listAutomationReads(
  params: AutomationReadListParams = {},
): Promise<ApiResponse<AutomationReadList>> {
  const query = automationReadListQuery(params);
  return api.get('/api/v1/automations', { params: readParams(query, query) });
}

/** Scoped run feed. Script statuses such as `succeeded` are sent unchanged. */
export function listAutomationRunReads(
  params: AutomationRunReadParams = {},
): Promise<ApiResponse<AutomationRunReadList>> {
  const query = automationRunReadQuery(params);
  return api.get('/api/v1/automations/executions', { params: readParams(query, query) });
}

/** One agent UUID or one full `data:<job_key>`. The route takes no scope. */
export function getAutomationRead(id: string): Promise<ApiResponse<AutomationRead>> {
  return api.get(automationItemPath(id));
}

/** One task's run page. The route takes no scope, status, or thread. */
export function listAutomationExecutionReads(
  id: string,
  params: AutomationReadPageParams = {},
): Promise<ApiResponse<AutomationRunReadList>> {
  const query = automationExecutionReadQuery(params);
  return api.get(automationItemPath(id, '/executions'), { params: readParams(query) });
}

/** One task's modification history. The route takes no scope. */
export function listAutomationHistory(
  id: string,
  params: AutomationReadPageParams = {},
): Promise<ApiResponse<AutomationHistory>> {
  const query = automationHistoryQuery(params);
  return api.get(automationItemPath(id, '/history'), { params: readParams(query) });
}
