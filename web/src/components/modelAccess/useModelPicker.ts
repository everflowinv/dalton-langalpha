import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useAllModels } from '@/hooks/useAllModels';
import { useModels } from '@/hooks/useModels';
import { usePreferences } from '@/hooks/usePreferences';
import { useReadStoredPreferences } from '@/hooks/useStoredPreferences';
import { useConfiguredProviders } from '@/hooks/useConfiguredProviders';
import { useProviderCatalog } from '@/hooks/useProviderCatalog';
import { useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import { useAvailableModels, useLocalProviderModels, useRefreshAvailableModels } from '@/hooks/useProviderModels';
import { useDebouncedValue } from '@/lib/useDebouncedValue';
import { modelPrefs, pruneModelReferences, splitPreferenceWrite, starredModels } from '@/lib/modelPreferences';
import { replaceProviderModels } from '@/lib/customModels';
import { connectionBuiltIns, seedPicks, servedElsewhere, takenElsewhere } from '@/lib/connectionModels';
import { apiErrorDetail } from '@/api/errors';
import type { CustomModelEntry } from '@/components/model/types';
import type { ListedModel } from '@/api/model';
import { planIncludes, type ModelMetadataEntry } from '@/hooks/useFilteredModels';
import { slugifyModelName } from './slugifyModelName';

/** The name a custom model may take (`CUSTOM_MODEL_NAME_RE` on the server). */
const NAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,62}$/;
const MODALITIES = new Set(['text', 'image', 'pdf']);

/** What a listed model takes beyond text, as an entry saves it; null for text
 *  alone, which an entry says by naming none. */
function listedModalities(listed: ListedModel): string[] | null {
  // OpenRouter's `file` is a PDF, as the server reads it.
  const modalities = [...new Set((listed.input_modalities ?? []).map((m) => (m === 'file' ? 'pdf' : m)))]
    .filter((m) => MODALITIES.has(m));
  return modalities.some((m) => m !== 'text') ? ['text', ...modalities.filter((m) => m !== 'text')] : null;
}

/** The custom-models entry an untuned listed model is saved as. */
export function entryForListed(
  listed: ListedModel,
  provider: string,
  taken: ReadonlySet<string>,
): CustomModelEntry {
  // The provider's own id where the server accepts it as a name and it names
  // no other model; prefixed with the provider otherwise, so it never shadows
  // a built-in of the same name, numbered on where that is taken too.
  let name = NAME_RE.test(listed.id) && !taken.has(listed.id) ? listed.id : slugifyModelName(`${provider}/${listed.id}`);
  if (!name) name = slugifyModelName(listed.id) || 'model';
  for (let n = 2, stem = name; taken.has(name); n++) name = `${stem.slice(0, 62 - String(n).length)}-${n}`;
  const entry: CustomModelEntry = { name, model_id: listed.id, provider };
  const modalities = listedModalities(listed);
  if (modalities) entry.input_modalities = modalities;
  if (typeof listed.context_window === 'number' && listed.context_window > 0) entry.context = listed.context_window;
  if (listed.reasoning) entry.reasoning = listed.reasoning;
  return entry;
}

/**
 * Saved entries with the levels, window and inputs the provider's list now
 * gives each, so a pick follows the provider: one saved before lists carried
 * levels gets them on the next save, and a window lowered or an input dropped
 * stops being sent as the model's. A field the list leaves out, and an entry
 * it does not name, stay as saved.
 */
export function withListedSpecs(entries: CustomModelEntry[], listed: readonly ListedModel[]): CustomModelEntry[] {
  const byId = new Map(listed.map((m) => [m.id, m]));
  return entries.map((cm) => {
    const now = byId.get(cm.model_id);
    if (!now) return cm;
    const context = typeof now.context_window === 'number' && now.context_window > 0 ? now.context_window : null;
    const next: CustomModelEntry = {
      ...cm,
      ...(context ? { context } : {}),
      ...(now.reasoning ? { reasoning: now.reasoning } : {}),
    };
    if (Array.isArray(now.input_modalities)) {
      const modalities = listedModalities(now);
      if (modalities) next.input_modalities = modalities;
      else delete next.input_modalities;
    }
    return next;
  });
}

const NO_MODELS: Record<string, ModelMetadataEntry> = {};

/**
 * Which models a provider's connection offers, and which of them the user has
 * picked: the provider's own list (Ready to use, then the untuned rest), the
 * manifest's when the list could not be read, a local server's own list, and
 * the user's own models for it. `save` writes the picks as the configured list
 * (`starred_models`) and any new custom models, and takes a model just
 * unpicked out of everything that names it, unless another connection still
 * serves it.
 *
 * `justConnected`: the connection was just made, so with nothing of its own on
 * the list yet the picker starts on every model it offers. Anywhere else it
 * opens on what is saved, which may be nothing.
 */
export function useModelPicker({
  provider,
  brandKey,
  justConnected = false,
}: {
  provider: string;
  brandKey?: string;
  justConnected?: boolean;
}) {
  const { t } = useTranslation();
  const brand = brandKey ?? provider;
  const { metadata, isLoading: modelsLoading } = useAllModels();
  const { models: modelsData } = useModels();
  const { providers: configured, configuredSet } = useConfiguredProviders();
  const { entryFor } = useProviderCatalog();
  const { preferences, isLoaded: prefsLoaded } = usePreferences();
  const readStored = useReadStoredPreferences();
  const updatePreferences = useUpdatePreferences();

  const entry = entryFor(provider);
  const isCustomProvider = (modelPrefs(preferences).custom_providers ?? []).some((cp) => cp.name === provider);
  const isLocal = entry?.dynamic_models === true;
  const searchFirst = entry?.sdk === 'openrouter' || provider === 'openrouter';
  const listable = !!entry && !isLocal && !isCustomProvider;

  const [query, setQuery] = useState('');
  const debounced = useDebouncedValue(query.trim(), 300);
  const base = useAvailableModels(listable ? provider : null);
  const searched = useAvailableModels(listable && searchFirst && debounced ? provider : null, { q: debounced });
  const local = useLocalProviderModels(isLocal ? provider : null);
  const refreshList = useRefreshAvailableModels();
  const [refreshing, setRefreshing] = useState(false);

  const allCustom = useMemo(() => modelPrefs(preferences).custom_models ?? [], [preferences]);
  // The manifest's own rows, before the user's entries are merged over them:
  // a saved entry named after one of these reaches that model through this
  // provider and keeps its tuning, so it is ready to use, not the user's own.
  const manifestMeta = modelsData?.model_metadata ?? NO_MODELS;
  // The manifest's models for this provider: a variant without entries of
  // its own (a coding plan) shares its brand's.
  const builtIn = useMemo<string[]>(
    () => connectionBuiltIns(provider, brand, manifestMeta, allCustom),
    [provider, brand, manifestMeta, allCustom],
  );

  // The user's own models under this exact provider, by name (what every
  // preference stores).
  const savedCustom = useMemo(() => allCustom.filter((cm) => cm.provider === provider), [allCustom, provider]);
  const savedCustomNames = useMemo(() => savedCustom.map((cm) => cm.name || cm.model_id), [savedCustom]);
  const savedTuned = useMemo(
    () => savedCustomNames.filter((m) => !!manifestMeta[m] && !builtIn.includes(m)),
    [savedCustomNames, manifestMeta, builtIn],
  );

  const list = base.data && base.data.provider === provider ? base.data : null;
  const listOk = list?.status === 'ok';
  const listFallback = list?.status === 'fallback';
  const listSettled = !listable || !base.isLoading;

  // A subscription's plan, which some of its models are limited to.
  const plan = configured.find((p) => p.provider === provider && p.access_type === 'oauth')?.plan_type;

  // Ready to use: the manifest models the list confirms, or every one when
  // there is no list to confirm them, less those the plan does not include.
  const ready = useMemo(() => {
    // A name a custom entry under another provider took routes there, so the
    // list's word for it is not this connection's to act on.
    const confirmed = (listOk ? list!.ready.filter((m) => metadata[m] && !takenElsewhere(m, provider, allCustom)) : builtIn)
      .filter((m) => planIncludes(metadata[m], plan));
    return [...confirmed, ...savedTuned.filter((m) => !confirmed.includes(m))];
  }, [listOk, list, builtIn, metadata, provider, allCustom, savedTuned, plan]);
  const missing = listOk ? list!.missing : [];

  // Picked models not saved yet (from the list, or added by hand), by the
  // name they will be saved under.
  const [pending, setPending] = useState<ReadonlyMap<string, CustomModelEntry>>(new Map());

  const savedIds = useMemo(() => new Set(savedCustom.map((cm) => cm.model_id)), [savedCustom]);
  const pendingIds = useMemo(() => new Set([...pending.values()].map((e) => e.model_id)), [pending]);
  /** The entry, saved or not, a listed model is already picked as. */
  const findByModelId = (id: string) =>
    savedCustom.find((cm) => cm.model_id === id) ?? [...pending.values()].find((e) => e.model_id === id);

  const others = useMemo<ListedModel[]>(() => {
    if (isLocal) return (local.data ?? []).map((m) => ({ id: m.id, display_name: m.name }));
    return list?.others ?? [];
  }, [isLocal, local.data, list]);
  const results = searchFirst && debounced && searched.data?.provider === provider ? searched.data.others : others;

  const yours = useMemo(
    () => [...new Set([...savedCustomNames, ...pending.keys()])].filter((m) => !ready.includes(m)),
    [savedCustomNames, pending, ready],
  );
  /** Every name this view decides about: what is saved under this provider,
   *  and its own manifest models. Picks outside it belong to other providers,
   *  a coding plan's brand models among them until picked here. */
  const owned = useMemo(
    () => [...new Set([
      ...[...builtIn, ...ready].filter((m) => manifestMeta[m]?.provider === provider),
      ...savedCustomNames,
      ...yours,
    ])],
    [builtIn, ready, manifestMeta, provider, savedCustomNames, yours],
  );

  const existingStarred = useMemo(() => starredModels(preferences), [preferences]);

  // Seeded once everything it reads has arrived: the picks already saved for
  // this provider, or for a connection just made every model it offers. Null
  // until the user changes something, so the seed follows the data until then.
  const [picked, setPicked] = useState<Set<string> | null>(null);
  // Loaded, not present: a new user has no preferences row yet (a 404 read as null).
  const inputsReady = !modelsLoading && prefsLoaded && listSettled && (!isLocal || !local.isLoading);
  const seed = useMemo(() => seedPicks({
    provider,
    candidates: [...ready, ...savedCustomNames],
    savedNames: savedCustomNames,
    starred: existingStarred,
    manifest: manifestMeta,
    justConnected,
  }), [provider, ready, savedCustomNames, existingStarred, manifestMeta, justConnected]);
  const selected = picked ?? seed;
  const initialized = picked !== null || inputsReady;
  const setSelected = (update: (prev: Set<string>) => Set<string>) => setPicked((prev) => update(prev ?? seed));

  const toggle = (name: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const addPending = (e: CustomModelEntry) => {
    setPending((prev) => new Map(prev).set(e.name, e));
    setSelected((prev) => new Set(prev).add(e.name));
  };

  /** Picks or unpicks an untuned listed model. */
  const toggleListed = (listed: ListedModel) => {
    const existing = findByModelId(listed.id);
    if (existing) toggle(existing.name);
    else addPending(entryForListed(listed, provider, new Set([...Object.keys(metadata), ...pending.keys()])));
  };
  const listedChecked = (listed: ListedModel) => {
    const existing = findByModelId(listed.id);
    return !!existing && selected.has(existing.name);
  };

  /** Drops an unsaved model, or unpicks a saved one (saving then removes it). */
  const removeYours = (name: string) => {
    setPending((prev) => {
      if (!prev.has(name)) return prev;
      const next = new Map(prev);
      next.delete(name);
      return next;
    });
    setSelected((prev) => {
      const next = new Set(prev);
      next.delete(name);
      return next;
    });
  };

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    setRefreshing(true);
    try {
      await refreshList(provider);
    } catch {
      // The list on screen stays; the server allows one refresh per 10s.
    }
    // Not a `finally`, which keeps the React Compiler off this hook.
    setRefreshing(false);
  };

  /** Writes the picks. Resolves true when saved. */
  const save = async (): Promise<boolean> => {
    setSaving(true);
    setError(null);
    const failed = (e: unknown) => {
      const detail = apiErrorDetail(e);
      setError(typeof detail === 'string' ? detail : t('modelAccess.models.saveFailed'));
      setSaving(false);
      return false;
    };
    // This provider's picks go onto what is stored now, so another
    // provider's models and stars saved from another tab are kept.
    let stored: Awaited<ReturnType<typeof readStored>>;
    try {
      stored = await readStored();
    } catch (e) {
      return failed(e);
    }
    const storedCustom = modelPrefs(stored).custom_models ?? [];
    // Unpicked here, and run by no other connection: a name another one still
    // serves keeps its place on the list and its settings.
    const ctx = {
      customModels: storedCustom, manifest: manifestMeta, connected: configuredSet, keyCandidates: modelsData?.key_candidates,
    };
    const unpicked = (m: string) => owned.includes(m) && !selected.has(m) && !servedElsewhere(m, provider, ctx);
    const fromOthers = starredModels(stored).filter((m) => !selected.has(m) && !unpicked(m));
    // A saved model stays while picked; unpicking one of the user's own removes it.
    const keptSaved = withListedSpecs(savedCustom.filter((cm) => selected.has(cm.name || cm.model_id)), others);
    const keptNames = new Set(keptSaved.map((cm) => cm.name || cm.model_id));
    const added = [...pending.values()].filter((e) => selected.has(e.name) && !keptNames.has(e.name));
    // A parent-catalog model picked under a variant (a coding plan) needs an
    // entry naming the variant, or its key would not be the one used.
    const variantPicks = [...selected]
      .filter((m) => builtIn.includes(m) && !keptNames.has(m))
      .filter((m) => {
        const own = metadata[m]?.provider;
        return !!own && own !== provider;
      })
      .map((m): CustomModelEntry => ({ name: m, model_id: manifestMeta[m]?.model_id ?? m, provider }));
    const custom = replaceProviderModels(storedCustom, provider, [...keptSaved, ...added, ...variantPicks]);
    try {
      await updatePreferences.mutateAsync(splitPreferenceWrite({
        ...pruneModelReferences(stored, unpicked),
        starred_models: [...fromOthers, ...selected],
        custom_models: custom.length > 0 ? custom : null,
      }));
    } catch (e) {
      return failed(e);
    }
    setPending(new Map());
    setSaving(false);
    return true;
  };

  return {
    provider,
    entry,
    metadata,
    manifestMeta,
    loading: !initialized,
    listing: listable && (base.isFetching || refreshing),
    listOk,
    listFallback,
    fallbackReason: listFallback ? list!.reason ?? null : null,
    listable,
    isLocal,
    localError: isLocal && local.isError,
    searchFirst,
    query,
    setQuery,
    searching: searchFirst && !!debounced && searched.isFetching,
    searchedFor: debounced,
    ready,
    missing,
    others,
    results,
    yours,
    savedIds,
    pendingIds,
    selected,
    toggle,
    toggleListed,
    listedChecked,
    addManual: addPending,
    removeYours,
    refresh,
    refreshing,
    save,
    saving,
    error,
  };
}

export type ModelPickerState = ReturnType<typeof useModelPicker>;
