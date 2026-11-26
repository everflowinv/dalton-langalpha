import { useMemo, useState } from 'react';
import {
  Copy,
  Eye,
  EyeOff,
  History,
  MoreHorizontal,
  Pencil,
  Plus,
  RotateCcw,
  Search,
  Server,
  Trash2,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { MobileBottomSheet } from '@/components/ui/mobile-bottom-sheet';
import { ModalShell } from '@/components/ui/ModalShell';
import {
  DaltonInlineNotice,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';
import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { useCapabilities } from '@/hooks/useCapabilities';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useLocale } from '@/hooks/useLocale';
import { useModels } from '@/hooks/useModels';
import { formatTimestamp } from '@/lib/format';
import { cn } from '@/lib/utils';
import type {
  TeamCatalogModelItem,
  TeamCatalogPriceSourceKind,
  TeamCatalogProviderVariant,
  TeamCatalogRetiredItem,
} from '@/pages/Settings/utils/daltonApi';
import { TeamModelEditSheet, type ProviderOptionItem, type TeamModelEditMode } from './TeamModelEditSheet';
import { TeamModelHistoryDialog } from './TeamModelHistoryDialog';
import { TeamModelLegacyImportDialog } from './TeamModelLegacyImportDialog';
import { TeamModelRetireDialog } from './TeamModelRetireDialog';
import {
  parseCatalogApiError,
  useRestoreTeamCatalogModel,
  useSetTeamCatalogModelVisibility,
  useTeamModelCatalog,
} from './useTeamModelCatalog';

type StatusFilter = 'all' | 'visible' | 'hidden' | 'retired' | 'team_modified';

function isOAuthIdentifier(value: string): boolean {
  return value.trim().toLowerCase().endsWith('-oauth');
}

function formatContextWindow(ctx: number | null): string {
  if (!ctx) return '-';
  if (ctx >= 1_000_000) {
    const m = ctx / 1_000_000;
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`;
  }
  if (ctx >= 1_000) {
    return `${Math.round(ctx / 1_000)}K`;
  }
  return String(ctx);
}

function formatPriceRate(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '-';
  return `$${value}`;
}

function resolvePriceSourceKind(model: TeamCatalogModelItem): TeamCatalogPriceSourceKind | null {
  const isSub = model.dalton_meta?.billing === 'subscription';
  const meta = isSub ? model.dalton_meta?.notional_pricing : model.pricing_meta;
  if (!meta) return null;
  if (meta.estimated) return 'estimated';
  if (meta.source_kind === 'official' || meta.source_kind === 'estimated' || meta.source_kind === 'third_party') {
    return meta.source_kind;
  }
  return null;
}

/**
 * Team Model Catalog admin card mounted in Settings -> Team Management -> Models & Providers.
 * Supports full model CRUD, catalog provider variants with write-only credentials,
 * visibility toggles, transactional retire/replace, revision history & rollback,
 * legacy overlay import repair, Core capability filtering, and `< 768px` mobile layout.
 */
export function TeamModelCatalogCard() {
  const { t } = useTranslation();
  const locale = useLocale();
  const isMobile = useIsMobile();
  const { isAdmin } = useCallerIdentity();
  const { hasCapability, profile } = useCapabilities();
  const oauthSyncEnabled = hasCapability('native_oauth_sync');
  const isCoreProfile = profile === 'core';

  const catalogQuery = useTeamModelCatalog(true);
  const { models: publicModelsData } = useModels();
  const visibilityMutation = useSetTeamCatalogModelVisibility();
  const restoreMutation = useRestoreTeamCatalogModel();

  const [search, setSearch] = useState('');
  const [providerFilter, setProviderFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [notice, setNotice] = useState<{
    tone: 'success' | 'error' | 'info' | 'warning';
    message: string;
  } | null>(null);

  // Sheet / Dialog states
  const [editSheet, setEditSheet] = useState<{
    open: boolean;
    mode: TeamModelEditMode;
    model: TeamCatalogModelItem | null;
    variant: TeamCatalogProviderVariant | null;
  }>({ open: false, mode: 'create', model: null, variant: null });

  const [retireTarget, setRetireTarget] = useState<TeamCatalogModelItem | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [legacyDialogOpen, setLegacyDialogOpen] = useState(false);
  const [mobileActionModel, setMobileActionModel] = useState<TeamCatalogModelItem | null>(null);
  const [confirmHideLastCurated, setConfirmHideLastCurated] = useState<TeamCatalogModelItem | null>(null);

  const data = catalogQuery.data;
  const revision = data?.revision ?? 0;

  // Enforce Core capability filtering: zero *-oauth exposure when native_oauth_sync is disabled.
  const allModels = useMemo(() => {
    const raw = data?.models ?? [];
    if (oauthSyncEnabled) return raw;
    return raw.filter((m) => !isOAuthIdentifier(m.model_key) && !isOAuthIdentifier(m.provider));
  }, [data?.models, oauthSyncEnabled]);

  const retiredItems = useMemo(() => {
    const raw = data?.retired ?? [];
    if (oauthSyncEnabled) return raw;
    return raw.filter((r) => !isOAuthIdentifier(r.key) && !isOAuthIdentifier(r.provider));
  }, [data?.retired, oauthSyncEnabled]);

  const providerVariants = useMemo(() => {
    const raw = data?.provider_variants ?? [];
    if (oauthSyncEnabled) return raw;
    return raw.filter(
      (v) => !isOAuthIdentifier(v.provider_slug) && !isOAuthIdentifier(v.parent_provider),
    );
  }, [data?.provider_variants, oauthSyncEnabled]);

  const readonlyLegacyRetiredKeys = useMemo(
    () => retiredItems.filter((r) => r.scope === 'readonly_legacy').map((r) => r.key),
    [retiredItems],
  );

  const curatedCandidateSet = useMemo(
    () => new Set(data?.dalton_setup_candidates ?? []),
    [data?.dalton_setup_candidates],
  );

  const visibleCuratedKeys = useMemo(
    () => allModels.filter((m) => m.visible && curatedCandidateSet.has(m.model_key)).map((m) => m.model_key),
    [allModels, curatedCandidateSet],
  );

  const isLastVisibleCuratedEverflow = (modelKey: string): boolean =>
    !isCoreProfile && visibleCuratedKeys.length === 1 && visibleCuratedKeys[0] === modelKey;

  // Build unified provider options for filter dropdown and EditSheet
  const providerOptions = useMemo<ProviderOptionItem[]>(() => {
    const map = new Map<string, ProviderOptionItem>();
    for (const v of providerVariants) {
      map.set(v.provider_slug, {
        provider: v.provider_slug,
        displayName: v.display_name || v.provider_slug,
        isVariant: true,
        sdk: v.sdk,
        billing: v.billing,
        baseUrl: v.base_url,
        credentialConfigured: v.credential_configured,
        keyFingerprint: v.key_fingerprint,
      });
    }
    for (const p of publicModelsData?.provider_catalog ?? []) {
      if (!oauthSyncEnabled && isOAuthIdentifier(p.provider)) continue;
      if (!map.has(p.provider)) {
        map.set(p.provider, {
          provider: p.provider,
          displayName: p.display_name || p.provider,
          isVariant: false,
          sdk: p.sdk,
          billing: p.access_type === 'oauth' || p.access_type === 'coding_plan' ? 'subscription' : 'cash',
        });
      }
    }
    for (const m of allModels) {
      if (!map.has(m.provider)) {
        map.set(m.provider, {
          provider: m.provider,
          displayName: m.provider_display_name || m.provider,
          isVariant: m.catalog_defined_provider,
          billing: m.dalton_meta?.billing === 'subscription' ? 'subscription' : 'cash',
        });
      }
    }
    return [...map.values()].sort((a, b) => a.displayName.localeCompare(b.displayName));
  }, [providerVariants, publicModelsData?.provider_catalog, allModels, oauthSyncEnabled]);

  const parentProviderGroups = useMemo(() => {
    const raw = data?.parent_provider_groups ?? [];
    return oauthSyncEnabled ? raw : raw.filter((g) => !isOAuthIdentifier(g));
  }, [data?.parent_provider_groups, oauthSyncEnabled]);

  const supportedVariantSdks = useMemo(
    () => data?.supported_variant_sdks ?? [],
    [data?.supported_variant_sdks],
  );

  const normalizedQuery = search.trim().toLowerCase();

  const filteredModels = useMemo(() => {
    if (statusFilter === 'retired') return [];
    return allModels.filter((m) => {
      if (providerFilter !== 'all' && m.provider !== providerFilter) return false;
      if (statusFilter === 'visible' && !m.visible) return false;
      if (statusFilter === 'hidden' && m.visible) return false;
      if (statusFilter === 'team_modified' && m.source === 'factory') return false;
      if (normalizedQuery) {
        const hay = `${m.display_name} ${m.model_key} ${m.model_id} ${m.provider} ${m.provider_display_name}`.toLowerCase();
        if (!hay.includes(normalizedQuery)) return false;
      }
      return true;
    });
  }, [allModels, providerFilter, statusFilter, normalizedQuery]);

  const filteredRetired = useMemo(() => {
    if (statusFilter !== 'retired') return [];
    return retiredItems.filter((r) => {
      if (providerFilter !== 'all' && r.provider && r.provider !== providerFilter) return false;
      if (normalizedQuery) {
        const hay = `${r.key} ${r.provider}`.toLowerCase();
        if (!hay.includes(normalizedQuery)) return false;
      }
      return true;
    });
  }, [retiredItems, providerFilter, statusFilter, normalizedQuery]);

  const executeToggleVisibility = async (model: TeamCatalogModelItem, nextVisible: boolean) => {
    setNotice(null);
    try {
      await visibilityMutation.mutateAsync({
        modelKey: model.model_key,
        visible: nextVisible,
        expected_revision: revision,
      });
      setNotice({
        tone: 'success',
        message: nextVisible
          ? t('settings:dalton.teamCatalog.unhideSuccess', { name: model.display_name || model.model_key })
          : t('settings:dalton.teamCatalog.hideSuccess', { name: model.display_name || model.model_key }),
      });
    } catch (err) {
      const parsed = parseCatalogApiError(err, t('settings:dalton.teamCatalog.saveFailed'));
      setNotice({ tone: 'error', message: parsed.message });
    }
  };

  const handleToggleVisibility = (model: TeamCatalogModelItem) => {
    const nextVisible = !model.visible;
    if (!nextVisible && isLastVisibleCuratedEverflow(model.model_key)) {
      setConfirmHideLastCurated(model);
      return;
    }
    void executeToggleVisibility(model, nextVisible);
  };

  const handleRestoreRetired = async (item: TeamCatalogRetiredItem) => {
    setNotice(null);
    try {
      await restoreMutation.mutateAsync({
        modelKey: item.key,
        expected_revision: revision,
      });
      setNotice({
        tone: 'success',
        message: t('settings:dalton.teamCatalog.restoreSuccess', { model: item.key }),
      });
    } catch (err) {
      const parsed = parseCatalogApiError(err, t('settings:dalton.teamCatalog.saveFailed'));
      setNotice({ tone: 'error', message: parsed.message });
    }
  };

  const openEditForModel = (model: TeamCatalogModelItem, mode: 'edit' | 'clone') => {
    const matchedVariant = providerVariants.find((v) => v.provider_slug === model.provider) ?? null;
    setEditSheet({ open: true, mode, model, variant: matchedVariant });
  };

  const statusPills: Array<{ id: StatusFilter; label: string; count: number }> = [
    { id: 'all', label: t('settings:dalton.teamCatalog.filterAll'), count: allModels.length },
    {
      id: 'visible',
      label: t('settings:dalton.teamCatalog.filterVisible'),
      count: allModels.filter((m) => m.visible).length,
    },
    {
      id: 'hidden',
      label: t('settings:dalton.teamCatalog.filterHidden'),
      count: allModels.filter((m) => !m.visible).length,
    },
    {
      id: 'retired',
      label: t('settings:dalton.teamCatalog.filterRetired'),
      count: retiredItems.length,
    },
    {
      id: 'team_modified',
      label: t('settings:dalton.teamCatalog.filterTeamModified'),
      count: allModels.filter((m) => m.source !== 'factory').length,
    },
  ];

  const renderPriceBadge = (model: TeamCatalogModelItem) => {
    const kind = resolvePriceSourceKind(model);
    if (!kind) return null;
    const tone = kind === 'official' ? 'success' : kind === 'estimated' ? 'warning' : 'accent';
    const label =
      kind === 'official'
        ? t('settings:dalton.teamCatalog.priceSourceOfficial')
        : kind === 'estimated'
          ? t('settings:dalton.teamCatalog.priceSourceEstimated')
          : t('settings:dalton.teamCatalog.priceSourceThirdParty');
    return <DaltonStatusBadge tone={tone} label={label} />;
  };

  const renderPricingText = (model: TeamCatalogModelItem) => {
    const isSub = model.dalton_meta?.billing === 'subscription';
    const card = isSub ? model.dalton_meta?.notional_pricing : model.pricing;
    if (!card || (card.input == null && card.output == null)) {
      return isSub
        ? t('settings:dalton.teamCatalog.subscriptionNotionalUnset')
        : '-';
    }
    const prefix = isSub ? `${t('settings:dalton.teamCatalog.notionalPrefix')} ` : '';
    return `${prefix}${formatPriceRate(card.input)} / ${formatPriceRate(card.output)}`;
  };

  const renderModelBadges = (model: TeamCatalogModelItem) => (
    <div className="flex flex-wrap items-center gap-1.5">
      <DaltonStatusBadge
        dot
        tone={model.provider_connected ? 'success' : 'neutral'}
        label={model.provider_display_name || model.provider}
      />
      {model.source === 'team_added' && (
        <DaltonStatusBadge tone="accent" label={t('settings:dalton.teamCatalog.sourceTeamAdded')} />
      )}
      {model.source === 'team_overridden' && (
        <DaltonStatusBadge tone="warning" label={t('settings:dalton.teamCatalog.sourceTeamOverridden')} />
      )}
      {!model.visible && (
        <DaltonStatusBadge tone="neutral" label={t('settings:dalton.teamCatalog.statusHidden')} />
      )}
      {curatedCandidateSet.has(model.model_key) && (
        <DaltonStatusBadge tone="success" label={t('settings:dalton.teamCatalog.curatedBadge')} />
      )}
      {renderPriceBadge(model)}
    </div>
  );

  const renderRatingsText = (model: TeamCatalogModelItem) => {
    const parts: string[] = [];
    if (model.intelligence != null) parts.push(`${t('settings:dalton.teamCatalog.ratingIntelShort')}${model.intelligence}`);
    if (model.speed != null) parts.push(`${t('settings:dalton.teamCatalog.ratingSpeedShort')}${model.speed}`);
    if (model.plain_language != null) parts.push(`${t('settings:dalton.teamCatalog.ratingPlainShort')}${model.plain_language}`);
    return parts.length > 0 ? parts.join(' · ') : '-';
  };

  return (
    <>
      <DaltonSettingsCard
        id="dalton-team-model-catalog"
        testId="dalton-team-model-catalog-card"
        title={t('settings:dalton.teamCatalog.title')}
        description={t('settings:dalton.teamCatalog.description')}
        layout="padded"
        bodyClassName="space-y-4"
        action={
          <div className="flex flex-wrap items-center gap-2 min-w-0">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 text-xs gap-1.5"
              disabled={!isAdmin || catalogQuery.isLoading}
              onClick={() => setHistoryOpen(true)}
              data-testid="team-catalog-history-btn"
            >
              <History className="h-3.5 w-3.5" />
              <span>{t('settings:dalton.teamCatalog.historyButton', { revision })}</span>
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 text-xs gap-1"
              disabled={!isAdmin || catalogQuery.isLoading}
              onClick={() =>
                setEditSheet({ open: true, mode: 'create_variant', model: null, variant: null })
              }
              data-testid="team-catalog-add-variant-btn"
            >
              <Server className="h-3.5 w-3.5" />
              <span>{t('settings:dalton.teamCatalog.addVariantButton')}</span>
            </Button>
            <Button
              type="button"
              size="sm"
              className="h-8 text-xs gap-1"
              disabled={!isAdmin || catalogQuery.isLoading}
              onClick={() =>
                setEditSheet({ open: true, mode: 'create', model: null, variant: null })
              }
              data-testid="team-catalog-add-model-btn"
            >
              <Plus className="h-3.5 w-3.5" />
              <span>{t('settings:dalton.teamCatalog.addModelButton')}</span>
            </Button>
            <DaltonRefreshButton
              busy={catalogQuery.isFetching}
              onClick={() => void catalogQuery.refetch()}
              testId="team-catalog-refresh-btn"
            />
          </div>
        }
      >
        {catalogQuery.isLoading ? (
          <DaltonLoadingRow label={t('settings:dalton.teamCatalog.loading')} />
        ) : catalogQuery.isError ? (
          <DaltonInlineNotice tone="error" testId="team-catalog-load-error">
            {t('settings:dalton.teamCatalog.loadFailed')}
          </DaltonInlineNotice>
        ) : (
          <>
            {/* Legacy overlay import failed alert banner */}
            {data?.legacy_sync_import_status === 'failed' && (
              <DaltonInlineNotice
                tone="warning"
                testId="team-catalog-legacy-failed-banner"
                action={
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      className="h-7 text-xs"
                      onClick={() => setLegacyDialogOpen(true)}
                      data-testid="team-catalog-open-legacy-repair-btn"
                    >
                      {t('settings:dalton.teamCatalog.legacyRepairButton')}
                    </Button>
                  </div>
                }
              >
                <div className="font-medium">
                  {t('settings:dalton.teamCatalog.legacyFailedBannerTitle')}
                </div>
                {data.legacy_sync_import_error && (
                  <div className="text-[11px] opacity-90 mt-0.5 break-all">
                    {data.legacy_sync_import_error}
                  </div>
                )}
              </DaltonInlineNotice>
            )}

            {notice && (
              <DaltonInlineNotice tone={notice.tone} testId="team-catalog-notice">
                {notice.message}
              </DaltonInlineNotice>
            )}

            {/* Catalog-defined provider variants strip */}
            {providerVariants.length > 0 && (
              <div className="space-y-1.5" data-testid="team-catalog-variants-strip">
                <div className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
                  {t('settings:dalton.teamCatalog.variantsHeading', {
                    count: providerVariants.length,
                  })}
                </div>
                <div className="flex flex-wrap gap-2">
                  {providerVariants.map((v) => (
                    <div
                      key={v.provider_slug}
                      className="flex flex-wrap items-center gap-2 rounded-md border px-2.5 py-1.5 text-xs"
                      style={{
                        borderColor: 'var(--color-border-muted)',
                        backgroundColor: 'var(--color-bg-elevated)',
                      }}
                      data-testid={`variant-chip-${v.provider_slug}`}
                    >
                      <span className="font-medium" style={{ color: 'var(--color-text-primary)' }}>
                        {v.display_name}
                      </span>
                      <span className="font-mono text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                        {v.provider_slug}
                      </span>
                      <DaltonStatusBadge
                        dot
                        tone={v.credential_configured ? 'success' : 'warning'}
                        label={
                          v.credential_configured
                            ? v.key_fingerprint || t('settings:dalton.teamCatalog.credentialReady')
                            : t('settings:dalton.teamCatalog.variantCredentialMissing')
                        }
                      />
                      {isAdmin && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-6 px-1.5 text-[11px]"
                          onClick={() =>
                            setEditSheet({ open: true, mode: 'edit_variant', model: null, variant: v })
                          }
                          data-testid={`edit-variant-${v.provider_slug}-btn`}
                        >
                          {t('settings:dalton.teamCatalog.editAction')}
                        </Button>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Filter controls: search + provider select + status pills */}
            <div className="space-y-2.5">
              <div className="flex flex-col sm:flex-row gap-2">
                <div className="relative flex-1 min-w-0">
                  <Search
                    className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5"
                    style={{ color: 'var(--color-text-tertiary)' }}
                    aria-hidden="true"
                  />
                  <Input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder={t('settings:dalton.teamCatalog.searchPlaceholder')}
                    className="pl-8 text-base md:text-sm"
                    data-testid="team-catalog-search-input"
                  />
                </div>
                <select
                  value={providerFilter}
                  onChange={(e) => setProviderFilter(e.target.value)}
                  aria-label={t('settings:dalton.teamCatalog.providerFilterLabel')}
                  className="rounded-md border px-3 py-2 text-base md:text-sm sm:w-52 shrink-0"
                  style={{
                    backgroundColor: 'var(--color-bg-input)',
                    borderColor: 'var(--color-border-muted)',
                    color: 'var(--color-text-primary)',
                  }}
                  data-testid="team-catalog-provider-filter"
                >
                  <option value="all">{t('settings:dalton.teamCatalog.allProvidersOption')}</option>
                  {providerOptions.map((opt) => (
                    <option key={opt.provider} value={opt.provider}>
                      {opt.displayName}
                    </option>
                  ))}
                </select>
              </div>

              <div className="flex flex-wrap items-center gap-1.5" role="tablist">
                {statusPills.map((pill) => {
                  const active = statusFilter === pill.id;
                  return (
                    <button
                      key={pill.id}
                      type="button"
                      role="tab"
                      aria-selected={active}
                      onClick={() => setStatusFilter(pill.id)}
                      data-testid={`team-catalog-filter-${pill.id}`}
                      className={cn(
                        'tap-target rounded-full border px-2.5 py-1 text-xs font-medium transition-colors',
                      )}
                      style={{
                        borderColor: active
                          ? 'var(--color-accent-primary)'
                          : 'var(--color-border-muted)',
                        backgroundColor: active
                          ? 'color-mix(in srgb, var(--color-accent-primary) 12%, transparent)'
                          : 'var(--color-bg-elevated)',
                        color: active
                          ? 'var(--color-accent-primary)'
                          : 'var(--color-text-secondary)',
                      }}
                    >
                      {pill.label} ({pill.count})
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Retired list view */}
            {statusFilter === 'retired' ? (
              filteredRetired.length === 0 ? (
                <div
                  className="rounded-md border border-dashed py-8 text-center text-xs"
                  style={{
                    borderColor: 'var(--color-border-muted)',
                    color: 'var(--color-text-tertiary)',
                  }}
                >
                  {t('settings:dalton.teamCatalog.emptyRetired')}
                </div>
              ) : (
                <div className="space-y-2" data-testid="team-catalog-retired-list">
                  {filteredRetired.map((item) => {
                    const lockedByLegacy = item.scope === 'readonly_legacy' || !item.restorable;
                    return (
                      <div
                        key={item.key}
                        className="flex flex-wrap items-center justify-between gap-3 rounded-lg border px-3.5 py-3"
                        style={{
                          borderColor: 'var(--color-border-muted)',
                          backgroundColor: 'var(--color-bg-card)',
                        }}
                        data-testid={`retired-row-${item.key}`}
                      >
                        <div className="min-w-0 space-y-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <span
                              className="font-mono text-xs font-semibold break-all"
                              style={{ color: 'var(--color-text-primary)' }}
                            >
                              {item.key}
                            </span>
                            {item.provider && (
                              <DaltonStatusBadge tone="neutral" label={item.provider} />
                            )}
                            {lockedByLegacy && (
                              <DaltonStatusBadge
                                tone="warning"
                                label={t('settings:dalton.teamCatalog.legacyLockedBadge')}
                              />
                            )}
                          </div>
                          <div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
                            {lockedByLegacy
                              ? t('settings:dalton.teamCatalog.legacyLockedRestoreHint')
                              : item.retired_at
                                ? t('settings:dalton.teamCatalog.retiredAtMeta', {
                                    time: formatTimestamp(item.retired_at, locale),
                                  })
                                : t('settings:dalton.teamCatalog.teamRetiredMeta')}
                          </div>
                        </div>

                        {isAdmin && (
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            className="h-8 text-xs gap-1"
                            disabled={lockedByLegacy || restoreMutation.isPending}
                            title={
                              lockedByLegacy
                                ? t('settings:dalton.teamCatalog.legacyLockedRestoreHint')
                                : undefined
                            }
                            onClick={() => void handleRestoreRetired(item)}
                            data-testid={`restore-model-${item.key}-btn`}
                          >
                            <RotateCcw className="h-3.5 w-3.5" />
                            <span>{t('settings:dalton.teamCatalog.restoreAction')}</span>
                          </Button>
                        )}
                      </div>
                    );
                  })}
                </div>
              )
            ) : filteredModels.length === 0 ? (
              <div
                className="rounded-md border border-dashed py-8 text-center text-xs"
                style={{
                  borderColor: 'var(--color-border-muted)',
                  color: 'var(--color-text-tertiary)',
                }}
              >
                {t('settings:dalton.teamCatalog.emptyModels')}
              </div>
            ) : isMobile ? (
              /* < 768px Mobile Single-Column Card Stream */
              <div className="space-y-3" data-testid="team-catalog-mobile-cards">
                {filteredModels.map((model) => (
                  <article
                    key={model.model_key}
                    className="rounded-lg border p-3.5 space-y-2.5"
                    style={{
                      borderColor: 'var(--color-border-muted)',
                      backgroundColor: 'var(--color-bg-card)',
                    }}
                    data-testid={`catalog-model-card-${model.model_key}`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div
                          className="text-sm font-semibold break-words"
                          style={{ color: 'var(--color-text-primary)' }}
                        >
                          {model.display_name}
                        </div>
                        <div
                          className="font-mono text-xs break-all"
                          style={{ color: 'var(--color-text-tertiary)' }}
                        >
                          {model.model_key}
                        </div>
                      </div>
                      {isAdmin && (
                        <div className="flex items-center gap-1 shrink-0">
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            className="h-8 text-xs"
                            onClick={() => openEditForModel(model, 'edit')}
                            data-testid={`mobile-edit-${model.model_key}-btn`}
                          >
                            {t('settings:dalton.teamCatalog.editAction')}
                          </Button>
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            className="tap-target h-8 w-8 p-0"
                            aria-label={t('settings:dalton.teamCatalog.moreActionsLabel')}
                            onClick={() => setMobileActionModel(model)}
                            data-testid={`mobile-more-${model.model_key}-btn`}
                          >
                            <MoreHorizontal className="h-4 w-4" />
                          </Button>
                        </div>
                      )}
                    </div>

                    {renderModelBadges(model)}

                    <div
                      className="grid grid-cols-2 gap-2 text-xs pt-1 border-t"
                      style={{
                        borderColor: 'var(--color-border-muted)',
                        color: 'var(--color-text-secondary)',
                      }}
                    >
                      <div>
                        <span style={{ color: 'var(--color-text-tertiary)' }}>
                          {t('settings:dalton.teamCatalog.colPricing')}:{' '}
                        </span>
                        <span className="tabular-nums">{renderPricingText(model)}</span>
                      </div>
                      <div>
                        <span style={{ color: 'var(--color-text-tertiary)' }}>
                          {t('settings:dalton.teamCatalog.colWindow')}:{' '}
                        </span>
                        <span className="tabular-nums">{formatContextWindow(model.context)}</span>
                      </div>
                      <div>
                        <span style={{ color: 'var(--color-text-tertiary)' }}>
                          {t('settings:dalton.teamCatalog.colModalities')}:{' '}
                        </span>
                        <span>{(model.input_modalities ?? ['text']).join(', ')}</span>
                      </div>
                      <div>
                        <span style={{ color: 'var(--color-text-tertiary)' }}>
                          {t('settings:dalton.teamCatalog.colRatings')}:{' '}
                        </span>
                        <span className="tabular-nums">{renderRatingsText(model)}</span>
                      </div>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              /* >= 768px Desktop Structured List */
              <div className="space-y-2" data-testid="team-catalog-desktop-list">
                {filteredModels.map((model) => (
                  <div
                    key={model.model_key}
                    className="flex flex-wrap lg:flex-nowrap items-center justify-between gap-3 rounded-lg border px-3.5 py-3"
                    style={{
                      borderColor: 'var(--color-border-muted)',
                      backgroundColor: 'var(--color-bg-card)',
                    }}
                    data-testid={`catalog-model-row-${model.model_key}`}
                  >
                    <div className="min-w-0 flex-1 space-y-1.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <span
                          className="text-sm font-medium"
                          style={{ color: 'var(--color-text-primary)' }}
                        >
                          {model.display_name}
                        </span>
                        <span
                          className="font-mono text-xs"
                          style={{ color: 'var(--color-text-tertiary)' }}
                        >
                          {model.model_key}
                        </span>
                      </div>

                      {renderModelBadges(model)}

                      <div
                        className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs"
                        style={{ color: 'var(--color-text-secondary)' }}
                      >
                        <span className="tabular-nums">
                          {t('settings:dalton.teamCatalog.colPricing')}: {renderPricingText(model)}
                        </span>
                        <span className="tabular-nums">
                          {t('settings:dalton.teamCatalog.colWindow')}: {formatContextWindow(model.context)}
                        </span>
                        <span>
                          {t('settings:dalton.teamCatalog.colModalities')}:{' '}
                          {(model.input_modalities ?? ['text']).join(', ')}
                        </span>
                        {model.reasoning?.efforts?.length ? (
                          <span>
                            {t('settings:dalton.teamCatalog.colReasoning')}:{' '}
                            {model.reasoning.efforts.join('/')}
                          </span>
                        ) : null}
                        <span className="tabular-nums">
                          {t('settings:dalton.teamCatalog.colRatings')}: {renderRatingsText(model)}
                        </span>
                      </div>
                    </div>

                    {isAdmin && (
                      <div className="flex flex-wrap items-center gap-1.5 shrink-0">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="h-7 px-2 text-xs gap-1"
                          onClick={() => openEditForModel(model, 'edit')}
                          data-testid={`edit-model-${model.model_key}-btn`}
                        >
                          <Pencil className="h-3 w-3" />
                          <span>{t('settings:dalton.teamCatalog.editAction')}</span>
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-7 px-2 text-xs gap-1"
                          onClick={() => openEditForModel(model, 'clone')}
                          data-testid={`clone-model-${model.model_key}-btn`}
                        >
                          <Copy className="h-3 w-3" />
                          <span>{t('settings:dalton.teamCatalog.cloneAction')}</span>
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-7 px-2 text-xs gap-1"
                          disabled={visibilityMutation.isPending}
                          onClick={() => handleToggleVisibility(model)}
                          data-testid={`toggle-visibility-${model.model_key}-btn`}
                        >
                          {model.visible ? (
                            <>
                              <EyeOff className="h-3 w-3" />
                              <span>{t('settings:dalton.teamCatalog.hideAction')}</span>
                            </>
                          ) : (
                            <>
                              <Eye className="h-3 w-3" />
                              <span>{t('settings:dalton.teamCatalog.showAction')}</span>
                            </>
                          )}
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-7 px-2 text-xs gap-1"
                          onClick={() => setRetireTarget(model)}
                          data-testid={`retire-model-${model.model_key}-btn`}
                        >
                          <Trash2 className="h-3 w-3" />
                          <span>{t('settings:dalton.teamCatalog.retireAction')}</span>
                        </Button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </DaltonSettingsCard>

      {/* Mobile Action Sheet for secondary row actions */}
      <MobileBottomSheet
        open={mobileActionModel !== null}
        onClose={() => setMobileActionModel(null)}
        height="50vh"
      >
        {mobileActionModel && (
          <div
            className="space-y-2 pb-[max(0.75rem,env(safe-area-inset-bottom))]"
            data-testid="mobile-model-action-sheet"
          >
            <div className="px-1 pb-2 border-b" style={{ borderColor: 'var(--color-border-muted)' }}>
              <div className="text-sm font-semibold" style={{ color: 'var(--color-text-primary)' }}>
                {mobileActionModel.display_name}
              </div>
              <div className="font-mono text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {mobileActionModel.model_key}
              </div>
            </div>
            <Button
              type="button"
              variant="ghost"
              className="w-full justify-start gap-2 h-11 text-sm"
              onClick={() => {
                const target = mobileActionModel;
                setMobileActionModel(null);
                openEditForModel(target, 'clone');
              }}
              data-testid="mobile-action-clone"
            >
              <Copy className="h-4 w-4" />
              <span>{t('settings:dalton.teamCatalog.cloneAction')}</span>
            </Button>
            <Button
              type="button"
              variant="ghost"
              className="w-full justify-start gap-2 h-11 text-sm"
              onClick={() => {
                const target = mobileActionModel;
                setMobileActionModel(null);
                handleToggleVisibility(target);
              }}
              data-testid="mobile-action-visibility"
            >
              {mobileActionModel.visible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              <span>
                {mobileActionModel.visible
                  ? t('settings:dalton.teamCatalog.hideAction')
                  : t('settings:dalton.teamCatalog.showAction')}
              </span>
            </Button>
            <Button
              type="button"
              variant="ghost"
              className="w-full justify-start gap-2 h-11 text-sm"
              style={{ color: 'var(--color-loss)' }}
              onClick={() => {
                const target = mobileActionModel;
                setMobileActionModel(null);
                setRetireTarget(target);
              }}
              data-testid="mobile-action-retire"
            >
              <Trash2 className="h-4 w-4" />
              <span>{t('settings:dalton.teamCatalog.retireAction')}</span>
            </Button>
          </div>
        )}
      </MobileBottomSheet>

      {/* Risk confirmation modal before hiding the last visible curated model in Everflow */}
      {confirmHideLastCurated && (
        <ModalShell
          labelId="hide-last-curated-title"
          title={t('settings:dalton.teamCatalog.lastCuratedHideTitle')}
          subtitle={t('settings:dalton.teamCatalog.lastCuratedHideWarning')}
          width="narrow"
          onClose={() => setConfirmHideLastCurated(null)}
          footer={
            <div className="flex items-center justify-end gap-2 w-full">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setConfirmHideLastCurated(null)}
              >
                {t('common.cancel')}
              </Button>
              <Button
                type="button"
                variant="destructive"
                size="sm"
                onClick={() => {
                  const target = confirmHideLastCurated;
                  setConfirmHideLastCurated(null);
                  void executeToggleVisibility(target, false);
                }}
                data-testid="confirm-hide-last-curated-btn"
              >
                {t('settings:dalton.teamCatalog.hideAction')}
              </Button>
            </div>
          }
        >
          <div className="text-xs font-mono" style={{ color: 'var(--color-text-secondary)' }}>
            {confirmHideLastCurated.display_name} ({confirmHideLastCurated.model_key})
          </div>
        </ModalShell>
      )}

      {/* 5-Group Model / Variant Edit Sheet */}
      <TeamModelEditSheet
        open={editSheet.open}
        mode={editSheet.mode}
        onClose={() => setEditSheet((prev) => ({ ...prev, open: false }))}
        revision={revision}
        model={editSheet.model}
        variant={editSheet.variant}
        allModels={allModels}
        providerOptions={providerOptions}
        parentProviderGroups={parentProviderGroups}
        supportedVariantSdks={supportedVariantSdks}
        curatedSetup={data?.curated_setup ?? null}
        effectiveCuratedCandidates={data?.dalton_setup_candidates ?? []}
        onSaved={(message) => setNotice({ tone: 'success', message })}
      />

      {/* Retire & Batch Replacement Dialog */}
      <TeamModelRetireDialog
        open={retireTarget !== null}
        onClose={() => setRetireTarget(null)}
        model={retireTarget}
        revision={revision}
        activeModels={allModels}
        isLastVisibleCuratedEverflow={
          retireTarget ? isLastVisibleCuratedEverflow(retireTarget.model_key) : false
        }
        onRetired={(modelKey, replacedCount) =>
          setNotice({
            tone: 'success',
            message: t('settings:dalton.teamCatalog.retireSuccess', {
              model: modelKey,
              replaced: replacedCount,
            }),
          })
        }
      />

      {/* Version History & Rollback Dialog */}
      <TeamModelHistoryDialog
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        currentRevision={revision}
        activeModels={allModels}
        onRolledBack={(newRev, targetRev, restoredTasksCount) =>
          setNotice({
            tone: 'success',
            message: t('settings:dalton.teamCatalog.rollbackSuccessShort', {
              newRevision: newRev,
              targetRevision: targetRev,
              restored: restoredTasksCount,
            }),
          })
        }
      />

      {/* Legacy Native Overlay Import Failure Repair Dialog */}
      <TeamModelLegacyImportDialog
        open={legacyDialogOpen}
        onClose={() => setLegacyDialogOpen(false)}
        revision={revision}
        errorSummary={data?.legacy_sync_import_error ?? null}
        readonlyLegacyRetiredKeys={readonlyLegacyRetiredKeys}
        onResolved={(action) =>
          setNotice({
            tone: 'success',
            message:
              action === 'retry_import'
                ? t('settings:dalton.teamCatalog.legacyRetrySuccess')
                : t('settings:dalton.teamCatalog.legacyAbandonSuccess'),
          })
        }
      />
    </>
  );
}
