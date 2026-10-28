import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useMutationState } from '@tanstack/react-query';
import { Search, Pin } from 'lucide-react';
import { Link, useLocation } from 'react-router';
import { Select } from '@/components/ui/select';
import { usePreferences } from '@/hooks/usePreferences';
import { PREFERENCE_MUTATION_KEY, useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import { ModelTierConfig } from '@/components/model/ModelTierConfig';
import { ModelSelector } from '@/components/model/ModelSelector';
import { FallbackModelsPicker } from '@/components/model/FallbackModelsPicker';
import { PayerOrder } from '@/components/model/PayerOrder';
import { PayerBadge } from '@/components/model/PayerBadge';
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
// DALTON-HOOK(settings-section): Dalton provider status, budget, and OAuth/native sync cards
import { DaltonSection } from './dalton/DaltonSection';
import { SettingRow, SettingsCardBody, SettingsGroup, ToggleRow } from '../components/SettingsLayout';
import { useSettingCopy } from '../components/useSettingCopy';
import { SaveStatus } from '../components/SaveStatus';
import { useSaveFailedToast } from './usePreferenceWrite';
import { SETTINGS, settingAt } from '../sections';
import { BACKGROUND_AUTO, backgroundDefaultChoice, isModelWrite, modelPrefs, readDefaultModelScope, splitPreferenceWrite } from '@/lib/modelPreferences';
import { useModelSlotCopy } from '@/components/model/useModelSlotCopy';
import { paidByDeployment, paidByUser } from '@/lib/modelPayer';
import type { ModelSlot, DefaultModelScope, PreferencePatch } from '@/lib/modelPreferences';

type ModelsMode = 'simple' | 'advanced';

const MODE_STORAGE_KEY = 'settings:modelMode';

/** View state, not configuration — which is why it stays on the device rather
 *  than travelling with the account like everything else in this section. */
function readStoredMode(): ModelsMode {
  try {
    return localStorage.getItem(MODE_STORAGE_KEY) === 'advanced' ? 'advanced' : 'simple';
  } catch {
    return 'simple';
  }
}

/** Models: connections, the default and Background models, quick-access
 * models, and behind the advanced switch per-model tuning, account defaults
 * and routing.
 *
 * Every control writes a patch of the keys it owns and reads its value back
 * from the preferences cache. Nothing here is mirrored into local state: this
 * section has no text input, so there is no keystroke to debounce, and a mirror
 * would only be a second copy that a failed write leaves disagreeing with the
 * settings a scheduled turn actually runs. */
export function ModelsPanel() {
  const { preferences: prefsData } = usePreferences();
  const { mutate } = useUpdatePreferences();
  // DALTON-HOOK(settings-section): customModels is read to keep custom models out of the Dalton selectors
  const { models: visibleModels, metadata: modelMetadata, payers, systemDefaults: hookSystemDefaults, customModels, validModelNames, catalogModelNames, isLoading: modelsLoading, compactionProfiles } = useAllModels();
  const { t } = useTranslation();
  const copy = useSettingCopy();
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

  const [mode, setMode] = useState<ModelsMode>(readStoredMode);
  const isAdvanced = mode === 'advanced';
  const changeMode = (next: ModelsMode) => {
    setMode(next);
    try { localStorage.setItem(MODE_STORAGE_KEY, next); } catch { /* private mode */ }
  };
  // The settings search opening on a setting only Advanced draws turns it on.
  // Keyed on the navigation, so finding the same setting again reopens it.
  const { hash, key: navigation } = useLocation();
  const [openedFor, setOpenedFor] = useState('');
  if (settingAt(hash)?.advanced && navigation !== openedFor) {
    setOpenedFor(navigation);
    setMode('advanced');
  }

  const [showModelPicker, setShowModelPicker] = useState(false);
  const [modelPickerSearch, setModelPickerSearch] = useState('');
  const modelPickerRef = useRef<HTMLDivElement>(null);

  // Model routing and tuning live in model_preference.
  const mPref = modelPrefs(prefsData);
  const favorites = useFavoriteModels();
  // DALTON-HOOK(settings-section): Dalton specialized model routing preferences
  const daltonAuditModel = (mPref.dalton_audit_model as string | null | undefined) ?? '';
  const daltonLanguageModel = (mPref.dalton_language_model as string | null | undefined) ?? '';
  const daltonAuditFallback = Array.isArray(mPref.dalton_audit_fallback_models)
    ? (mPref.dalton_audit_fallback_models as string[])
    : [];
  const daltonAuditTimeoutS =
    typeof mPref.dalton_audit_timeout_s === 'number' ? mPref.dalton_audit_timeout_s : null;
  const daltonAssistModel = (mPref.dalton_assist_model as string | null | undefined) ?? '';
  const daltonAssistFallback = Array.isArray(mPref.dalton_assist_fallback_models)
    ? (mPref.dalton_assist_fallback_models as string[])
    : [];
  const daltonReviewerModels = Array.isArray(mPref.dalton_reviewer_models)
    ? (mPref.dalton_reviewer_models as string[])
    : [];
  const daltonExtractModel = (mPref.dalton_extract_model as string | null | undefined) ?? '';
  // Every name the catalog still carries, access aside. A fallback the user
  // temporarily cannot reach is still configured; one the manifest dropped is
  // not, and _resolve_fallback_clients skips it without saying so, leaving the
  // picker promising resilience that no longer exists.
  const storedFallbacks = mPref.fallback_models ?? hookSystemDefaults?.fallback_models ?? [];
  // An empty catalog is "not loaded yet", never "every model was removed".
  const fallbackModels = modelsLoading || catalogModelNames.size === 0
    ? storedFallbacks
    : storedFallbacks.filter((m) => catalogModelNames.has(m));
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

  // Every control in this section writes through the same mutation, the ones the
  // child components own included, so the status watches the write rather than
  // any single caller's copy of it. Only model writes made since the section
  // opened count: a "Saved" on arrival answers nothing the reader did.
  const [openedAt] = useState(() => Date.now());
  const writes = useMutationState({
    filters: {
      mutationKey: PREFERENCE_MUTATION_KEY,
      predicate: (mutation) => mutation.state.submittedAt >= openedAt && isModelWrite(mutation.state.variables),
    },
    select: (m) => ({
      status: m.state.status,
      at: m.state.submittedAt,
      // A default change asked about its threads reports its own failure.
      reported: 'apply_default_to' in (m.state.variables as object),
    }),
  });
  const saveStatus = writes.some((w) => w.status === 'pending') ? 'pending' : writes.at(-1)?.status ?? 'idle';

  // The status sits at the top, far above the advanced groups, so a failed
  // write is also said where the reader is: once per failure.
  const lastFailure = Math.max(0, ...writes.filter((w) => w.status === 'error' && !w.reported).map((w) => w.at));
  const toastSaveFailed = useSaveFailedToast();
  const toastedFailure = useRef(0);
  useEffect(() => {
    if (lastFailure <= toastedFailure.current) return;
    toastedFailure.current = lastFailure;
    toastSaveFailed();
  }, [lastFailure, toastSaveFailed]);

  const shownStatus = saveStatus === 'pending' ? 'saving' : saveStatus === 'success' ? 'saved' : saveStatus;

  return (
    <>
      {/* Connections first: every pick below is among the models they reach. */}
      <SettingsGroup title={t(SETTINGS.modelAccess.labelKey)}>
        <SettingsCardBody id={SETTINGS.modelAccess.anchor}>
          <ModelAccessSection />
        </SettingsCardBody>
      </SettingsGroup>

      <SettingsGroup title={t(SETTINGS.defaultModels.labelKey)} action={<SaveStatus status={shownStatus} />}>
        {/* Default + Background model selectors. Routing lives in its own group
            below in Advanced, so the component's own drawer stays closed. */}
        <SettingsCardBody id={SETTINGS.defaultModels.anchor}>
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
        </SettingsCardBody>
        <SettingRow {...copy(SETTINGS.defaultModelScope)} htmlFor="default-model-scope-select" wide>
          <Select
            id="default-model-scope-select"
            value={defaultModelScope}
            onChange={(e) => write({ default_model_scope: e.target.value as DefaultModelScope })}
          >
            <option value="ask">{t('settings.defaultModelChange.scopeAsk')}</option>
            <option value="new_threads">{t('settings.defaultModelChange.newThreads')}</option>
            <option value="existing_threads">{t('settings.defaultModelChange.scopeExistingThreads')}</option>
          </Select>
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup title={t(SETTINGS.quickAccess.labelKey)} description={t(SETTINGS.quickAccess.descKey)}>
        {/* The picker is inside the ref, for its click-outside close. */}
        <SettingsCardBody id={SETTINGS.quickAccess.anchor}>
          <div ref={modelPickerRef}>
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
                data-search-focus
                className="inline-flex items-center px-2 py-1 rounded text-xs font-medium"
                style={{
                  border: '1px dashed var(--color-border-default)',
                  color: 'var(--color-text-secondary)',
                }}
              >
                + {t('settings.addModels', 'Add')}
              </button>
            </div>

            {showModelPicker && (
              <div
                className="mt-3 rounded-lg overflow-hidden"
                style={{ border: '1px solid var(--color-border-muted)', background: 'var(--color-bg-page)' }}
              >
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
                              className={`w-full flex items-center justify-between px-2 py-1.5 rounded-md text-xs transition-colors ${isStarred ? 'bg-(--color-bg-tag)' : 'hover:bg-(--color-bg-elevated)'}`}
                              style={{ color: 'var(--color-text-primary)' }}
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
        </SettingsCardBody>
      </SettingsGroup>

      {/* Off, the page holds only what a first-time user has a reason to
          touch; on, it adds per-model tuning, account defaults and routing. */}
      <SettingsGroup>
        <ToggleRow
          {...copy(SETTINGS.modelDetail)}
          checked={isAdvanced}
          onChange={() => changeMode(isAdvanced ? 'simple' : 'advanced')}
        />
      </SettingsGroup>

      {isAdvanced && (
        <>
          {/* Defaults first: they are the only place each setting is spelled
              out, and the table below is bare columns. Read the explanation,
              then the exceptions to it. */}
          <SettingsGroup title={t(SETTINGS.accountDefaults.labelKey)}>
            <SettingsCardBody id={SETTINGS.accountDefaults.anchor}>
              <AccountTuningDefaults
                metadata={modelMetadata}
                validModelNames={validModelNames}
                compactionProfiles={compactionProfiles}
              />
            </SettingsCardBody>
          </SettingsGroup>

          <SettingsGroup title={t(SETTINGS.perModel.labelKey)}>
            <SettingsCardBody id={SETTINGS.perModel.anchor}>
              <PerModelMatrix
                metadata={modelMetadata}
                validModelNames={validModelNames}
                compactionProfiles={compactionProfiles}
                pinnedGuidance={hookSystemDefaults?.prompt_guidance ?? null}
              />
            </SettingsCardBody>
          </SettingsGroup>

          <SettingsGroup title={t(SETTINGS.routing.labelKey)}>
            <SettingsCardBody id={SETTINGS.routing.anchor} className="flex flex-col gap-4">
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

              {/* DALTON-HOOK(settings-section): Dalton specialized model routing controls */}
              <div
                id={SETTINGS.daltonRouting.anchor}
                className="pt-2 border-t flex flex-col gap-4"
                style={{ borderColor: 'var(--color-border-muted)' }}
              >
                <h4 className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('settings.modelTuning.daltonHeading')}
                </h4>
                <div id={SETTINGS.daltonAuditModel.anchor}>
                  <ModelSelector
                    label={t('settings.modelTuning.daltonAuditModel')}
                    description={`${t('settings.modelTuning.daltonAuditModelDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_audit_model)}`}
                    value={daltonAuditModel}
                    onChange={(v) => write({ dalton_audit_model: v || null })}
                    models={daltonSystemModels}
                    placeholder={daltonDefaultHint(hookSystemDefaults?.dalton_audit_model)}
                    payers={payers}
                    metadata={modelMetadata}
                  />
                </div>
                <div id={SETTINGS.daltonLanguageModel.anchor}>
                  <ModelSelector
                    label={t('settings.modelTuning.daltonLanguageModel')}
                    description={`${t('settings.modelTuning.daltonLanguageModelDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_language_model)}`}
                    value={daltonLanguageModel}
                    onChange={(v) => write({ dalton_language_model: v || null })}
                    models={daltonSystemModels}
                    placeholder={daltonDefaultHint(hookSystemDefaults?.dalton_language_model)}
                    payers={payers}
                    metadata={modelMetadata}
                  />
                </div>
                <div id={SETTINGS.daltonAuditFallbackModels.anchor}>
                  <FallbackModelsPicker
                    label={t('settings.modelTuning.daltonAuditFallbackModels')}
                    description={`${t('settings.modelTuning.daltonAuditFallbackModelsDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_audit_fallback_models)}`}
                    selected={daltonAuditFallback}
                    onChange={(list) => write({ dalton_audit_fallback_models: list.length > 0 ? list : null })}
                    models={daltonSystemModels}
                    metadata={modelMetadata}
                    payers={payers}
                  />
                </div>
                <div id={SETTINGS.daltonAuditTimeout.anchor} className="flex flex-col gap-1.5">
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
                    data-testid="dalton-audit-timeout-input"
                  />
                  {auditTimeoutError && (
                    <p role="alert" className="text-xs" style={{ color: 'var(--color-icon-danger)' }}>
                      {auditTimeoutError}
                    </p>
                  )}
                </div>
                <div id={SETTINGS.daltonAssistModel.anchor}>
                  <ModelSelector
                    label={t('settings.modelTuning.daltonAssistModel')}
                    description={`${t('settings.modelTuning.daltonAssistModelDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_assist_model)}`}
                    value={daltonAssistModel}
                    onChange={(v) => write({ dalton_assist_model: v || null })}
                    models={daltonSystemModels}
                    placeholder={daltonDefaultHint(hookSystemDefaults?.dalton_assist_model)}
                    payers={payers}
                    metadata={modelMetadata}
                  />
                </div>
                <div id={SETTINGS.daltonAssistFallbackModels.anchor}>
                  <FallbackModelsPicker
                    label={t('settings.modelTuning.daltonAssistFallbackModels')}
                    description={`${t('settings.modelTuning.daltonAssistFallbackModelsDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_assist_fallback_models)}`}
                    selected={daltonAssistFallback}
                    onChange={(list) => write({ dalton_assist_fallback_models: list.length > 0 ? list : null })}
                    models={daltonSystemModels}
                    metadata={modelMetadata}
                    payers={payers}
                  />
                </div>
                <div id={SETTINGS.daltonReviewerModels.anchor}>
                  <FallbackModelsPicker
                    label={t('settings.modelTuning.daltonReviewerModels')}
                    description={`${t('settings.modelTuning.daltonReviewerModelsDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_reviewer_models)}`}
                    selected={daltonReviewerModels}
                    onChange={(list) => write({ dalton_reviewer_models: list.length > 0 ? list : null })}
                    models={daltonSystemModels}
                    metadata={modelMetadata}
                    payers={payers}
                  />
                </div>
                <div id={SETTINGS.daltonExtractModel.anchor}>
                  <ModelSelector
                    label={t('settings.modelTuning.daltonExtractModel')}
                    description={`${t('settings.modelTuning.daltonExtractModelDesc')} ${daltonDefaultHint(hookSystemDefaults?.dalton_extract_model)}`}
                    value={daltonExtractModel}
                    onChange={(v) => write({ dalton_extract_model: v || null })}
                    models={daltonSystemModels}
                    placeholder={daltonDefaultHint(hookSystemDefaults?.dalton_extract_model)}
                    payers={payers}
                    metadata={modelMetadata}
                  />
                </div>
              </div>
            </SettingsCardBody>
          </SettingsGroup>
        </>
      )}

      {/* DALTON-HOOK(settings-section): Dalton provider status, budget, and OAuth/native sync cards */}
      <DaltonSection advanced={isAdvanced} />
    </>
  );
}
