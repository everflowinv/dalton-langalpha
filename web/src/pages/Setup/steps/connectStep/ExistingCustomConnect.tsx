import { useState, useEffect, useId } from 'react';
import { useNavigate } from 'react-router';
import { useQueryClient } from '@tanstack/react-query';
import { AlertTriangle } from 'lucide-react';
import { Loader } from '@/components/ui/loader';
import { Input } from '@/components/ui/input';
import { WizardStepHeader } from '@/components/ui/wizard';
import { usePreferences } from '@/hooks/usePreferences';
import { useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import { queryKeys } from '@/lib/queryKeys';
import { api } from '@/api/client';
import { useTranslation } from 'react-i18next';
import { useCallback } from 'react';
import { ModalityChips, ModelCheckRow, useModalityState, useProviderConnected, type LocationState } from './shared';
import { SetupFooter } from '../../SetupFooter';
import { modelPrefs } from '@/lib/modelPreferences';
import type { CustomModelEntry } from '@/components/model/types';

/** A discovered model's modalities before any is toggled. */
const NO_MODALITIES: ReadonlySet<string> = new Set();

export function ExistingCustomConnect({ state }: { state: LocationState }) {
  const navigate = useNavigate();
  const connected = useProviderConnected();
  const queryClient = useQueryClient();
  const { preferences } = usePreferences();
  const updatePreferences = useUpdatePreferences();
  const { t } = useTranslation();
  const headingId = useId();
  const availableLabelId = useId();
  const modelNameId = useId();

  const method = state.method ?? 'api_key';
  const isExistingCustom = state.isExistingCustom ?? false;
  const provider = state.provider ?? '';
  const displayName = state.displayName ?? provider;
  const brandKey = state.brandKey ?? provider;
  const dynamicModels = state.dynamicModels ?? false;

  const [customModelName, setCustomModelName] = useState('');
  const [customModelId, setCustomModelId] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { modelModalities, manualModalities, toggleDiscoveredModality, toggleManualModality, buildModalitiesArray } = useModalityState();

  // Dynamic model discovery state (for local providers)
  const [discoveredModels, setDiscoveredModels] = useState<Array<{ id: string; name: string }>>([]);
  const [selectedModelIds, setSelectedModelIds] = useState<Set<string>>(new Set());
  const [loadingModels, setLoadingModels] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);

  // Fetch models from provider when entering isExistingCustom (dynamic providers)
  useEffect(() => {
    if (!isExistingCustom || !dynamicModels || !provider) return;
    let cancelled = false;
    setLoadingModels(true);
    setModelsError(null);
    api.get(`/api/v1/providers/${provider}/models`)
      .then(({ data }) => {
        if (cancelled) return;
        const models = (data.models ?? []) as Array<{ id: string; name: string }>;
        setDiscoveredModels(models);
      })
      .catch((err) => {
        if (cancelled) return;
        const detail = err?.response?.data?.detail;
        setModelsError(typeof detail === 'string' ? detail : t('setup.errorFetchModels'));
      })
      .finally(() => { if (!cancelled) setLoadingModels(false); });
    return () => { cancelled = true; };
  }, [isExistingCustom, dynamicModels, provider, t]);

  const handleBack = useCallback(() => {
    navigate('/setup/provider', { state: { method } });
  }, [navigate, method]);

  // ---------------------------------------------------------------------------
  // ---------------------------------------------------------------------------
  // Add model to existing custom provider
  // ---------------------------------------------------------------------------

  const handleAddModelToExisting = useCallback(async () => {
    // Support both: selected from discovered list, or manual entry
    const hasSelected = selectedModelIds.size > 0;
    const hasManual = customModelName.trim();
    if (!hasSelected && !hasManual) {
      setError(t('setup.errorNoModelName'));
      return;
    }

    // Shadow semantics: collisions with built-in names are allowed. The
    // resolver picks the user's custom entry first when the name matches.

    setSaving(true);
    setError(null);

    try {
      const modelPref = modelPrefs(preferences);
      const existingModels = modelPref.custom_models ?? [];

      const newModels: CustomModelEntry[] = [];

      if (hasSelected) {
        for (const id of selectedModelIds) {
          const entry: CustomModelEntry = { name: id, model_id: id, provider };
          const mods = buildModalitiesArray(modelModalities.get(id) ?? new Set());
          if (mods) entry.input_modalities = mods;
          newModels.push(entry);
        }
      } else {
        const entry: CustomModelEntry = {
          name: customModelName.trim(),
          model_id: customModelId.trim() || customModelName.trim(),
          provider,
        };
        const mods = buildModalitiesArray(manualModalities);
        if (mods) entry.input_modalities = mods;
        newModels.push(entry);
      }

      // Deduplicate: replace existing entries with same name, append truly new ones
      const newNames = new Set(newModels.map((m) => m.name));
      const deduped = existingModels.filter((m) => !newNames.has(m.name));

      await updatePreferences.mutateAsync({
        model_preference: {
          custom_models: [...deduped, ...newModels],
        },
      });

      await queryClient.invalidateQueries({ queryKey: queryKeys.models.all });

      connected({ method, provider, displayName, brandKey });
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      const detail = err?.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : err?.message ?? t('setup.errorAddModel'));
      setSaving(false);
    }
  }, [selectedModelIds, customModelName, customModelId, provider, preferences, updatePreferences, queryClient, connected, method, displayName, brandKey, t, modelModalities, manualModalities, buildModalitiesArray]);

    const toggleModel = (id: string) => {
      setSelectedModelIds((prev) => {
        const next = new Set(prev);
        if (next.has(id)) next.delete(id); else next.add(id);
        return next;
      });
    };

    const hasSelection = selectedModelIds.size > 0 || customModelName.trim();

    return (
      <div>
        <WizardStepHeader
          titleId={headingId}
          title={t('setup.addModelTo', { provider: displayName })}
          helper={
            dynamicModels
              ? t('setup.selectModelsDesc', { defaultValue: 'Select models available on your server.' })
              : t('setup.addModelToDesc')
          }
        />
        <div className="flex flex-col gap-4 sm:gap-6">
        {/* Discovered models list (dynamic providers) */}
        {dynamicModels && (
          <div
            className="rounded-lg p-4 flex flex-col gap-3"
            style={{
              background: 'var(--color-bg-surface)',
              border: '1px solid var(--color-border-default)',
            }}
          >
            <label id={availableLabelId} className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {t('setup.availableModels', { defaultValue: 'Available models' })}
            </label>
            {loadingModels && (
              <div className="flex items-center gap-2 py-4 justify-center">
                <span aria-hidden="true" className="shrink-0">
                  <Loader size={16} className="text-(--color-text-tertiary)" />
                </span>
                <span className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('setup.fetchingModels', { defaultValue: 'Fetching models...' })}
                </span>
              </div>
            )}
            {modelsError && (
              <div role="alert" className="flex items-center gap-2 py-2">
                <AlertTriangle className="h-4 w-4 shrink-0" style={{ color: 'var(--color-warning, #f59e0b)' }} />
                <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
                  {modelsError}
                </span>
              </div>
            )}
            {!loadingModels && !modelsError && discoveredModels.length === 0 && (
              <p className="text-sm py-2" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('setup.noModelsFound', { defaultValue: 'No models found. Make sure your server is running and has models loaded.' })}
              </p>
            )}
            {discoveredModels.length > 0 && (
              <div role="group" aria-labelledby={availableLabelId} className="flex flex-col gap-1.5 max-h-[280px] overflow-y-auto">
                {discoveredModels.map((m) => (
                  <div key={m.id} className="flex flex-col">
                    <ModelCheckRow
                      checked={selectedModelIds.has(m.id)}
                      onToggle={() => toggleModel(m.id)}
                      label={m.id}
                    />
                    {selectedModelIds.has(m.id) && (
                      <ModalityChips
                        active={modelModalities.get(m.id) ?? NO_MODALITIES}
                        onToggle={(mod) => toggleDiscoveredModality(m.id, mod)}
                        className="pl-10 py-1"
                      />
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Manual model entry — fallback or for non-dynamic providers */}
        {(!dynamicModels || (dynamicModels && !loadingModels && discoveredModels.length === 0)) && (
          <div
            className="rounded-lg p-4 flex flex-col gap-3"
            style={{
              background: 'var(--color-bg-surface)',
              border: '1px solid var(--color-border-default)',
            }}
          >
            <label htmlFor={modelNameId} className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
              {t('setup.modelLabel')}
            </label>
            <Input
              id={modelNameId}
              value={customModelName}
              onChange={(e) => {
                setCustomModelName(e.target.value);
                if (!customModelId) setCustomModelId(e.target.value);
              }}
              placeholder={t('setup.modelDisplayNamePlaceholder')}
              autoComplete="off"
            />
            <Input
              value={customModelId}
              onChange={(e) => setCustomModelId(e.target.value)}
              placeholder={t('setup.modelIdPlaceholder')}
              className="font-mono text-xs"
              autoComplete="off"
            />
            <p className="text-[0.6875rem]" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('setup.modelIdHint')}
            </p>
            <ModalityChips active={manualModalities} onToggle={toggleManualModality} className="pt-1" />
          </div>
        )}

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
            onClick: handleAddModelToExisting,
            disabled: !hasSelection,
            busy: saving,
          }}
        />
      </div>
    );
}
