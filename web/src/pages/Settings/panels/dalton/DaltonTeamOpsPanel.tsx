import { useTranslation } from 'react-i18next';
import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { useCapabilities } from '@/hooks/useCapabilities';
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
  const { isAdmin } = useCallerIdentity();
  const { hasCapability, isLoading: capsLoading } = useCapabilities();
  const canChannelFeishu = hasCapability('channel_feishu');
  const canChannelDiscord = hasCapability('channel_discord');
  const canChannelWeixin = hasCapability('channel_weixin');
  const canDataJobs = hasCapability('data_jobs');

  if (capsLoading) {
    return <DaltonLoadingRow label={t('dalton.common.loading')} />;
  }

  return (
    <div className="space-y-8">
      {isAdmin && canChannelWeixin && <DaltonWeixinBotCard />}
      {canChannelFeishu && <DaltonFeishuGroupsCard />}
      {isAdmin && canChannelDiscord && <DaltonDiscordChannelsCard />}
      {canDataJobs && <DaltonSystemJobsCard />}
      {isAdmin && (canChannelFeishu || canChannelDiscord) && (
        <DaltonNotifyTargetsCard allowManualInput />
      )}
    </div>
  );
}
