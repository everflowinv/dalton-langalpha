import { useQuery } from '@tanstack/react-query';
import { useAuth } from '@/contexts/AuthContext';
import { isDaltonAuth } from '@/config/daltonAuth';
import { queryKeys } from '@/lib/queryKeys';

// Kept out of config/daltonAuth.ts: that module is imported widely as a plain constant, and
// pulling the auth context and React Query into it would load them into every importer.
// DALTON-HOOK(team-credentials): team-shared credentials hint & PM disconnect permission
export const TEAM_SHARED_CREDENTIALS_HINT = '团队共用：在这里连接后全队都能使用';

export interface TeamCredentialsStatus {
  sharing: 'team' | 'off';
  enabled: boolean;
  holder_user_id: string | null;
  credential_user_id: string;
  is_pm: boolean;
  can_disconnect: boolean;
  can_invite?: boolean;
}

export function useTeamCredentials(): {
  isShared: boolean;
  canDisconnect: boolean;
  isPm: boolean;
  canInvite: boolean;
} {
  const { userId } = useAuth();

  const { data, isError } = useQuery<TeamCredentialsStatus>({
    queryKey: queryKeys.dalton.credentialsStatus(userId),
    queryFn: async () => {
      const { api } = await import('@/api/client');
      const res = await api.get<TeamCredentialsStatus>('/api/v1/dalton/credentials/status');
      return res.data;
    },
    enabled: isDaltonAuth && Boolean(userId),
    staleTime: 5 * 60_000,
    retry: 1,
  });

  if (!isDaltonAuth) {
    // OSS local mode: the local dev user is the default PM for credentials,
    // while `canInvite` requires explicit DALTON_PM_USER_IDS under Dalton auth.
    return { isShared: false, canDisconnect: true, isPm: true, canInvite: false };
  }
  return {
    isShared: Boolean(data?.enabled),
    canDisconnect: !isError && Boolean(data?.can_disconnect),
    isPm: !isError && Boolean(data?.is_pm),
    canInvite: !isError && Boolean(data?.can_invite),
  };
}
