import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import {
  checkNativeModels,
  deleteDaltonTaskCap,
  fetchDaltonBudget,
  fetchDaltonBudgetToday,
  fetchNativeModelsStatus,
  fetchOpenClawHealth,
  fetchOpenClawStatus,
  restoreNativeModels,
  retireNativeModels,
  syncNativeModels,
  fetchDaltonSystemJobs,
  runDaltonSystemJobNow,
  syncOpenClawModels,
  toggleDaltonSystemJob,
  updateDaltonBudget,
  upsertDaltonTaskCap,
} from '../utils/daltonApi';

export function useOpenClawStatus(enabled = true) {
  return useQuery({
    queryKey: queryKeys.dalton.openclawStatus(),
    queryFn: fetchOpenClawStatus,
    enabled,
    staleTime: 30_000,
  });
}

export function useOpenClawHealth(enabled = true) {
  return useQuery({
    queryKey: queryKeys.dalton.openclawHealth(),
    queryFn: fetchOpenClawHealth,
    enabled,
    refetchInterval: enabled ? 60_000 : false,
    staleTime: 15_000,
  });
}

export function useOpenClawSync() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (params: { dry_run: boolean; force?: boolean }) => syncOpenClawModels(params),
    onSuccess: (res) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.all });
      if (res.applied) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.models.all });
      }
    },
  });
}

// Native-subscription model sync: status is a plain read of the last stored
// check — opening the settings page never triggers a provider fetch.
export function useNativeModelsStatus(enabled = true) {
  return useQuery({
    queryKey: queryKeys.dalton.nativeModelsStatus(),
    queryFn: fetchNativeModelsStatus,
    enabled,
    staleTime: 30_000,
  });
}

export function useNativeModelsCheck() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: checkNativeModels,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.nativeModelsStatus() });
    },
  });
}

export function useNativeModelsSync() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (params: { models: string[] }) => syncNativeModels(params),
    onSuccess: (res) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.all });
      if (res.applied) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.models.all });
      }
    },
  });
}

// Retire/restore rewrite the runtime overlay (the `retired` list), so both the
// Dalton status and the model lists every worker serves must refresh.
export function useNativeModelsRetire() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (params: { keys: string[] }) => retireNativeModels(params),
    onSuccess: (res) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.all });
      if (res.retired_keys.length > 0) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.models.all });
      }
    },
  });
}

export function useNativeModelsRestore() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (params: { keys: string[] }) => restoreNativeModels(params),
    onSuccess: (res) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.all });
      if (res.restored_keys.length > 0) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.models.all });
      }
    },
  });
}

export function useDaltonBudget(enabled = true) {
  return useQuery({
    queryKey: queryKeys.dalton.budget(),
    queryFn: fetchDaltonBudget,
    enabled,
    staleTime: 30_000,
  });
}

export function useDaltonBudgetToday(enabled = true) {
  return useQuery({
    queryKey: queryKeys.dalton.budgetToday(),
    queryFn: fetchDaltonBudgetToday,
    enabled,
    refetchInterval: enabled ? 30_000 : false,
    staleTime: 10_000,
  });
}

export function useUpdateDaltonBudget() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateDaltonBudget,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.budget() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.budgetToday() });
    },
  });
}

export function useUpsertDaltonTaskCap() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (params: { scopeKind: 'workspace' | 'thread'; scopeId: string; capUsd: number }) =>
      upsertDaltonTaskCap(params.scopeKind, params.scopeId, params.capUsd),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.budget() });
    },
  });
}

export function useDeleteDaltonTaskCap() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (params: { scopeKind: 'workspace' | 'thread'; scopeId: string }) =>
      deleteDaltonTaskCap(params.scopeKind, params.scopeId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.budget() });
    },
  });
}

export function useDaltonSystemJobs(enabled = true) {
  return useQuery({
    queryKey: queryKeys.dalton.systemJobs(),
    queryFn: fetchDaltonSystemJobs,
    enabled,
    staleTime: 15_000,
  });
}

export function useToggleDaltonSystemJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (params: { key: string; enabled: boolean }) =>
      toggleDaltonSystemJob(params.key, params.enabled),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.systemJobs() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.automations.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.workspaces.all });
    },
  });
}

export function useRunDaltonSystemJobNow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => runDaltonSystemJobNow(key),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.systemJobs() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.automations.all });
    },
  });
}
