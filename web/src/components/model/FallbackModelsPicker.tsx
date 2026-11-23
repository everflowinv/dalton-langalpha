import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Search, Pin } from "lucide-react"
import type { CustomModelEntry, ProviderModelsData } from "./types"
import type { ModelMetadataEntry } from "@/hooks/useFilteredModels"
import { modelLabel, modelMatches } from "@/lib/modelLabel"
import { useModelSlotCopy } from "./useModelSlotCopy"
import { PayerBadge } from "./PayerBadge"
import type { Payer } from "@/lib/modelPayer"
// DALTON-HOOK(antigravity-series): collapse Antigravity series in FallbackModelsPicker & format chips as Series · Effort
import {
  buildSelectableAntigravitySeries,
  formatAntigravityVariantLabel,
  indexSelectableSeries,
  inspectSavedAntigravityModel,
  type AntigravitySeriesCatalog,
  type SelectableAntigravitySeries,
} from "@/lib/antigravitySeries"
import { effortLabelFor } from "@/lib/modelTuning"

// ---------------------------------------------------------------------------
// Fallback models picker — add/remove from accessible models
// ---------------------------------------------------------------------------

export function FallbackModelsPicker({
  selected,
  onChange,
  models,
  filterProviders,
  metadata,
  // DALTON-HOOK(settings-section): optional custom label and description for Dalton fallback/reviewer pickers
  label,
  description,
  payers,
  skipped,
  seriesCatalog,
  rawMetadata,
  customModels,
  hideEffortPicker = false,
}: {
  selected: string[]
  onChange: (models: string[]) => void
  models: Record<string, ProviderModelsData>
  filterProviders?: string[]
  /** Optional model metadata: chips and rows print its display_name */
  metadata?: Record<string, ModelMetadataEntry>
  label?: string
  description?: string
  /** Who pays for each model. A fallback only runs on the payer of the turn
   *  it stands in for, so the badge says which turns it can serve. */
  payers?: Record<string, Payer>
  /** Listed but not run on every turn, shown faded; the reason is its title. */
  skipped?: { models: ReadonlySet<string>; reason: string }
  // DALTON-HOOK(antigravity-series): optional series catalog for collapsing & effort-pill picking
  seriesCatalog?: AntigravitySeriesCatalog
  rawMetadata?: Record<string, ModelMetadataEntry>
  customModels?: readonly CustomModelEntry[]
  hideEffortPicker?: boolean
}) {
  const { t } = useTranslation()
  const copy = useModelSlotCopy()
  const [showAdd, setShowAdd] = useState(false)
  const [search, setSearch] = useState("")
  const containerRef = useRef<HTMLDivElement>(null)

  // Close on click outside
  useEffect(() => {
    if (!showAdd) return
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setShowAdd(false)
      }
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [showAdd])

  const selectedSet = useMemo(() => new Set(selected), [selected])

  const handleToggle = useCallback(
    (model: string) => {
      if (selectedSet.has(model)) {
        onChange(selected.filter((m) => m !== model))
      } else {
        onChange([...selected, model])
      }
    },
    [selected, selectedSet, onChange],
  )

  const handleRemove = useCallback(
    (model: string) => onChange(selected.filter((m) => m !== model)),
    [selected, onChange],
  )

  // DALTON-HOOK(antigravity-series): series chip labels & selectable series index
  const rawMeta = rawMetadata ?? metadata
  const seriesOpts = useMemo(
    () => ({ seriesCatalog, rawMetadata: rawMeta, customModels }),
    [seriesCatalog, rawMeta, customModels],
  )
  const chipLabel = useCallback(
    (m: string) => formatAntigravityVariantLabel(m, { ...seriesOpts, t, includeEffort: true }) ?? modelLabel(m, metadata),
    [seriesOpts, t, metadata],
  )
  const selectableById = useMemo(
    () => indexSelectableSeries(
      buildSelectableAntigravitySeries(seriesCatalog, models, {
        rawMetadata: rawMeta,
        customModels,
      }),
    ),
    [seriesCatalog, models, rawMeta, customModels],
  )

  type PickerItem =
    | { kind: "model"; model: string }
    | { kind: "series"; selectable: SelectableAntigravitySeries }

  // DALTON-HOOK(antigravity-series): Filter providers for the grouped dropdown, collapsing Antigravity variants by series
  const filteredGroups = useMemo(() => {
    const query = search.trim().toLowerCase()
    const groups: { provider: string; displayName: string; items: PickerItem[] }[] = []
    for (const [provider, pd] of Object.entries(models)) {
      if (filterProviders && !filterProviders.includes(provider)) continue
      const provModels = pd.models ?? []
      const seenSeries = new Set<string>()
      const items: PickerItem[] = []
      for (const m of provModels) {
        const hit = inspectSavedAntigravityModel(m, seriesOpts)
        if (!hit) {
          if (modelMatches(m, query, metadata)) items.push({ kind: "model", model: m })
          continue
        }
        const sid = hit.series.series_id
        if (seenSeries.has(sid)) continue
        seenSeries.add(sid)
        const selectable = selectableById[sid]
        if (!selectable) continue
        const matchesSeries = !query
          || selectable.displayName.toLowerCase().includes(query)
          || selectable.series.series_prefix.toLowerCase().includes(query)
          || Object.values(selectable.variants).some((v) => modelMatches(v, query, metadata))
        if (matchesSeries) items.push({ kind: "series", selectable })
      }
      if (items.length > 0) {
        groups.push({
          provider,
          displayName: pd.display_name ?? provider.charAt(0).toUpperCase() + provider.slice(1),
          items,
        })
      }
    }
    return groups
  }, [models, filterProviders, search, metadata, seriesOpts, selectableById])

  return (
    <div ref={containerRef} className="flex flex-col gap-1.5">
      {/* DALTON-HOOK(settings-section): render custom label/description when provided */}
      <label
        className="text-sm font-medium"
        style={{ color: "var(--color-text-primary)" }}
      >
        {label ?? t("settings.fallbackModels")}
      </label>
      <p
        className="text-xs leading-relaxed"
        style={{ color: "var(--color-text-tertiary)" }}
      >
        {description ?? copy.fallbackDesc}
      </p>

      {/* Selected chips with remove */}
      <div className="flex flex-wrap gap-1.5 mt-1">
        {selected.map((m) => (
          <span
            key={m}
            className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs"
            title={skipped?.models.has(m) ? skipped.reason : undefined}
            style={{
              background: "var(--color-bg-surface)",
              border: "1px solid var(--color-border-default)",
              color: "var(--color-text-secondary)",
              opacity: skipped?.models.has(m) ? 0.5 : undefined,
            }}
          >
            {/* DALTON-HOOK(antigravity-series): format stored variant chip as Series · Effort */}
            {chipLabel(m)}
            <PayerBadge payer={payers?.[m]} />
            <button
              type="button"
              onClick={() => handleRemove(m)}
              className="ml-0.5 hover:opacity-70"
              style={{ color: "var(--color-text-tertiary)" }}
              aria-label={t("settings.removeModel", { model: chipLabel(m) })}
            >
              &times;
            </button>
          </span>
        ))}

        {/* Add button */}
        <button
          type="button"
          onClick={() => { setShowAdd((v) => !v); setSearch("") }}
          className="inline-flex items-center px-2 py-1 rounded text-xs font-medium"
          style={{
            border: "1px dashed var(--color-border-default)",
            color: "var(--color-accent-primary)",
          }}
        >
          + {t("settings.addModels")}
        </button>
      </div>

      {/* Grouped searchable picker */}
      {showAdd && (
        <div
          className="rounded-lg mt-1 overflow-hidden"
          style={{
            border: "1px solid var(--color-border-muted)",
            background: "var(--color-bg-card)",
          }}
        >
          {/* Search */}
          <div className="px-3 pt-3 pb-2">
            <div className="relative">
              <Search
                className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5"
                style={{ color: "var(--color-text-tertiary)" }}
              />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t("common.search")}
                className="w-full rounded-md pl-8 pr-3 py-1.5 text-xs"
                style={{
                  backgroundColor: "var(--color-bg-elevated)",
                  border: "1px solid var(--color-border-muted)",
                  color: "var(--color-text-primary)",
                }}
                autoFocus
              />
            </div>
          </div>
          {/* Provider groups */}
          {/* DALTON-HOOK(antigravity-series): render collapsed Antigravity series rows with optional inline effort pills */}
          <div className="px-1 pb-1 max-h-[280px] overflow-y-auto">
            {filteredGroups.map(({ provider, displayName, items }) => (
              <div key={provider} className="mb-1">
                <div
                  className="px-2 py-1 text-[0.625rem] font-semibold uppercase tracking-wider"
                  style={{ color: "var(--color-text-tertiary)" }}
                >
                  {displayName}
                </div>
                {items.map((item) => {
                  if (item.kind === "model") {
                    const m = item.model
                    const isSelected = selectedSet.has(m)
                    return (
                      <button
                        key={m}
                        type="button"
                        onClick={() => handleToggle(m)}
                        className="w-full flex items-center justify-between gap-2 px-2 py-1.5 rounded-md text-xs transition-colors"
                        style={{
                          color: isSelected
                            ? "var(--color-accent-light)"
                            : "var(--color-text-primary)",
                          backgroundColor: isSelected
                            ? "var(--color-accent-soft)"
                            : "transparent",
                        }}
                        onMouseEnter={(e) => {
                          if (!isSelected) e.currentTarget.style.backgroundColor = "var(--color-bg-elevated)"
                        }}
                        onMouseLeave={(e) => {
                          if (!isSelected) e.currentTarget.style.backgroundColor = "transparent"
                        }}
                      >
                        <span>{modelLabel(m, metadata)}</span>
                        <PayerBadge payer={payers?.[m]} className="ml-auto" />
                        {isSelected && (
                          <Pin
                            className="h-3 w-3 shrink-0"
                            style={{ color: "var(--color-accent-primary)" }}
                          />
                        )}
                      </button>
                    )
                  }
                  // DALTON-HOOK(antigravity-series): onboarding single-click series row or inline effort toggles
                  const { selectable } = item
                  const selectedVariants = selectable.efforts.filter((lv) => selectedSet.has(selectable.variants[lv]))
                  const anySelected = selectedVariants.length > 0
                  const defaultSelected = selectedSet.has(selectable.defaultVariantKey)
                  if (hideEffortPicker || selectable.efforts.length <= 1) {
                    const targetKey = selectable.defaultVariantKey
                    return (
                      <button
                        key={selectable.seriesId}
                        type="button"
                        onClick={() => handleToggle(targetKey)}
                        className="w-full flex items-center justify-between gap-2 px-2 py-1.5 rounded-md text-xs transition-colors"
                        style={{
                          color: defaultSelected
                            ? "var(--color-accent-light)"
                            : "var(--color-text-primary)",
                          backgroundColor: defaultSelected
                            ? "var(--color-accent-soft)"
                            : "transparent",
                        }}
                      >
                        <span>{selectable.displayName}</span>
                        <PayerBadge payer={payers?.[targetKey]} className="ml-auto" />
                        {defaultSelected && (
                          <Pin
                            className="h-3 w-3 shrink-0"
                            style={{ color: "var(--color-accent-primary)" }}
                          />
                        )}
                      </button>
                    )
                  }
                  return (
                    <div
                      key={selectable.seriesId}
                      className="flex items-center justify-between gap-2 px-2 row-inset rounded-md text-xs"
                      style={{
                        backgroundColor: anySelected ? "var(--color-accent-soft)" : "transparent",
                      }}
                    >
                      <button
                        type="button"
                        onClick={() => handleToggle(selectable.defaultVariantKey)}
                        className="min-w-0 truncate text-left font-medium hover:underline"
                        style={{
                          color: anySelected ? "var(--color-accent-light)" : "var(--color-text-primary)",
                        }}
                      >
                        {selectable.displayName}
                      </button>
                      <PayerBadge payer={payers?.[selectable.defaultVariantKey]} className="shrink-0" />
                      {/* DALTON-HOOK(antigravity-series): effort toggle buttons inside series row */}
                      <div className="ml-auto flex flex-wrap items-center gap-1">
                        {selectable.efforts.map((lv) => {
                          const variantKey = selectable.variants[lv]
                          const active = selectedSet.has(variantKey)
                          const effText = effortLabelFor(t, lv) ?? lv
                          return (
                            <button
                              key={lv}
                              type="button"
                              aria-label={`${selectable.displayName} ${effText}`}
                              aria-pressed={active}
                              onClick={() => handleToggle(variantKey)}
                              className="px-1.5 py-0.5 rounded text-[0.6875rem] transition-colors"
                              style={{
                                border: active
                                  ? "1px solid var(--color-accent-primary)"
                                  : "1px solid var(--color-border-muted)",
                                color: active
                                  ? "var(--color-accent-light)"
                                  : "var(--color-text-secondary)",
                                backgroundColor: active
                                  ? "var(--color-bg-elevated)"
                                  : "transparent",
                              }}
                            >
                              {effText}
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  )
                })}
              </div>
            ))}
            {filteredGroups.length === 0 && (
              <p className="px-3 py-2 text-xs" style={{ color: "var(--color-text-tertiary)" }}>
                {t("settings.fallbackModelsEmpty")}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

