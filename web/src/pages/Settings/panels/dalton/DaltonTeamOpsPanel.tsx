import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { isDaltonAuth } from '@/config/daltonAuth';
import { useCapabilities } from '@/hooks/useCapabilities';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { queryKeys } from '@/lib/queryKeys';
import { fetchDaltonChannelBindings } from '@/pages/Settings/utils/daltonApi';
import { DaltonLoadingRow } from '@/components/dalton/primitives';
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
  const { t } = useTranslation();
  const { isPm, callerTier, callerTierError } = useTeamCredentials();
  const { hasCapability, isLoading: capsLoading } = useCapabilities();
  const isAdmin = callerTier === 'admin' || (!isDaltonAuth && isPm);
  const canChannelFeishu = hasCapability('channel_feishu');
  const canChannelDiscord = hasCapability('channel_discord');
  const canChannelWeixin = hasCapability('channel_weixin');
  const canDataJobs = hasCapability('data_jobs');
  const canAnyChannel =
    canChannelFeishu || canChannelDiscord || canChannelWeixin || hasCapability('channel_gateway');
  const bindingsQuery = useQuery({
    queryKey: queryKeys.dalton.channelBindings(),
    queryFn: fetchDaltonChannelBindings,
    enabled: canAnyChannel,
    retry: false,
  });
  const backendUserTier = bindingsQuery.data?.user_tier ?? null;
  const resolvedDaltonTier =
    !callerTierError && callerTier ? callerTier : backendUserTier;
  const isDiscordAdmin = isDaltonAuth
    ? resolvedDaltonTier === 'admin' && (backendUserTier === null || backendUserTier === 'admin')
    : backendUserTier === 'admin';

  if (capsLoading) {
    return <DaltonLoadingRow label={t('dalton.common.loading')} />;
  }

  return (
    <div className="space-y-8">
      {isAdmin && canChannelWeixin && <DaltonWeixinBotCard />}
      {canChannelFeishu && <DaltonFeishuGroupsCard />}
      {isDiscordAdmin && canChannelDiscord && <DaltonDiscordChannelsCard />}
      {canDataJobs && <DaltonSystemJobsCard />}
      {isAdmin && (canChannelFeishu || canChannelDiscord) && (
        <DaltonNotifyTargetsCard allowManualInput />
      )}
    </div>
  );
}
