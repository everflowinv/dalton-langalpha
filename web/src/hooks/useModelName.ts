import { useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getAvailableModels } from '@/api/model';
import { getModelDisplayName } from '@/components/ui/chat-input.helpers';
import type { ModelMetadataEntry } from '@/hooks/useFilteredModels';
import { payerLabel, payerOfBilling } from '@/lib/modelPayer';
import { CACHE_ONLY_META, queryKeys } from '@/lib/queryKeys';

/**
 * A model's name as the composer pill prints it, for a surface handed a
 * manifest key (a stream event, a saved preference). Read from the catalog
 * the composer already loaded rather than fetched: a shared link has no
 * composer and may have no session to fetch with, and there the key, tidied,
 * will do.
 */
export function useModelName(): (id: string) => string {
  const metadata = useModelMetadata();
  return useCallback((id: string) => getModelDisplayName(id, metadata), [metadata]);
}

/** The catalog's model entries, read the same way: undefined until the
 *  composer has loaded them. */
export function useModelMetadata(): Record<string, ModelMetadataEntry> | undefined {
  return useCachedModels().data?.model_metadata;
}

function useCachedModels() {
  return useQuery({
    queryKey: queryKeys.models.all,
    queryFn: getAvailableModels,
    enabled: false,
    meta: CACHE_ONLY_META,
  });
}

/** Who pays for a call on `model`, as its tag reads, from the `billing_type`
 *  a stream event reported; undefined where the event names no payer. The
 *  event's `provider`, the one the call went through, wins over the catalog's:
 *  a model the user added on a sign-in may share another provider's name. */
export function usePaidByLabel(): (
  paidBy: string | undefined, model: string, provider?: string,
) => string | undefined {
  const { t } = useTranslation();
  const { data } = useCachedModels();
  return useCallback((paidBy: string | undefined, model: string, provider?: string) => {
    const entry = data?.model_metadata?.[model];
    // The route's own access, where the event names it: a model the user added
    // on a local server is in no catalog entry, or in a built-in's it shadows.
    const route = provider ? data?.provider_catalog?.find((p) => p.provider === provider) : undefined;
    const payer = payerOfBilling(
      paidBy,
      provider ? { ...entry, provider, ...(route ? { access_type: route.access_type } : {}) } : entry,
    );
    return payer ? payerLabel(t, payer) : undefined;
  }, [t, data]);
}
