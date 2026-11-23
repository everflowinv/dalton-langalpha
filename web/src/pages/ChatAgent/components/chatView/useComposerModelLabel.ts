import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { getModelDisplayName } from '@/components/ui/chat-input.helpers';
import { useAllModels } from '@/hooks/useAllModels';
// DALTON-HOOK(antigravity-series): format Antigravity variants as Series · Effort to match the composer pill.
import { EMPTY_ANTIGRAVITY_SERIES_CATALOG, formatAntigravityVariantLabel } from '@/lib/antigravitySeries';

/** Prints a model the way the composer pill does, so a notice names it as
 *  the control it sits on. */
export function useComposerModelLabel(): (model: string) => string {
  const { t } = useTranslation();
  const { metadata, seriesCatalog, rawApiResponse, customModels } = useAllModels();
  const rawMetadata = rawApiResponse?.model_metadata ?? null;
  const catalog = seriesCatalog ?? EMPTY_ANTIGRAVITY_SERIES_CATALOG;
  return useCallback(
    (model: string) =>
      formatAntigravityVariantLabel(model, {
        seriesCatalog: catalog,
        rawMetadata,
        customModels: customModels ?? [],
        t,
        includeEffort: true,
      }) ?? getModelDisplayName(model, metadata),
    [catalog, rawMetadata, customModels, t, metadata],
  );
}
