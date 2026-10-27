import { AnimatePresence } from '@/lib/framer';
import { SwapCell } from '@/components/ui/crossfade';
import { useTranslation } from 'react-i18next';
import { Eye } from 'lucide-react';
import { useDashboardContext } from '../framework/DashboardDataContext';
import { registerWidget } from '../framework/WidgetRegistry';
import { WatchlistConfigSchema } from '../framework/configSchemas';
import { useWidgetContextExport } from '../framework/contextSnapshot';
import {
  serializeQuoteRowsToMarkdown,
  serializeQuoteRowToMarkdown,
  wrapWidgetContext,
} from '../framework/snapshotSerializers';
import { RowAttachButton } from '../../components/RowAttachButton';
import type { WidgetRenderProps } from '../types';
import { formatSignedPct2 } from '@/lib/quotes'; // DALTON-HOOK(extended-hours-quotes)
import { watchlistQuoteRow } from '../../hooks/useWatchlistData';
import {
  HoldingsAddButton,
  HoldingsSkeleton,
  useHoldingsSkeletonRows,
  WatchlistRowItem,
} from './_holdingsPrimitives';

type WatchlistConfig = Record<string, never>;

function WatchlistWidget({ instance }: WidgetRenderProps<WatchlistConfig>) {
  const { t } = useTranslation();
  const { watchlist, watchlistHandlers, dashboard } = useDashboardContext();
  const showSkeleton = watchlist.loading && watchlist.rows.length === 0;
  const skeletonRows = useHoldingsSkeletonRows('watchlist', watchlist.loaded ? watchlist.rows.length : null);

  // Register snapshot exporters: full table + per-row.
  useWidgetContextExport(instance.id, {
    full: () => {
      // DALTON-HOOK(extended-hours-quotes): Pass marketStatus so full watchlist export includes extended-hours quotes.
      const rows = watchlist.rows.map((r) => watchlistQuoteRow(r, dashboard.marketStatus));
      const body = serializeQuoteRowsToMarkdown(rows);
      const text = wrapWidgetContext('watchlist.list', { count: rows.length }, body);
      return {
        widget_type: 'watchlist.list',
        widget_id: instance.id,
        label: t('dashboard.widgets.watchlist.title') + ' · ' + rows.length,
        description: rows.length ? `${rows.length} symbol${rows.length === 1 ? '' : 's'}` : 'empty',
        captured_at: new Date().toISOString(),
        text,
        data: { rows },
      };
    },
    rows: (rowId: string) => {
      const row = watchlist.rows.find((r) => (r.watchlist_item_id ?? r.symbol) === rowId);
      if (!row) return null;
      // DALTON-HOOK(extended-hours-quotes): Pass dashboard.marketStatus so watchlistQuoteRow exports
      // the settled close and regular change alongside the pre/post-market price and move from getExtendedHoursInfo.
      const cleaned = watchlistQuoteRow(row, dashboard.marketStatus);
      const body = serializeQuoteRowToMarkdown(cleaned);
      const text = wrapWidgetContext('watchlist.list/row', { symbol: row.symbol }, body);
      const labelPct = cleaned.changePercent;
      return {
        widget_type: 'watchlist.list/row',
        widget_id: `${instance.id}/${rowId}`,
        label:
          row.symbol +
          (row.quoteAvailable !== false && labelPct !== undefined
            ? ` · ${formatSignedPct2(labelPct)}`
            : ''),
        description: t('dashboard.widgets.watchlist.title'),
        captured_at: new Date().toISOString(),
        text,
        data: { row: cleaned },
      };
    },
  });

  return (
    <div className="dashboard-glass-card p-5 flex flex-col h-full">
      <div
        className="flex items-baseline justify-between mb-3 pb-3 border-b"
        style={{ borderColor: 'var(--color-border-muted)' }}
      >
        <div className="flex items-baseline gap-2.5 min-w-0">
          <Eye
            className="h-3.5 w-3.5 shrink-0 self-center"
            style={{ color: 'var(--color-text-tertiary)' }}
          />
          <span
            className="text-[0.625rem] font-semibold uppercase tracking-[0.14em]"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {t('dashboard.widgets.watchlist.header')}
          </span>
          {/* Held but unseen while the rows load: a count of 0 that turns into
              the real one reads as a change, not a load. */}
          <span
            className="title-font text-lg leading-none dashboard-mono"
            style={{ color: 'var(--color-text-primary)', visibility: showSkeleton ? 'hidden' : undefined }}
          >
            {watchlist.rows.length}
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
          {showSkeleton ? (
            <HoldingsSkeleton count={skeletonRows} />
          ) : (
            <AnimatePresence initial={false}>
              {watchlist.rows.map((row) => (
                <div
                  key={row.watchlist_item_id ?? row.symbol}
                  className="row-attach-host relative"
                >
                  <WatchlistRowItem
                    item={row}
                    marketStatus={dashboard.marketStatus}
                    onDelete={watchlistHandlers.onDelete}
                  />
                  <span className="absolute right-1 top-1/2 -translate-y-1/2">
                    <RowAttachButton
                      instanceId={instance.id}
                      rowId={String(row.watchlist_item_id ?? row.symbol)}
                    />
                  </span>
                </div>
              ))}
            </AnimatePresence>
          )}

          <HoldingsAddButton label={t('dashboard.widgets.watchlist.addSymbol')} onClick={watchlistHandlers.onAdd} />
        </SwapCell>
      </div>
    </div>
  );
}

registerWidget<WatchlistConfig>({
  type: 'watchlist.list',
  titleKey: 'dashboard.widgets.watchlist.title',
  descriptionKey: 'dashboard.widgets.watchlist.description',
  category: 'personal',
  icon: Eye,
  component: WatchlistWidget,
  defaultConfig: {},
  configSchema: WatchlistConfigSchema,
  defaultSize: { w: 4, h: 26 },
  minSize: { w: 3, h: 15 },
});

export default WatchlistWidget;
