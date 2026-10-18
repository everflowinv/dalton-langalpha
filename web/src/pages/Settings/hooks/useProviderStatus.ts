import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import {
  fetchDaltonAvailableModels,
  fetchDaltonProvidersStatus,
  probeDaltonProvider,
} from '../utils/daltonApi';

export function useProviderStatus(enabled = true) {
  return useQuery({
    queryKey: queryKeys.dalton.providersStatus(),
    queryFn: fetchDaltonProvidersStatus,
    enabled,
    staleTime: 30_000,
    retry: false,
  });
}

export function useProviderProbe() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (params: { provider: string; model?: string | null }) =>
      probeDaltonProvider(params.provider, params.model),
    onSettled: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.dalton.providersStatus(),
      });
    },
  });
}

export function useAvailableModels(enabled = true) {
  return useQuery({
    queryKey: queryKeys.dalton.modelsAvailable(),
    queryFn: fetchDaltonAvailableModels,
    enabled,
    staleTime: 30_000,
    retry: false,
  });
}
