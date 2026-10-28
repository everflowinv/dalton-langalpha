import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocale } from '@/hooks/useLocale';
import { AnimatePresence } from '@/lib/framer';
import { SwapCell } from '@/components/ui/crossfade';
import { Briefcase } from 'lucide-react';
import { useDashboardContext } from '../framework/DashboardDataContext';
import { registerWidget } from '../framework/WidgetRegistry';
import { useWidgetContextExport } from '../framework/contextSnapshot';
import {
  serializeQuoteRowsToMarkdown,
  serializeQuoteRowToMarkdown,
  wrapWidgetContext,
} from '../framework/snapshotSerializers';
import { PortfolioConfigSchema } from '../framework/configSchemas';
import { RowAttachButton } from '../../components/RowAttachButton';
import type { WidgetRenderProps } from '../types';
import type { PortfolioRow } from '../../hooks/usePortfolioData';
import { deriveQuoteDisplay, formatSignedPct2, roundQuote2, type MarketStatusInput } from '@/lib/quotes'; // DALTON-HOOK(extended-hours-quotes)
import {
  HoldingsAddButton,
  HoldingsSkeleton,
  useHoldingsSkeletonRows,
  PortfolioNavSummary,
  PortfolioRowItem,
} from './_holdingsPrimitives';
import { formatPortfolioNavMarkdownLine, portfolioSummary } from './_holdingsHelpers';
import { normalizePortfolioCurrency } from '../../utils/portfolioSummary';

type PortfolioConfig = { valuesHidden?: boolean };

// DALTON-HOOK(extended-hours-quotes): Derive price, change, session, and pre/post-market fields from shared deriveQuoteDisplay.
export function rowToQuote(r: PortfolioRow, marketStatus?: MarketStatusInput) {
  const hasQuote = r.quoteAvailable !== false;
  if (!hasQuote) {
    return {
      symbol: r.symbol,
      price: undefined,
      shares: r.quantity ?? undefined,
      marketValue: undefined,
      changePercent: undefined,
      currency: normalizePortfolioCurrency(r.currency),
    };
  }
  const d = deriveQuoteDisplay(r, marketStatus);
  const price = d.mainPrice != null ? roundQuote2(d.mainPrice) : undefined;
  const change = d.mainChange != null ? roundQuote2(d.mainChange) : undefined;
  const changePercent = d.mainChangePct != null ? roundQuote2(d.mainChangePct) : undefined;
  const unrealizedPlPercent =
    r.unrealizedPlPercent != null ? roundQuote2(r.unrealizedPlPercent) : undefined;
  const extPrice = d.extPrice != null ? roundQuote2(d.extPrice) : undefined;
  const extChange = d.extChange != null ? roundQuote2(d.extChange) : undefined;
  const extPct = d.extChangePct != null ? roundQuote2(d.extChangePct) : undefined;
  // DALTON-HOOK(extended-hours-quotes): Keep quote change/changePercent separate from holding unrealizedPlPercent.
  return {
    symbol: r.symbol,
    ...(d.session !== 'regular' ? { session: d.session } : {}),
    price,
    ...(change !== undefined ? { change } : {}),
    ...(changePercent !== undefined ? { changePercent } : {}),
    shares: r.quantity ?? undefined,
    marketValue: r.marketValue ?? undefined,
    ...(unrealizedPlPercent !== undefined ? { unrealizedPlPercent } : {}),
    ...(d.session === 'pre' && extPrice !== undefined
      ? {
          preMarket: extPrice,
          ...(extChange !== undefined ? { preMarketChange: extChange } : {}),
          ...(extPct !== undefined ? { preMarketChangePercent: extPct } : {}),
        }
      : {}),
    ...(d.session === 'post' && extPrice !== undefined
      ? {
          postMarket: extPrice,
          ...(extChange !== undefined ? { postMarketChange: extChange } : {}),
          ...(extPct !== undefined ? { postMarketChangePercent: extPct } : {}),
        }
      : {}),
    currency: normalizePortfolioCurrency(r.currency),
  };
}

function PortfolioWidget({ instance, updateConfig }: WidgetRenderProps<PortfolioConfig>) {
  const { t } = useTranslation();
  const locale = useLocale();
  const { portfolio, portfolioHandlers, dashboard } = useDashboardContext();

  useWidgetContextExport(instance.id, {
    full: () => {
      // DALTON-HOOK(extended-hours-quotes): Pass dashboard.marketStatus so portfolio export includes session and extended-hours quotes.
      const rows = portfolio.rows.map((r) => rowToQuote(r, dashboard.marketStatus));
      const summary = portfolioSummary(portfolio.rows);
      const navLine = formatPortfolioNavMarkdownLine(summary, locale);
      const lines: string[] = [];
      if (navLine) lines.push(navLine, '');
      lines.push(serializeQuoteRowsToMarkdown(rows));
      const text = wrapWidgetContext('portfolio.holdings', { count: rows.length }, lines.join('\n'));
      return {
        widget_type: 'portfolio.holdings',
        widget_id: instance.id,
        label: `${t('dashboard.widgets.portfolio.title')} · ${rows.length}`,
        description: rows.length ? `${rows.length} holding${rows.length === 1 ? '' : 's'}` : 'empty',
        captured_at: new Date().toISOString(),
        text,
        data: { rows, summary },
      };
    },
    rows: (rowId: string) => {
      const row = portfolio.rows.find((r) => (r.user_portfolio_id ?? r.symbol) === rowId);
      if (!row) return null;
      // DALTON-HOOK(extended-hours-quotes): Pass dashboard.marketStatus to rowToQuote and format label via formatSignedPct2.
      const quote = rowToQuote(row, dashboard.marketStatus);
      const body = serializeQuoteRowToMarkdown(quote);
      const text = wrapWidgetContext('portfolio.holdings/row', { symbol: row.symbol }, body);
      return {
        widget_type: 'portfolio.holdings/row',
        widget_id: `${instance.id}/${rowId}`,
        label:
          row.symbol +
          (row.quoteAvailable !== false && row.unrealizedPlPercent != null
            ? ` · ${formatSignedPct2(row.unrealizedPlPercent)}`
            : ''),
        description: t('dashboard.widgets.portfolio.title'),
        captured_at: new Date().toISOString(),
        text,
        data: { row: quote },
      };
    },
  });
  const [valuesHidden, setValuesHidden] = useState(!!instance.config.valuesHidden);

  const toggleHidden = () => {
    const next = !valuesHidden;
    setValuesHidden(next);
    updateConfig({ valuesHidden: next });
  };

  const showSkeleton = portfolio.loading && portfolio.rows.length === 0;
  const skeletonRows = useHoldingsSkeletonRows('portfolio', portfolio.loaded ? portfolio.rows.length : null);
  const hasRealHoldings = portfolio.hasRealHoldings;

  return (
    <div className="dashboard-glass-card p-5 flex flex-col h-full">
      <div
        className="flex items-baseline justify-between mb-3 pb-3 border-b"
        style={{ borderColor: 'var(--color-border-muted)' }}
      >
        <div className="flex items-baseline gap-2.5 min-w-0">
          <Briefcase
            className="h-3.5 w-3.5 shrink-0 self-center"
            style={{ color: 'var(--color-text-tertiary)' }}
          />
          <span
            className="text-[0.625rem] font-semibold uppercase tracking-[0.14em]"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {t('dashboard.widgets.portfolio.header')}
          </span>
          {/* Held but unseen while the rows load: a count of 0 that turns into
              the real one reads as a change, not a load. */}
          <span
            className="title-font text-lg leading-none dashboard-mono"
            style={{ color: 'var(--color-text-primary)', visibility: showSkeleton ? 'hidden' : undefined }}
          >
            {portfolio.rows.length}
          </span>
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto pr-1">
        {/* The skeleton crossfades to the rows in place. Rows present when the
            list first renders just are; only one added later enters. */}
        <SwapCell
          as="div"
          className="items-start"
          swapKey={showSkeleton ? 'loading' : 'ready'}
          layerClassName="flex flex-col gap-1"
        >
          {hasRealHoldings && (
            <PortfolioNavSummary
              rows={portfolio.rows}
              valuesHidden={valuesHidden}
              onToggleHidden={toggleHidden}
            />
          )}

          {showSkeleton ? (
            <HoldingsSkeleton count={skeletonRows} />
          ) : (
            <AnimatePresence initial={false}>
              {portfolio.rows.map((row) => (
                <div
                  key={row.user_portfolio_id ?? row.symbol}
                  className="row-attach-host relative"
                >
                  <PortfolioRowItem
                    item={row}
                    marketStatus={dashboard.marketStatus}
                    valuesHidden={valuesHidden}
                    onEdit={portfolioHandlers.onEdit}
                    onDelete={portfolioHandlers.onDelete}
                  />
                  <span className="absolute right-1 top-1/2 -translate-y-1/2">
                    <RowAttachButton
                      instanceId={instance.id}
                      rowId={String(row.user_portfolio_id ?? row.symbol)}
                    />
                  </span>
                </div>
              ))}
            </AnimatePresence>
          )}

          <HoldingsAddButton label={t('dashboard.widgets.portfolio.addTransaction')} onClick={portfolioHandlers.onAdd} />
        </SwapCell>
      </div>
    </div>
  );
}

registerWidget<PortfolioConfig>({
  type: 'portfolio.holdings',
  titleKey: 'dashboard.widgets.portfolio.title',
  descriptionKey: 'dashboard.widgets.portfolio.description',
  category: 'personal',
  icon: Briefcase,
  component: PortfolioWidget,
  defaultConfig: { valuesHidden: false },
  configSchema: PortfolioConfigSchema,
  defaultSize: { w: 4, h: 26 },
  minSize: { w: 3, h: 15 },
});

export default PortfolioWidget;
