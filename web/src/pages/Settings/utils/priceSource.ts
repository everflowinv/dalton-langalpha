import type { DaltonStatusTone } from '@/components/dalton/primitives';

export type PriceSourceKind = 'official' | 'estimated' | 'third_party';

export interface PriceSourceMeta {
  source_kind?: PriceSourceKind | string | null;
  estimated?: boolean | null;
}

export interface PriceSourceInput {
  pricing_meta?: PriceSourceMeta | null;
  notional_pricing?: PriceSourceMeta | null;
  dalton_meta?: {
    billing?: string | null;
    notional_pricing?: PriceSourceMeta | null;
  } | null;
  billing?: string | null;
}

/**
 * Resolve the normalized price source badge kind from a single pricing metadata object.
 * `estimated: true` always takes precedence over `source_kind`.
 */
export function resolvePriceSourceKindFromMeta(
  meta: PriceSourceMeta | null | undefined,
): PriceSourceKind | null {
  if (!meta) return null;
  if (meta.estimated) return 'estimated';
  if (
    meta.source_kind === 'official' ||
    meta.source_kind === 'estimated' ||
    meta.source_kind === 'third_party'
  ) {
    return meta.source_kind;
  }
  return null;
}

/**
 * Resolve the display price source badge kind for a catalog model item or public
 * `/api/v1/models` metadata entry. Returns `null` when no source information exists.
 */
export function resolvePriceSourceKind(
  model: PriceSourceInput | null | undefined,
): PriceSourceKind | null {
  if (!model) return null;
  const isSub =
    model.dalton_meta?.billing === 'subscription' ||
    model.billing === 'subscription';
  if (isSub) {
    return resolvePriceSourceKindFromMeta(
      model.dalton_meta?.notional_pricing ?? model.notional_pricing,
    );
  }
  return resolvePriceSourceKindFromMeta(
    model.pricing_meta ?? model.notional_pricing,
  );
}

/** Semantic status tone for each price source kind. */
export function priceSourceBadgeTone(kind: PriceSourceKind): DaltonStatusTone {
  if (kind === 'official') return 'success';
  if (kind === 'estimated') return 'warning';
  return 'accent';
}

/**
 * Default-namespace i18n key for each price source kind so badges can be rendered
 * safely in both Settings panels and the chat model picker without cross-namespace
 * lazy-loading dependencies.
 */
export function priceSourceLabelKey(kind: PriceSourceKind): string {
  if (kind === 'official') return 'dalton.priceSource.official';
  if (kind === 'estimated') return 'dalton.priceSource.estimated';
  return 'dalton.priceSource.thirdParty';
}
