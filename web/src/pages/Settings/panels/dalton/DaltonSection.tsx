import React from 'react';
import { DaltonBudgetCard } from './DaltonBudgetCard';
import { DaltonInvitesCard } from './DaltonInvitesCard';
import { DaltonNotifyTargetsCard } from './DaltonNotifyTargetsCard';
import { DaltonSystemJobsCard } from './DaltonSystemJobsCard';
import { FeishuReadScopesCard } from './FeishuReadScopesCard';
import { ProviderStatusCard } from './ProviderStatusCard';

export function DaltonSection({ advanced = false }: { advanced?: boolean } = {}) {
  return (
    <div className="flex flex-col gap-3">
      <DaltonInvitesCard />
      <FeishuReadScopesCard />
      <ProviderStatusCard />
      <DaltonBudgetCard />
      <DaltonNotifyTargetsCard />
      <DaltonSystemJobsCard
        scopes={advanced ? ['company', 'home', 'host', 'ops'] : ['company', 'home']}
      />
    </div>
  );
}
