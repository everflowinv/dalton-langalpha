/**
 * Dalton Personal Watchlist tab & compact row component for Dashboard PortfolioWatchlistWidget.
 * Keeps upstream `PortfolioWatchlistWidget.tsx` and `_holdingsPrimitives.tsx` to thin hooks.
 */

import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ArrowRight } from 'lucide-react';
import { AnimatePresence, motion } from '@/lib/framer';
import { TWEEN } from '@/lib/motion';
import { TickerLabel } from '@/components/TickerLogo';
import { isDaltonBuild } from '@/config/brand';
import { useLocale } from '@/hooks/useLocale';
import { DASH } from '@/lib/format';
import { formatMoney, quoteCurrency } from '@/lib/bars';
import {
  deriveQuoteDisplay,
  formatSignedPct2,
  roundQuote2,
  useQuotes,
  type MarketStatusInput,
} from '@/lib/quotes';
import type { PersonalWatchlistCompany, WatchlistTier } from '@/lib/dalton/watchlist';
import { TermChange } from '@/pages/ChatAgent/components/charts/TermCard';
import { ExtendedHours, formatExtendedQuotePrice } from '@/components/dalton/ExtendedHours';
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
  const extType = quoteDisplay.session === 'regular' ? null : quoteDisplay.session;
  const extLabel = extType === 'pre' ? 'PM' : extType === 'post' ? 'AH' : null;
  const mainPrice = quoteDisplay.mainPrice;
  const mainChangePct = quoteDisplay.mainChangePct;
  const extDisplayPrice = quoteDisplay.extPrice;
  const extPct = quoteDisplay.extChangePct;

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
        {hasQuote && extType && extLabel && extDisplayPrice != null && extPct != null && (
          <span className="block">
            <ExtendedHours
              label={extLabel}
              price={formatExtendedQuotePrice(extDisplayPrice, code, locale, item.places)}
              pct={roundQuote2(Number(extPct))}
            />
          </span>
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
