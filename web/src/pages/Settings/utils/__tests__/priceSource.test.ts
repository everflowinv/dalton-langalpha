import { describe, expect, it } from 'vitest';
import { withCustomModels } from '@/hooks/useFilteredModels';
import {
  priceSourceBadgeTone,
  priceSourceLabelKey,
  resolvePriceSourceKind,
  resolvePriceSourceKindFromMeta,
} from '../priceSource';

describe('priceSource shared resolver', () => {
  it('resolves official, estimated, and third_party from cash pricing_meta', () => {
    expect(resolvePriceSourceKind({ pricing_meta: { source_kind: 'official', estimated: false } })).toBe('official');
    expect(resolvePriceSourceKind({ pricing_meta: { source_kind: 'third_party', estimated: false } })).toBe('third_party');
    expect(resolvePriceSourceKind({ pricing_meta: { source_kind: 'estimated' } })).toBe('estimated');
  });

  it('gives estimated: true precedence over source_kind', () => {
    expect(resolvePriceSourceKind({ pricing_meta: { source_kind: 'official', estimated: true } })).toBe('estimated');
    expect(resolvePriceSourceKind({ pricing_meta: { source_kind: 'third_party', estimated: true } })).toBe('estimated');
    expect(resolvePriceSourceKind({ notional_pricing: { estimated: true } })).toBe('estimated');
  });

  it('uses dalton_meta.notional_pricing for subscription models and never falls back to cash pricing_meta', () => {
    expect(
      resolvePriceSourceKind({
        dalton_meta: {
          billing: 'subscription',
          notional_pricing: { source_kind: 'third_party', estimated: false },
        },
        pricing_meta: { source_kind: 'official' },
      }),
    ).toBe('third_party');

    expect(
      resolvePriceSourceKind({
        dalton_meta: {
          billing: 'subscription',
          notional_pricing: null,
        },
        pricing_meta: { source_kind: 'official' },
      }),
    ).toBeNull();
  });

  it('resolves public /api/v1/models notional_pricing without billing flag', () => {
    expect(resolvePriceSourceKind({ notional_pricing: { estimated: true } })).toBe('estimated');
    expect(resolvePriceSourceKind({ notional_pricing: { source_kind: 'official', estimated: false } })).toBe('official');
  });

  it('returns null when source metadata is missing, empty, or invalid', () => {
    expect(resolvePriceSourceKind(null)).toBeNull();
    expect(resolvePriceSourceKind(undefined)).toBeNull();
    expect(resolvePriceSourceKind({})).toBeNull();
    expect(resolvePriceSourceKind({ pricing_meta: { estimated: false } })).toBeNull();
    expect(resolvePriceSourceKind({ pricing_meta: { source_kind: 'unknown' } })).toBeNull();
    expect(resolvePriceSourceKindFromMeta(undefined)).toBeNull();
  });

  it('maps each kind to its semantic tone and default-namespace label key', () => {
    expect(priceSourceBadgeTone('official')).toBe('success');
    expect(priceSourceBadgeTone('estimated')).toBe('warning');
    expect(priceSourceBadgeTone('third_party')).toBe('accent');

    expect(priceSourceLabelKey('official')).toBe('dalton.priceSource.official');
    expect(priceSourceLabelKey('estimated')).toBe('dalton.priceSource.estimated');
    expect(priceSourceLabelKey('third_party')).toBe('dalton.priceSource.thirdParty');
  });

  it('withCustomModels preserves price source metadata only when shadowing the same provider and model_id', () => {
    const rawMetadata = {
      'model-shared': {
        provider: 'openai',
        model_id: 'model-shared',
        pricing: { input: 2, output: 10 },
        pricing_meta: { source_kind: 'official' as const, estimated: false },
      },
    };
    const same = withCustomModels(
      rawMetadata,
      [{ name: 'model-shared', model_id: 'model-shared', provider: 'openai' }],
      () => undefined,
    )['model-shared'];
    const otherProv = withCustomModels(
      rawMetadata,
      [{ name: 'model-shared', model_id: 'model-shared', provider: 'custom-gw' }],
      () => undefined,
    )['model-shared'];

    expect(same.pricing_meta).toEqual({ source_kind: 'official', estimated: false });
    expect(otherProv.pricing_meta).toBeUndefined();
  });
});
