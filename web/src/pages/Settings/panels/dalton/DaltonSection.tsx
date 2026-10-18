import React from 'react';
import { DaltonBudgetCard } from './DaltonBudgetCard';
import { ProviderStatusCard } from './ProviderStatusCard';

export function DaltonSection() {
  return (
    <div className="flex flex-col gap-3">
      <ProviderStatusCard />
      <DaltonBudgetCard />
    </div>
  );
}
