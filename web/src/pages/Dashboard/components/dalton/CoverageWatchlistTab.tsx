/**
 * Dalton Personal Watchlist tab & compact row component for Dashboard PortfolioWatchlistWidget.
 * Keeps upstream `PortfolioWatchlistWidget.tsx` and `_holdingsPrimitives.tsx` to thin hooks.
 */

import type { ReactNode } from 'react';
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
  type MarketStatusInput,
} from '@/lib/quotes';
import type { WatchlistTier } from '@/lib/dalton/watchlist';
import { TermChange } from '@/pages/ChatAgent/components/charts/TermCard';
import {
  coverageQuoteRow,
  MAX_DASHBOARD_COVERAGE_QUOTES,
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

export function normalizeCoverageContext(
  rawCoverage: UseCoverageWatchlistDataResult | undefined,
): UseCoverageWatchlistDataResult {
  return (
    rawCoverage ?? ({
      rows: EMPTY_COVERAGE_ROWS,
      totalCount: 0,
      quotedLimit: MAX_DASHBOARD_COVERAGE_QUOTES,
      counts: { core: 0, watch: 0, radar: 0, total: 0, groups: 0 },
      loading: false,
      loaded: true,
      refetch: async () => undefined,
    } as unknown as UseCoverageWatchlistDataResult)
  );
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
  coverageTier,
  marketStatus,
  headerLabel,
}: {
  instanceId: string;
  coverage: UseCoverageWatchlistDataResult;
  filteredRows: CoverageWatchlistRow[];
  coverageTier: WatchlistTier | 'all';
  marketStatus: MarketStatusData | null;
  headerLabel: string;
}) {
  const rows = filteredRows.map((r) => coverageQuoteRow(r, marketStatus));
  const body = serializeQuoteRowsToMarkdown(rows);
  const text = wrapWidgetContext(
    'personal.portfolioWatchlist',
    { tab: 'coverage', tier: coverageTier, count: coverage.totalCount, shown: rows.length },
    body,
  );
  return {
    widget_type: 'personal.portfolioWatchlist',
    widget_id: instanceId,
    label: `${headerLabel} · ${coverage.totalCount}`,
    description: rows.length ? `${rows.length} symbol${rows.length === 1 ? '' : 's'}` : 'empty',
    captured_at: new Date().toISOString(),
    text,
    data: { tab: 'coverage', tier: coverageTier, totalCount: coverage.totalCount, rows },
  };
}

export function buildCoverageRowContextExport({
  instanceId,
  rowId,
  coverage,
  marketStatus,
  headerLabel,
}: {
  instanceId: string;
  rowId: string;
  coverage: UseCoverageWatchlistDataResult;
  marketStatus: MarketStatusData | null;
  headerLabel: string;
}) {
  const row = coverage.rows.find((r) => r.symbol === rowId);
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
  coverageTier,
  onSelectTier,
  showSkeleton,
  skeletonRows,
  marketStatus,
}: {
  instanceId: string;
  coverage: UseCoverageWatchlistDataResult;
  filteredRows: CoverageWatchlistRow[];
  coverageTier: WatchlistTier | 'all';
  onSelectTier: (tier: WatchlistTier | 'all') => void;
  showSkeleton: boolean;
  skeletonRows: number;
  marketStatus: MarketStatusData | null;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();

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
        {coverage.totalCount > coverage.quotedLimit ? (
          <span style={{ color: 'var(--color-text-tertiary)' }}>
            {t('dashboard:widgets.portfolioWatchlist.coverageTruncated', {
              shown: coverage.quotedLimit,
              total: coverage.totalCount,
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
