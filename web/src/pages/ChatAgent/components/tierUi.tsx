/**
 * The resource-tier vocabulary and its picker.
 *
 * Two surfaces choose a tier (changing a computer's spec, and creating
 * one), and a tier means the same thing in both, so the ladder, the copy
 * and the keyboard behaviour live here rather than once per dialog.
 */
import { useTranslation } from 'react-i18next';

import { isPlatformMode } from '@/config/hostMode';
import type {
  ComputerProviderOption,
  ComputerTierSpec,
  ResourceTier,
  WorkspaceCapacity,
  WorkspaceQuota,
} from '@/types/api';

import { RadioCardGroup, type RadioCardOption } from './radioCardGroup';

type Translate = ReturnType<typeof useTranslation>['t'];

/** Every tier, smallest first. */
export const TIER_ORDER: ResourceTier[] = ['standard', 'performance', 'max', 'xlarge'];

/**
 * What a picker offers when the provider's own list is unknown: still
 * loading, an older server, or a computer on a provider no longer offered.
 * The sizes every hosted provider runs, so xlarge (E2B's alone) is not one.
 */
const BASE_TIERS: readonly ResourceTier[] = ['standard', 'performance', 'max'];

/** Coerce an unknown/legacy tier value to a known tier (defaults to standard). */
export function normalizeTier(tier: unknown): ResourceTier {
  return TIER_ORDER.find((id) => id === tier) ?? 'standard';
}

/** Localized display name for a tier. */
export function tierLabel(t: Translate, tier: ResourceTier): string {
  return t(`workspace.tier.${tier}`);
}

/** The sizes a picker shows, and what each one is when the server said. */
export interface TierOffer {
  tiers: ResourceTier[];
  /** Empty when unknown, and a card then shows its name alone. */
  specs: readonly ComputerTierSpec[];
}

/**
 * The tiers `provider` offers, smallest first, with `current` kept in so the
 * tier a computer is on always renders. Unknown falls back to the base ladder.
 */
export function offeredTiers(
  provider: ComputerProviderOption | null | undefined,
  current: ResourceTier | null = null,
): TierOffer {
  const specs = provider?.tiers;
  return {
    tiers: TIER_ORDER.filter((id) =>
      id === current || (specs ? specs.some((s) => s.tier === id) : BASE_TIERS.includes(id))),
    specs: specs ?? [],
  };
}

/** A tier's count quota. Only elevated tiers carry one, and only in platform mode. */
export function tierCapacity(
  quota: WorkspaceQuota | null | undefined,
  tier: ResourceTier,
): WorkspaceCapacity | null {
  if (tier === 'performance') return quota?.performance ?? null;
  if (tier === 'max') return quota?.max ?? null;
  if (tier === 'xlarge') return quota?.xlarge ?? null;
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

/**
 * Whether only a bigger plan opens `tier`: the plan leaves it out and holds
 * nothing of its size or above. Each plan holds one computer at its top size,
 * and a size it leaves out below that top no upgrade opens either, so a Max
 * plan is never told to upgrade for a Medium.
 */
export function needsBiggerPlan(quota: WorkspaceQuota | null | undefined, tier: ResourceTier): boolean {
  if (tierBlockReason(tierCapacity(quota, tier)) !== 'not_on_plan') return false;
  return TIER_ORDER.slice(TIER_ORDER.indexOf(tier)).every((id) => (tierCapacity(quota, id)?.limit ?? 0) === 0);
}

interface TierRadioGroupProps {
  /** The sizes on offer, from {@link offeredTiers}. */
  offer: TierOffer;
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
 * The tier picker, three across where the space allows and two by two for
 * four, which three across would leave one short. In platform mode
 * each elevated tier shows its remaining count quota and goes unselectable
 * when the plan excludes it or its quota is spent.
 */
export function TierRadioGroup({
  offer,
  value,
  onChange,
  label,
  quota,
  currentTier = null,
}: TierRadioGroupProps) {
  const { t } = useTranslation();
  const options: RadioCardOption<ResourceTier>[] = offer.tiers.map((id) => {
    const capacity = tierCapacity(quota, id);
    return {
      value: id,
      title: tierLabel(t, id),
      facts: tierFacts(t, offer.specs.find((s) => s.tier === id)),
      // The current tier takes no new slot, so what is left of the quota
      // says nothing about it.
      note: id === currentTier
        ? t('computer.spec.current', 'Current')
        : isPlatformMode && capacity ? capacityNote(t, capacity) : null,
      disabled: id !== currentTier && isTierBlocked(capacity),
    };
  });
  return (
    <RadioCardGroup
      options={options}
      value={value}
      onChange={onChange}
      label={label}
      columns={options.length === 4 ? 2 : 3}
    />
  );
}

/**
 * vCPU, RAM and disk, from the provider's config rather than the catalogs,
 * so the card says what the machine will be. None while that is unknown:
 * a name with no numbers beats numbers that may be wrong.
 */
function tierFacts(t: Translate, spec: ComputerTierSpec | undefined): string[] | undefined {
  if (!spec) return undefined;
  return [
    t('workspace.tierFact.cpu', '{{count}} vCPU', { count: spec.cpu }),
    t('workspace.tierFact.memory', '{{count}} GB RAM', { count: spec.memory_gib }),
    t('workspace.tierFact.disk', '{{count}} GB disk', { count: spec.disk_gib }),
  ];
}

function capacityNote(t: Translate, capacity: WorkspaceCapacity): string {
  if (capacity.limit < 0) return t('workspace.quotaUnlimited', 'Unlimited');
  if (capacity.limit === 0) return t('workspace.quotaNotOnPlan', 'Not on your plan');
  return t('workspace.quotaRemaining', '{{remaining}} of {{limit}} left', {
    remaining: Math.max(0, capacity.limit - capacity.used),
    limit: capacity.limit,
  });
}
