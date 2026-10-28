import type { KeyOption, Plan } from './providerOptions';

type T = (key: string) => string;

export function planName(t: T, plan: Plan): string {
  return plan === 'coding_plan' ? t('modelAccess.codingPlan') : t('modelAccess.keyPicker.planApi');
}

export function regionName(t: T, region: string | null): string {
  if (region === 'cn') return t('setup.regionChina');
  if (region === 'sg') return t('setup.regionSingapore');
  if (region === 'intl' || !region) return t('setup.regionInternational');
  return region;
}

/** "Coding plan · China": which of a provider's ways in an option is. */
export function optionName(t: T, o: KeyOption): string {
  return `${planName(t, o.plan)} · ${regionName(t, o.region)}`;
}
