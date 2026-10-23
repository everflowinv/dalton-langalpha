import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import {
  fetchWorkspaceTracking,
  updateWorkspaceTracking,
  type WorkspaceTrackingResponse,
} from '../../utils/api/workspaces';

export function useWorkspaceTracking(
  workspaceId: string | null | undefined,
  enabled = true,
) {
  const id = workspaceId ?? '';
  return useQuery<WorkspaceTrackingResponse>({
    queryKey: queryKeys.dalton.tracking(id),
    queryFn: () => fetchWorkspaceTracking(id),
    enabled: Boolean(id) && enabled,
    staleTime: 30_000,
    retry: false,
  });
}

export function useUpdateWorkspaceTracking(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (enabled: boolean) => updateWorkspaceTracking(workspaceId, enabled),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.dalton.tracking(workspaceId), updated);
      void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.systemJobs() });
    },
  });
}
