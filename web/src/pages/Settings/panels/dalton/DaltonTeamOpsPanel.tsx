import { isDaltonAuth } from '@/config/daltonAuth';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { DaltonFeishuGroupsCard } from './DaltonFeishuGroupsCard';
import { DaltonNotifyTargetsCard } from './DaltonNotifyTargetsCard';
import { DaltonSystemJobsCard } from './DaltonSystemJobsCard';
import { DaltonWeixinBotCard } from './DaltonWeixinBotCard';

/**
 * Team Management -> Team Channels & Ops panel:
 * - Admin-only: WeChat team bot QR login, host/ops system jobs, manual notify target override.
 * - Internal+: Feishu team groups, team research system jobs.
 */
export function DaltonTeamOpsPanel() {
  const { isPm, callerTier } = useTeamCredentials();
  const isAdmin = callerTier === 'admin' || (!isDaltonAuth && isPm);

  return (
    <div className="space-y-8">
      {isAdmin && <DaltonWeixinBotCard />}
      <DaltonFeishuGroupsCard />
      <DaltonSystemJobsCard />
      {isAdmin && <DaltonNotifyTargetsCard allowManualInput />}
    </div>
  );
}
