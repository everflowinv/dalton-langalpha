import { isPlatformMode } from '@/config/hostMode';
import { PlanPanel } from '../PlanPanel';
import { DaltonBudgetCard } from './DaltonBudgetCard';

/** Usage & Budget section panel: personal daily budget card (+ PlanPanel on hosted builds). */
export function DaltonBudgetPanel() {
  return (
    <div className="space-y-8">
      {isPlatformMode && <PlanPanel />}
      <DaltonBudgetCard />
    </div>
  );
}
