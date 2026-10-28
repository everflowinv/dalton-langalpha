/**
 * Writing `custom_models`, which every provider's models share: a write
 * replaces one provider's entries and passes every other provider's through
 * untouched, so no writer can drop models it does not own.
 */
import type { CustomModelEntry } from '@/components/model/types';

export function withoutProvider(all: readonly CustomModelEntry[], provider: string): CustomModelEntry[] {
  return all.filter((m) => m.provider !== provider);
}

/** `provider`'s entries become `next`; the rest keep their order. */
export function replaceProviderModels(
  all: readonly CustomModelEntry[],
  provider: string,
  next: readonly CustomModelEntry[],
): CustomModelEntry[] {
  return [...withoutProvider(all, provider), ...next];
}

/**
 * Adds `batch` to `provider`'s entries, the batch winning on a name both
 * hold. Setting up a provider again under a name it already has keeps the
 * models added to it since (the picker's Add model) rather than wiping them.
 */
export function mergeProviderModels(
  all: readonly CustomModelEntry[],
  provider: string,
  batch: readonly CustomModelEntry[],
): CustomModelEntry[] {
  const names = new Set(batch.map((m) => m.name));
  const kept = all.filter((m) => m.provider === provider && !names.has(m.name));
  return replaceProviderModels(all, provider, [...kept, ...batch]);
}
