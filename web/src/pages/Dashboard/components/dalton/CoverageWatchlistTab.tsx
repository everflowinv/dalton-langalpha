/**
 * Dalton Personal Watchlist tab & compact row component for Dashboard PortfolioWatchlistWidget.
 * Keeps upstream `PortfolioWatchlistWidget.tsx` and `_holdingsPrimitives.tsx` to thin hooks.
 */

import { useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { QueryClientContext } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, ArrowRight, BookmarkPlus } from 'lucide-react';
import { AnimatePresence, motion } from '@/lib/framer';
import { TWEEN } from '@/lib/motion';
import { TickerLabel } from '@/components/TickerLogo';
import { ContextMenuGroup, ContextMenuItem, ContextMenuSeparator } from '@/components/ui/context-menu';
import { useToast } from '@/components/ui/use-toast';
import { apiErrorDetailMessage, apiErrorStatus } from '@/api/errors';
import { isDaltonBuild } from '@/config/brand';
import { useLocale } from '@/hooks/useLocale';
import { DASH } from '@/lib/format';
import { formatMoney, quoteCurrency } from '@/lib/bars';
import { queryKeys } from '@/lib/queryKeys';
import {
  deriveQuoteDisplay,
  formatSignedPct2,
  roundQuote2,
  useQuotes,
  type MarketStatusInput,
} from '@/lib/quotes';
import {
  WATCHLIST_TIERS,
  mutateWatchlist,
  previewWatchlistMutation,
  type PersonalWatchlistCompany,
  type WatchlistPreviewResponse,
  type WatchlistTier,
} from '@/lib/dalton/watchlist';
import { TermChange } from '@/pages/ChatAgent/components/charts/TermCard';
import {
  buildCoverageRowsForCompanies,
  coverageQuoteRow,
  MAX_DASHBOARD_COVERAGE_QUOTES,
  setActiveDashboardCoverageTier,
  useCoverageWatchlistData,
  type CoverageWatchlistRow,
} from '../../hooks/useCoverageWatchlistData';

export type UseCoverageWatchlistDataResult = ReturnType<typeof useCoverageWatchlistData>;
import { RowAttachButton } from '../RowAttachButton';
import {
  serializeQuoteRowToMarkdown,
  serializeQuoteRowsToMarkdown,
  wrapWidgetContext,
} from '../../widgets/framework/snapshotSerializers';
import {
  HoldingsSkeleton,
  WatchlistTable,
} from '../../widgets/definitions/_holdingsPrimitives';

type MarketStatusData = MarketStatusInput;

export type PortfolioWatchlistTabKey = 'coverage' | 'watchlist' | 'portfolio';

const EMPTY_COVERAGE_ROWS: CoverageWatchlistRow[] = [];
const EMPTY_COMPANIES: PersonalWatchlistCompany[] = [];

export function normalizeCoverageContext(
  rawCoverage: UseCoverageWatchlistDataResult | undefined,
): UseCoverageWatchlistDataResult {
  return (
    rawCoverage ?? ({
      rows: EMPTY_COVERAGE_ROWS,
      allCompanies: EMPTY_COMPANIES,
      quotesBySymbol: {},
      totalCount: 0,
      quotedLimit: MAX_DASHBOARD_COVERAGE_QUOTES,
      counts: { core: 0, watch: 0, radar: 0, total: 0, groups: 0 },
      loading: false,
      loaded: true,
      refetch: async () => undefined,
    } as unknown as UseCoverageWatchlistDataResult)
  );
}

type QuotesMap = ReturnType<typeof useQuotes>['quotes'];
const EMPTY_QUOTES: QuotesMap = {};

function CoverageTierQuotesBridge({
  symbols,
  onQuotes,
}: {
  symbols: string[];
  onQuotes: (quotes: QuotesMap) => void;
}) {
  const { quotes } = useQuotes(symbols, {
    staleTime: 1000 * 30,
    refetchInterval: 60000,
    enabled: isDaltonBuild && symbols.length > 0,
  });
  useEffect(() => {
    onQuotes(quotes);
  }, [quotes, onQuotes]);
  return null;
}

/**
 * Filter personal watchlist companies by `coverageTier` BEFORE slicing the 50-symbol
 * live quote cap, fetching any unquoted filtered listed symbols on demand.
 */
export function useFilteredCoverageWatchlistRows(
  coverage: UseCoverageWatchlistDataResult,
  coverageTier: WatchlistTier | 'all',
): {
  rows: CoverageWatchlistRow[];
  filteredTotalCount: number;
  quotesBridge: ReactNode;
} {
  useEffect(() => {
    setActiveDashboardCoverageTier(coverageTier);
    return () => setActiveDashboardCoverageTier('all');
  }, [coverageTier]);

  const allCompanies = coverage.allCompanies ?? EMPTY_COMPANIES;
  const filteredCompanies = useMemo(() => {
    if (allCompanies.length === 0) return EMPTY_COMPANIES;
    return coverageTier === 'all'
      ? allCompanies
      : allCompanies.filter((c) => c.tier === coverageTier);
  }, [allCompanies, coverageTier]);

  const extraSymbols = useMemo(() => {
    if (coverageTier === 'all' || filteredCompanies.length === 0) return [];
    const baseQuotes = coverage.quotesBySymbol ?? {};
    const out: string[] = [];
    let listedSeen = 0;
    for (const c of filteredCompanies) {
      if (c.is_private) continue;
      if (listedSeen >= MAX_DASHBOARD_COVERAGE_QUOTES) break;
      listedSeen += 1;
      const sym = c.ticker.toUpperCase();
      if (!(sym in baseQuotes)) out.push(sym);
    }
    return out;
  }, [coverageTier, filteredCompanies, coverage.quotesBySymbol]);

  const [tierQuotes, setTierQuotes] = useState<QuotesMap>(EMPTY_QUOTES);
  const effectiveTierQuotes = extraSymbols.length > 0 ? tierQuotes : EMPTY_QUOTES;
  const quotesBridge =
    isDaltonBuild && extraSymbols.length > 0 ? (
      <CoverageTierQuotesBridge symbols={extraSymbols} onQuotes={setTierQuotes} />
    ) : null;

  const rows = useMemo(() => {
    // Fallback for test mocks that only populate `coverage.rows` without `allCompanies`
    if (allCompanies.length === 0) {
      return coverageTier === 'all'
        ? coverage.rows
        : coverage.rows.filter((r) => r.tier === coverageTier);
    }
    if (coverageTier === 'all') return coverage.rows;
    const mergedQuotes = { ...(coverage.quotesBySymbol ?? {}), ...effectiveTierQuotes };
    return buildCoverageRowsForCompanies(filteredCompanies, mergedQuotes);
  }, [allCompanies.length, coverage.rows, coverage.quotesBySymbol, coverageTier, filteredCompanies, effectiveTierQuotes]);

  const filteredTotalCount =
    allCompanies.length > 0
      ? filteredCompanies.length
      : coverageTier === 'all'
        ? coverage.totalCount
        : coverage.counts[coverageTier] ?? rows.length;

  return { rows, filteredTotalCount, quotesBridge };
}

export function resolveInitialPortfolioWatchlistTab(
  savedDefaultTab: PortfolioWatchlistTabKey | undefined,
  coverageTotalCount: number,
): PortfolioWatchlistTabKey {
  if (savedDefaultTab) {
    if (!isDaltonBuild && savedDefaultTab === 'coverage') return 'watchlist';
    return savedDefaultTab;
  }
  if (isDaltonBuild && coverageTotalCount > 0) return 'coverage';
  return 'watchlist';
}

export function getPortfolioWatchlistTabs(
  t: (key: string) => string,
): { key: PortfolioWatchlistTabKey; label: string }[] {
  const base: { key: PortfolioWatchlistTabKey; label: string }[] = [
    { key: 'watchlist', label: t('dashboard:widgets.portfolioWatchlist.headerWatchlist') },
    { key: 'portfolio', label: t('dashboard:widgets.portfolioWatchlist.headerHoldings') },
  ];
  if (!isDaltonBuild) return base;
  return [
    { key: 'coverage', label: t('dashboard:widgets.portfolioWatchlist.headerCoverage') },
    ...base,
  ];
}

export function buildCoverageFullContextExport({
  instanceId,
  coverage,
  filteredRows,
  filteredTotalCount,
  coverageTier,
  marketStatus,
  headerLabel,
}: {
  instanceId: string;
  coverage: UseCoverageWatchlistDataResult;
  filteredRows: CoverageWatchlistRow[];
  filteredTotalCount?: number;
  coverageTier: WatchlistTier | 'all';
  marketStatus: MarketStatusData | null;
  headerLabel: string;
}) {
  const effectiveTotal = filteredTotalCount ?? coverage.totalCount;
  const rows = filteredRows.map((r) => coverageQuoteRow(r, marketStatus));
  const body = serializeQuoteRowsToMarkdown(rows);
  const text = wrapWidgetContext(
    'personal.portfolioWatchlist',
    { tab: 'coverage', tier: coverageTier, count: effectiveTotal, shown: rows.length },
    body,
  );
  return {
    widget_type: 'personal.portfolioWatchlist',
    widget_id: instanceId,
    label: `${headerLabel} · ${effectiveTotal}`,
    description: rows.length ? `${rows.length} symbol${rows.length === 1 ? '' : 's'}` : 'empty',
    captured_at: new Date().toISOString(),
    text,
    data: { tab: 'coverage', tier: coverageTier, totalCount: effectiveTotal, rows },
  };
}

export function buildCoverageRowContextExport({
  instanceId,
  rowId,
  coverage,
  filteredRows,
  marketStatus,
  headerLabel,
}: {
  instanceId: string;
  rowId: string;
  coverage: UseCoverageWatchlistDataResult;
  filteredRows?: CoverageWatchlistRow[];
  marketStatus: MarketStatusData | null;
  headerLabel: string;
}) {
  const row = (filteredRows ?? coverage.rows).find((r) => r.symbol === rowId)
    ?? coverage.rows.find((r) => r.symbol === rowId);
  if (!row) return null;
  const quote = coverageQuoteRow(row, marketStatus);
  const body = serializeQuoteRowToMarkdown(quote);
  const text = wrapWidgetContext(
    'personal.portfolioWatchlist/row',
    { tab: 'coverage', symbol: row.symbol, tier: row.tier },
    body,
  );
  const labelPct = quote.changePercent;
  return {
    widget_type: 'personal.portfolioWatchlist/row',
    widget_id: `${instanceId}/${rowId}`,
    label:
      row.symbol +
      (!row.is_private && row.quoteAvailable && labelPct !== undefined
        ? ` · ${formatSignedPct2(labelPct)}`
        : ''),
    description: headerLabel,
    captured_at: new Date().toISOString(),
    text,
    data: { row: quote, tab: 'coverage', tier: row.tier },
  };
}

const COVERAGE_TIER_LABEL_KEY = {
  core: 'dashboard:widgets.portfolioWatchlist.tierCore',
  watch: 'dashboard:widgets.portfolioWatchlist.tierWatch',
  radar: 'dashboard:widgets.portfolioWatchlist.tierRadar',
} as const;

export function CoverageWatchlistRowItem({
  item,
  marketStatus,
  attach,
}: {
  item: CoverageWatchlistRow;
  marketStatus: MarketStatusData | null;
  attach?: ReactNode;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const navigate = useNavigate();
  const hasQuote = !item.is_private && item.quoteAvailable !== false;
  const code = quoteCurrency(item.currency, item.symbol, item.assetClass);
  const quoteDisplay = deriveQuoteDisplay(item, marketStatus);
  const mainPrice = quoteDisplay.mainPrice;
  const mainChangePct = quoteDisplay.mainChangePct;

  return (
    <motion.tr
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={TWEEN.quick}
      className="row-attach-host"
      data-clickable=""
      data-testid={`coverage-row-${item.symbol}`}
      onClick={() => navigate(`/market?symbol=${encodeURIComponent(item.symbol)}`)}
    >
      <td className="term-fill term-code term-strong dash-attach-cell">
        <TickerLabel symbol={item.symbol}>
          <span title={item.name || item.symbol}>{item.symbol}</span>
        </TickerLabel>
        <span
          className="ml-1.5 text-[10px] font-normal px-1 py-0.5 rounded"
          style={{
            backgroundColor: 'var(--color-bg-elevated)',
            color: 'var(--color-text-tertiary)',
          }}
        >
          {t(COVERAGE_TIER_LABEL_KEY[item.tier])}
        </span>
        {attach}
      </td>
      <td className="term-num">
        {item.is_private ? (
          <span className="term-t3">{t('dashboard:widgets.portfolioWatchlist.unlisted')}</span>
        ) : hasQuote && mainPrice != null ? (
          formatMoney(
            item.places && item.places > 2 ? Number(mainPrice) : roundQuote2(Number(mainPrice)),
            code,
            locale,
            { upTo: item.places },
          )
        ) : (
          <span className="term-t3">{DASH}</span>
        )}
      </td>
      <td className="term-num">
        {item.is_private || !hasQuote ? (
          <span className="term-t3">{DASH}</span>
        ) : mainChangePct != null ? (
          <TermChange value={roundQuote2(Number(mainChangePct))} />
        ) : (
          <span className="term-t3">{DASH}</span>
        )}
      </td>
    </motion.tr>
  );
}

export function CoverageWatchlistTabPanel({
  instanceId,
  coverage,
  filteredRows,
  filteredTotalCount,
  coverageTier,
  onSelectTier,
  showSkeleton,
  skeletonRows,
  marketStatus,
}: {
  instanceId: string;
  coverage: UseCoverageWatchlistDataResult;
  filteredRows: CoverageWatchlistRow[];
  filteredTotalCount?: number;
  coverageTier: WatchlistTier | 'all';
  onSelectTier: (tier: WatchlistTier | 'all') => void;
  showSkeleton: boolean;
  skeletonRows: number;
  marketStatus: MarketStatusData | null;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const effectiveTotal = filteredTotalCount ?? coverage.totalCount;

  return (
    <div>
      <div className="flex flex-wrap items-center gap-1 px-1 pb-1.5" role="group">
        {(
          [
            ['all', t('dashboard:widgets.portfolioWatchlist.tierAll')],
            ['core', t('dashboard:widgets.portfolioWatchlist.tierCore')],
            ['watch', t('dashboard:widgets.portfolioWatchlist.tierWatch')],
            ['radar', t('dashboard:widgets.portfolioWatchlist.tierRadar')],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            aria-pressed={coverageTier === key}
            onClick={() => onSelectTier(key)}
            className="text-[11px] px-2 py-0.5 rounded-full border widget-drag-cancel"
            style={{
              backgroundColor: coverageTier === key ? 'var(--color-bg-hover)' : 'transparent',
              borderColor: 'var(--color-border-muted)',
              color:
                coverageTier === key
                  ? 'var(--color-text-primary)'
                  : 'var(--color-text-tertiary)',
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <WatchlistTable>
        {showSkeleton ? (
          <HoldingsSkeleton count={skeletonRows} />
        ) : (
          <AnimatePresence initial={false}>
            {filteredRows.map((row) => (
              <CoverageWatchlistRowItem
                key={row.symbol}
                item={row}
                marketStatus={marketStatus}
                attach={<RowAttachButton instanceId={instanceId} rowId={row.symbol} />}
              />
            ))}
          </AnimatePresence>
        )}
      </WatchlistTable>
      <div className="flex items-center justify-between px-2 pt-2 text-xs">
        {effectiveTotal > filteredRows.length ? (
          <span style={{ color: 'var(--color-text-tertiary)' }}>
            {t('dashboard:widgets.portfolioWatchlist.coverageTruncated', {
              shown: filteredRows.length,
              total: effectiveTotal,
            })}
          </span>
        ) : (
          <span />
        )}
        <button
          type="button"
          onClick={() => navigate('/market')}
          className="inline-flex items-center gap-1 font-medium hover:underline widget-drag-cancel"
          style={{ color: 'var(--color-accent-light)' }}
        >
          <span>{t('dashboard:widgets.portfolioWatchlist.viewAllCoverage')}</span>
          <ArrowRight className="h-3 w-3" />
        </button>
      </div>
    </div>
  );
}

interface PendingTierChange {
  action: 'add' | 'set_tier';
  symbol: string;
  currentTier: WatchlistTier | null;
  nextTier: WatchlistTier;
  preview: WatchlistPreviewResponse;
}

const TIER_I18N_KEY: Record<WatchlistTier, string> = {
  core: 'dashboard:widgets.portfolioWatchlist.tierCore',
  watch: 'dashboard:widgets.portfolioWatchlist.tierWatch',
  radar: 'dashboard:widgets.portfolioWatchlist.tierRadar',
};

/**
 * One-click right-click context menu handler for Dashboard Watchlist ("自选股") & Portfolio ("持仓") rows.
 * Adds the ticker to the user's personal watchlist (`core` / `watch` / `radar`) without mutating
 * the source `watchlists` / `user_portfolios` tables.
 */
export function useQuickAddToCoverage(coverage: UseCoverageWatchlistDataResult) {
  const { t } = useTranslation();
  const queryClient = useContext(QueryClientContext);
  const { toast } = useToast();
  const [pendingTierChange, setPendingTierChange] = useState<PendingTierChange | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);

  const existingByTicker = useMemo(() => {
    const map = new Map<string, PersonalWatchlistCompany>();
    for (const c of coverage.allCompanies ?? []) {
      map.set(c.ticker.toUpperCase(), c);
    }
    return map;
  }, [coverage.allCompanies]);

  const handleAdd = useCallback(
    async (rawSymbol: string, tier: WatchlistTier) => {
      const symbol = rawSymbol.trim().toUpperCase();
      if (!symbol) return;
      const tierLabel = t(TIER_I18N_KEY[tier]);
      const existing = existingByTicker.get(symbol);

      if (existing && existing.tier === tier) {
        toast({
          title: t('dashboard:widgets.portfolioWatchlist.alreadyInCoverage', {
            symbol,
            tier: tierLabel,
          }),
        });
        return;
      }

      if (existing && existing.tier !== tier) {
        setModalError(null);
        try {
          const preview = await previewWatchlistMutation({
            action: 'set_tier',
            tickers: [symbol],
            tier,
          });
          if (!preview.ok || preview.precheck_error) {
            toast({
              variant: 'destructive',
              title: preview.precheck_error || t('dashboard:widgets.portfolioWatchlist.quickAddFailed'),
            });
            return;
          }
          setPendingTierChange({
            action: 'set_tier',
            symbol,
            currentTier: existing.tier,
            nextTier: tier,
            preview,
          });
        } catch (err) {
          toast({
            variant: 'destructive',
            title: apiErrorDetailMessage(err) || t('dashboard:widgets.portfolioWatchlist.quickAddFailed'),
          });
        }
        return;
      }

      try {
        const res = await mutateWatchlist({
          action: 'add',
          tickers: [symbol],
          tier,
        });
        const addedCount = res?.added_tickers?.length ?? res?.added?.length ?? 0;
        if (!res?.ok || addedCount === 0) {
          const notFound = res?.not_found?.length ? ` (${res.not_found.join(', ')})` : '';
          toast({
            variant: 'destructive',
            title: `${t('dashboard:widgets.portfolioWatchlist.quickAddFailed')}${notFound}`,
          });
          return;
        }
        if (queryClient) {
          await queryClient.invalidateQueries({ queryKey: queryKeys.dalton.watchlist() });
        }
        coverage.refetch?.();
        toast({
          title: t('dashboard:widgets.portfolioWatchlist.quickAddSuccess', {
            symbol,
            tier: tierLabel,
          }),
        });
      } catch (err) {
        if (apiErrorStatus(err) === 409) {
          // Preview with the original `add` action first so paused-task workspaces are NOT dropped.
          // Only switch to `set_tier` if the server confirms the ticker is already in the roster.
          try {
            const addPreview = await previewWatchlistMutation({
              action: 'add',
              tickers: [symbol],
              tier,
            });
            const oldTier = addPreview.affected_tickers?.[0]?.old_tier as WatchlistTier | null | undefined;
            if (oldTier) {
              if (oldTier === tier) {
                if (queryClient) {
                  await queryClient.invalidateQueries({ queryKey: queryKeys.dalton.watchlist() });
                }
                coverage.refetch?.();
                toast({
                  title: t('dashboard:widgets.portfolioWatchlist.alreadyInCoverage', {
                    symbol,
                    tier: tierLabel,
                  }),
                });
                return;
              }
              const setTierPreview = await previewWatchlistMutation({
                action: 'set_tier',
                tickers: [symbol],
                tier,
              });
              if (setTierPreview.ok && setTierPreview.snapshot_id) {
                setModalError(null);
                setPendingTierChange({
                  action: 'set_tier',
                  symbol,
                  currentTier: oldTier,
                  nextTier: tier,
                  preview: setTierPreview,
                });
                return;
              }
            } else if (addPreview.ok && addPreview.snapshot_id) {
              setModalError(null);
              setPendingTierChange({
                action: 'add',
                symbol,
                currentTier: null,
                nextTier: tier,
                preview: addPreview,
              });
              return;
            }
          } catch {
            // fall through to destructive toast
          }
        }
        toast({
          variant: 'destructive',
          title: apiErrorDetailMessage(err) || t('dashboard:widgets.portfolioWatchlist.quickAddFailed'),
        });
      }
    },
    [coverage, existingByTicker, queryClient, t, toast],
  );

  const handleConfirmTierChange = useCallback(async () => {
    if (!pendingTierChange?.preview.snapshot_id) return;
    setSubmitting(true);
    setModalError(null);
    try {
      const res = await mutateWatchlist({
        action: pendingTierChange.action,
        snapshot_id: pendingTierChange.preview.snapshot_id,
      });
      const affectedCount =
        pendingTierChange.action === 'add'
          ? (res?.added_tickers?.length ?? res?.added?.length ?? 0)
          : (res?.updated?.length ?? 0);
      if (!res?.ok || affectedCount === 0) {
        const notFound = res?.not_found?.length ? ` (${res.not_found.join(', ')})` : '';
        setModalError(`${t('dashboard:widgets.portfolioWatchlist.quickAddFailed')}${notFound}`);
        return;
      }
      if (queryClient) {
        await queryClient.invalidateQueries({ queryKey: queryKeys.dalton.watchlist() });
      }
      coverage.refetch?.();
      toast({
        title: t('dashboard:widgets.portfolioWatchlist.quickAddSuccess', {
          symbol: pendingTierChange.symbol,
          tier: t(TIER_I18N_KEY[pendingTierChange.nextTier]),
        }),
      });
      setPendingTierChange(null);
    } catch (err) {
      if (apiErrorStatus(err) === 409) {
        setModalError(t('dashboard:widgets.portfolioWatchlist.scopeChanged'));
      } else {
        setModalError(
          apiErrorDetailMessage(err) || t('dashboard:widgets.portfolioWatchlist.quickAddFailed'),
        );
      }
    } finally {
      setSubmitting(false);
    }
  }, [coverage, pendingTierChange, queryClient, t, toast]);

  const renderMenuItems = useCallback(
    (symbol: string, withSeparator = false): ReactNode => {
      if (!isDaltonBuild || !symbol) return null;
      return (
        <>
          <ContextMenuGroup>
            {WATCHLIST_TIERS.map((tier) => (
              <ContextMenuItem
                key={tier}
                data-testid={`quick-add-coverage-${symbol.toUpperCase()}-${tier}`}
                onSelect={() => void handleAdd(symbol, tier)}
              >
                <BookmarkPlus className="h-3.5 w-3.5" />
                {t('dashboard:widgets.portfolioWatchlist.addToCoverageTier', {
                  tier: t(TIER_I18N_KEY[tier]),
                })}
              </ContextMenuItem>
            ))}
          </ContextMenuGroup>
          {withSeparator && <ContextMenuSeparator />}
        </>
      );
    },
    [handleAdd, t],
  );

  const confirmModal = pendingTierChange ? (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={t('dashboard:widgets.portfolioWatchlist.confirmTierChangeTitle')}
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ backgroundColor: 'var(--color-bg-overlay)' }}
      onClick={() => !submitting && setPendingTierChange(null)}
    >
      <div
        className="w-full max-w-sm rounded-xl border p-4 shadow-xl space-y-3"
        style={{
          backgroundColor: 'var(--color-bg-card)',
          borderColor: 'var(--color-border-muted)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-sm font-semibold" style={{ color: 'var(--color-text-primary)' }}>
          {pendingTierChange.preview.card_title ||
            t('dashboard:widgets.portfolioWatchlist.confirmTierChangeTitle')}
        </h3>
        {pendingTierChange.preview.card_description && (
          <p className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            {pendingTierChange.preview.card_description}
          </p>
        )}
        <div
          className="rounded-md border px-2.5 py-2 text-xs flex items-center justify-between"
          style={{ borderColor: 'var(--color-border-muted)' }}
        >
          <span className="font-mono font-medium" style={{ color: 'var(--color-text-primary)' }}>
            {pendingTierChange.symbol}
          </span>
          <span style={{ color: 'var(--color-text-secondary)' }}>
            {pendingTierChange.currentTier
              ? `${t(TIER_I18N_KEY[pendingTierChange.currentTier])} → ${t(TIER_I18N_KEY[pendingTierChange.nextTier])}`
              : `+ ${t(TIER_I18N_KEY[pendingTierChange.nextTier])}`}
          </span>
        </div>
        {modalError && (
          <div className="flex items-start gap-1.5 text-xs" style={{ color: 'var(--color-warning)' }}>
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
            <span>{modalError}</span>
          </div>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            disabled={submitting}
            onClick={() => setPendingTierChange(null)}
            className="px-3 py-1.5 rounded-md text-xs font-medium"
            style={{ backgroundColor: 'var(--color-bg-hover)', color: 'var(--color-text-secondary)' }}
          >
            {t('dashboard:widgets.common.cancel')}
          </button>
          <button
            type="button"
            disabled={submitting}
            onClick={() => void handleConfirmTierChange()}
            className="px-3.5 py-1.5 rounded-md text-xs font-medium disabled:opacity-50"
            style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
          >
            {t('dashboard:widgets.common.done')}
          </button>
        </div>
      </div>
    </div>
  ) : null;

  return { renderMenuItems, confirmModal };
}
