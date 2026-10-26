/**
 * Workspace management endpoints.
 */
import { api } from '@/api/client';
import type { Workspace, WorkspaceQuota, WorkspacesResponse } from '@/types/api';
import { streamStatusEvents } from './statusStream';

// The shared API client sets no global timeout. Workspace-management ops
// legitimately run tens of seconds (a spec change rebuilds the sandbox,
// duplicate provisions one), so these bounds are generous — they convert a
// network hang into a visible failure rather than race the server.
const WORKSPACE_MUTATION_TIMEOUT_MS = 120000;

const WORKSPACE_QUERY_TIMEOUT_MS = 15000;

export async function getWorkspaces(limit: number = 20, offset: number = 0, sortBy: string = 'custom', includeHome: boolean = false): Promise<WorkspacesResponse> {
  const { data } = await api.get<WorkspacesResponse>('/api/v1/workspaces', {
    params: { limit, offset, sort_by: sortBy, ...(includeHome ? { include_home: true } : {}) },
  });
  return data;
}

export async function createWorkspace(name: string, description: string = '', config: Record<string, unknown> = {}): Promise<Workspace> {
  const { data } = await api.post<Workspace>('/api/v1/workspaces', { name, description, config });
  return data;
}

export async function deleteWorkspace(workspaceId: string) {
  if (!workspaceId) throw new Error('Workspace ID is required');
  const id = String(workspaceId).trim();
  if (!id) throw new Error('Workspace ID cannot be empty');
  await api.delete(`/api/v1/workspaces/${id}`);
}

export async function getWorkspace(workspaceId: string): Promise<Workspace> {
  if (!workspaceId) throw new Error('Workspace ID is required');
  const { data } = await api.get<Workspace>(`/api/v1/workspaces/${workspaceId}`);
  return data;
}

/**
 * Get or create the user's Home, the workspace All workspaces runs in.
 * Idempotent, so safe to call on every app load.
 */
export async function getHomeWorkspace(): Promise<Workspace> {
  const { data } = await api.post<Workspace>('/api/v1/workspaces/home');
  return data;
}

export async function updateWorkspace(workspaceId: string, updates: Record<string, unknown>) {
  if (!workspaceId) throw new Error('Workspace ID is required');
  const { data } = await api.put(`/api/v1/workspaces/${workspaceId}`, updates);
  return data;
}

export async function reorderWorkspaces(items: Array<{ workspace_id: string; sort_order: number }>) {
  if (!items?.length) throw new Error('Reorder items are required');
  await api.post('/api/v1/workspaces/reorder', { items });
}

/**
 * Rename a workspace. Thin wrapper over the existing update endpoint
 * (PUT /api/v1/workspaces/{id} with { name }).
 */
export async function renameWorkspace(workspaceId: string, name: string) {
  if (!workspaceId) throw new Error('Workspace ID is required');
  const { data } = await api.put(`/api/v1/workspaces/${workspaceId}`, { name }, {
    timeout: WORKSPACE_MUTATION_TIMEOUT_MS,
  });
  return data;
}

/**
 * Duplicate a workspace (copies persisted files; always-on is reset to off on
 * the copy so it re-checks entitlement). Returns the new workspace record.
 */
export async function duplicateWorkspace(workspaceId: string) {
  if (!workspaceId) throw new Error('Workspace ID is required');
  const { data } = await api.post(`/api/v1/workspaces/${workspaceId}/duplicate`, null, {
    timeout: WORKSPACE_MUTATION_TIMEOUT_MS,
  });
  return data;
}

/**
 * Fetch per-tier workspace count quotas. Platform mode only — every field is null
 * in OSS mode, so callers should treat null as "no limit to show".
 */
export async function getWorkspaceQuota(): Promise<WorkspaceQuota> {
  const { data } = await api.get('/api/v1/workspaces/quota', {
    timeout: WORKSPACE_QUERY_TIMEOUT_MS,
  });
  return data;
}

export interface WorkspaceActionResponse {
  workspace_id: string;
  status: string;
  message?: string;
}

/**
 * Start (or warm) a stopped workspace. When { lazy: true }, the backend
 * returns 202 immediately and continues the restart in a background task.
 */
export async function startWorkspace(
  workspaceId: string,
  opts: { lazy?: boolean } = {},
): Promise<WorkspaceActionResponse> {
  if (!workspaceId) throw new Error('Workspace ID is required');
  const params = opts.lazy ? '?lazy=true' : '';
  const { data } = await api.post(`/api/v1/workspaces/${workspaceId}/start${params}`);
  return data;
}

/**
 * Subscribe to workspace lifecycle status via SSE. Invokes `onStatus`
 * for each status transition reported by the backend, passing an optional
 * `sandboxState` refinement (e.g. 'archived') when present so callers can
 * show a slow-restore spinner, and the `computerId` of the machine the
 * workspace runs on when the backend reports one. Resolves when the stream
 * closes (terminal status, server timeout, or aborted via the AbortController
 * signal). Best-effort: network errors resolve without throwing so callers
 * don't need defensive wrappers.
 *
 * This is the fallback channel once a workspace names a computer: the machine
 * is what actually moves, so `streamComputerEvents` is the subscription a
 * surface showing several workspaces should open.
 */
export async function streamWorkspaceEvents(
  workspaceId: string,
  onStatus: (
    status: string,
    sandboxState?: string,
    computerId?: string,
  ) => void,
  signal: AbortSignal,
): Promise<void> {
  if (!workspaceId) return;
  await streamStatusEvents(
    `/api/v1/workspaces/${workspaceId}/events`,
    (frame) => onStatus(frame.status, frame.sandbox_state, frame.computer_id),
    signal,
  );
}

// DALTON-HOOK(coverage-tracking): company workspace auto-tracking API requests
export interface WorkspaceTrackingTask {
  automation_id: string | null;
  name: string;
  description?: string | null;
  trigger_type?: 'cron' | 'price' | 'once' | string;
  cron_expression?: string | null;
  timezone?: string | null;
  status: 'active' | 'paused' | 'missing' | string;
  exists: boolean;
  next_run_at?: string | null;
  paused_by_user?: boolean;
  standard?: boolean;
}

// DALTON-HOOK(coverage-tracking): company workspace auto-tracking response contract
export interface WorkspaceTrackingResponse {
  ok: boolean;
  workspace_id: string;
  ticker: string;
  tier: string;
  timezone: string;
  status: 'enabled' | 'disabled' | 'incomplete' | 'not_applicable';
  enabled: boolean;
  paused_by_user: boolean;
  label: string;
  missing_tasks: string[];
  paused_tasks: string[];
  active_tasks: string[];
  tasks: WorkspaceTrackingTask[];
  can_edit?: boolean;
}

// DALTON-HOOK(coverage-tracking): fetch company workspace auto-tracking status
export async function fetchWorkspaceTracking(
  workspaceId: string,
): Promise<WorkspaceTrackingResponse> {
  const { data } = await api.get<WorkspaceTrackingResponse>(
    `/api/v1/dalton/tracking/${encodeURIComponent(workspaceId)}`,
  );
  return data;
}

export async function updateWorkspaceTracking(
  workspaceId: string,
  enabled: boolean,
): Promise<WorkspaceTrackingResponse> {
  const { data } = await api.post<WorkspaceTrackingResponse>(
    `/api/v1/dalton/tracking/${encodeURIComponent(workspaceId)}`,
    { enabled },
  );
  return data;
}

// --- Threads ---
