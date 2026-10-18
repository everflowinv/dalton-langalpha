import React from 'react';
import { DaltonBudgetCard } from './DaltonBudgetCard';
import { OpenClawCard } from './OpenClawCard';

export function DaltonSection() {
  return (
    <div className="flex flex-col gap-3">
      <OpenClawCard />
      <DaltonBudgetCard />
    </div>
  );
}
