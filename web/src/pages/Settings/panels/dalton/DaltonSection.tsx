import React from 'react';
import { DaltonBudgetCard } from './DaltonBudgetCard';
import { DaltonCosSharingCard } from './DaltonCosSharingCard';
import { DaltonInvitesCard } from './DaltonInvitesCard';
import { DaltonNotifyTargetsCard } from './DaltonNotifyTargetsCard';
import { DaltonSystemJobsCard } from './DaltonSystemJobsCard';
import { FeishuReadScopesCard } from './FeishuReadScopesCard';
import { ProviderStatusCard } from './ProviderStatusCard';

export function DaltonSection({ advanced = false }: { advanced?: boolean } = {}) {
  return (
    <div className="flex flex-col gap-3">
      <DaltonCosSharingCard />
      <div id="dalton-invites">
        <DaltonInvitesCard />
      </div>
      <div id="dalton-feishu-scopes">
        <FeishuReadScopesCard />
      </div>
      <div id="dalton-providers">
        <ProviderStatusCard />
      </div>
      <DaltonBudgetCard />
      <div id="dalton-notify-targets">
        <DaltonNotifyTargetsCard />
      </div>
      <div id="dalton-system-jobs">
        <DaltonSystemJobsCard
          scopes={advanced ? ['company', 'home', 'host', 'ops'] : ['company', 'home']}
        />
      </div>
    </div>
  );
}
