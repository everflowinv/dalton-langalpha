import { useId, useMemo } from "react"
import { useTranslation } from "react-i18next"
import { Select } from "@/components/ui/select"
import type { CustomModelEntry, ProviderModelsData } from "./types"
import type { ModelMetadataEntry } from "@/hooks/useFilteredModels"
import { modelLabel } from "@/lib/modelLabel"
import type { Payer } from "@/lib/modelPayer"
import { usePayerLabel } from "@/hooks/usePayerLabel"
// DALTON-HOOK(antigravity-series): collapse Antigravity variants into series + companion effort dropdown
import {
  buildSelectableAntigravitySeries,
  indexSelectableSeries,
  inspectSavedAntigravityModel,
  resolveLowEffortVariantKey,
  type AntigravitySeriesCatalog,
} from "@/lib/antigravitySeries"
import { effortLabelFor, effortOptions } from "@/lib/modelTuning"

export interface ModelSelectorProps {
  /** Label shown above the select. Absent, `ariaLabel` names it and the
   *  surrounding block's heading is the visible label. */
  label?: string
  ariaLabel?: string
  /** Description shown below the label */
  description?: string
  /** Currently selected model name */
  value: string
  /** Callback when selection changes */
  onChange: (model: string) => void
  /** Available models grouped by provider */
  models: Record<string, ProviderModelsData>
  /** Optional: limit to specific providers (filter the models map) */
  filterProviders?: string[]
  /** Placeholder text when nothing selected */
  placeholder?: string
  /** An option beside the placeholder that names a choice rather than a model */
  autoOption?: { value: string; label: string }
  /** Required field indicator */
  required?: boolean
  /** Who pays for each model, printed after its name: an option holds text only. */
  payers?: Record<string, Payer>
  /** Optional model metadata: options print its display_name in place of the key. */
  metadata?: Record<string, ModelMetadataEntry>
  /** Models listed but not pickable (above the plan), each in its provider's group. */
  disabledModels?: Record<string, ProviderModelsData>
  /** Names a saved value no group lists, which would otherwise show as the
   *  placeholder and hide that the setting points somewhere unusable. */
  missingLabel?: (value: string) => string
  /** How an option prints its model; the manifest's name, else the key. */
  nameOf?: (model: string) => string
  // DALTON-HOOK(automation-model): render the select with placeholder & missingLabel even when models is empty
  allowEmpty?: boolean
  /** Why a listed model is held back, printed after its name; one the user
   *  can fix stays a pick, and the fix is the surrounding block's to offer,
   *  since an option holds text only. */
  heldBy?: (model: string) => { reason: string; pickable: boolean } | null
  // DALTON-HOOK(antigravity-series): optional series catalog for collapsing & companion effort select
  seriesCatalog?: AntigravitySeriesCatalog
  rawMetadata?: Record<string, ModelMetadataEntry>
  customModels?: readonly CustomModelEntry[]
  /** Hide the companion effort selector (used in Setup onboarding and Background slot). */
  hideEffortPicker?: boolean
  /** Pick the series' `low` (or lowest) variant instead of its default effort (used for Background slot). */
  preferLowEffort?: boolean
}

export function ModelSelector({
  label,
  description,
  value,
  onChange,
  models,
  filterProviders,
  placeholder,
  autoOption,
  required = false,
  payers,
  metadata,
  ariaLabel,
  disabledModels,
  missingLabel,
  nameOf,
  allowEmpty = false,
  heldBy,
  // DALTON-HOOK(antigravity-series): optional series catalog, hideEffortPicker, and preferLowEffort props
  seriesCatalog,
  rawMetadata,
  customModels,
  hideEffortPicker = false,
  preferLowEffort = false,
}: ModelSelectorProps) {
  const { t } = useTranslation()
  const payerLabel = usePayerLabel()
  const id = useId()
  const name = nameOf ?? ((m: string) => modelLabel(m, metadata))
  const rawMeta = rawMetadata ?? metadata
  const seriesOpts = useMemo(
    () => ({ seriesCatalog, rawMetadata: rawMeta, customModels }),
    [seriesCatalog, rawMeta, customModels],
  )
  const selectedHit = useMemo(
    () => inspectSavedAntigravityModel(value, seriesOpts),
    [value, seriesOpts],
  )

  const filteredModels = useMemo(() => {
    if (!filterProviders || filterProviders.length === 0) return models
    const result: Record<string, ProviderModelsData> = {}
    for (const provider of filterProviders) {
      if (models[provider]) {
        result[provider] = models[provider]
      }
    }
    return result
  }, [models, filterProviders])

  const hasModels = useMemo(
    () =>
      Object.values(filteredModels).some(
        (pd) => pd.models && pd.models.length > 0,
      ),
    [filteredModels],
  )

  const selectableById = useMemo(
    () => indexSelectableSeries(
      buildSelectableAntigravitySeries(seriesCatalog, filteredModels, {
        rawMetadata: rawMeta,
        customModels,
      }),
    ),
    [seriesCatalog, filteredModels, rawMeta, customModels],
  )

  const groups = useMemo(() => {
    const keys = [...new Set([...Object.keys(filteredModels), ...Object.keys(disabledModels ?? {})])]
    return keys.map((provider) => {
      const rawModelList = filteredModels[provider]?.models ?? []
      const rawLocked = (disabledModels?.[provider]?.models ?? []).filter(
        (m) => !rawModelList.includes(m),
      )
      // DALTON-HOOK(antigravity-series): collapse Antigravity variants per provider group into 1 option
      const seenSeries = new Set<string>()
      const options: Array<{ value: string; label: string }> = []
      for (const m of rawModelList) {
        const hit = inspectSavedAntigravityModel(m, seriesOpts)
        if (!hit) {
          options.push({ value: m, label: name(m) })
          continue
        }
        const sid = hit.series.series_id
        if (seenSeries.has(sid)) continue
        seenSeries.add(sid)
        const sel = selectableById[sid]
        const defaultPickKey = preferLowEffort
          ? (resolveLowEffortVariantKey(sel) ?? sel?.defaultVariantKey ?? m)
          : (sel?.defaultVariantKey ?? m)
        const optionValue = selectedHit?.series.series_id === sid && rawModelList.includes(value)
          ? value
          : defaultPickKey
        options.push({ value: optionValue, label: hit.displayName })
      }
      const lockedOptions: Array<{ value: string; label: string }> = []
      for (const m of rawLocked) {
        const hit = inspectSavedAntigravityModel(m, seriesOpts)
        if (!hit) {
          lockedOptions.push({ value: m, label: name(m) })
          continue
        }
        const sid = hit.series.series_id
        if (seenSeries.has(sid)) continue
        seenSeries.add(sid)
        lockedOptions.push({ value: m, label: hit.displayName })
      }
      return {
        provider,
        display:
          filteredModels[provider]?.display_name ??
          disabledModels?.[provider]?.display_name ??
          provider.charAt(0).toUpperCase() + provider.slice(1),
        options,
        lockedOptions,
      }
    })
  }, [filteredModels, disabledModels, seriesOpts, selectableById, selectedHit, value, name, preferLowEffort])

  const listed =
    !value ||
    value === autoOption?.value ||
    groups.some((g) => g.options.some((o) => o.value === value) || g.lockedOptions.some((o) => o.value === value))

  // DALTON-HOOK(antigravity-series): resolve series effort ladder for companion effort dropdown
  const selectedSeriesSelectable = selectedHit ? selectableById[selectedHit.series.series_id] : undefined
  const seriesEfforts = useMemo(
    () => (selectedHit ? effortOptions(selectedSeriesSelectable?.efforts ?? [], selectedHit.effort) : []),
    [selectedHit, selectedSeriesSelectable],
  )
  const seriesDefaultEffort = selectedHit
    ? (selectedSeriesSelectable?.defaultEffort ?? selectedHit.series.default_effort)
    : null
  const showSeriesEffortPicker = !hideEffortPicker && !!selectedHit && seriesEfforts.length > 1

  return (
    <div className="flex flex-col gap-1.5">
      {label && <label
        htmlFor={id}
        className="block font-medium"
        style={{
          fontSize: "0.875rem",
          fontWeight: 500,
          color: "var(--color-text-secondary)",
        }}
      >
        {label}
        {required && (
          <span
            className="ml-0.5"
            style={{ color: "var(--color-loss, #ef4444)" }}
            aria-hidden="true"
          >
            *
          </span>
        )}
      </label>}

      {description && (
        <p
          className="mt-0"
          style={{
            fontSize: "0.75rem",
            fontWeight: 400,
            color: "var(--color-text-tertiary)",
            margin: 0,
          }}
        >
          {description}
        </p>
      )}

      {/* DALTON-HOOK(automation-model): allow rendering default & saved-unavailable options when models is empty */}
      {/* DALTON-HOOK(antigravity-series): render companion effort dropdown beside Antigravity series select */}
      {hasModels || allowEmpty ? (
        <div className={showSeriesEffortPicker ? "flex items-center gap-2" : undefined}>
          <Select
            id={id}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            required={required}
            aria-label={label ? undefined : ariaLabel}
            className={showSeriesEffortPicker ? "min-w-0 flex-1" : undefined}
          >
            <option value="">{placeholder ?? t("settings:selectModel")}</option>
            {autoOption && <option value={autoOption.value}>{autoOption.label}</option>}
            {!listed && missingLabel && <option value={value}>{missingLabel(value)}</option>}
            {groups.map(({ provider, display, options, lockedOptions }) => {
              if (options.length === 0 && lockedOptions.length === 0) return null
              return (
                <optgroup key={provider} label={display}>
                  {options.map((opt) => {
                    const payer = payers?.[opt.value]
                    const held = heldBy?.(opt.value)
                    const suffix = [payer && payerLabel(payer), held?.reason].filter(Boolean).map((s) => ` · ${s}`).join("")
                    return (
                      <option key={opt.value} value={opt.value} disabled={held ? !held.pickable : undefined}>
                        {opt.label}{suffix}
                      </option>
                    )
                  })}
                  {lockedOptions.map((opt) => (
                    <option key={opt.value} value={opt.value} disabled>
                      {opt.label} · {t("payer.locked")}
                    </option>
                  ))}
                </optgroup>
              )
            })}
          </Select>
          {/* DALTON-HOOK(antigravity-series): companion effort dropdown for selected Antigravity series */}
          {showSeriesEffortPicker && selectedHit && (
            <Select
              aria-label={`${label ?? ariaLabel ?? selectedHit.displayName} ${t("chat.modelSelector.reasoningEffort")}`}
              value={selectedHit.effort}
              onChange={(e) => {
                const nextEffort = e.target.value
                const nextKey = selectedSeriesSelectable?.variants[nextEffort]
                  ?? selectedHit.series.variants[nextEffort]
                if (nextKey && nextKey !== value) onChange(nextKey)
              }}
              className="w-auto min-w-28 shrink-0"
            >
              {seriesEfforts.map((lv) => {
                const effLabel = effortLabelFor(t, lv) ?? lv
                const text = lv === seriesDefaultEffort
                  ? `${effLabel} (${t("chat.modelSelector.defaultBadge")})`
                  : effLabel
                return (
                  <option key={lv} value={lv}>
                    {text}
                  </option>
                )
              })}
            </Select>
          )}
        </div>
      ) : (
        <p
          className="text-sm py-2"
          style={{ color: "var(--color-text-tertiary)" }}
        >
          {t("settings:noModelsAvailable")}
        </p>
      )}
    </div>
  )
}
