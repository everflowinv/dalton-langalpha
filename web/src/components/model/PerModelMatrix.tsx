import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { usePreferences } from '@/hooks/usePreferences';
import { useFeatureEnabled } from '@/hooks/useFeatures';
import { useModelProfileWriter } from '@/hooks/useModelProfile';
import { modelPrefs } from '@/lib/modelPreferences';
import { Select } from '@/components/ui/select';
import { CompactSelect, EffortSelect } from './EffortSelect';
import {
  COMPACTION_PROFILE_ORDER,
  EFFORT_LABELS,
  GUIDANCE_LABELS,
  GUIDANCE_LEVELS,
  SPEED_LABELS,
  hasEffortOptions,
  isCodexModel,
  resolveTuning,
} from '@/lib/modelTuning';
import type { GuidanceLevel } from '@/lib/modelTuning';
import type { CompactionProfileCatalog, CompactionProfileName } from '@/hooks/useAllModels';
import type { ModelMetadataEntry } from '@/hooks/useFilteredModels';
import { modelLabel } from '@/lib/modelLabel';
// DALTON-HOOK(antigravity-series): collapse addable Antigravity variants by series, allow picking explicit effort, & label rows as Series · Effort
import type { CustomModelEntry } from './types';
import {
  formatAntigravityVariantLabel,
  inspectSavedAntigravityModel,
  type AntigravitySeriesCatalog,
} from '@/lib/antigravitySeries';
import { EFFORT_ORDER, effortLabelFor, resolveEffort } from '@/lib/modelTuning';

export interface PerModelMatrixProps {
  metadata: Record<string, ModelMetadataEntry>;
  validModelNames: Set<string>;
  compactionProfiles: CompactionProfileCatalog | null;
  /** Deployment-pinned `prompt.guidance`; null on the `auto` default. */
  pinnedGuidance?: string | null;
  seriesCatalog?: AntigravitySeriesCatalog;
  rawMetadata?: Record<string, ModelMetadataEntry>;
  customModels?: readonly CustomModelEntry[];
}

/** A cell whose setting the model has no control for — stated, not left blank,
 *  so an empty cell always means "inherits" and never "impossible". */
function NotSupported({ label }: { label: string }) {
  return (
    <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
      {label}
    </span>
  );
}

/**
 * Every model the user has tuned, one row each.
 *
 * The table is the only place that answers "what have I overridden, and where?"
 * — the composer menu can set a model's effort but never shows that five other
 * models carry settings of their own. An empty cell reads as the account
 * default, so the inheritance is on screen rather than implied.
 */
export function PerModelMatrix({
  metadata,
  validModelNames,
  compactionProfiles,
  pinnedGuidance = null,
  // DALTON-HOOK(antigravity-series): series labeling & collapsed addable options
  seriesCatalog,
  rawMetadata,
  customModels,
}: PerModelMatrixProps) {
  const { t } = useTranslation();
  const { preferences } = usePreferences();
  const { writeProfile, clearProfile } = useModelProfileWriter();
  // Rows the user opened but has not set anything on yet. They carry no server
  // state — a profile only exists once a field is picked.
  const [pendingRows, setPendingRows] = useState<string[]>([]);
  // DALTON-HOOK(antigravity-series): track selected series + explicit effort when adding an Antigravity series row
  const [selectedAddSeriesId, setSelectedAddSeriesId] = useState<string | null>(null);
  const [selectedAddEffort, setSelectedAddEffort] = useState<string>('');
  const rawMeta = rawMetadata ?? metadata;
  const seriesOpts = useMemo(
    () => ({ seriesCatalog, rawMetadata: rawMeta, customModels }),
    [seriesCatalog, rawMeta, customModels],
  );
  const rowLabel = (m: string) =>
    formatAntigravityVariantLabel(m, { ...seriesOpts, t, includeEffort: true }) ?? modelLabel(m, metadata);

  const account = useMemo(() => modelPrefs(preferences), [preferences]);
  const profiles = useMemo(() => account.profiles ?? {}, [account]);

  const rows = useMemo(() => {
    const configured = Object.keys(profiles).filter(
      (m) => profiles[m] && Object.keys(profiles[m]).length > 0,
    );
    return [...new Set([...configured, ...pendingRows])].sort();
  }, [profiles, pendingRows]);

  // A profile whose model the user cannot currently reach still gets a row.
  // Its metadata is gone, so the cells have no ladder to offer and render
  // inert -- but the settings are live the moment the model comes back, and a
  // hidden row is one the user can neither see nor clear. Nothing scrubs these
  // either: the server-side sweep only fires on an unresolvable *selected*
  // model, so a profile alone never reaches it.
  const unreachable = useMemo(
    () => new Set(rows.filter((m) => !validModelNames.has(m))),
    [rows, validModelNames],
  );

  // DALTON-HOOK(antigravity-series): collapse Antigravity variants in "+ Add model" by series while allowing explicit effort selection
  type AddableOption =
    | { kind: 'model'; value: string; label: string }
    | {
        kind: 'series';
        value: string;
        seriesId: string;
        label: string;
        defaultEffort: string;
        efforts: string[];
        variants: Record<string, string>;
      };

  const addableOptions = useMemo<AddableOption[]>(() => {
    const raw = [...validModelNames].filter((m) => !rows.includes(m)).sort();
    const seriesBuckets = new Map<
      string,
      { displayName: string; defaultEffort: string; totalVariants: number; variants: Map<string, string> }
    >();
    const nonSeries: AddableOption[] = [];
    for (const m of raw) {
      const hit = inspectSavedAntigravityModel(m, seriesOpts);
      if (!hit) {
        nonSeries.push({ kind: 'model', value: m, label: modelLabel(m, metadata) });
        continue;
      }
      const sid = hit.series.series_id;
      let bucket = seriesBuckets.get(sid);
      if (!bucket) {
        bucket = {
          displayName: hit.displayName,
          defaultEffort: hit.series.default_effort,
          totalVariants: Object.keys(hit.series.variants).length,
          variants: new Map(),
        };
        seriesBuckets.set(sid, bucket);
      }
      bucket.variants.set(hit.effort, m);
    }
    const collapsedSeries: AddableOption[] = [];
    for (const [seriesId, bucket] of seriesBuckets.entries()) {
      const rawEfforts = [...bucket.variants.keys()];
      const orderedEfforts = [
        ...EFFORT_ORDER.filter((lv) => rawEfforts.includes(lv)),
        ...rawEfforts.filter((lv) => !EFFORT_ORDER.includes(lv)),
      ];
      if (orderedEfforts.length === 1) {
        const onlyEffort = orderedEfforts[0];
        const key = bucket.variants.get(onlyEffort);
        if (key) {
          const singleLabel = bucket.totalVariants > 1
            ? (formatAntigravityVariantLabel(key, { ...seriesOpts, t, includeEffort: true }) ?? bucket.displayName)
            : bucket.displayName;
          collapsedSeries.push({ kind: 'model', value: key, label: singleLabel });
        }
        continue;
      }
      const defaultEffort = resolveEffort(bucket.defaultEffort, orderedEfforts) ?? orderedEfforts[0];
      const variants: Record<string, string> = {};
      for (const lv of orderedEfforts) {
        const key = bucket.variants.get(lv);
        if (key) variants[lv] = key;
      }
      collapsedSeries.push({
        kind: 'series',
        value: `series:${seriesId}`,
        seriesId,
        label: bucket.displayName,
        defaultEffort,
        efforts: orderedEfforts,
        variants,
      });
    }
    return [...nonSeries, ...collapsedSeries].sort((a, b) => a.label.localeCompare(b.label));
  }, [validModelNames, rows, seriesOpts, metadata, t]);

  // DALTON-HOOK(antigravity-series): resolve active series & effort for explicit tier addition
  const activeAddSeries = useMemo(
    () => (
      selectedAddSeriesId
        ? addableOptions.find(
            (opt): opt is Extract<AddableOption, { kind: 'series' }> =>
              opt.kind === 'series' && opt.seriesId === selectedAddSeriesId,
          ) ?? null
        : null
    ),
    [selectedAddSeriesId, addableOptions],
  );
  const activeAddEffort = activeAddSeries
    ? (activeAddSeries.efforts.includes(selectedAddEffort) ? selectedAddEffort : activeAddSeries.defaultEffort)
    : '';

  const profileOptions = useMemo(
    () =>
      COMPACTION_PROFILE_ORDER.filter((name) => compactionProfiles?.[name]).map((name) => ({
        value: name,
        label: t(`settings.compactionProfiles.${name}.label`),
      })),
    [compactionProfiles, t],
  );

  const guidanceOptions = useMemo(
    () => GUIDANCE_LEVELS.map((level) => ({ value: level, label: t(GUIDANCE_LABELS[level]) })),
    [t],
  );

  // The account-wide switch is the provider_compaction feature; with it off
  // there is nothing per model to choose.
  const providerCompaction = useFeatureEnabled('provider_compaction');

  // Each provider's trade-off is stated once under the table rather than per
  // row, and only for the providers a reachable row actually takes.
  const nativeKinds = useMemo(
    () =>
      new Set(
        providerCompaction
          ? rows
              .filter((m) => !unreachable.has(m))
              .map((m) => metadata[m]?.native_compaction)
              .filter((kind) => !!kind)
          : [],
      ),
    [rows, unreachable, metadata, providerCompaction],
  );
  // Every column after the model's but the reset button's.
  const settingColumns = providerCompaction ? 5 : 4;

  const plainDefault = t('settings.modelTuning.default');
  /** "Default" alone says nothing about what will actually happen, so name the
   *  value it resolves to whenever the account or the model declares one. */
  const defaultLabelFor = (value: string | null) =>
    value ? t('settings.modelTuning.defaultNamed', { value }) : plainDefault;

  const drop = (model: string) => {
    setPendingRows((prev) => prev.filter((m) => m !== model));
    clearProfile(model);
  };

  return (
    <div className="flex flex-col gap-2">
      {/* No label of its own: the tab gives this a section heading, the same
          as the account defaults it overrides. */}
      <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
        {t('settings.modelTuning.perModelDesc')}
      </p>

      {rows.length > 0 && (
        <div className="overflow-x-auto">
          {/* Below this width the cells squeeze their labels into ellipses
              instead of the container scrolling, which is the whole point of
              the overflow wrapper. The cells' 1px sides are the browser's
              default cell padding, which the columns were laid out on. */}
          <table className="w-full text-xs" style={{ borderCollapse: 'collapse', minWidth: '800px' }}>
            <thead>
              <tr style={{ color: 'var(--color-text-tertiary)' }}>
                <th className="text-left font-medium py-1 pl-px pr-3">{t('settings.modelTuning.colModel')}</th>
                <th className="text-left font-medium py-1 pl-px pr-3">{t('settings.modelTuning.colEffort')}</th>
                <th className="text-left font-medium py-1 pl-px pr-3">{t('settings.modelTuning.colContext')}</th>
                <th className="text-left font-medium py-1 pl-px pr-3">{t('settings.modelTuning.colGuidance')}</th>
                <th className="text-left font-medium py-1 pl-px pr-3">{t('settings.modelTuning.colSpeed')}</th>
                {providerCompaction && (
                  <th className="text-left font-medium py-1 pl-px pr-3">{t('settings.nativeCompaction')}</th>
                )}
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((model) => {
                if (unreachable.has(model)) {
                  return (
                    <tr key={model} style={{ borderTop: '1px solid var(--color-border-muted)' }}>
                      {/* DALTON-HOOK(antigravity-series): display unreachable variant as Series · Effort */}
                      <td className="py-2 pl-px pr-3 align-middle" style={{ color: 'var(--color-text-tertiary)' }}>
                        {rowLabel(model)}
                      </td>
                      <td className="py-2 pl-px pr-3 align-middle" colSpan={settingColumns}>
                        <NotSupported label={t('settings.modelTuning.modelUnavailable')} />
                      </td>
                      <td className="py-2 px-px align-middle">
                        <button
                          type="button"
                          onClick={() => drop(model)}
                          aria-label={`${t('settings.modelTuning.resetModel')} ${model}`}
                          title={t('settings.modelTuning.resetModel')}
                          className="hover:opacity-70"
                          style={{ color: 'var(--color-text-tertiary)' }}
                        >
                          <X className="h-3.5 w-3.5" />
                        </button>
                      </td>
                    </tr>
                  );
                }
                const meta = metadata[model];
                const { profile, efforts, inherited } = resolveTuning(
                  account,
                  profiles[model] ?? {},
                  meta,
                  pinnedGuidance,
                );
                const storedEffort = profile.reasoning_effort ?? '';
                const storedSpeed = profile.fast_mode ?? null;
                const supportsSpeed = isCodexModel(meta) || storedSpeed !== null;
                const storedNative = profile.native_compaction ?? null;
                // A stored choice stays listed where the provider no longer
                // offers it, so it can still be cleared.
                const offersNative = inherited.native_compaction !== null || storedNative !== null;
                return (
                  <tr key={model} style={{ borderTop: '1px solid var(--color-border-muted)' }}>
                    {/* DALTON-HOOK(antigravity-series): display reachable Antigravity variant as Series · Effort */}
                    <td className="py-2 pl-px pr-3 align-middle" style={{ color: 'var(--color-text-primary)' }}>
                      {rowLabel(model)}
                    </td>
                    <td className="py-2 pl-px pr-3 align-middle">
                      {hasEffortOptions(efforts, storedEffort) ? (
                        <EffortSelect
                          value={storedEffort}
                          onChange={(v) => writeProfile(model, { reasoning_effort: v || null })}
                          ariaLabel={`${model} ${t('settings.modelTuning.colEffort')}`}
                          defaultLabel={defaultLabelFor(
                            inherited.reasoning_effort && EFFORT_LABELS[inherited.reasoning_effort]
                              ? t(EFFORT_LABELS[inherited.reasoning_effort])
                              : null,
                          )}
                          efforts={efforts}
                        />
                      ) : (
                        <NotSupported label={t('settings.modelTuning.notSupported')} />
                      )}
                    </td>
                    <td className="py-2 pl-px pr-3 align-middle">
                      <CompactSelect
                        value={profile.compaction_profile ?? ''}
                        onChange={(v) =>
                          writeProfile(model, { compaction_profile: (v as CompactionProfileName) || null })
                        }
                        ariaLabel={`${model} ${t('settings.modelTuning.colContext')}`}
                        defaultLabel={defaultLabelFor(
                          inherited.compaction_profile
                            ? t(`settings.compactionProfiles.${inherited.compaction_profile}.label`)
                            : null,
                        )}
                        options={profileOptions}
                      />
                    </td>
                    <td className="py-2 pl-px pr-3 align-middle">
                      <CompactSelect
                        value={profile.prompt_guidance ?? ''}
                        onChange={(v) => writeProfile(model, { prompt_guidance: (v as GuidanceLevel) || null })}
                        ariaLabel={`${model} ${t('settings.modelTuning.colGuidance')}`}
                        defaultLabel={defaultLabelFor(t(GUIDANCE_LABELS[inherited.prompt_guidance]))}
                        options={guidanceOptions}
                      />
                    </td>
                    <td className="py-2 pl-px pr-3 align-middle">
                      {supportsSpeed ? (
                        <CompactSelect
                          value={storedSpeed === true ? 'fast' : storedSpeed === false ? 'standard' : ''}
                          onChange={(v) => writeProfile(model, { fast_mode: v === '' ? null : v === 'fast' })}
                          ariaLabel={`${model} ${t('settings.modelTuning.colSpeed')}`}
                          defaultLabel={defaultLabelFor(
                            t(SPEED_LABELS[inherited.fast_mode ? 'fast' : 'standard']),
                          )}
                          options={[
                            { value: 'standard', label: t(SPEED_LABELS.standard) },
                            { value: 'fast', label: t(SPEED_LABELS.fast) },
                          ]}
                        />
                      ) : (
                        <NotSupported label={t('settings.modelTuning.notSupported')} />
                      )}
                    </td>
                    {providerCompaction && (
                      <td className="py-2 pl-px pr-3 align-middle">
                        {offersNative ? (
                          <CompactSelect
                            value={storedNative === true ? 'on' : storedNative === false ? 'off' : ''}
                            onChange={(v) => writeProfile(model, { native_compaction: v === '' ? null : v === 'on' })}
                            ariaLabel={`${model} ${t('settings.nativeCompaction')}`}
                            defaultLabel={defaultLabelFor(
                              inherited.native_compaction === null
                                ? null
                                : t(inherited.native_compaction ? 'settings.nativeCompactionOn' : 'settings.nativeCompactionOff'),
                            )}
                            options={[
                              { value: 'on', label: t('settings.nativeCompactionOn') },
                              { value: 'off', label: t('settings.nativeCompactionOff') },
                            ]}
                          />
                        ) : (
                          <NotSupported label={t('settings.modelTuning.notSupported')} />
                        )}
                      </td>
                    )}
                    <td className="py-2 px-px align-middle">
                      <button
                        type="button"
                        onClick={() => drop(model)}
                        aria-label={`${t('settings.modelTuning.resetModel')} ${model}`}
                        title={t('settings.modelTuning.resetModel')}
                        className="hover:opacity-70"
                        style={{ color: 'var(--color-text-tertiary)' }}
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {nativeKinds.size > 0 && (
        <div className="flex flex-col gap-1">
          {nativeKinds.has('anthropic') && (
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('settings.nativeCompactionAnthropicDesc')}
            </p>
          )}
          {nativeKinds.has('openai') && (
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              {t('settings.nativeCompactionOpenAIDesc')}
            </p>
          )}
        </div>
      )}

      {/* DALTON-HOOK(antigravity-series): "+ Add a model" collapses series and offers explicit effort selection before adding */}
      {addableOptions.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 self-start">
          <Select
            value={activeAddSeries ? activeAddSeries.value : ''}
            onChange={(e) => {
              const nextValue = e.target.value;
              if (!nextValue) {
                setSelectedAddSeriesId(null);
                setSelectedAddEffort('');
                return;
              }
              const picked = addableOptions.find((opt) => opt.value === nextValue);
              if (picked?.kind === 'series') {
                setSelectedAddSeriesId(picked.seriesId);
                setSelectedAddEffort(picked.defaultEffort);
                return;
              }
              setSelectedAddSeriesId(null);
              setSelectedAddEffort('');
              setPendingRows((prev) => (prev.includes(nextValue) ? prev : [...prev, nextValue]));
            }}
            aria-label={t('settings.modelTuning.addModel')}
            className="text-xs"
          >
            <option value="">+ {t('settings.modelTuning.addModel')}</option>
            {addableOptions.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </Select>
          {activeAddSeries && (
            <>
              <Select
                value={activeAddEffort}
                onChange={(e) => setSelectedAddEffort(e.target.value)}
                aria-label={`${activeAddSeries.label} ${t('chat.modelSelector.reasoningEffort')}`}
                className="w-auto min-w-28 text-xs"
              >
                {activeAddSeries.efforts.map((lv) => {
                  const effLabel = effortLabelFor(t, lv) ?? lv;
                  const text = lv === activeAddSeries.defaultEffort
                    ? `${effLabel} (${t('chat.modelSelector.defaultBadge')})`
                    : effLabel;
                  return (
                    <option key={lv} value={lv}>
                      {text}
                    </option>
                  );
                })}
              </Select>
              <button
                type="button"
                onClick={() => {
                  const variantKey = activeAddSeries.variants[activeAddEffort];
                  if (variantKey) {
                    setPendingRows((prev) => (prev.includes(variantKey) ? prev : [...prev, variantKey]));
                  }
                  setSelectedAddSeriesId(null);
                  setSelectedAddEffort('');
                }}
                className="inline-flex items-center px-2.5 py-1.5 rounded-md text-xs font-medium transition-colors"
                style={{
                  border: '1px solid var(--color-border-default)',
                  color: 'var(--color-text-primary)',
                  backgroundColor: 'var(--color-bg-elevated)',
                }}
              >
                + {t('settings.addModels')}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
