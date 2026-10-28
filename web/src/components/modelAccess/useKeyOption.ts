import { useState } from 'react';
import { findOption, plansOf, regionsOf, startingOption, type KeyOption, type Plan, type ProviderGroup } from './providerOptions';

export interface KeyOptionChoice {
  option: KeyOption;
  plans: Plan[];
  regions: string[];
  choosePlan: (plan: Plan) => void;
  chooseRegion: (region: string) => void;
}

/**
 * A provider's plan and region as the user picks them, starting on a saved
 * key where there is one. Picking a plan keeps the region when that plan has
 * it, since China and international keys are separate keyspaces.
 */
export function useKeyOption(group: ProviderGroup, configured: ReadonlySet<string>): KeyOptionChoice {
  const start = startingOption(group, configured);
  const [plan, setPlan] = useState<Plan>(start.plan);
  const [region, setRegion] = useState(start.region);
  const regions = regionsOf(group, plan);
  const option = findOption(group, plan, region) ?? findOption(group, plan, regions[0]) ?? start;
  return {
    option,
    plans: plansOf(group),
    regions,
    choosePlan: (next) => {
      setPlan(next);
      const available = regionsOf(group, next);
      if (!available.includes(region)) setRegion(available[0]);
    },
    chooseRegion: setRegion,
  };
}
