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
import { getModelDisplayName } from '@/components/ui/chat-input.helpers';
import { isDaltonBuild } from '@/config/brand';
import {
  DALTON_BACKGROUND_MODEL,
  DALTON_DEFAULT_MODEL,
  dedupeDaltonBackgroundModels,
  filterDaltonPayers,
  filterDaltonProviderModels,
  stripEffortFromModelLabel,
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
  const { models, slotModels, metadata, payers, systemDefaults, isLoading } = team.all;
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
  const [advancedModels, setAdvancedModels] = useState<{
    compactionModel: string;
    fetchModel: string;
    fallbackModels: string[];
  }>({
    compactionModel: modelPref.compaction_model ?? '',
    fetchModel: modelPref.fetch_model ?? '',
    fallbackModels: modelPref.fallback_models ?? [],
  });
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A first run starts with a default model rather than an empty required
  // slot: the deployment's own when this user can run it, else the first model
  // they just picked. Set during render like the Background fill below.
  // From the slot models, so the fill never lands on a conversation-only
  // model (Claude Code); the user may still pick one.
  // DALTON-HOOK(dalton-setup): validate picks against currently available models and seed from Dalton-filtered slots
  const availablePrimary = Object.values(visibleModels).flatMap((p) => p.models ?? []);
  const fillable = Object.values(visibleSlotModels).flatMap((p) => p.models ?? []);
  const primaryValid = Boolean(primaryModel) && availablePrimary.includes(primaryModel);
  const backgroundValid = Boolean(backgroundModel) && fillable.includes(backgroundModel);
  const catalogReady = !isLoading && !(isDaltonBuild && team.isLoading) && !team.unread;
  const primaryFill = !primaryValid && catalogReady && prefsLoaded
    ? (isDaltonBuild
      ? [DALTON_DEFAULT_MODEL, systemDefaults?.default_model, ...starredModels(preferences)]
      : [systemDefaults?.default_model, ...starredModels(preferences)]
    ).find(
        (m): m is string => typeof m === 'string' && fillable.includes(m) && availablePrimary.includes(m),
      ) ?? fillable.find((m) => availablePrimary.includes(m))
    : undefined;
  if (primaryFill && primaryFill !== primaryModel) setPrimaryModel(primaryFill);

  // A first run fills both defaults, so an empty Background slot takes a model to
  // go with the primary, set during render so the slot never paints empty.
  // The fill is a pick like any other: it stays when the primary changes, and
  // nothing is saved before Continue.
  // DALTON-HOOK(dalton-setup): validate both primary and background against the live catalog before enabling Continue
  const effectivePrimary = primaryValid ? primaryModel : (primaryFill ?? '');
  const backgroundFill = !backgroundValid && effectivePrimary && catalogReady && prefsLoaded
    ? (isDaltonBuild && fillable.includes(DALTON_BACKGROUND_MODEL)
      ? DALTON_BACKGROUND_MODEL
      : suggestBackgroundModel(visibleSlotModels, effectivePrimary))
    : undefined;
  if (backgroundFill && backgroundFill !== backgroundModel) setBackgroundModel(backgroundFill);

  const canContinue = prefsLoaded && catalogReady && primaryValid && backgroundValid;

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
      // DALTON-HOOK(dalton-setup): drop unavailable advanced routing models before saving under Dalton
      const validSlot = (m: string) => (isDaltonBuild ? (fillable.includes(m) ? m : null) : (m || null));
      const compaction = validSlot(advancedModels.compactionModel);
      const fetchModel = validSlot(advancedModels.fetchModel);
      const fallbacks = isDaltonBuild
        ? advancedModels.fallbackModels.filter((m) => fillable.includes(m))
        : advancedModels.fallbackModels;

      await updatePreferences.mutateAsync(splitPreferenceWrite({
        preferred_model: primaryModel,
        preferred_background_model: backgroundModel,
        compaction_model: compaction,
        fetch_model: fetchModel,
        fallback_models: fallbacks,
      }));

      navigate('/setup/ready');
    } catch (e: unknown) {
      const detail = apiErrorDetail(e);
      setError(typeof detail === 'string' ? detail : (e instanceof Error && e.message) || t('setup.errorSavePrefs'));
    } finally {
      setSaving(false);
    }
  }, [canContinue, primaryModel, backgroundModel, advancedModels, fillable, updatePreferences, navigate, t]);

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  // DALTON-HOOK(dalton-setup): wait for team-model reads and surface retry on failed reads
  if (isLoading || (isDaltonBuild && team.isLoading)) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader size={20} className="text-(--color-text-tertiary)" label={t('common.loading')} />
      </div>
    );
  }
  if (isDaltonBuild && team.shared && team.unread) {
    return <StepLoadFailed onRetry={team.retry} />;
  }

  // In platform mode, models are already tier-filtered by useAllModels.
  // In OSS mode, they're filtered by configured providers.
  // Either way, `models` is the correct set to display.
  const teamEmpty = isDaltonBuild && team.shared && (team.empty || availablePrimary.length === 0);

  return (
    <div>
      <WizardStepHeader titleId={headingId} title={t('setup.chooseYourModels')} helper={copy.chooseDesc} />
      <div className="flex flex-col gap-4 sm:gap-6">
      {teamEmpty ? (
        /* DALTON-HOOK(dalton-setup): reuse DefaultStep's contact-admin & retry notice when team models are empty */
        <div
          data-testid="setup-defaults-team-models-empty"
          className="shell-box flex items-center justify-between gap-3 px-4 py-3"
        >
          <p className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
            {t('setup.answers.default.teamModelsEmpty')}
          </p>
          <RowButton onClick={team.retry}>{t('setup.answers.tryAgain')}</RowButton>
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
          {isDaltonBuild && team.available ? t('setup.daltonModelAccessNote') : t('setup.modelAccessReminder')}
        </p>
      </div>

      {/* DALTON-HOOK(dalton-setup): dedupe background effort variants & omit effort suffix in Dalton */}
      <ModelTierConfig
        models={visibleModels}
        backgroundModels={dedupeDaltonBackgroundModels(visibleSlotModels, isDaltonBuild)}
        primaryModel={primaryModel}
        onPrimaryModelChange={setPrimaryModel}
        backgroundModel={backgroundModel}
        onBackgroundModelChange={setBackgroundModel}
        backgroundNameOf={isDaltonBuild ? (m) => stripEffortFromModelLabel(getModelDisplayName(m, metadata)) : undefined}
        showExplainer
        payers={visiblePayers}
        metadata={metadata}
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
          {t('settings.modelTuning.advanced')}
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
                {/* DALTON-HOOK(dalton-setup): pass Dalton-filtered slot models and payers */}
                <ModelSelector
                  label={t('settings.modelTuning.fetchModel')}
                  description={t('settings.modelTuning.fetchModelDesc')}
                  value={advancedModels.fetchModel}
                  onChange={(v) => handleAdvancedChange({ fetchModel: v })}
                  models={visibleSlotModels}
                  placeholder={copy.defaultsToBackground}
                  payers={visiblePayers}
                  metadata={metadata}
                />
                <ModelSelector
                  label={t('settings.modelTuning.compactionModel')}
                  description={t('settings.modelTuning.compactionModelDesc')}
                  value={advancedModels.compactionModel}
                  onChange={(v) => handleAdvancedChange({ compactionModel: v })}
                  models={visibleSlotModels}
                  placeholder={copy.defaultsToConversation}
                  payers={visiblePayers}
                  metadata={metadata}
                />
                {/* DALTON-HOOK(dalton-setup): pass Dalton-filtered slot models & payers */}
                <FallbackModelsPicker
                  selected={advancedModels.fallbackModels}
                  onChange={(list) => handleAdvancedChange({ fallbackModels: list })}
                  models={visibleSlotModels}
                  metadata={metadata}
                  payers={visiblePayers}
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
          {t('settings.failedToLoadModels')}
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
        back={{ label: t('setup.back'), onClick: handleBack }}
        primary={{
          label: saving ? t('setup.saving') : t('setup.continue'),
          onClick: handleNext,
          disabled: !canContinue,
          busy: saving,
        }}
      />
    </div>
  );
}
