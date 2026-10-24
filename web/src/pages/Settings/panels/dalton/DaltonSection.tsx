import React from 'react';
import { DaltonBudgetCard } from './DaltonBudgetCard';
import { DaltonNotifyTargetsCard } from './DaltonNotifyTargetsCard';
import { DaltonSystemJobsCard } from './DaltonSystemJobsCard';
import { ProviderStatusCard } from './ProviderStatusCard';

export function DaltonSection() {
  return (
    <div className="flex flex-col gap-3">
      <ProviderStatusCard />
      <DaltonBudgetCard />
      <DaltonNotifyTargetsCard />
      <DaltonSystemJobsCard />
    </div>
  );
}
