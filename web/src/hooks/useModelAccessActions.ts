import { useCallback } from 'react';
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { disconnectOAuth, isSubscriptionProvider, subscriptionKey } from '@/api/oauth';
import { deleteUserApiKey } from '@/api/model';
import { useReadStoredPreferences } from './useStoredPreferences';
import { useUpdatePreferences } from './useUpdatePreferences';
import { useModels } from './useModels';
import { useConfiguredProviders, type ConfiguredProvider } from './useConfiguredProviders';
import { modelPrefs, pruneModelReferences, splitPreferenceWrite, type PreferencePatch } from '@/lib/modelPreferences';
import { withoutProvider } from '@/lib/customModels';
import { modelsLeavingWith } from '@/lib/connectionModels';

/**
 * Everything a new, changed or removed connection makes stale. Awaited, so a
 * caller that moves on next reads the new connection. The models catalog
 * (`queryKeys.models.all`) is not among it: the server builds that from the
 * deployment's manifest alone, the same for every user, and a user's own
 * models and providers reach the client through their preferences.
 */
export async function refreshAfterAccessChange(queryClient: QueryClient, provider: string): Promise<void> {
  const oauthKey = isSubscriptionProvider(provider) ? queryKeys.oauth.status(subscriptionKey(provider)) : null;
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: queryKeys.user.me() }),
    queryClient.invalidateQueries({ queryKey: queryKeys.user.apiKeys() }),
    queryClient.invalidateQueries({ queryKey: queryKeys.platform.models() }),
    // The server drops its cached list on every change; ours follows.
    queryClient.invalidateQueries({ queryKey: queryKeys.providerModels.all }),
    ...(oauthKey ? [queryClient.invalidateQueries({ queryKey: oauthKey })] : []),
  ]);
}

export function useRefreshAfterAccessChange() {
  const queryClient = useQueryClient();
  return useCallback((provider: string) => refreshAfterAccessChange(queryClient, provider), [queryClient]);
}

/**
 * Disconnects a subscription or removes a key, then takes the provider's
 * models out of everything that names them (defaults, fallbacks, the picked
 * list, per-model settings), except a model another connection still serves.
 * Rejects on failure, so the caller can say so. Once the connection is gone
 * the views refresh even if the cleanup fails, so none shows it still there.
 */
export function useRemoveAccess() {
  const queryClient = useQueryClient();
  const readStored = useReadStoredPreferences();
  const updatePreferences = useUpdatePreferences();
  const { models: modelsData } = useModels();
  const { configuredSet } = useConfiguredProviders();

  const pruneReferences = useCallback(async (name: string) => {
    // From what is stored now, so a change another tab saved since is kept.
    const preferences = await readStored();
    const modelPref = modelPrefs(preferences);
    const customProviders = modelPref.custom_providers ?? [];
    const customModels = modelPref.custom_models ?? [];
    const remainingProviders = customProviders.filter((cp) => cp.name !== name);
    const remainingModels = withoutProvider(customModels, name);
    const removed = modelsLeavingWith(name, {
      customModels,
      manifest: modelsData?.model_metadata ?? {},
      connected: configuredSet,
      keyCandidates: modelsData?.key_candidates,
    });
    // One flat patch: splitPreferenceWrite routes each key to its column. A
    // key left as it was stays out of it.
    const patch: PreferencePatch = {
      ...(remainingProviders.length !== customProviders.length
        ? { custom_providers: remainingProviders.length > 0 ? remainingProviders : null }
        : {}),
      ...(remainingModels.length !== customModels.length
        ? { custom_models: remainingModels.length > 0 ? remainingModels : null }
        : {}),
      ...pruneModelReferences(preferences, (m) => removed.has(m)),
    };

    if (Object.keys(patch).length > 0) await updatePreferences.mutateAsync(splitPreferenceWrite(patch));
  }, [readStored, updatePreferences, modelsData, configuredSet]);

  return useCallback(async (provider: ConfiguredProvider) => {
    const name = provider.provider;
    if (isSubscriptionProvider(name)) await disconnectOAuth(name);
    else await deleteUserApiKey(name);
    await pruneReferences(name).finally(() => refreshAfterAccessChange(queryClient, name));
  }, [pruneReferences, queryClient]);
}
