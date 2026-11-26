import { api } from '@/api/client';

export const MONITOR_CATEGORIES = [
  'overview',
  'us',
  'cn',
  'hk',
  'asia',
  'stocks',
  'portfolio',
  'other',
] as const;

export type MonitorCategory = (typeof MONITOR_CATEGORIES)[number];

export const MONITOR_VISIBILITIES = ['own', 'members', 'company', 'public'] as const;

export type MonitorVisibility = (typeof MONITOR_VISIBILITIES)[number];

export interface MonitorSummary {
  monitor_id: string;
  slug: string;
  artifact_id?: string;
  artifact_code?: string | null;
  workspace_id?: string | null;
  entry_path?: string | null;
  owner_user_id?: string;
  owner_name: string | null;
  owner_email: string | null;
  title: string;
  category: MonitorCategory;
  sort_order: number;
  visibility: MonitorVisibility;
  member_user_ids: string[];
  published_seq: number;
  keep_updated: boolean;
  source_kind: 'workspace' | 'data_job';
  source_ref: string | null;
  acl_revision: number;
  last_publish_error: string | null;
  last_publish_failed_at: string | null;
  data_as_of: string | null;
  created_at: string;
  updated_at: string;
  can_manage: boolean;
}

export interface MonitorVersion {
  seq: number;
  entry_path: string;
  origin: string;
  title: string | null;
  label: string | null;
  byte_len: number;
  published_at: string;
}

export interface MonitorDetail {
  monitor: MonitorSummary;
  versions: MonitorVersion[];
  monitor_grant: string;
  frame_base: string;
  data_base: string;
  expires_in: number;
  root_nonce?: string;
  root_proof?: string;
  cut?: boolean;
  auto_publish_paused?: boolean;
  auto_publish_pause_token?: string | null;
  auto_publish_pause_notice?: string | null;
}

export interface MonitorGrantResponse {
  monitor_grant: string;
  frame_base: string;
  data_base: string;
  expires_in: number;
  acl_revision: number;
}

export interface CompanyMemberOption {
  user_id: string;
  email: string | null;
  name: string | null;
  tier: 'admin' | 'internal';
}

export interface PublishMonitorPayload {
  workspace_id: string;
  path: string;
  title?: string;
  slug?: string;
  category: MonitorCategory;
  sort_order?: number;
  visibility: MonitorVisibility;
  member_user_ids?: string[];
  keep_updated: boolean;
  label?: string | null;
  files?: readonly string[];
  loaded?: readonly string[];
  earlier?: readonly string[];
  page_sha256?: string;
  confirm_pause_token?: string | null;
}

export interface UpdateMonitorPayload {
  title?: string;
  category?: MonitorCategory;
  sort_order?: number;
  visibility?: MonitorVisibility;
  member_user_ids?: string[];
  keep_updated?: boolean;
}

export function monitorPermalink(slug: string, origin = typeof window !== 'undefined' ? window.location.origin : ''): string {
  return `${origin}/monitors/${encodeURIComponent(slug)}`;
}

export function publicMonitorPermalink(slug: string, origin = typeof window !== 'undefined' ? window.location.origin : ''): string {
  return `${origin}/m/${encodeURIComponent(slug)}`;
}

export function monitorShareUrl(
  slug: string,
  visibility: MonitorVisibility,
  origin = typeof window !== 'undefined' ? window.location.origin : '',
): string {
  return visibility === 'public'
    ? publicMonitorPermalink(slug, origin)
    : monitorPermalink(slug, origin);
}

export async function listMonitors(): Promise<MonitorSummary[]> {
  const { data } = await api.get<{ monitors: MonitorSummary[] }>('/api/v1/dalton/monitors');
  return data.monitors ?? [];
}

export async function getMonitorDetail(slug: string): Promise<MonitorDetail> {
  const { data } = await api.get<MonitorDetail>(`/api/v1/dalton/monitors/${encodeURIComponent(slug)}`);
  return data;
}

export async function getPublicMonitorDetail(slug: string): Promise<MonitorDetail> {
  const baseURL = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '';
  const res = await fetch(`${baseURL}/api/v1/dalton/monitors/public/${encodeURIComponent(slug)}`);
  if (!res.ok) {
    throw Object.assign(new Error(`Request failed with status ${res.status}`), {
      response: { status: res.status },
    });
  }
  return (await res.json()) as MonitorDetail;
}

export async function getMonitorByArtifact(artifactId: string): Promise<MonitorSummary | null> {
  const { data } = await api.get<{ monitor: MonitorSummary | null }>(
    `/api/v1/dalton/monitors/by-artifact/${encodeURIComponent(artifactId)}`,
  );
  return data.monitor ?? null;
}

export async function publishMonitor(payload: PublishMonitorPayload): Promise<MonitorDetail> {
  const { data } = await api.post<MonitorDetail>('/api/v1/dalton/monitors', payload);
  return data;
}

export async function updateMonitor(slug: string, payload: UpdateMonitorPayload): Promise<MonitorDetail> {
  const { data } = await api.patch<MonitorDetail>(`/api/v1/dalton/monitors/${encodeURIComponent(slug)}`, payload);
  return data;
}

export async function mintMonitorGrant(slug: string): Promise<MonitorGrantResponse> {
  const { data } = await api.post<MonitorGrantResponse>(
    `/api/v1/dalton/monitors/${encodeURIComponent(slug)}/grant`,
  );
  return data;
}

export async function deleteMonitor(slug: string): Promise<void> {
  await api.delete(`/api/v1/dalton/monitors/${encodeURIComponent(slug)}`);
}

export async function listCompanyMembers(): Promise<CompanyMemberOption[]> {
  const { data } = await api.get<{ members: CompanyMemberOption[] }>('/api/v1/dalton/monitors/company-members');
  return data.members ?? [];
}
