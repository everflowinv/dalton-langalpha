import { useMemo } from 'react';
import { useModels } from './useModels';
import type { ProviderCatalogEntry } from '@/components/model/types';
import {
  groupProviders,
  optionsOf,
  type KeyOption,
  type ProviderGroup,
} from '@/components/modelAccess/providerOptions';

/** Where a saved key sits among its provider's plans and regions. */
export interface KeyPlace {
  group: ProviderGroup;
  option: KeyOption;
}

const NO_ENTRIES: ProviderCatalogEntry[] = [];

/**
 * The deployment's provider catalog, read the ways every surface reads it:
 * the key providers folded by group, the local servers, and the entry, place
 * and key option a saved provider belongs to (a region is a variant inside
 * its plan's entry, not an entry of its own).
 *
 * A local server is one by its access type: `dynamic_models` says the server
 * lists its own models, which an API provider could also do, and such a
 * provider belongs with the keys, not in both lists.
 */
export function useProviderCatalog() {
  const { models, isLoading } = useModels();
  const catalog = models?.provider_catalog ?? NO_ENTRIES;
  return useMemo(() => {
    const keyGroups = groupProviders(catalog.filter(
      (e) => (e.access_type === 'api_key' || e.access_type === 'coding_plan') && e.byok_eligible !== false,
    ));
    const localEntries = catalog.filter((e) => e.access_type === 'local');

    const entryFor = (provider: string): ProviderCatalogEntry | null =>
      catalog.find((e) => e.provider === provider)
      ?? catalog.find((e) => e.region_variants?.some((v) => v.provider === provider))
      ?? null;

    const placeOf = (provider: string): KeyPlace | undefined => {
      for (const group of keyGroups) {
        const option = group.options.find((o) => o.provider === provider);
        if (option) return { group, option };
      }
      return undefined;
    };

    /** The key option saved under `provider`, whichever list its entry is in. */
    const optionFor = (provider: string): KeyOption | null => {
      const entry = entryFor(provider);
      return entry ? optionsOf(entry).find((o) => o.provider === provider) ?? null : null;
    };

    return { catalog, keyGroups, localEntries, entryFor, placeOf, optionFor, isLoading };
  }, [catalog, isLoading]);
}
