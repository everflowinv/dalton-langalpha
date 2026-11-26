/**
 * Making a model just connected in setup the account's default: connecting is
 * what picks the default, and the default step then opens on it.
 */
import { useIsFetching, useQueryClient } from '@tanstack/react-query';
import type { AvailableModels } from '@/api/model';
import { useAllModels } from '@/hooks/useAllModels';
import { useConfiguredProviders } from '@/hooks/useConfiguredProviders';
import { queryKeys } from '@/lib/queryKeys';
import { useDefaultModel } from '@/hooks/useSlotDefaultModels';
import { useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import { CLAUDE_CODE, claudeCodeStartModel } from '@/lib/claudeCode';
import { splitPreferenceWrite } from '@/lib/modelPreferences';
import { listedFor } from '@/lib/connectionModels';
import { useProviderCatalog } from '@/hooks/useProviderCatalog';
import type { ConnectionOf } from '@/lib/providerCatalog';
import type { CustomModelEntry } from '@/components/model/types';
// DALTON-HOOK(dalton-setup): never auto-save a default outside the 5 curated candidates in Dalton
// DALTON-HOOK(standalone-core): unlock BYOK/custom connected default in Core profile when curated 5 are absent
import { isDaltonBuild } from '@/config/brand';
import { applyDaltonProviderToolProbes, pickDaltonConnectedDefault } from '@/config/daltonSetup';
import { strongestModel, type RatedMetadata } from './defaultChoices';

/** The model a provider just connected starts on, the one the default step
 *  recommends: for Claude Code, a Claude Code Default already saved, else its
 *  own pick (`claudeCodeStartModel`); for any other, the strongest
 *  (`strongestModel`), else the first it lists. Null until its models are
 *  listed. */
export function connectedModel(
  provider: string,
  models: Record<string, { models?: string[] }>,
  metadata: RatedMetadata,
  accountDefault: string | null,
  customModels: readonly CustomModelEntry[],
  connectionOf: ConnectionOf,
  daltonProfile?: string | null,
  daltonSetupCandidates?: readonly string[] | null,
): string | null {
  const offered = listedFor(provider, models, metadata, customModels, connectionOf);
  if (provider === CLAUDE_CODE) return claudeCodeStartModel(offered, metadata, accountDefault);
  // DALTON-HOOK(dalton-setup): restrict auto-saved connected default to curated candidates
  // DALTON-HOOK(standalone-core): Core profile falls back to connected tool-calling models
  // DALTON-HOOK(team-model-catalog): pass version-aligned `dalton_setup_candidates`
  if (isDaltonBuild) {
    return pickDaltonConnectedDefault(offered, metadata, daltonProfile, daltonSetupCandidates);
  }
  return strongestModel(offered, metadata)?.model ?? offered[0] ?? null;
}

export function useSaveConnectedDefault() {
  const { models, metadata, customModels, isLoading, listedProviders, rawApiResponse } = useAllModels();
  const { providers, configuredSet } = useConfiguredProviders();
  const { connectionOf } = useProviderCatalog();
  const accountDefault = useDefaultModel();
  // DALTON-HOOK(standalone-core): merge Core recorded tool-calling probe verdicts into metadata
  const effectiveMetadata = isDaltonBuild
    ? applyDaltonProviderToolProbes(metadata, providers, customModels)
    : metadata;
  const update = useUpdatePreferences();
  const queryClient = useQueryClient();
  // A list read ending re-renders the reader, whose `settled` reads it, even
  // where its answer is unchanged.
  useIsFetching({ queryKey: queryKeys.providerModels.all });
  // The catalog offers what a credential's own list leaves in only once that
  // list was read, fresh and in full: one still loading or being read again,
  // one that failed, or one that fell back to the manifest leaves in models
  // it may not serve. A credential with no such list (a provider of the
  // user's own, a local server) has none to vouch for a model.
  const listState = (provider: string) =>
    queryClient.getQueryState<AvailableModels>(queryKeys.providerModels.available(provider, ''));
  const listRead = (provider: string) => {
    const state = listState(provider);
    return state?.status === 'success' && state.fetchStatus === 'idle' && !state.isInvalidated && state.data?.status === 'ok';
  };
  /** Whether what `provider` starts on is decided, a model or none: its
   *  models listed and, for any but Claude Code, the connection known and
   *  its own list, where it has one, read to an end, an answer or a failure. */
  const settled = (provider: string) => {
    if (isLoading) return false;
    if (provider === CLAUDE_CODE) return true;
    if (!configuredSet.has(provider)) return false;
    if (!listedProviders.includes(provider)) return true;
    const state = listState(provider);
    return !!state && state.fetchStatus === 'idle' && (state.status === 'error' || (state.status === 'success' && !state.isInvalidated));
  };
  /** The model `provider` would start on, null until that is decided
   *  (`settled`) and, for any but Claude Code, without its own list read
   *  (`listRead`). */
  // DALTON-HOOK(team-model-catalog): forward server `dalton_setup_candidates` into connectedModel
  const modelFor = (provider: string) =>
    !settled(provider) || (provider !== CLAUDE_CODE && !listRead(provider))
      ? null
      : connectedModel(
          provider,
          models,
          effectiveMetadata,
          accountDefault,
          customModels,
          connectionOf,
          rawApiResponse?.dalton_profile,
          rawApiResponse?.dalton_setup_candidates,
        );
  /** Resolves whether a default was saved. */
  const save = async (model: string): Promise<boolean> => {
    try {
      await update.mutateAsync(splitPreferenceWrite({ preferred_model: model }));
      return true;
    } catch {
      return false;
    }
  };
  return { modelFor, settled, save, saving: update.isPending };
}
