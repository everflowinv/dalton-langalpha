import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import {
  deleteDaltonTaskCap,
  fetchDaltonBudget,
  fetchDaltonBudgetToday,
  fetchOpenClawHealth,
  fetchOpenClawStatus,
  syncOpenClawModels,
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
