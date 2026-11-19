import { useQuery } from '@tanstack/react-query';
import { isDaltonAuth } from '@/config/daltonAuth';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { queryKeys } from '@/lib/queryKeys';
import { fetchDaltonChannelBindings } from '@/pages/Settings/utils/daltonApi';
import { DaltonDiscordChannelsCard } from './DaltonDiscordChannelsCard';
import { DaltonFeishuGroupsCard } from './DaltonFeishuGroupsCard';
import { DaltonNotifyTargetsCard } from './DaltonNotifyTargetsCard';
import { DaltonSystemJobsCard } from './DaltonSystemJobsCard';
import { DaltonWeixinBotCard } from './DaltonWeixinBotCard';

/**
 * Team Management -> Team Channels & Ops panel:
 * - Admin-only: WeChat team bot QR login, Discord report channels, manual notify target override.
 * - Internal+: Feishu team groups, team research system jobs.
 */
export function DaltonTeamOpsPanel() {
  const { isPm, callerTier, callerTierError } = useTeamCredentials();
  const isAdmin = callerTier === 'admin' || (!isDaltonAuth && isPm);
  const bindingsQuery = useQuery({
    queryKey: queryKeys.dalton.channelBindings(),
    queryFn: fetchDaltonChannelBindings,
    retry: false,
  });
  const backendUserTier = bindingsQuery.data?.user_tier ?? null;
  const resolvedDaltonTier =
    !callerTierError && callerTier ? callerTier : backendUserTier;
  const isDiscordAdmin = isDaltonAuth
    ? resolvedDaltonTier === 'admin' && (backendUserTier === null || backendUserTier === 'admin')
    : backendUserTier === 'admin';

  return (
    <div className="space-y-8">
      {isAdmin && <DaltonWeixinBotCard />}
      <DaltonFeishuGroupsCard />
      {isDiscordAdmin && <DaltonDiscordChannelsCard />}
      <DaltonSystemJobsCard />
      {isAdmin && <DaltonNotifyTargetsCard allowManualInput />}
    </div>
  );
}
