import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { apiErrorDetail, apiErrorStatus } from '@/api/errors';
import { queryKeys } from '@/lib/queryKeys';
import {
  createTeamCatalogModel,
  createTeamCatalogProviderVariant,
  fetchTeamCatalogRevisionDetail,
  fetchTeamCatalogRevisions,
  fetchTeamModelCatalog,
  resolveTeamCatalogLegacyImport,
  restoreTeamCatalogModel,
  restoreTeamCatalogModelDefault,
  retireTeamCatalogModel,
  rollbackTeamCatalog,
  setTeamCatalogModelVisibility,
  testCallTeamCatalogModel,
  updateTeamCatalogCuratedSetup,
  updateTeamCatalogModel,
  updateTeamCatalogProviderVariant,
  type TeamCatalogLegacyResolvePayload,
  type TeamCatalogModelUpsertPayload,
  type TeamCatalogProviderVariantUpsertPayload,
  type TeamCatalogRetirePayload,
  type TeamCatalogRollbackPayload,
  type TeamCatalogTestCallPayload,
} from '@/pages/Settings/utils/daltonApi';

export interface MissingVariantCredentialDetail {
  provider_slug: string;
  required_base_url: string;
  current_bound_base_url?: string | null;
}

export interface ParsedCatalogApiError {
  status: number | null;
  code: string | null;
  message: string;
  detail: Record<string, unknown> | null;
  missingVariantCredentials: MissingVariantCredentialDetail[];
  automations: Array<{ automation_id?: string; name?: string; user_id?: string }>;
  userSubagents: Array<{ user_subagent_id?: string; name?: string; user_id?: string }>;
  pluginSubagents: Array<{ plugin_subagent_id?: string; name?: string; user_id?: string }>;
  userPreferences: Array<{ user_id?: string; slots?: string[] }>;
  agentConfig: Record<string, string[]>;
  daltonSettings: Record<string, string[]>;
  bundledPluginSubagents: Record<string, string[]>;
  currentRevision: number | null;
}

/** Extract structured error info from a catalog API failure (400/409/422/429/503). */
export function parseCatalogApiError(err: unknown, fallbackMessage: string): ParsedCatalogApiError {
  const status = apiErrorStatus(err) ?? null;
  const rawDetail = apiErrorDetail(err);
  const detailObj =
    rawDetail && typeof rawDetail === 'object' && !Array.isArray(rawDetail)
      ? (rawDetail as Record<string, unknown>)
      : null;

  const code = typeof detailObj?.code === 'string' ? detailObj.code : null;
  let message = fallbackMessage;
  if (typeof detailObj?.message === 'string' && detailObj.message.trim()) {
    message = detailObj.message;
  } else if (typeof rawDetail === 'string' && rawDetail.trim()) {
    message = rawDetail;
  } else if (Array.isArray(rawDetail) && rawDetail.length > 0) {
    const first = rawDetail[0] as { msg?: unknown; loc?: unknown };
    if (typeof first?.msg === 'string') {
      const loc = Array.isArray(first.loc) ? first.loc.join('.') : '';
      message = loc ? `${loc}: ${first.msg}` : first.msg;
    }
  } else if (err instanceof Error && err.message) {
    message = err.message;
  }

  const missingVariantCredentials = Array.isArray(detailObj?.missing_variant_credentials)
    ? (detailObj.missing_variant_credentials as MissingVariantCredentialDetail[])
    : [];
  const automations = Array.isArray(detailObj?.automations)
    ? (detailObj.automations as Array<{ automation_id?: string; name?: string; user_id?: string }>)
    : [];
  const userSubagents = Array.isArray(detailObj?.user_subagents)
    ? (detailObj.user_subagents as Array<{ user_subagent_id?: string; name?: string; user_id?: string }>)
    : [];
  const pluginSubagents = Array.isArray(detailObj?.plugin_subagents)
    ? (detailObj.plugin_subagents as Array<{ plugin_subagent_id?: string; name?: string; user_id?: string }>)
    : [];
  const userPreferences = Array.isArray(detailObj?.user_preferences)
    ? (detailObj.user_preferences as Array<{ user_id?: string; slots?: string[] }>)
    : [];
  const agentConfig =
    detailObj?.agent_config && typeof detailObj.agent_config === 'object'
      ? (detailObj.agent_config as Record<string, string[]>)
      : {};
  const daltonSettings =
    detailObj?.dalton_settings && typeof detailObj.dalton_settings === 'object'
      ? (detailObj.dalton_settings as Record<string, string[]>)
      : {};
  const bundledPluginSubagents =
    detailObj?.bundled_plugin_subagents && typeof detailObj.bundled_plugin_subagents === 'object'
      ? (detailObj.bundled_plugin_subagents as Record<string, string[]>)
      : {};
  const currentRevision =
    typeof detailObj?.current_revision === 'number' ? detailObj.current_revision : null;

  return {
    status,
    code,
    message,
    detail: detailObj,
    missingVariantCredentials,
    automations,
    userSubagents,
    pluginSubagents,
    userPreferences,
    agentConfig,
    daltonSettings,
    bundledPluginSubagents,
    currentRevision,
  };
}

function invalidateAfterCatalogWrite(queryClient: QueryClient) {
  void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.teamModelCatalog() });
  void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.nativeModelsStatus() });
  void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.providersStatus() });
  void queryClient.invalidateQueries({ queryKey: [...queryKeys.dalton.all, 'team-models-available'] });
  void queryClient.invalidateQueries({ queryKey: queryKeys.models.all });
  void queryClient.invalidateQueries({ queryKey: queryKeys.providerModels.all });
}

export function useTeamModelCatalog(enabled = true) {
  return useQuery({
    queryKey: queryKeys.dalton.teamModelCatalog(),
    queryFn: fetchTeamModelCatalog,
    enabled,
    staleTime: 30_000,
  });
}

export function useTeamCatalogRevisions(enabled: boolean, limit = 50, offset = 0) {
  return useQuery({
    queryKey: queryKeys.dalton.teamModelRevisions(limit, offset),
    queryFn: () => fetchTeamCatalogRevisions(limit, offset),
    enabled,
    staleTime: 15_000,
  });
}

export function useTeamCatalogRevisionDetail(revision: number | null) {
  return useQuery({
    queryKey: queryKeys.dalton.teamModelRevisionDetail(revision ?? -1),
    queryFn: () => fetchTeamCatalogRevisionDetail(revision!),
    enabled: revision !== null && revision >= 0,
    staleTime: 60_000,
  });
}

export function useCreateTeamCatalogModel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: TeamCatalogModelUpsertPayload) => createTeamCatalogModel(payload),
    onSuccess: () => invalidateAfterCatalogWrite(queryClient),
  });
}

export function useUpdateTeamCatalogModel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ modelKey, payload }: { modelKey: string; payload: TeamCatalogModelUpsertPayload }) =>
      updateTeamCatalogModel(modelKey, payload),
    onSuccess: () => invalidateAfterCatalogWrite(queryClient),
  });
}

export function useSetTeamCatalogModelVisibility() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      modelKey,
      visible,
      expected_revision,
      comment,
    }: {
      modelKey: string;
      visible: boolean;
      expected_revision?: number;
      comment?: string | null;
    }) => setTeamCatalogModelVisibility(modelKey, { visible, expected_revision, comment }),
    onSuccess: () => invalidateAfterCatalogWrite(queryClient),
  });
}

export function useRetireTeamCatalogModel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ modelKey, payload }: { modelKey: string; payload: TeamCatalogRetirePayload }) =>
      retireTeamCatalogModel(modelKey, payload),
    onSuccess: () => invalidateAfterCatalogWrite(queryClient),
  });
}

export function useRestoreTeamCatalogModel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      modelKey,
      expected_revision,
      comment,
    }: {
      modelKey: string;
      expected_revision?: number;
      comment?: string | null;
    }) => restoreTeamCatalogModel(modelKey, { expected_revision, comment }),
    onSuccess: () => invalidateAfterCatalogWrite(queryClient),
  });
}

export function useRestoreTeamCatalogModelDefault() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      modelKey,
      expected_revision,
      comment,
    }: {
      modelKey: string;
      expected_revision?: number;
      comment?: string | null;
    }) => restoreTeamCatalogModelDefault(modelKey, { expected_revision, comment }),
    onSuccess: () => invalidateAfterCatalogWrite(queryClient),
  });
}

export function useUpsertTeamCatalogProviderVariant() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      isUpdate,
      payload,
    }: {
      isUpdate: boolean;
      payload: TeamCatalogProviderVariantUpsertPayload;
    }) =>
      isUpdate
        ? updateTeamCatalogProviderVariant(payload.provider_slug, payload)
        : createTeamCatalogProviderVariant(payload),
    onSuccess: () => invalidateAfterCatalogWrite(queryClient),
  });
}

export function useUpdateTeamCatalogCuratedSetup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: {
      curated_setup: string[] | null;
      expected_revision?: number;
      comment?: string | null;
    }) => updateTeamCatalogCuratedSetup(payload),
    onSuccess: () => invalidateAfterCatalogWrite(queryClient),
  });
}

export function useRollbackTeamCatalog() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: TeamCatalogRollbackPayload) => rollbackTeamCatalog(payload),
    onSuccess: () => invalidateAfterCatalogWrite(queryClient),
  });
}

export function useTestCallTeamCatalogModel() {
  return useMutation({
    mutationFn: (payload: TeamCatalogTestCallPayload) => testCallTeamCatalogModel(payload),
  });
}

export function useResolveTeamCatalogLegacyImport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: TeamCatalogLegacyResolvePayload) =>
      resolveTeamCatalogLegacyImport(payload),
    onSuccess: () => invalidateAfterCatalogWrite(queryClient),
  });
}
