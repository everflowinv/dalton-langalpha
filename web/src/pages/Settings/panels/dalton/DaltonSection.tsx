import React from 'react';
import { useDaltonCoverageUnits } from '@/lib/dalton/coverage';
import { DaltonBudgetCard } from './DaltonBudgetCard';
import { DaltonCosSharingCard } from './DaltonCosSharingCard';
import { DaltonCoverageCard } from './DaltonCoverageCard';
import { DaltonInvitesCard } from './DaltonInvitesCard';
import { DaltonNotifyTargetsCard } from './DaltonNotifyTargetsCard';
import { DaltonSystemJobsCard } from './DaltonSystemJobsCard';
import { FeishuReadScopesCard } from './FeishuReadScopesCard';
import { ProviderStatusCard } from './ProviderStatusCard';
import type { DaltonSystemJobScope } from '../../utils/daltonApi';

export function DaltonSection({ advanced = false }: { advanced?: boolean } = {}) {
  // 行业层 P4：系统任务面板的 sector 组只在后端开关开启时进入 scopes，
  // 关闭时不出现任何行业相关的组或文案（开关状态由 coverage/units 下发）。
  const coverageUnits = useDaltonCoverageUnits();
  const sectorEnabled = coverageUnits.data?.enabled === true;
  const baseScopes: DaltonSystemJobScope[] = sectorEnabled
    ? advanced
      ? ['company', 'sector', 'home', 'host', 'ops']
      : ['company', 'sector', 'home']
    : advanced
      ? ['company', 'home', 'host', 'ops']
      : ['company', 'home'];

  return (
    <div className="flex flex-col gap-3">
      <DaltonCoverageCard />
      <DaltonCosSharingCard />
      <DaltonInvitesCard />
      <FeishuReadScopesCard />
      <div id="dalton-providers">
        <ProviderStatusCard />
      </div>
      <DaltonBudgetCard />
      <div id="dalton-notify-targets">
        <DaltonNotifyTargetsCard />
      </div>
      <div id="dalton-system-jobs">
        <DaltonSystemJobsCard scopes={baseScopes} />
      </div>
    </div>
  );
}
