import React, { useState, useCallback, useEffect, useMemo } from 'react';
import { X, ChevronLeft, ChevronRight, BarChart3, Sunrise, Sunset } from 'lucide-react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useWatchlistData } from '../../Dashboard/hooks/useWatchlistData';
import { usePortfolioData } from '../../Dashboard/hooks/usePortfolioData';
import { useMarketDataWSContext } from '../contexts/MarketDataWSContext';
import AddWatchlistItemDialog from '../../Dashboard/components/AddWatchlistItemDialog';
import AddPortfolioHoldingDialog from '../../Dashboard/components/AddPortfolioHoldingDialog';
import ConfirmDialog from '../../Dashboard/components/ConfirmDialog';
import { getExtendedHoursInfo } from '@/lib/marketUtils';
import { deriveQuoteDisplay, formatSignedPct2, roundQuote2 } from '@/lib/quotes'; // DALTON-HOOK(extended-hours-quotes)
import { grouped2 } from '@/lib/format';
import { useLocale } from '@/hooks/useLocale';
import { EXT_COLOR_PRE, EXT_COLOR_POST } from '../utils/chartConstants';
import { useIsMobile } from '@/hooks/useIsMobile';
import './MarketSidebarPanel.css';

interface SidebarRow {
  symbol: string;
  price: number;
  previousClose?: number | null;
  // DALTON-HOOK(extended-hours-quotes): Extended-hours and regular-session fields for accurate sidebar quotes.
  regularClose?: number | null;
  regularTradingChange?: number | null;
  regularTradingChangePercent?: number | null;
  earlyTradingChange?: number | null;
  lateTradingChange?: number | null;
  lastMinuteClose?: number | null;
  lastTradePrice?: number | null;
  liveTickAt?: number | null;
  isPositive: boolean;
  changePercent?: number;
  unrealizedPlPercent?: number;
  watchlist_item_id?: string;
  user_portfolio_id?: string;
  earlyTradingChangePercent?: number | null;
  lateTradingChangePercent?: number | null;
  early_trading_change_percent?: number | null;
  late_trading_change_percent?: number | null;
  [key: string]: unknown;
}

interface DeleteConfirmState {
  open: boolean;
  title: string;
  message: string;
  onConfirm: (() => Promise<void>) | null;
}


interface MarketSidebarPanelProps {
  activeSymbol: string | null;
  onSymbolClick?: (symbol: string) => void;
  marketStatus: Record<string, unknown> | null;
}

function MarketSidebarPanel({ activeSymbol, onSymbolClick, marketStatus }: MarketSidebarPanelProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const locale = useLocale();
  const [expanded, setExpanded] = useState(false);
  const effectiveExpanded = isMobile || expanded;
  const [activeTab, setActiveTab] = useState('watchlist');
  const watchlist = useWatchlistData();
  const portfolio = usePortfolioData();
  const { prices: wsPrices, connectionStatus: wsStatus, subscribe: wsSubscribe, unsubscribe: wsUnsubscribe } = useMarketDataWSContext();

  // Stable symbol string — only changes when the actual set of symbols changes,
  // not when the rows array reference is replaced by a polling fetch.
  const sidebarSymbolsKey = useMemo(() => {
    const all = [...new Set([
      ...watchlist.rows.map((r) => r.symbol),
      ...portfolio.rows.map((r) => r.symbol),
    ])].filter(Boolean).sort();
    return all.join(',');
  }, [watchlist.rows, portfolio.rows]);

  // Subscribe all sidebar symbols to WS feed
  useEffect(() => {
    const symbols = sidebarSymbolsKey ? sidebarSymbolsKey.split(',') : [];
    if (symbols.length) wsSubscribe(symbols);
    return () => { if (symbols.length) wsUnsubscribe(symbols); };
  }, [sidebarSymbolsKey, wsSubscribe, wsUnsubscribe]);

  const [deleteConfirm, setDeleteConfirm] = useState<DeleteConfirmState>({
    open: false,
    title: '',
    message: '',
    onConfirm: null,
  });

  const handlePortfolioDelete = useCallback(
    (holdingId: string) => {
      setDeleteConfirm(portfolio.handleDelete(holdingId) as DeleteConfirmState);
    },
    [portfolio.handleDelete]
  );

  const runDeleteConfirm = useCallback(async () => {
    if (deleteConfirm.onConfirm) await deleteConfirm.onConfirm();
    setDeleteConfirm((p) => ({ ...p, open: false }));
  }, [deleteConfirm.onConfirm]);

  // DALTON-HOOK(extended-hours-quotes): Round sidebar prices and percentages with shared roundQuote2 / formatSignedPct2.
  const formatPrice = (price: number | null | undefined): string => {
    if (price == null || price === 0) return '--';
    return grouped2(roundQuote2(Number(price)), locale);
  };

  const formatChange = (val: number | null | undefined): string => {
    if (val == null) return '--';
    return formatSignedPct2(val, locale);
  };

  const changeClass = (isPositive: boolean, val: number | null | undefined): string => {
    if (val == null || val === 0) return 'market-sidebar-row-change--neutral';
    return isPositive ? 'market-sidebar-row-change--positive' : 'market-sidebar-row-change--negative';
  };

  const renderRows = (items: SidebarRow[], keyField: string, changeField: string, onDelete: (id: string) => void) => {
    return items.map((row) => {
      const isActive = activeSymbol && row.symbol === activeSymbol.toUpperCase();
      // DALTON-HOOK(extended-hours-quotes): Derive main quote and extended-hours figures from shared deriveQuoteDisplay.
      const wsTick = wsPrices.get(row.symbol) ?? null;
      const quoteDisplay = deriveQuoteDisplay(
        row,
        marketStatus as Parameters<typeof getExtendedHoursInfo>[0],
        wsTick,
      );
      const extType = quoteDisplay.session === 'regular' ? null : quoteDisplay.session;
      const mainPrice = quoteDisplay.mainPrice;
      const isWatchlistRow = changeField === 'changePercent';
      const displayChangePct = isWatchlistRow
        ? quoteDisplay.mainChangePct
        : (row[changeField] as number | null | undefined);
      const displayIsPositive =
        isWatchlistRow && extType
          ? (quoteDisplay.mainChange != null
              ? quoteDisplay.mainChange >= 0
              : displayChangePct != null
                ? displayChangePct >= 0
                : row.isPositive)
          : row.isPositive;
      const extDisplayPrice = quoteDisplay.extPrice;
      const extPct = quoteDisplay.extChangePct;
      return (
        <div
          key={row[keyField] as string}
          className={`market-sidebar-row${isActive ? ' market-sidebar-row--active' : ''}`}
          onClick={() => onSymbolClick?.(row.symbol)}
        >
          <span className="market-sidebar-row-symbol">{row.symbol}</span>
          {/* DALTON-HOOK(extended-hours-quotes): Render mainPrice, regular changePercent, and extDisplayPrice. */}
          <span className="market-sidebar-row-price">{formatPrice(mainPrice)}</span>
          <span className={`market-sidebar-row-change ${changeClass(displayIsPositive, displayChangePct)}`}>
            {formatChange(displayChangePct)}
            {extType && extDisplayPrice != null && extPct != null && (
              <span className="market-sidebar-row-ext" style={{ display: 'inline-flex', alignItems: 'center', gap: 2, color: extType === 'pre' ? EXT_COLOR_PRE : EXT_COLOR_POST }}>
                {extType === 'pre' ? <Sunrise size={10} /> : <Sunset size={10} />}
                {formatPrice(extDisplayPrice)} {formatSignedPct2(extPct, locale)}
              </span>
            )}
          </span>
          <span className="market-sidebar-row-actions">
            <button
              className="market-sidebar-row-delete"
              onClick={(e: React.MouseEvent) => { e.stopPropagation(); onDelete(row[keyField] as string); }}
              title={t('marketView.sidebar.remove')}
            >
              <X size={12} />
            </button>
          </span>
        </div>
      );
    });
  };

  const renderSkeletons = () =>
    Array.from({ length: 4 }).map((_, i) => (
      <div key={i} className="market-sidebar-skeleton">
        <div className="market-sidebar-skeleton-bar" style={{ width: 48 }} />
        <div className="market-sidebar-skeleton-bar" style={{ width: 54, marginLeft: 'auto' }} />
        <div className="market-sidebar-skeleton-bar" style={{ width: 52 }} />
      </div>
    ));

  const isWatchlist = activeTab === 'watchlist';
  const currentLoading = isWatchlist ? watchlist.loading : portfolio.loading;

  // Overlay WS live prices onto rows
  const currentRows = useMemo(() => {
    const rows = isWatchlist ? watchlist.rows : portfolio.rows;
    return rows.map((row) => {
      const ws = wsPrices.get(row.symbol);
      if (!ws) return row;
      // DALTON-HOOK(extended-hours-quotes): Stamp liveTickAt from WS PriceUpdate when overlaying sidebar rows.
      if (isWatchlist) {
        return {
          ...row,
          price: ws.price,
          change: ws.change,
          changePercent: ws.changePercent ?? row.changePercent,
          isPositive: ws.change >= 0,
          liveTickAt: ws.timestamp ?? row.liveTickAt ?? null,
        };
      }
      // Portfolio: only overlay price and direction — preserve unrealizedPlPercent
      return {
        ...row,
        price: ws.price,
        isPositive: ws.change >= 0,
        liveTickAt: ws.timestamp ?? row.liveTickAt ?? null,
      };
    });
  }, [isWatchlist, watchlist.rows, portfolio.rows, wsPrices]);

  // Collapsed state — thin toggle strip
  if (!effectiveExpanded) {
    return (
      <div className="market-sidebar market-sidebar--collapsed">
        <button
          className="market-sidebar-expand-btn"
          onClick={() => setExpanded(true)}
          title={t('marketView.sidebar.show')}
        >
          <BarChart3 size={16} />
          <ChevronLeft size={14} />
        </button>

        {/* Dialogs still need to be mounted for add operations */}
        <AddWatchlistItemDialog
          open={watchlist.modalOpen}
          onClose={() => watchlist.setModalOpen(false)}
          onAdd={watchlist.handleAdd as any}
          watchlistId={watchlist.currentWatchlistId ?? undefined}
        />
        <AddPortfolioHoldingDialog
          open={portfolio.modalOpen}
          onClose={() => portfolio.setModalOpen(false)}
          onAdd={portfolio.handleAdd as any}
        />
      </div>
    );
  }

  return (
    <div className="market-sidebar">
      <ConfirmDialog
        open={deleteConfirm.open}
        title={deleteConfirm.title}
        message={deleteConfirm.message}
        confirmLabel={t('common.delete')}
        onConfirm={runDeleteConfirm}
        onOpenChange={(open: boolean) => !open && setDeleteConfirm((p) => ({ ...p, open: false }))}
      />

      {/* Tab toggle */}
      <div className="market-sidebar-tabs">
        <button
          className={`market-sidebar-tab${activeTab === 'watchlist' ? ' market-sidebar-tab--active' : ''}`}
          onClick={() => setActiveTab('watchlist')}
        >
          {t('marketView.header.watchlist')}
        </button>
        <button
          className={`market-sidebar-tab${activeTab === 'portfolio' ? ' market-sidebar-tab--active' : ''}`}
          onClick={() => setActiveTab('portfolio')}
        >
          {t('marketView.sidebar.portfolio')}
        </button>
        {!isMobile && (
          <button
            className="market-sidebar-collapse-btn"
            onClick={() => setExpanded(false)}
            title={t('marketView.sidebar.collapse')}
          >
            <ChevronRight size={14} />
          </button>
        )}
      </div>

      {/* Section header */}
      <div className="market-sidebar-section-header">
        <span className="market-sidebar-section-title">
          {isWatchlist ? t('marketView.header.watchlist') : t('marketView.sidebar.portfolio')}
          {wsStatus === 'connected' && wsPrices.size > 0 && <span className="market-sidebar-live-dot" title={t('marketView.sidebar.livePrices')} />}
        </span>
        <button
          className="market-sidebar-add-btn"
          onClick={() =>
            isWatchlist
              ? watchlist.setModalOpen(true)
              : portfolio.setModalOpen(true)
          }
          title={isWatchlist ? t('marketView.sidebar.addToWatchlist') : t('marketView.sidebar.addHolding')}
        >
          +
        </button>
      </div>

      {/* List */}
      <div className="market-sidebar-list">
        {currentLoading
          ? renderSkeletons()
          : currentRows.length === 0
            ? (
              <div className="market-sidebar-empty">
                <div className="market-sidebar-empty-text">
                  {isWatchlist
                    ? t('marketView.sidebar.emptyWatchlist')
                    : t('marketView.sidebar.emptyPortfolio')}
                </div>
              </div>
            )
            : isWatchlist
              ? renderRows(currentRows as SidebarRow[], 'watchlist_item_id', 'changePercent', watchlist.handleDelete)
              : renderRows(currentRows as SidebarRow[], 'user_portfolio_id', 'unrealizedPlPercent', handlePortfolioDelete)}
      </div>

      {/* Footer */}
      {currentRows.length > 0 && !currentLoading && (
        <div className="market-sidebar-footer">
          <button
            className="market-sidebar-footer-link"
            onClick={() => navigate('/')}
          >
            {t('marketView.sidebar.viewAll')}
          </button>
        </div>
      )}

      {/* Dialogs */}
      <AddWatchlistItemDialog
        open={watchlist.modalOpen}
        onClose={() => watchlist.setModalOpen(false)}
        onAdd={watchlist.handleAdd as any}
        watchlistId={watchlist.currentWatchlistId ?? undefined}
      />
      <AddPortfolioHoldingDialog
        open={portfolio.modalOpen}
        onClose={() => portfolio.setModalOpen(false)}
        onAdd={portfolio.handleAdd as any}
      />
    </div>
  );
}

export default MarketSidebarPanel;
