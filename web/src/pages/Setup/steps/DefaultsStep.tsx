import { useState, useCallback, useId } from 'react';
import { useNavigate } from 'react-router';
import { motion, AnimatePresence } from '@/lib/framer';
import { ChevronRight, Info } from 'lucide-react';
import { cn } from '@/lib/utils';
import { WizardStepHeader } from '@/components/ui/wizard';
import { TWEEN } from '@/lib/motion';
import { SetupFooter } from '../SetupFooter';
import { Loader } from '@/components/ui/loader';
import { ModelTierConfig } from '@/components/model/ModelTierConfig';
import { ModelSelector } from '@/components/model/ModelSelector';
import { FallbackModelsPicker } from '@/components/model/FallbackModelsPicker';
import { usePreferences } from '@/hooks/usePreferences';
import { useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import { useTranslation } from 'react-i18next';
import { modelPrefs, splitPreferenceWrite, starredModels } from '@/lib/modelPreferences';
import { apiErrorDetail } from '@/api/errors';
import { suggestBackgroundModel } from './suggestBackgroundModel';
import { useModelSlotCopy } from '@/components/model/useModelSlotCopy';
// DALTON-HOOK(dalton-setup): Dalton build defaults, unified team-model availability, and credits/Claude Code filtering
// DALTON-HOOK(antigravity-series): series-only display & hide effort pickers across onboarding dropdowns
import { isDaltonBuild } from '@/config/brand';
import { formatAntigravityVariantLabel } from '@/lib/antigravitySeries';
import { modelLabel } from '@/lib/modelLabel';
import {
  DALTON_BACKGROUND_MODEL,
  DALTON_BACKGROUND_SERIES,
  DALTON_DEFAULT_MODEL,
  DALTON_DEFAULT_SERIES,
  filterDaltonCuratedModels,
  filterDaltonPayers,
  filterDaltonProviderModels,
  resolveDaltonSeriesDefaultModel,
} from '@/config/daltonSetup';
import { RowButton, StepLoadFailed } from '../answers/parts';
import { useDaltonTeamModels } from '../answers/useTeamModels';

// ---------------------------------------------------------------------------
// DefaultsStep — Step 5: Set default primary + Background models
// ---------------------------------------------------------------------------

/**
 * Setup sits outside the onboarding provider, so after a reload this step can
 * mount before the preferences have arrived. The slots are seeded once, from
 * what is saved, so the form mounts again when they arrive; until then nothing
 * is filled and Continue stays off, or it would write the first-run fills over
 * the saved defaults.
 */
export default function DefaultsStep() {
  const { isLoaded, isLoading } = usePreferences();
  return (
    <DefaultsForm key={isLoaded ? 'loaded' : 'unread'} prefsLoaded={isLoaded} prefsFailed={!isLoaded && !isLoading} />
  );
}

function DefaultsForm({ prefsLoaded, prefsFailed }: { prefsLoaded: boolean; prefsFailed: boolean }) {
  const navigate = useNavigate();
  // DALTON-HOOK(dalton-setup): read unified Dalton team-model availability instead of raw sharing flag
  const team = useDaltonTeamModels();
  const { models, slotModels, metadata, payers, systemDefaults, isLoading, seriesCatalog, customModels, rawApiResponse } = team.all;
  const rawMetadata = rawApiResponse?.model_metadata ?? metadata;
  const { preferences } = usePreferences();
  const updatePreferences = useUpdatePreferences();
  const { t } = useTranslation();
  const copy = useModelSlotCopy();
  const headingId = useId();

  // DALTON-HOOK(dalton-setup): filter out credits, Claude Code, and rejected-credential models under Dalton
  const visiblePayers = filterDaltonPayers(payers, isDaltonBuild);
  const visibleModels = filterDaltonProviderModels(models, metadata, payers, isDaltonBuild, team.rejectedProviders);
  const visibleSlotModels = filterDaltonProviderModels(slotModels, metadata, payers, isDaltonBuild, team.rejectedProviders);

  // ---------------------------------------------------------------------------
  // Selection state — seed from existing preferences if available
  // ---------------------------------------------------------------------------

  const modelPref = modelPrefs(preferences);

  const [primaryModel, setPrimaryModel] = useState<string>(
    () => modelPref.preferred_model ?? '',
  );
  const [backgroundModel, setBackgroundModel] = useState<string>(
    () => modelPref.preferred_background_model ?? '',
  );
  // DALTON-HOOK(dalton-setup): snapshot saved advanced routing models so Continue preserves untouched fields in Dalton builds
  const [initialAdvanced] = useState(() => ({
    compactionModel: modelPref.compaction_model ?? '',
    fetchModel: modelPref.fetch_model ?? '',
    fallbackModels: modelPref.fallback_models ?? [],
  }));
  const [advancedModels, setAdvancedModels] = useState<{
    compactionModel: string;
    fetchModel: string;
    fallbackModels: string[];
  }>(() => ({
    compactionModel: initialAdvanced.compactionModel,
    fetchModel: initialAdvanced.fetchModel,
    fallbackModels: initialAdvanced.fallbackModels,
  }));
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A first run starts with a default model rather than an empty required
  // slot: the deployment's own when this user can run it, else the first model
  // they just picked. Set during render like the Background fill below.
  // From the slot models, so the fill never lands on a conversation-only
  // model (Claude Code); the user may still pick one.
  // DALTON-HOOK(dalton-setup): enforce curated 5 primary candidates & live catalog membership in Dalton builds; non-Dalton preserves upstream behavior
  // DALTON-HOOK(standalone-core): pass runtime dalton_profile & metadata so Core unlocks BYOK/custom models
  const curatedModels = filterDaltonCuratedModels(visibleModels, isDaltonBuild, { profile: rawApiResponse?.dalton_profile, metadata });
  const curatedSlotModels = filterDaltonCuratedModels(visibleSlotModels, isDaltonBuild, { profile: rawApiResponse?.dalton_profile, metadata });
  const availablePrimary = Object.values(curatedModels).flatMap((p) => p.models ?? []);
  const fillable = Object.values(visibleSlotModels).flatMap((p) => p.models ?? []);
  const curatedSlotFillable = Object.values(curatedSlotModels).flatMap((p) => p.models ?? []);
  const primaryValid = Boolean(primaryModel) && (!isDaltonBuild || availablePrimary.includes(primaryModel));
  const backgroundValid = Boolean(backgroundModel) && (!isDaltonBuild || fillable.includes(backgroundModel));
  const catalogReady = !isLoading && (!isDaltonBuild || (!team.isLoading && !team.unread && !team.all.unread));
  const daltonPrimaryDefault = isDaltonBuild
    ? resolveDaltonSeriesDefaultModel(DALTON_DEFAULT_SERIES, availablePrimary, {
        seriesCatalog,
        fallbackModel: DALTON_DEFAULT_MODEL,
      })
    : null;
  const primaryFill = !primaryValid && catalogReady && prefsLoaded
    ? (isDaltonBuild
      ? [daltonPrimaryDefault, systemDefaults?.default_model, ...starredModels(preferences)]
      : [systemDefaults?.default_model, ...starredModels(preferences)]
    ).find(
        (m): m is string => typeof m === 'string' && fillable.includes(m) && (!isDaltonBuild || availablePrimary.includes(m)),
      ) ?? (isDaltonBuild ? (availablePrimary.find((m) => fillable.includes(m)) ?? availablePrimary[0]) : fillable[0])
    : undefined;
  if (primaryFill && primaryFill !== primaryModel) setPrimaryModel(primaryFill);

  // A first run fills both defaults, so an empty Background slot takes a model to
  // go with the primary, set during render so the slot never paints empty.
  // The fill is a pick like any other: it stays when the primary changes, and
  // nothing is saved before Continue.
  // DALTON-HOOK(dalton-setup): keep non-Dalton background auto-pick identical to upstream; Dalton resolves Hydrogen -low after catalog is ready
  const effectivePrimary = primaryValid ? primaryModel : (primaryFill ?? '');
  const backgroundFill = isDaltonBuild
    ? (!backgroundValid && effectivePrimary && catalogReady && prefsLoaded
      ? (resolveDaltonSeriesDefaultModel(DALTON_BACKGROUND_SERIES, fillable, {
          seriesCatalog,
          fallbackModel: DALTON_BACKGROUND_MODEL,
          preferLowEffort: true,
        }) ?? suggestBackgroundModel(visibleSlotModels, effectivePrimary))
      : undefined)
    : (!backgroundModel && primaryModel && prefsLoaded
      ? suggestBackgroundModel(visibleSlotModels, primaryModel)
      : undefined);
  if (backgroundFill && backgroundFill !== backgroundModel) setBackgroundModel(backgroundFill);

  const teamEmpty = isDaltonBuild && (team.empty || availablePrimary.length === 0);
  const canContinue = isDaltonBuild
    ? prefsLoaded && catalogReady && !teamEmpty && primaryValid && backgroundValid
    : prefsLoaded && Boolean(primaryModel && backgroundModel);

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------

  const handleBack = useCallback(() => {
    navigate('/setup/models');
  }, [navigate]);

  const handleAdvancedChange = useCallback(
    (updated: { compactionModel?: string; fetchModel?: string; fallbackModels?: string[] }) => {
      setAdvancedModels((prev) => ({ ...prev, ...updated }));
    },
    [],
  );

  const handleNext = useCallback(async () => {
    if (!canContinue) return;

    setSaving(true);
    setError(null);

    try {
      // Leave unset rather than pinning to today's Background pick: the server
      // resolves a blank fetch model from the Background model, and a
      // blank compaction model from the conversation's own, at turn time, so a
      // later change in Settings keeps following it.
      if (isDaltonBuild) {
        // DALTON-HOOK(dalton-setup): preserve untouched saved advanced routing models in Dalton; only write changed fields
        const sameList = (a: readonly string[], b: readonly string[]) =>
          a.length === b.length && a.every((m, i) => m === b[i]);
        const changedSlot = (next: string, initial: string): string | null | undefined =>
          next === initial ? undefined : (!next ? null : curatedSlotFillable.includes(next) ? next : undefined);
        const compaction = changedSlot(advancedModels.compactionModel, initialAdvanced.compactionModel);
        const fetchModel = changedSlot(advancedModels.fetchModel, initialAdvanced.fetchModel);
        const fallbacks = sameList(advancedModels.fallbackModels, initialAdvanced.fallbackModels)
          ? undefined
          : advancedModels.fallbackModels.filter((m) =>
              initialAdvanced.fallbackModels.includes(m)
                ? fillable.includes(m)
                : curatedSlotFillable.includes(m),
            );

        await updatePreferences.mutateAsync(splitPreferenceWrite({
          preferred_model: primaryModel,
          preferred_background_model: backgroundModel,
          ...(compaction !== undefined ? { compaction_model: compaction } : {}),
          ...(fetchModel !== undefined ? { fetch_model: fetchModel } : {}),
          ...(fallbacks !== undefined && !sameList(fallbacks, initialAdvanced.fallbackModels)
            ? { fallback_models: fallbacks }
            : {}),
        }));
      } else {
        // DALTON-HOOK(dalton-setup): preserve upstream unconditional advanced-field write in non-Dalton builds
        await updatePreferences.mutateAsync(splitPreferenceWrite({
          preferred_model: primaryModel,
          preferred_background_model: backgroundModel,
          compaction_model: advancedModels.compactionModel || null,
          fetch_model: advancedModels.fetchModel || null,
          fallback_models: advancedModels.fallbackModels,
        }));
      }

      navigate('/setup/ready');
    } catch (e: unknown) {
      const detail = apiErrorDetail(e);
      setError(typeof detail === 'string' ? detail : (e instanceof Error && e.message) || t('setup:errorSavePrefs'));
    } finally {
      setSaving(false);
    }
  }, [canContinue, primaryModel, backgroundModel, initialAdvanced, advancedModels, fillable, curatedSlotFillable, updatePreferences, navigate, t]);

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  // DALTON-HOOK(antigravity-series): format Antigravity keys purely as series names without effort suffix in Dalton onboarding
  const formatModelName = (m: string) =>
    formatAntigravityVariantLabel(m, { seriesCatalog, rawMetadata, customModels }) ?? modelLabel(m, metadata);

  // DALTON-HOOK(dalton-setup): wait for team-model reads and surface retry on failed reads
  if (isLoading || (isDaltonBuild && team.isLoading)) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader size={20} className="text-(--color-text-tertiary)" label={t('common.loading')} />
      </div>
    );
  }
  if (isDaltonBuild && (team.unread || team.all.unread)) {
    return <StepLoadFailed onRetry={team.retry} />;
  }

  // In platform mode, models are already tier-filtered by useAllModels.
  // In OSS mode, they're filtered by configured providers.
  // Either way, `models` is the correct set to display.

  return (
    <div>
      <WizardStepHeader titleId={headingId} title={t('setup:chooseYourModels')} helper={copy.chooseDesc} />
      <div className="flex flex-col gap-4 sm:gap-6">
      {teamEmpty ? (
        /* DALTON-HOOK(dalton-setup): reuse DefaultStep's contact-admin & retry notice when team models are empty */
        <div
          data-testid="setup-defaults-team-models-empty"
          className="shell-box flex items-center justify-between gap-3 px-4 py-3"
        >
          <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            {t('setup:answers.default.teamModelsEmpty')}
          </p>
          <RowButton onClick={team.retry}>{t('setup:answers.tryAgain')}</RowButton>
        </div>
      ) : (
        <>
      {/* Model access reminder: a neutral panel with the accent on its icon
          alone; amber annotates, it does not wash or ring (DESIGN.md, accent
          discipline). */}
      <div className="shell-box flex items-start gap-2.5 px-3.5 py-3">
        <Info
          className="h-4 w-4 shrink-0 mt-0.5"
          style={{ color: 'var(--color-accent-primary)' }}
        />
        <p
          className="text-xs leading-relaxed"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          {/* DALTON-HOOK(dalton-setup): show team-configured note only when Dalton team models are actually available */}
          {isDaltonBuild && team.available ? t('setup:daltonModelAccessNote') : t('setup:modelAccessReminder')}
        </p>
      </div>

      {/* DALTON-HOOK(dalton-setup): pass curated primary models under Dalton; background retains slot models for Hydrogen Low */}
      {/* DALTON-HOOK(antigravity-series): collapse background effort variants to -low & omit effort suffix via series catalog */}
      <ModelTierConfig
        models={curatedModels}
        backgroundModels={visibleSlotModels}
        primaryModel={primaryModel}
        onPrimaryModelChange={setPrimaryModel}
        backgroundModel={backgroundModel}
        onBackgroundModelChange={setBackgroundModel}
        showExplainer
        payers={visiblePayers}
        metadata={metadata}
        seriesCatalog={seriesCatalog}
        rawMetadata={rawMetadata}
        customModels={customModels}
        hideEffortPicker
      />

      {/* Routing models. Folded away because a first run never needs them, and
          named with the same keys the Settings tab uses so one control does not
          read two ways. */}
      <div>
        <button
          type="button"
          onClick={() => setAdvancedOpen((v) => !v)}
          className="inline-flex items-center gap-1 text-xs font-medium transition-colors"
          style={{ color: 'var(--color-text-tertiary)' }}
          aria-expanded={advancedOpen}
        >
          <ChevronRight
            className={cn('h-3 w-3 transition-transform', advancedOpen && 'rotate-90')}
          />
          {t('settings:modelTuning.advanced')}
        </button>

        <AnimatePresence initial={false}>
          {advancedOpen && (
            // Opacity and a short lift, not height: the fold opens at once and
            // fades in, so nothing below it is animated through a layout pass.
            <motion.div
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, transition: TWEEN.exit }}
              transition={TWEEN.quick}
            >
              <div className="flex flex-col pt-4" style={{ gap: '24px' }}>
                {/* DALTON-HOOK(dalton-setup): pass Dalton curated slot models and payers */}
                {/* DALTON-HOOK(antigravity-series): hide effort pickers & format series names in Advanced routing dropdowns */}
                <ModelSelector
                  label={t('settings:modelTuning.fetchModel')}
                  description={t('settings:modelTuning.fetchModelDesc')}
                  value={advancedModels.fetchModel}
                  onChange={(v) => handleAdvancedChange({ fetchModel: v })}
                  models={curatedSlotModels}
                  nameOf={isDaltonBuild ? formatModelName : undefined}
                  missingLabel={isDaltonBuild ? formatModelName : undefined}
                  placeholder={copy.defaultsToBackground}
                  payers={visiblePayers}
                  metadata={metadata}
                  seriesCatalog={seriesCatalog}
                  rawMetadata={rawMetadata}
                  customModels={customModels}
                  hideEffortPicker
                />
                <ModelSelector
                  label={t('settings:modelTuning.compactionModel')}
                  description={t('settings:modelTuning.compactionModelDesc')}
                  value={advancedModels.compactionModel}
                  onChange={(v) => handleAdvancedChange({ compactionModel: v })}
                  models={curatedSlotModels}
                  nameOf={isDaltonBuild ? formatModelName : undefined}
                  missingLabel={isDaltonBuild ? formatModelName : undefined}
                  placeholder={copy.defaultsToConversation}
                  payers={visiblePayers}
                  metadata={metadata}
                  seriesCatalog={seriesCatalog}
                  rawMetadata={rawMetadata}
                  customModels={customModels}
                  hideEffortPicker
                />
                {/* DALTON-HOOK(dalton-setup): pass Dalton curated slot models & payers */}
                {/* DALTON-HOOK(antigravity-series): hide effort pickers & collapse series in FallbackModelsPicker */}
                <FallbackModelsPicker
                  selected={advancedModels.fallbackModels}
                  onChange={(list) => handleAdvancedChange({ fallbackModels: list })}
                  models={curatedSlotModels}
                  metadata={metadata}
                  payers={visiblePayers}
                  seriesCatalog={seriesCatalog}
                  rawMetadata={rawMetadata}
                  customModels={customModels}
                  hideEffortPicker
                />
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
        </>
      )}

      {/* A read that failed: nothing is filled, and nothing can be saved over it. */}
      {prefsFailed && (
        <p role="alert" className="text-sm" style={{ color: 'var(--color-loss)' }}>
          {t('settings:failedToLoadModels')}
        </p>
      )}

      {/* Error */}
      {error && (
        <p role="alert" className="text-sm" style={{ color: 'var(--color-loss)' }}>
          {error}
        </p>
      )}
      </div>

      <SetupFooter
        back={{ label: t('setup:back'), onClick: handleBack }}
        primary={{
          label: saving ? t('setup:saving') : t('setup:continue'),
          onClick: handleNext,
          disabled: !canContinue,
          busy: saving,
        }}
      />
    </div>
  );
}
