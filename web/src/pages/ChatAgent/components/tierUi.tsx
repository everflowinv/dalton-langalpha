/**
 * The resource-tier vocabulary and its picker.
 *
 * Two surfaces choose a tier (changing a computer's spec, and creating
 * one), and a tier means the same thing in both, so the ladder, the copy
 * and the keyboard behaviour live here rather than once per dialog.
 */
import { useTranslation } from 'react-i18next';

import { isPlatformMode } from '@/config/hostMode';
import type { ResourceTier, WorkspaceCapacity, WorkspaceQuota } from '@/types/api';

import { RadioCardGroup, type RadioCardOption } from './radioCardGroup';

type Translate = ReturnType<typeof useTranslation>['t'];

/** Tier presets, ordered for the radiogroup. */
export const TIER_ORDER: ResourceTier[] = ['standard', 'performance', 'max'];

/** Coerce an unknown/legacy tier value to a known tier (defaults to standard). */
export function normalizeTier(tier: unknown): ResourceTier {
  return tier === 'performance' || tier === 'max' ? tier : 'standard';
}

/** Localized display name for a tier. */
export function tierLabel(t: Translate, tier: ResourceTier): string {
  return t(`workspace.tier.${tier}`);
}

/** A tier's count quota. Only elevated tiers carry one, and only in platform mode. */
export function tierCapacity(
  quota: WorkspaceQuota | null | undefined,
  tier: ResourceTier,
): WorkspaceCapacity | null {
  if (tier === 'performance') return quota?.performance ?? null;
  if (tier === 'max') return quota?.max ?? null;
  return null;
}

/**
 * Why a tier cannot be picked: the plan excludes it, or its quota is spent.
 * Null when it can. A negative limit is unlimited.
 */
export function tierBlockReason(capacity: WorkspaceCapacity | null | undefined): 'not_on_plan' | 'used_up' | null {
  if (!isPlatformMode || !capacity) return null;
  if (capacity.limit === 0) return 'not_on_plan';
  return capacity.limit > 0 && capacity.used >= capacity.limit ? 'used_up' : null;
}

export function isTierBlocked(capacity: WorkspaceCapacity | null | undefined): boolean {
  return tierBlockReason(capacity) !== null;
}

interface TierRadioGroupProps {
  value: ResourceTier;
  onChange: (tier: ResourceTier) => void;
  /** Shown above the cards, and the group's accessible name. */
  label: string;
  /** Per-tier count quotas (platform mode only); null/undefined hides the capacity hint. */
  quota?: WorkspaceQuota | null;
  /**
   * The tier the computer is on now. It reads "Current" in place of a quota
   * note, and is never disabled: re-selecting it consumes no new slot,
   * mirroring the backend's skip.
   */
  currentTier?: ResourceTier | null;
}

/**
 * The tier picker, three across where the space allows. In platform mode
 * each elevated tier shows its remaining count quota and goes unselectable
 * when the plan excludes it or its quota is spent.
 */
export function TierRadioGroup({
  value,
  onChange,
  label,
  quota,
  currentTier = null,
}: TierRadioGroupProps) {
  const { t } = useTranslation();
  const options: RadioCardOption<ResourceTier>[] = TIER_ORDER.map((id) => {
    const capacity = tierCapacity(quota, id);
    return {
      value: id,
      title: tierLabel(t, id),
      facts: tierFacts(t, id),
      // The current tier takes no new slot, so what is left of the quota
      // says nothing about it.
      note: id === currentTier
        ? t('computer.spec.current', 'Current')
        : isPlatformMode && capacity ? capacityNote(t, capacity) : null,
      disabled: id !== currentTier && isTierBlocked(capacity),
    };
  });
  return <RadioCardGroup options={options} value={value} onChange={onChange} label={label} columns={3} />;
}

/**
 * vCPU, RAM and disk as separate facts. The catalogs write a spec as one
 * dotted line, which is how a stacked card still shows it; a narrow column
 * gives each its own line. A locale without the dot keeps its one line.
 */
function tierFacts(t: Translate, tier: ResourceTier): string[] {
  return t(`workspace.tierSpec.${tier}`)
    .split('·')
    .map((part) => part.trim())
    .filter(Boolean);
}

function capacityNote(t: Translate, capacity: WorkspaceCapacity): string {
  if (capacity.limit < 0) return t('workspace.quotaUnlimited', 'Unlimited');
  if (capacity.limit === 0) return t('workspace.quotaNotOnPlan', 'Not on your plan');
  return t('workspace.quotaRemaining', '{{remaining}} of {{limit}} left', {
    remaining: Math.max(0, capacity.limit - capacity.used),
    limit: capacity.limit,
  });
}
