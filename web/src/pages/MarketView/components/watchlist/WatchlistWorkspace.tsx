import React, { useState, useMemo, useCallback, useEffect } from 'react';
import { useNavigate } from 'react-router';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Plus, Layers, Search, MessageSquare, AlertTriangle, RefreshCw } from 'lucide-react';
import type { ComposerScope } from '@/components/ui/chat-input.types';
import type { Workspace } from '@/types/api';
import { useToast } from '@/components/ui/use-toast';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useWorkspace } from '@/hooks/useWorkspace';
import { portfolioHoldingsQuery } from '@/hooks/membershipQueries';
import { queryKeys } from '@/lib/queryKeys';
import { apiErrorDetailMessage, apiErrorStatus } from '@/api/errors';
import {
  BATCH1_WATCHLIST_COLUMNS,
  DEFAULT_WATCHLIST_COLUMNS,
  buildPersonalWatchlistContextText,
  flattenWatchlistCompanies,
  mutateWatchlist,
  previewWatchlistMutation,
  reconcileWatchlist,
  usePersonalWatchlist,
  usePersonalWatchlistMetrics,
  type PersonalWatchlistCompany,
  type WatchlistColumnKey,
  type WatchlistPreviewResponse,
  type WatchlistTier,
} from '@/lib/dalton/watchlist';
import { useTeamReadOnly } from '../../../ChatAgent/components/dalton/TeamSharing';
import { buildMarketViewUrl } from '../../utils/marketRoute';
import MarketChatPanel from '../MarketChatPanel';
import { WatchlistColumnPicker } from './WatchlistColumnPicker';
import { WatchlistAddModal } from './WatchlistAddModal';
import { WatchlistFullTable } from './WatchlistFullTable';

const STORAGE_COLS_KEY = 'dalton:watchlist:visibleColumns:v1';
const SESSION_FILTER_KEY = 'dalton:watchlist:tierFilter:v1';

interface WatchlistWorkspaceProps {
  scope: ComposerScope;
  onScopeChange: (scope: ComposerScope) => void;
  activeWorkspaceId: string | null;
  workspaces: Workspace[];
  selectedWorkspaceId: string | null;
  workspacePending?: boolean;
  onWorkspaceChange: (id: string) => void;
  chatPanelWidth: number;
  onDragStart: (e: React.MouseEvent) => void;
  onJumpToChart: (symbol: string, timeframe?: string | null) => void;
}

interface PendingConfirmAction {
  kind: 'set_tier' | 'remove';
  company: PersonalWatchlistCompany;
  nextTier?: WatchlistTier;
  preview: WatchlistPreviewResponse;
}

function loadSavedColumns(): WatchlistColumnKey[] {
  try {
    const raw = localStorage.getItem(STORAGE_COLS_KEY);
    if (!raw) return [...DEFAULT_WATCHLIST_COLUMNS];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [...DEFAULT_WATCHLIST_COLUMNS];
    const allowed = new Set<string>(BATCH1_WATCHLIST_COLUMNS);
    const valid = parsed.filter((k): k is WatchlistColumnKey => typeof k === 'string' && allowed.has(k));
    return valid.length > 0 ? valid : [...DEFAULT_WATCHLIST_COLUMNS];
  } catch {
    return [...DEFAULT_WATCHLIST_COLUMNS];
  }
}

function loadSessionTierFilter(): WatchlistTier | 'all' {
  try {
    const raw = sessionStorage.getItem(SESSION_FILTER_KEY);
    if (raw === 'core' || raw === 'watch' || raw === 'radar' || raw === 'all') return raw;
  } catch {
    // ignore storage errors
  }
  return 'all';
}

const TIER_LABEL_KEY: Record<WatchlistTier, string> = {
  core: 'marketView:watchlist.tiers.core',
  watch: 'marketView:watchlist.tiers.watch',
  radar: 'marketView:watchlist.tiers.radar',
};

export function WatchlistWorkspace({
  scope,
  onScopeChange,
  activeWorkspaceId,
  workspaces,
  selectedWorkspaceId,
  workspacePending,
  onWorkspaceChange,
  chatPanelWidth,
  onDragStart,
  onJumpToChart,
}: WatchlistWorkspaceProps): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const isMobile = useIsMobile();

  const [tierFilter, setTierFilter] = useState<WatchlistTier | 'all'>(loadSessionTierFilter);
  const [searchQuery, setSearchQuery] = useState('');
  const [groupedByTier, setGroupedByTier] = useState(false);
  const [holdingsOnly, setHoldingsOnly] = useState(false);
  const [visibleColumns, setVisibleColumns] = useState<WatchlistColumnKey[]>(loadSavedColumns);
  const [addModalOpen, setAddModalOpen] = useState(false);
  const [chatOpen, setChatOpen] = useState(!isMobile);
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirmAction | null>(null);
  const [confirmSubmitting, setConfirmSubmitting] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [prefillMessage, setPrefillMessage] = useState('');

  useEffect(() => {
    try {
      sessionStorage.setItem(SESSION_FILTER_KEY, tierFilter);
    } catch {
      // ignore storage errors
    }
  }, [tierFilter]);

  const handleColumnsChange = useCallback((next: WatchlistColumnKey[]) => {
    setVisibleColumns(next);
    try {
      localStorage.setItem(STORAGE_COLS_KEY, JSON.stringify(next));
    } catch {
      // ignore storage errors
    }
  }, []);

  const { data: watchlistData, isLoading, isError, refetch } = usePersonalWatchlist(true);
  const allCompanies = useMemo(() => flattenWatchlistCompanies(watchlistData), [watchlistData]);

  const listedSymbols = useMemo(
    () => allCompanies.filter((c) => !c.is_private).map((c) => c.ticker),
    [allCompanies],
  );
  const { data: metricsData, isLoading: metricsLoading } = usePersonalWatchlistMetrics(
    listedSymbols,
    listedSymbols.length > 0,
  );
  const metricsMap = useMemo(() => metricsData?.items ?? {}, [metricsData]);

  const { data: holdingsData } = useQuery({
    ...portfolioHoldingsQuery,
    retry: false,
  });
  const holdingSymbols = useMemo(() => {
    const out = new Set<string>();
    for (const h of holdingsData ?? []) {
      const sym = String((h as { symbol?: string }).symbol || '').trim().toUpperCase();
      if (sym) out.add(sym);
    }
    return out;
  }, [holdingsData]);

  const filteredCompanies = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return allCompanies.filter((c) => {
      if (tierFilter !== 'all' && c.tier !== tierFilter) return false;
      if (holdingsOnly && !holdingSymbols.has(c.ticker.toUpperCase())) return false;
      if (q) {
        const matchTicker = c.ticker.toLowerCase().includes(q);
        const matchName = (c.name || '').toLowerCase().includes(q);
        const matchTag = (c.tags || []).some((tag) => tag.toLowerCase().includes(q));
        if (!matchTicker && !matchName && !matchTag) return false;
      }
      return true;
    });
  }, [allCompanies, tierFilter, holdingsOnly, holdingSymbols, searchQuery]);

  // Privacy boundary: ONLY inject personal watchlist context in private sessions
  // (Chief of Staff Home scope === 'all', or the user's own non-shared workspace).
  const { data: activeWorkspace } = useWorkspace(activeWorkspaceId);
  const teamReadOnly = useTeamReadOnly(activeWorkspaceId, activeWorkspace);
  const isPrivateChatSession =
    scope === 'all' ||
    (Boolean(activeWorkspace) &&
      !teamReadOnly.isColleague &&
      !(activeWorkspace as { is_shared?: boolean } | null)?.is_shared);

  const watchlistContextText = useMemo(() => {
    if (!isPrivateChatSession || !filteredCompanies.length) return null;
    return buildPersonalWatchlistContextText(filteredCompanies, metricsMap, tierFilter);
  }, [isPrivateChatSession, filteredCompanies, metricsMap, tierFilter]);

  const handleSelectSymbol = useCallback(
    (symbol: string) => {
      navigate(buildMarketViewUrl({ symbol }));
    },
    [navigate],
  );

  const handleRequestTierChange = useCallback(
    async (company: PersonalWatchlistCompany, nextTier: WatchlistTier) => {
      setConfirmError(null);
      try {
        const preview = await previewWatchlistMutation({
          action: 'set_tier',
          tickers: [company.ticker],
          tier: nextTier,
        });
        if (!preview.ok || preview.precheck_error) {
          toast({
            variant: 'destructive',
            title: preview.precheck_error || t('marketView:watchlist.errors.previewFailed'),
          });
          return;
        }
        setPendingConfirm({ kind: 'set_tier', company, nextTier, preview });
      } catch (err) {
        toast({
          variant: 'destructive',
          title: apiErrorDetailMessage(err) || t('marketView:watchlist.errors.previewFailed'),
        });
      }
    },
    [toast, t],
  );

  const handleRequestRemove = useCallback(
    async (company: PersonalWatchlistCompany) => {
      setConfirmError(null);
      try {
        const preview = await previewWatchlistMutation({
          action: 'remove',
          tickers: [company.ticker],
        });
        if (!preview.ok || preview.precheck_error) {
          toast({
            variant: 'destructive',
            title: preview.precheck_error || t('marketView:watchlist.errors.previewFailed'),
          });
          return;
        }
        setPendingConfirm({ kind: 'remove', company, preview });
      } catch (err) {
        toast({
          variant: 'destructive',
          title: apiErrorDetailMessage(err) || t('marketView:watchlist.errors.previewFailed'),
        });
      }
    },
    [toast, t],
  );

  const handleConfirmPendingAction = useCallback(async () => {
    if (!pendingConfirm) return;
    setConfirmSubmitting(true);
    setConfirmError(null);
    try {
      await mutateWatchlist({
        action: pendingConfirm.kind,
        tickers: [pendingConfirm.company.ticker],
        ...(pendingConfirm.nextTier ? { tier: pendingConfirm.nextTier } : {}),
        expected_action: pendingConfirm.preview.raw_action ?? pendingConfirm.kind,
        expected_target_tier: pendingConfirm.preview.target_tier ?? null,
        expected_group_id: pendingConfirm.preview.group_id ?? null,
        expected_override_user_pause: Boolean(pendingConfirm.preview.override_user_pause),
        expected_pause_versions: pendingConfirm.preview.expected_pause_versions,
        expected_roster_hash: pendingConfirm.preview.expected_roster_hash,
        expected_policy_versions: pendingConfirm.preview.expected_policy_versions,
        expected_target_tickers: pendingConfirm.preview.target_tickers,
      });
      await queryClient.invalidateQueries({ queryKey: queryKeys.dalton.watchlist() });
      setPendingConfirm(null);
    } catch (err) {
      if (apiErrorStatus(err) === 409) {
        setConfirmError(t('marketView:watchlist.errors.scopeChanged'));
      } else {
        setConfirmError(apiErrorDetailMessage(err) || t('marketView:watchlist.errors.mutateFailed'));
      }
    } finally {
      setConfirmSubmitting(false);
    }
  }, [pendingConfirm, queryClient, t]);

  const handleReconcilePending = useCallback(async () => {
    try {
      await reconcileWatchlist();
      await queryClient.invalidateQueries({ queryKey: queryKeys.dalton.watchlist() });
      toast({ title: t('marketView:watchlist.reconcileTriggered') });
    } catch (err) {
      toast({
        variant: 'destructive',
        title: apiErrorDetailMessage(err) || t('marketView:watchlist.errors.mutateFailed'),
      });
    }
  }, [queryClient, toast, t]);

  const counts = watchlistData?.counts ?? {
    core: 0,
    watch: 0,
    radar: 0,
    total: allCompanies.length,
    groups: 0,
  };

  const quickQueries = useMemo(
    () => [
      t('marketView:watchlist.quickQueries.lowestRsi'),
      t('marketView:watchlist.quickQueries.upcomingEarnings'),
    ],
    [t],
  );

  return (
    <div className="market-content-wrapper" data-testid="watchlist-workspace">
      <div className="market-left-panel flex flex-col min-w-0 flex-1" style={{ backgroundColor: 'var(--color-bg-card)' }}>
        {/* Toolbar */}
        <div
          className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 border-b"
          style={{ borderColor: 'var(--color-border-muted)' }}
        >
          <div className="flex flex-wrap items-center gap-1.5">
            <h2 className="text-sm font-semibold mr-2" style={{ color: 'var(--color-text-primary)' }}>
              {t('marketView:watchlist.title')}
            </h2>
            {(
              [
                ['all', t('marketView:watchlist.tiers.all'), counts.total],
                ['core', t('marketView:watchlist.tiers.core'), counts.core],
                ['watch', t('marketView:watchlist.tiers.watch'), counts.watch],
                ['radar', t('marketView:watchlist.tiers.radar'), counts.radar],
              ] as const
            ).map(([key, label, count]) => {
              const active = tierFilter === key;
              return (
                <button
                  key={key}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setTierFilter(key)}
                  className="px-2.5 py-1 rounded-full text-xs font-medium transition-colors"
                  style={{
                    backgroundColor: active ? 'var(--color-btn-primary-bg)' : 'var(--color-bg-page)',
                    color: active ? 'var(--color-btn-primary-text)' : 'var(--color-text-secondary)',
                  }}
                >
                  {label} <span className="tabular-nums opacity-80">{count}</span>
                </button>
              );
            })}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search
                className="h-3.5 w-3.5 absolute left-2.5 top-1/2 -translate-y-1/2"
                style={{ color: 'var(--color-text-tertiary)' }}
              />
              <input
                type="search"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder={t('marketView:watchlist.searchPlaceholder')}
                className="pl-8 pr-2.5 py-1 text-xs rounded-md border w-40 sm:w-48 focus:outline-none"
                style={{
                  backgroundColor: 'var(--color-bg-input)',
                  borderColor: 'var(--color-border-muted)',
                  color: 'var(--color-text-primary)',
                }}
              />
            </div>

            <button
              type="button"
              aria-pressed={groupedByTier}
              onClick={() => setGroupedByTier((v) => !v)}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md text-xs font-medium border"
              style={{
                backgroundColor: groupedByTier ? 'var(--color-bg-hover)' : 'var(--color-bg-card)',
                borderColor: 'var(--color-border-muted)',
                color: 'var(--color-text-secondary)',
              }}
            >
              <Layers className="h-3.5 w-3.5" />
              <span>{t('marketView:watchlist.groupByTier')}</span>
            </button>

            {holdingSymbols.size > 0 && (
              <button
                type="button"
                aria-pressed={holdingsOnly}
                onClick={() => setHoldingsOnly((v) => !v)}
                className="px-2.5 py-1.5 rounded-md text-xs font-medium border"
                style={{
                  backgroundColor: holdingsOnly ? 'var(--color-bg-hover)' : 'var(--color-bg-card)',
                  borderColor: 'var(--color-border-muted)',
                  color: 'var(--color-text-secondary)',
                }}
              >
                {t('marketView:watchlist.holdingsOnly')}
              </button>
            )}

            <WatchlistColumnPicker visibleColumns={visibleColumns} onChange={handleColumnsChange} />

            <button
              type="button"
              onClick={() => setAddModalOpen(true)}
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded-md text-xs font-medium"
              style={{
                backgroundColor: 'var(--color-btn-primary-bg)',
                color: 'var(--color-btn-primary-text)',
              }}
            >
              <Plus className="h-3.5 w-3.5" />
              <span>{t('marketView:watchlist.addAction')}</span>
            </button>

            <button
              type="button"
              aria-pressed={chatOpen}
              onClick={() => setChatOpen((v) => !v)}
              aria-label={t('marketView:watchlist.toggleChat')}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md text-xs font-medium border"
              style={{
                backgroundColor: chatOpen ? 'var(--color-bg-hover)' : 'var(--color-bg-card)',
                borderColor: 'var(--color-border-muted)',
                color: 'var(--color-text-secondary)',
              }}
            >
              <MessageSquare className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        {(watchlistData?.pending_reconcile_count ?? 0) > 0 && (
          <div
            className="flex items-center justify-between px-4 py-1.5 text-xs border-b"
            style={{
              backgroundColor: 'var(--color-bg-page)',
              borderColor: 'var(--color-border-muted)',
              color: 'var(--color-text-secondary)',
            }}
          >
            <span>
              {t('marketView:watchlist.pendingReconcileBanner', {
                count: watchlistData?.pending_reconcile_count ?? 0,
              })}
            </span>
            <button
              type="button"
              onClick={handleReconcilePending}
              className="inline-flex items-center gap-1 font-medium hover:underline"
              style={{ color: 'var(--color-accent-light)' }}
            >
              <RefreshCw className="h-3 w-3" />
              {t('marketView:watchlist.retryReconcile')}
            </button>
          </div>
        )}

        {/* Body states: loading / error / empty / table */}
        {isLoading ? (
          <div className="flex-1 flex items-center justify-center text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
            {t('marketView:watchlist.loading')}
          </div>
        ) : isError ? (
          <div className="flex-1 flex flex-col items-center justify-center gap-2 text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
            <span>{t('marketView:watchlist.loadError')}</span>
            <button
              type="button"
              onClick={() => void refetch()}
              className="px-3 py-1.5 rounded-md text-xs font-medium border"
              style={{ borderColor: 'var(--color-border-muted)', color: 'var(--color-text-primary)' }}
            >
              {t('common.retry')}
            </button>
          </div>
        ) : filteredCompanies.length === 0 ? (
          <div className="flex-1 flex flex-col items-center justify-center gap-3 p-6 text-center">
            <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
              {allCompanies.length === 0
                ? t('marketView:watchlist.emptyState')
                : t('marketView:watchlist.emptyFilterState')}
            </p>
            {allCompanies.length === 0 && (
              <button
                type="button"
                onClick={() => setAddModalOpen(true)}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-md text-xs font-medium"
                style={{
                  backgroundColor: 'var(--color-btn-primary-bg)',
                  color: 'var(--color-btn-primary-text)',
                }}
              >
                <Plus className="h-3.5 w-3.5" />
                {t('marketView:watchlist.addAction')}
              </button>
            )}
          </div>
        ) : (
          <WatchlistFullTable
            companies={filteredCompanies}
            metrics={metricsMap}
            metricsLoading={metricsLoading}
            visibleColumns={visibleColumns}
            groupedByTier={groupedByTier}
            holdingSymbols={holdingSymbols}
            onSelectSymbol={handleSelectSymbol}
            onRequestTierChange={handleRequestTierChange}
            onRequestRemove={handleRequestRemove}
            onOpenWorkspace={(wsId) => navigate(`/chat/${wsId}`)}
          />
        )}
      </div>

      {/* Right chat panel (reuses MarketChatPanel; injects personal watchlist context ONLY in private sessions) */}
      {chatOpen && (
        <>
          {!isMobile && <div className="market-resize-handle" onMouseDown={onDragStart} />}
          <div
            className={isMobile ? 'fixed inset-0 z-40 flex flex-col' : 'market-right-panel'}
            style={
              isMobile
                ? { backgroundColor: 'var(--color-bg-card)' }
                : { width: chatPanelWidth }
            }
          >
            {isMobile && (
              <div className="flex items-center justify-between px-4 py-2 border-b" style={{ borderColor: 'var(--color-border-muted)' }}>
                <span className="text-xs font-medium" style={{ color: 'var(--color-text-primary)' }}>
                  {t('marketView:watchlist.chatDrawerTitle')}
                </span>
                <button
                  type="button"
                  onClick={() => setChatOpen(false)}
                  className="text-xs px-2 py-1 rounded"
                  style={{ color: 'var(--color-text-secondary)' }}
                >
                  {t('common.close')}
                </button>
              </div>
            )}
            <div className="market-right-panel-inner flex-1 min-h-0">
              <MarketChatPanel
                symbol=""
                interval="1day"
                scope={scope}
                onScopeChange={onScopeChange}
                activeWorkspaceId={activeWorkspaceId}
                workspaces={workspaces}
                selectedWorkspaceId={selectedWorkspaceId}
                workspacePending={workspacePending}
                onWorkspaceChange={onWorkspaceChange}
                chartImage={null}
                chartImageDesc={null}
                onCaptureChart={() => {}}
                onClearChartImage={() => {}}
                prefillMessage={prefillMessage}
                onClearPrefill={() => setPrefillMessage('')}
                quickQueries={quickQueries}
                onQuickQuery={(q) => setPrefillMessage(q)}
                onShuffleQueries={() => {}}
                onNavigateSubagent={(tid, taskId) => navigate(`/chat/t/${tid}/${taskId}`)}
                onJumpToChart={onJumpToChart}
                placeholder={t('marketView:watchlist.chatPlaceholder')}
                watchlistContext={watchlistContextText}
              />
            </div>
          </div>
        </>
      )}

      <WatchlistAddModal
        open={addModalOpen}
        onClose={() => setAddModalOpen(false)}
        onSuccess={() => {
          void queryClient.invalidateQueries({ queryKey: queryKeys.dalton.watchlist() });
        }}
      />

      {/* Tier change / Remove preview confirmation modal */}
      {pendingConfirm && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={t('marketView:watchlist.confirmModal.title')}
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ backgroundColor: 'var(--color-bg-overlay)' }}
          onClick={() => !confirmSubmitting && setPendingConfirm(null)}
        >
          <div
            className="w-full max-w-md rounded-xl border p-5 shadow-xl space-y-3"
            style={{
              backgroundColor: 'var(--color-bg-card)',
              borderColor: 'var(--color-border-muted)',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-semibold" style={{ color: 'var(--color-text-primary)' }}>
              {pendingConfirm.preview.card_title || t('marketView:watchlist.confirmModal.title')}
            </h3>
            {pendingConfirm.preview.card_description && (
              <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
                {pendingConfirm.preview.card_description}
              </p>
            )}
            <div className="rounded-md border p-2.5 text-xs space-y-1.5" style={{ borderColor: 'var(--color-border-muted)' }}>
              <div className="flex items-center justify-between">
                <span className="font-mono font-medium" style={{ color: 'var(--color-text-primary)' }}>
                  {pendingConfirm.company.ticker}
                </span>
                <span style={{ color: 'var(--color-text-secondary)' }}>
                  {pendingConfirm.kind === 'set_tier' && pendingConfirm.nextTier
                    ? `${t(TIER_LABEL_KEY[pendingConfirm.company.tier])} → ${t(TIER_LABEL_KEY[pendingConfirm.nextTier])}`
                    : t('marketView:watchlist.confirmModal.removeAction')}
                </span>
              </div>
              {pendingConfirm.preview.affected_workspaces.length > 0 && (
                <div style={{ color: 'var(--color-text-tertiary)' }}>
                  {t('marketView:watchlist.confirmModal.affectedWorkspaces', {
                    count: pendingConfirm.preview.affected_workspaces.length,
                  })}
                </div>
              )}
              {pendingConfirm.preview.preserve_workspace_notice && (
                <div style={{ color: 'var(--color-text-tertiary)' }}>
                  {pendingConfirm.preview.preserve_workspace_notice}
                </div>
              )}
            </div>
            {confirmError && (
              <div className="flex items-start gap-2 text-xs" style={{ color: 'var(--color-warning)' }}>
                <AlertTriangle className="h-4 w-4 shrink-0" />
                <span>{confirmError}</span>
              </div>
            )}
            <div className="flex justify-end gap-2 pt-1">
              <button
                type="button"
                disabled={confirmSubmitting}
                onClick={() => setPendingConfirm(null)}
                className="px-3.5 py-2 rounded-md text-xs font-medium"
                style={{ backgroundColor: 'var(--color-bg-hover)', color: 'var(--color-text-secondary)' }}
              >
                {t('common.cancel')}
              </button>
              <button
                type="button"
                disabled={confirmSubmitting}
                onClick={handleConfirmPendingAction}
                className="px-4 py-2 rounded-md text-xs font-medium disabled:opacity-50"
                style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
              >
                {confirmSubmitting ? t('marketView:watchlist.submitting') : t('marketView:watchlist.confirmModal.confirm')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
