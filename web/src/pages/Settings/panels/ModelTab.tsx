import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useMutationState } from '@tanstack/react-query';
import { Search, Pin } from 'lucide-react';
import { Link } from 'react-router';
import { Select } from '@/components/ui/select';
import { useSearchTierAllows } from '@/hooks/useSearchTierAllows';
import { usePreferences } from '@/hooks/usePreferences';
import { PREFERENCE_MUTATION_KEY, useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import { ModelTierConfig } from '@/components/model/ModelTierConfig';
import { ModelSelector } from '@/components/model/ModelSelector';
import { FallbackModelsPicker } from '@/components/model/FallbackModelsPicker';
import { PayerOrder } from '@/components/model/PayerOrder';
import { isPlatformMode } from '@/config/hostMode';
import { PerModelMatrix } from '@/components/model/PerModelMatrix';
import { AccountTuningDefaults } from '@/components/model/AccountTuningDefaults';
import { DefaultModelScopeChoice } from '@/components/model/DefaultModelScopeChoice';
import { useDefaultModelChange } from '@/hooks/useDefaultModelChange';
import { useAllModels } from '@/hooks/useAllModels';
import { useFavoriteModels } from '@/hooks/useFavoriteModels';
import { modelLabel, modelMatches } from '@/lib/modelLabel';
import { Trans, useTranslation } from 'react-i18next';
import { ModelAccessSection } from './ModelAccessSection';
// DALTON-HOOK(settings-section): import DaltonSection
import { DaltonSection } from './dalton/DaltonSection';
import { BACKGROUND_AUTO, backgroundDefaultChoice, modelPrefs, readDefaultModelScope, splitPreferenceWrite } from '@/lib/modelPreferences';
import { useModelSlotCopy } from '@/components/model/useModelSlotCopy';
import { paidByDeployment, paidByUser } from '@/lib/modelPayer';
import { PayerBadge } from '@/components/model/PayerBadge';
import type { ModelSlot, DefaultModelScope, PreferencePatch, PreferencesLike } from '@/lib/modelPreferences';

type ModelTabMode = 'simple' | 'advanced';

const MODE_STORAGE_KEY = 'settings:modelMode';

/** View state, not configuration — which is why it stays on the device rather
 *  than travelling with the account like everything else on this tab. */
function readStoredMode(): ModelTabMode {
  try {
    return localStorage.getItem(MODE_STORAGE_KEY) === 'advanced' ? 'advanced' : 'simple';
  } catch {
    return 'simple';
  }
}

function SectionHeading({ children }: { children: React.ReactNode }) {
  return (
    <h3
      className="text-xs font-semibold uppercase tracking-wider"
      style={{ color: 'var(--color-text-tertiary)' }}
    >
      {children}
    </h3>
  );
}

/** Model tab: default/Background model selection, starred models, per-model tuning,
 * account defaults, advanced model routing, search provider/depth, and
 * connected accounts.
 *
 * Every control writes a patch of the keys it owns and reads its value back
 * from the preferences cache. Nothing here is mirrored into local state: this
 * tab has no text input, so there is no keystroke to debounce, and a mirror
 * would only be a second copy that a failed write leaves disagreeing with the
 * settings a scheduled turn actually runs. */
export function ModelTab() {
  const tierAllows = useSearchTierAllows();
  const { preferences: prefsData } = usePreferences();
  const { mutate } = useUpdatePreferences();
  // DALTON-HOOK(settings-section): customModels is read to keep custom models out of the Dalton selectors
  const { models: visibleModels, metadata: modelMetadata, payers, systemDefaults: hookSystemDefaults, customModels, validModelNames, rawModels, isLoading: modelsLoading, compactionProfiles, searchProviders } = useAllModels();
  const { t } = useTranslation();
  const modelSlotCopy = useModelSlotCopy();

  /** Write the keys a control owns, each routed to its own column. */
  const write = useCallback(
    (patch: PreferencePatch) => mutate(splitPreferenceWrite(patch)),
    [mutate],
  );

  // The two defaults change through the flow the chat banner uses, which may
  // ask about the threads on the old default before it writes. ModelTierConfig
  // stays a plain controlled pair because Setup shares it, and a first-run
  // pick has no threads to ask about.
  const label = useCallback((model: string) => modelLabel(model, modelMetadata), [modelMetadata]);
  const {
    question: defaultQuestion,
    draft: draftDefaults,
    request: requestDefault,
    clear: clearDefault,
  } = useDefaultModelChange(label);

  const [mode, setMode] = useState<ModelTabMode>(readStoredMode);
  const isAdvanced = mode === 'advanced';
  const changeMode = (next: ModelTabMode) => {
    setMode(next);
    try { localStorage.setItem(MODE_STORAGE_KEY, next); } catch { /* private mode */ }
  };

  const [showModelPicker, setShowModelPicker] = useState(false);
  const [modelPickerSearch, setModelPickerSearch] = useState('');
  const modelPickerRef = useRef<HTMLDivElement>(null);

  // Model routing/tuning lives in model_preference; the search prefs below
  // stayed in other_preference.
  const mPref = modelPrefs(prefsData);
  const otherPref = (prefsData as PreferencesLike | null)?.other_preference ?? {};
  const favorites = useFavoriteModels();
  // Every name the catalog still carries, access aside. A fallback the user
  // temporarily cannot reach is still configured; one the manifest dropped is
  // not, and _resolve_fallback_clients skips it without saying so, leaving the
  // picker promising resilience that no longer exists.
  const catalogNames = useMemo(() => {
    const names = new Set<string>();
    for (const data of Object.values(rawModels)) {
      for (const m of data.models ?? []) names.add(m);
    }
    return names;
  }, [rawModels]);
  const storedFallbacks = mPref.fallback_models ?? hookSystemDefaults?.fallback_models ?? [];
  // An empty catalog is "not loaded yet", never "every model was removed".
  const fallbackModels = modelsLoading || catalogNames.size === 0
    ? storedFallbacks
    : storedFallbacks.filter((m) => catalogNames.has(m));
  // The deployment's list, not one the user saved, keeps a turn their plan or
  // key pays for off the deployment: of its models, only those a key or
  // sign-in of theirs runs take such a turn. One with no payer here, above
  // the plan or a local server, is skipped there too. Saving the list makes
  // them the user's.
  const deploymentOnlyFallbacks = new Set(
    mPref.fallback_models == null ? fallbackModels.filter((m) => !paidByUser(payers[m])) : [],
  );
  // Once saved, those models are the user's choice and catch every turn, the
  // ones their plan or key pays for included, which the list no longer shows.
  const savedCrossesPayer = mPref.fallback_models != null
    && fallbackModels.some((m) => paidByDeployment(payers[m]))
    && Object.values(payers).some(paidByUser);

  // While the question is open each select shows the model the answer would
  // write, and a cancel puts both back on the saved ones.
  const shownDefault = (slot: ModelSlot) => draftDefaults?.[slot]
    ?? (slot === 'background' ? backgroundDefaultChoice(mPref) : mPref.preferred_model ?? '');
  const setDefault = (slot: ModelSlot) => (model: string) => {
    if (model === BACKGROUND_AUTO) clearDefault(slot, 'deployment');
    else if (model) requestDefault({ [slot]: model });
    else clearDefault(slot);
  };
  // What each slot runs on Auto. A deployment with no Background model runs Background
  // on its primary one.
  const deploymentPrimary = hookSystemDefaults?.default_model || undefined;
  const deploymentBackground = hookSystemDefaults?.background_model || deploymentPrimary;
  const autoModels = {
    primary: deploymentPrimary && label(deploymentPrimary),
    background: deploymentBackground && label(deploymentBackground),
  };
  const defaultModelScope = readDefaultModelScope(prefsData);

  // DALTON-HOOK(settings-section): format "留空 = 使用系统默认（xxx）" from backend system_defaults
  const daltonDefaultHint = (val: string | string[] | number | undefined | null) => {
    if (Array.isArray(val)) {
      const items = val.map((m) => m.trim()).filter(Boolean);
      return items.length > 0
        ? t('settings.modelTuning.daltonEmptyDefault', { value: items.join(', ') })
        : t('settings.modelTuning.daltonEmptyDefaultUnset');
    }
    if (typeof val === 'number' && Number.isFinite(val)) {
      return t('settings.modelTuning.daltonEmptyDefault', { value: String(val) });
    }
    if (typeof val === 'string' && val.trim().length > 0) {
      return t('settings.modelTuning.daltonEmptyDefault', { value: val.trim() });
    }
    return t('settings.modelTuning.daltonEmptyDefaultUnset');
  };

  // DALTON-HOOK(settings-section): Dalton pickers only offer system catalog models (exclude custom_models)
  const daltonSystemModels = useMemo(() => {
    const customSet = new Set<string>();
    for (const cm of customModels ?? []) {
      if (cm?.name) customSet.add(cm.name);
    }
    for (const cm of (mPref.custom_models as Array<{ name?: string }> | undefined) ?? []) {
      if (cm?.name) customSet.add(cm.name);
    }
    if (customSet.size === 0) return visibleModels;
    const filtered: typeof visibleModels = {};
    for (const [provider, data] of Object.entries(visibleModels)) {
      const sysList = (data.models ?? []).filter((m) => !customSet.has(m));
      if (sysList.length > 0) {
        filtered[provider] = { ...data, models: sysList };
      }
    }
    return filtered;
  }, [visibleModels, customModels, mPref.custom_models]);

  // DALTON-HOOK(settings-section): string draft state for Dalton figure audit timeout (60..1800)
  const storedAuditTimeout = mPref.dalton_audit_timeout_s;
  const [auditTimeoutDraft, setAuditTimeoutDraft] = useState<string>(() =>
    storedAuditTimeout != null ? String(storedAuditTimeout) : '',
  );
  const [auditTimeoutError, setAuditTimeoutError] = useState<string | null>(null);
  useEffect(() => {
    setAuditTimeoutDraft(storedAuditTimeout != null ? String(storedAuditTimeout) : '');
    setAuditTimeoutError(null);
  }, [storedAuditTimeout]);

  const commitAuditTimeout = useCallback(
    (rawInput: string) => {
      const trimmed = rawInput.trim();
      if (!trimmed) {
        setAuditTimeoutError(null);
        if (storedAuditTimeout != null) {
          write({ dalton_audit_timeout_s: null });
        }
        return;
      }
      const parsed = Number(trimmed);
      if (!Number.isInteger(parsed) || parsed < 60 || parsed > 1800) {
        setAuditTimeoutError('数字核验超时必须在 60–1800 秒之间');
        return;
      }
      setAuditTimeoutError(null);
      if (parsed !== storedAuditTimeout) {
        write({ dalton_audit_timeout_s: parsed });
      }
    },
    [storedAuditTimeout, write],
  );

  // Close starred-model picker on click outside
  useEffect(() => {
    if (!showModelPicker) return;
    const handler = (e: MouseEvent) => {
      if (modelPickerRef.current && !modelPickerRef.current.contains(e.target as Node)) {
        setShowModelPicker(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [showModelPicker]);

  const providerOptions = Object.entries(searchProviders ?? {});
  const canCustomizeSearchProvider = providerOptions.some(([, p]) => tierAllows(p.min_tier));
  // A stored provider the catalog no longer declares reads as Default.
  const rawSearchProvider = otherPref.search_provider;
  const searchProvider =
    typeof rawSearchProvider === 'string' && searchProviders?.[rawSearchProvider]
      ? rawSearchProvider
      : '';
  // Depth select renders only for providers declaring more than one level;
  // options mirror the manifest's ordered array verbatim. Depth levels are
  // provider-scoped, so a stored level the chosen provider does not declare
  // reads as Default too.
  const selectedProviderDepths = searchProvider
    ? searchProviders?.[searchProvider]?.depths ?? []
    : [];
  const depthOptions = selectedProviderDepths.length > 1 ? selectedProviderDepths : [];
  const canCustomizeSearchDepth = depthOptions.some(d => tierAllows(d.min_tier));
  const rawSearchDepth = otherPref.search_depth;
  const searchDepth =
    typeof rawSearchDepth === 'string' && depthOptions.some(d => d.name === rawSearchDepth)
      ? rawSearchDepth
      : '';

  // Every control on this tab writes through the same mutation, the ones the
  // child components own included, so the footer watches the write rather than
  // any single caller's copy of it.
  const saveStatuses = useMutationState({
    filters: { mutationKey: PREFERENCE_MUTATION_KEY },
    select: (m) => m.state.status,
  });
  const saveStatus = saveStatuses.includes('pending') ? 'pending' : saveStatuses.at(-1) ?? 'idle';

  return (
      <div className="space-y-6">
        {/* Connections first: every pick below is among the models they reach. */}
        <div className="flex flex-col gap-3">
          <SectionHeading>{t('modelAccess.title')}</SectionHeading>
          <ModelAccessSection />
        </div>

        {/* Simple hides everything a first-time user has no reason to touch;
            Advanced adds per-model tuning, account defaults and routing. */}
        <div className="flex items-center justify-between gap-4">
          <SectionHeading>{t('settings.modelTuning.sectionModels')}</SectionHeading>
          <div
            role="radiogroup"
            aria-label={t('settings.modelTuning.detailLevel')}
            className="inline-flex rounded-md p-0.5"
            style={{ background: 'var(--color-bg-surface)', border: '1px solid var(--color-border-muted)' }}
          >
            {(['simple', 'advanced'] as const).map((value) => (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={mode === value}
                onClick={() => changeMode(value)}
                className="rounded px-2.5 py-1 text-xs font-medium transition-colors"
                style={{
                  background: mode === value ? 'var(--color-bg-elevated)' : 'transparent',
                  color: mode === value ? 'var(--color-text-primary)' : 'var(--color-text-tertiary)',
                }}
              >
                {t(value === 'simple' ? 'settings.modelTuning.simple' : 'settings.modelTuning.advanced')}
              </button>
            ))}
          </div>
        </div>

        <div>
          {/* Default + Background model selectors. Routing lives in its own group
              below in Advanced, so the component's own drawer stays closed. */}
          <ModelTierConfig
            models={visibleModels}
            metadata={modelMetadata}
            primaryModel={shownDefault('default')}
            onPrimaryModelChange={setDefault('default')}
            backgroundModel={shownDefault('background')}
            onBackgroundModelChange={setDefault('background')}
            payers={payers}
            auto={autoModels}
          />
          {defaultQuestion && <DefaultModelScopeChoice className="mt-4" {...defaultQuestion} />}
          <PayerOrder className="mt-4" />

          <div className="flex flex-col gap-1.5" style={{ marginTop: '16px' }}>
            <label htmlFor="default-model-scope" className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {t('settings.defaultModelChange.scopeLabel')}
            </label>
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('settings.defaultModelChange.scopeDesc')}
            </p>
            <Select
              id="default-model-scope"
              value={defaultModelScope}
              onChange={(e) => write({ default_model_scope: e.target.value as DefaultModelScope })}
            >
              <option value="ask">{t('settings.defaultModelChange.scopeAsk')}</option>
              <option value="new_threads">{t('settings.defaultModelChange.newThreads')}</option>
              <option value="existing_threads">{t('settings.defaultModelChange.scopeExistingThreads')}</option>
            </Select>
          </div>

          {/* Favorite models, the picker's first tab */}
          <div ref={modelPickerRef} style={{ marginTop: '16px' }}>
          <div className="flex flex-col gap-1.5">
            <label className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {t('settings.starredModels')}
            </label>
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('settings.starredModelsDesc')}
            </p>
            <div className="flex flex-wrap items-center gap-1.5">
              {favorites.models.map((key) => (
                <span
                  key={key}
                  className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs"
                  style={{
                    background: 'var(--color-bg-surface)',
                    border: '1px solid var(--color-border-default)',
                    color: 'var(--color-text-secondary)',
                  }}
                >
                  {modelLabel(key, modelMetadata)}
                  <PayerBadge payer={payers[key]} />
                  <button
                    type="button"
                    onClick={() => favorites.remove(key)}
                    className="ml-0.5 hover:opacity-70"
                    style={{ color: 'var(--color-text-tertiary)' }}
                    aria-label={t('settings.removeModel', { model: modelLabel(key, modelMetadata) })}
                  >
                    &times;
                  </button>
                </span>
              ))}
              <button
                type="button"
                onClick={() => { setShowModelPicker(v => !v); setModelPickerSearch(''); }}
                className="inline-flex items-center px-2 py-1 rounded text-xs font-medium"
                style={{
                  border: '1px dashed var(--color-border-default)',
                  color: 'var(--color-text-secondary)',
                }}
              >
                + {t('settings.addModels', 'Add')}
              </button>
            </div>
          </div>

          {/* Collapsible model picker — hidden by default (inside ref for click-outside) */}
          {showModelPicker && (
            <div
              className="mt-3 rounded-lg overflow-hidden"
              style={{ border: '1px solid var(--color-border-muted)', background: 'var(--color-bg-card)' }}
            >
              {/* Search */}
              <div className="px-3 pt-3 pb-2">
                <div className="relative">
                  <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
                  <input
                    type="text"
                    value={modelPickerSearch}
                    onChange={(e) => setModelPickerSearch(e.target.value)}
                    placeholder={t('common.search')}
                    className="w-full rounded-md pl-8 pr-3 py-1.5 text-xs"
                    style={{
                      backgroundColor: 'var(--color-bg-elevated)',
                      border: '1px solid var(--color-border-muted)',
                      color: 'var(--color-text-primary)',
                    }}
                    autoFocus
                  />
                </div>
              </div>
              {/* Provider groups */}
              <div className="px-1 pb-1 max-h-[280px] overflow-y-auto">
                {Object.entries(visibleModels).map(([provider, providerData]) => {
                  const models: string[] = providerData?.models || [];
                  const query = modelPickerSearch.toLowerCase();
                  const filtered = models.filter(m => modelMatches(m, query, modelMetadata));
                  if (filtered.length === 0) return null;
                  const displayName = providerData?.display_name || provider.charAt(0).toUpperCase() + provider.slice(1);
                  return (
                    <div key={provider} className="mb-1">
                      <div className="px-2 py-1 text-[0.625rem] font-semibold uppercase tracking-wider" style={{ color: 'var(--color-text-tertiary)' }}>
                        {displayName}
                      </div>
                      {filtered.map((m) => {
                        const isStarred = favorites.models.includes(m);
                        return (
                          <button
                            key={m}
                            type="button"
                            onClick={() => (isStarred ? favorites.remove(m) : favorites.add(m))}
                            className="w-full flex items-center justify-between px-2 py-1.5 rounded-md text-xs transition-colors"
                            style={{
                              color: 'var(--color-text-primary)',
                              backgroundColor: isStarred ? 'var(--color-bg-tag)' : 'transparent',
                            }}
                            onMouseEnter={(e) => { if (!isStarred) e.currentTarget.style.backgroundColor = 'var(--color-bg-elevated)'; }}
                            onMouseLeave={(e) => { if (!isStarred) e.currentTarget.style.backgroundColor = 'transparent'; }}
                          >
                            <span className="flex min-w-0 items-center gap-2">
                              <span className="truncate">{modelLabel(m, modelMetadata)}</span>
                              <PayerBadge payer={payers[m]} />
                            </span>
                            {isStarred && <Pin className="h-3 w-3 shrink-0" style={{ color: 'var(--color-accent-primary)' }} />}
                          </button>
                        );
                      })}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
          </div>
        </div>

        {isAdvanced && (
          <>
            {/* Defaults first: they are the only place each setting is spelled
                out, and the table below is bare columns. Read the explanation,
                then the exceptions to it. */}
            <div className="flex flex-col gap-3">
              <SectionHeading>{t('settings.modelTuning.sectionDefaults')}</SectionHeading>
              <AccountTuningDefaults
                metadata={modelMetadata}
                validModelNames={validModelNames}
                compactionProfiles={compactionProfiles}
              />
            </div>

            <div className="flex flex-col gap-3">
              <SectionHeading>{t('settings.modelTuning.perModel')}</SectionHeading>
              <PerModelMatrix
                metadata={modelMetadata}
                validModelNames={validModelNames}
                compactionProfiles={compactionProfiles}
                pinnedGuidance={hookSystemDefaults?.prompt_guidance ?? null}
              />
            </div>

            <div className="flex flex-col gap-3">
              <SectionHeading>{t('settings.modelTuning.sectionRouting')}</SectionHeading>
              <div className="flex flex-col gap-4">
                <ModelSelector
                  label={t('settings.modelTuning.fetchModel')}
                  description={t('settings.modelTuning.fetchModelDesc')}
                  value={mPref.fetch_model ?? ''}
                  onChange={(v) => write({ fetch_model: v || null })}
                  models={visibleModels}
                  placeholder={modelSlotCopy.defaultsToBackground}
                  payers={payers}
                  metadata={modelMetadata}
                />
                <ModelSelector
                  label={t('settings.modelTuning.compactionModel')}
                  description={t('settings.modelTuning.compactionModelDesc')}
                  value={mPref.compaction_model ?? ''}
                  onChange={(v) => write({
                    compaction_model: v || null,
                    // Retire the legacy key so the back-compat shim in
                    // resolve_llm_config can't resurrect a stale value when the
                    // user clears compaction_model.
                    summarization_model: null,
                  })}
                  models={visibleModels}
                  placeholder={modelSlotCopy.defaultsToConversation}
                  payers={payers}
                  metadata={modelMetadata}
                />
                {/* A subagent's model is the subagent's own setting, kept with
                    the rest of it rather than as a routing slot here. */}
                <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                  <Trans
                    i18nKey="settings.modelTuning.subagentsLink"
                    components={{
                      pane: (
                        <Link
                          to="/plugins?tab=subagents"
                          className="underline underline-offset-2 hover:text-[var(--color-text-primary)]"
                        />
                      ),
                    }}
                  />
                </p>
                <FallbackModelsPicker
                  selected={fallbackModels}
                  onChange={(list) => write({ fallback_models: list })}
                  models={visibleModels}
                  metadata={modelMetadata}
                  payers={payers}
                  skipped={{
                    models: deploymentOnlyFallbacks,
                    reason: t(isPlatformMode ? 'settings.payerOrder.skipped' : 'settings.payerOrder.skippedSelfHosted'),
                  }}
                />
                {savedCrossesPayer && (
                  <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings.payerOrder.savedNote')}
                  </p>
                )}
                {deploymentOnlyFallbacks.size > 0 && (
                  <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                    {t('settings.payerOrder.skippedNote')}{' '}
                    <button
                      type="button"
                      onClick={() => write({ fallback_models: fallbackModels })}
                      className="underline underline-offset-2 hover:text-[var(--color-text-primary)]"
                    >
                      {t('settings.payerOrder.useAlways')}
                    </button>
                  </p>
                )}
                {/* DALTON-HOOK(settings-section): Dalton model routing settings */}
                <div className="pt-2 border-t flex flex-col gap-4" style={{ borderColor: 'var(--color-border-muted)' }}>
                  <h4 className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--color-text-secondary)' }}>
                    {t('settings.modelTuning.daltonHeading')}
                  </h4>
                  <ModelSelector
                    label={t('settings.modelTuning.daltonAuditModel')}
                    description={`${t('settings.modelTuning.daltonAuditModelDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_audit_model)}`}
                    value={mPref.dalton_audit_model ?? ''}
                    onChange={(v) => write({ dalton_audit_model: v || null })}
                    models={daltonSystemModels}
                    placeholder={daltonDefaultHint(hookSystemDefaults?.dalton_audit_model)}
                    payers={payers}
                    metadata={modelMetadata}
                  />
                  {/* DALTON-HOOK(settings-section): Dalton language-review model selector */}
                  <ModelSelector
                    label={t('settings.modelTuning.daltonLanguageModel')}
                    description={`${t('settings.modelTuning.daltonLanguageModelDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_language_model)}`}
                    value={mPref.dalton_language_model ?? ''}
                    onChange={(v) => write({ dalton_language_model: v || null })}
                    models={daltonSystemModels}
                    placeholder={daltonDefaultHint(hookSystemDefaults?.dalton_language_model)}
                    payers={payers}
                    metadata={modelMetadata}
                  />
                  <FallbackModelsPicker
                    label={t('settings.modelTuning.daltonAuditFallbackModels')}
                    description={`${t('settings.modelTuning.daltonAuditFallbackModelsDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_audit_fallback_models)}`}
                    selected={mPref.dalton_audit_fallback_models ?? []}
                    onChange={(list) => write({ dalton_audit_fallback_models: list.length > 0 ? list : null })}
                    models={daltonSystemModels}
                    metadata={modelMetadata}
                    payers={payers}
                  />
                  {/* DALTON-HOOK(settings-section): Dalton figure audit timeout and auxiliary/reviewer/extract models */}
                  <div className="flex flex-col gap-1.5">
                    <label
                      htmlFor="dalton-audit-timeout-input"
                      className="text-sm font-medium"
                      style={{ color: 'var(--color-text-primary)' }}
                    >
                      {t('settings.modelTuning.daltonAuditTimeoutS')}
                    </label>
                    <p className="text-xs leading-relaxed" style={{ color: 'var(--color-text-tertiary)' }}>
                      {`${t('settings.modelTuning.daltonAuditTimeoutSDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_audit_timeout_s ?? 300)}`}
                    </p>
                    <input
                      id="dalton-audit-timeout-input"
                      type="number"
                      min={60}
                      max={1800}
                      step={1}
                      value={auditTimeoutDraft}
                      placeholder={daltonDefaultHint(hookSystemDefaults?.dalton_audit_timeout_s ?? 300)}
                      onChange={(e) => {
                        setAuditTimeoutDraft(e.target.value);
                        if (auditTimeoutError) setAuditTimeoutError(null);
                      }}
                      onBlur={(e) => commitAuditTimeout(e.currentTarget.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault();
                          commitAuditTimeout(e.currentTarget.value);
                        }
                      }}
                      className="w-full max-w-xs rounded-md px-3 py-1.5 text-sm"
                      style={{
                        backgroundColor: 'var(--color-bg-input)',
                        border: '1px solid var(--color-border-muted)',
                        color: 'var(--color-text-primary)',
                      }}
                    />
                    {auditTimeoutError && (
                      <p role="alert" className="text-xs" style={{ color: 'var(--color-icon-danger)' }}>
                        {auditTimeoutError}
                      </p>
                    )}
                  </div>
                  {/* DALTON-HOOK(settings-section): Dalton assist, reviewer, and extract model selectors */}
                  <ModelSelector
                    label={t('settings.modelTuning.daltonAssistModel')}
                    description={`${t('settings.modelTuning.daltonAssistModelDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_assist_model)}`}
                    value={mPref.dalton_assist_model ?? ''}
                    onChange={(v) => write({ dalton_assist_model: v || null })}
                    models={daltonSystemModels}
                    placeholder={daltonDefaultHint(hookSystemDefaults?.dalton_assist_model)}
                    payers={payers}
                    metadata={modelMetadata}
                  />
                  <FallbackModelsPicker
                    label={t('settings.modelTuning.daltonAssistFallbackModels')}
                    description={`${t('settings.modelTuning.daltonAssistFallbackModelsDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_assist_fallback_models)}`}
                    selected={mPref.dalton_assist_fallback_models ?? []}
                    onChange={(list) => write({ dalton_assist_fallback_models: list.length > 0 ? list : null })}
                    models={daltonSystemModels}
                    metadata={modelMetadata}
                    payers={payers}
                  />
                  <FallbackModelsPicker
                    label={t('settings.modelTuning.daltonReviewerModels')}
                    description={`${t('settings.modelTuning.daltonReviewerModelsDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_reviewer_models)}`}
                    selected={mPref.dalton_reviewer_models ?? []}
                    onChange={(list) => write({ dalton_reviewer_models: list.length > 0 ? list : null })}
                    models={daltonSystemModels}
                    metadata={modelMetadata}
                    payers={payers}
                  />
                  <ModelSelector
                    label={t('settings.modelTuning.daltonExtractModel')}
                    description={`${t('settings.modelTuning.daltonExtractModelDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_extract_model)}`}
                    value={mPref.dalton_extract_model ?? ''}
                    onChange={(v) => write({ dalton_extract_model: v || null })}
                    models={daltonSystemModels}
                    placeholder={daltonDefaultHint(hookSystemDefaults?.dalton_extract_model)}
                    payers={payers}
                    metadata={modelMetadata}
                  />
                </div>
              </div>
            </div>
          </>
        )}

        {/* Web search */}
        <div className="flex flex-col gap-3">
          <SectionHeading>{t('settings.modelTuning.sectionSearch')}</SectionHeading>
          <div className="flex flex-col gap-1.5">
            <label className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {t('settings.searchProvider', 'Web Search Provider')}
            </label>
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('settings.searchProviderDesc', 'Search engine the agent uses for web searches.')}
            </p>
            <Select
              value={searchProvider}
              onChange={(e) => {
                const next = e.target.value;
                const depths = next ? searchProviders?.[next]?.depths ?? [] : [];
                // Depth levels are provider-scoped, so a stale level may not
                // exist on the new provider. A provider offering one level has
                // no depth control at all: leave the stored key standing rather
                // than deleting a level a switch back would want. Default is
                // not that case. It shows no control either, but resolve time
                // still reads the stored level against whichever provider the
                // deployment picks, so an invisible one would keep applying.
                write({
                  search_provider: next || null,
                  ...(!next || depths.length > 1 ? { search_depth: null } : {}),
                });
              }}
              disabled={!canCustomizeSearchProvider}
              aria-label={t('settings.searchProvider', 'Web Search Provider')}
            >
              <option value="">{t('settings.searchProviderDefault', 'Default')}</option>
              {providerOptions.map(([value, p]) => (
                <option key={value} value={value} disabled={!tierAllows(p.min_tier)}>
                  {p.display_name}
                </option>
              ))}
            </Select>
            {providerOptions.some(([, p]) => !tierAllows(p.min_tier)) && (
              <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('settings.searchProviderUpgradeHint', 'Some search providers are available on higher plans.')}
              </p>
            )}
          </div>

          {/* Web search depth — only for providers with multiple levels */}
          {depthOptions.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <label className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
                {t('settings.searchDepth', 'Search Depth')}
              </label>
              <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('settings.searchDepthDesc', 'How thoroughly the agent searches the web. Deeper levels use more credits per search.')}
              </p>
              <Select
                value={searchDepth}
                onChange={(e) => write({ search_depth: e.target.value || null })}
                disabled={!canCustomizeSearchDepth}
                aria-label={t('settings.searchDepth', 'Search Depth')}
              >
                <option value="">{t('settings.searchDepthDefault', 'Default')}</option>
                {depthOptions.map(d => (
                  <option key={d.name} value={d.name} disabled={!tierAllows(d.min_tier)}>
                    {t(`settings.searchDepthLevel.${d.name}`, d.display_name)}
                  </option>
                ))}
              </Select>
              {depthOptions.some(d => !tierAllows(d.min_tier)) && (
                <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('settings.searchDepthUpgradeHint', 'Deeper search levels are available on higher plans.')}
                </p>
              )}
            </div>
          )}
        </div>

        {/* DALTON-HOOK(settings-section): OpenClaw model sync & Dalton daily USD budget */}
        <DaltonSection advanced={isAdvanced} />

        {saveStatus !== 'idle' && (
          <div className="flex items-center justify-end pt-2">
            {saveStatus === 'pending' && (
              <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>{t('common.saving')}</span>
            )}
            {saveStatus === 'success' && (
              <span className="text-xs" style={{ color: 'var(--color-success)' }}>{t('common.saved')}</span>
            )}
            {saveStatus === 'error' && (
              <span className="text-xs" style={{ color: 'var(--color-loss)' }}>{t('settings.failedToSaveSettings')}</span>
            )}
          </div>
        )}
      </div>
  );
}
