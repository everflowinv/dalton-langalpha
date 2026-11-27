import { useCapabilities } from '@/hooks/useCapabilities';
import { useDaltonCoverageUnits } from '@/lib/dalton/coverage';
import { DaltonCoverageCard } from './DaltonCoverageCard';
import { DaltonTierPolicyCard } from './DaltonTierPolicyCard';

/** Dedicated Coverage Structure settings panel: personal tier policies (always) + team coverage units (capability-gated). */
export function DaltonCoveragePanel() {
  const { hasCapability } = useCapabilities();
  const hasColleagueSharing = hasCapability('colleague_sharing');
  const { data: coverageData } = useDaltonCoverageUnits(hasColleagueSharing);
  const showTeamCoverageUnits = hasColleagueSharing && coverageData?.enabled === true;

  return (
    <div className="space-y-6">
      <DaltonTierPolicyCard />
      {showTeamCoverageUnits && <DaltonCoverageCard />}
    </div>
  );
}
