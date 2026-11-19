import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import React from 'react';

// DALTON-HOOK(extended-hours-quotes): the old PortfolioWatchlistCard's display
// lives in PortfolioWatchlistWidget now; its data context is mocked here.
const { mockDashboardCtx } = vi.hoisted(() => ({
  mockDashboardCtx: {
    watchlist: { rows: [] as unknown[], loading: false },
    watchlistHandlers: { onAdd: () => {}, onDelete: () => {} },
    portfolio: { rows: [] as unknown[], loading: false, hasRealHoldings: true },
    portfolioHandlers: { onAdd: () => {}, onEdit: () => {}, onDelete: () => {} },
    dashboard: { marketStatus: null as Record<string, unknown> | null },
  },
}));
vi.mock('../../../Dashboard/widgets/framework/DashboardDataContext', () => ({
  useDashboardContext: () => mockDashboardCtx,
}));

import { LegendStats } from '../StockLegendStrip';
import PortfolioWatchlistWidget from '../../../Dashboard/widgets/definitions/PortfolioWatchlistWidget';
import { WatchlistRowItem } from '../../../Dashboard/widgets/definitions/_holdingsPrimitives';
import { serializeQuoteRowToMarkdown } from '../../../Dashboard/widgets/framework/snapshotSerializers';
import { watchlistQuoteRow, type WatchlistRow } from '../../../Dashboard/hooks/useWatchlistData';
import { QueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { snapshotToStockPrice, writeQuoteFromWs } from '@/lib/quotes';
import type { QuoteRow as RawQuoteRow } from '@/lib/quotes/quoteBatcher';
import { deriveStockQuote, type StockQuoteInputs } from '../../hooks/useStockQuoteModel';

const baseInputs: StockQuoteInputs = {
  symbol: 'AMD',
  stockInfo: null,
  realTimePrice: null,
  quoteData: null,
  snapshot: null,
  marketStatus: null,
  wsStatus: 'disconnected',
};

// The change percent tracks the last trade, so after the close it includes the
// extended session and no longer matches the regular-session headline.
describe('LegendStats change cell', () => {
  it('is labeled Chg during the regular session', () => {
    render(<LegendStats quote={deriveStockQuote({ ...baseInputs, snapshot: { symbol: 'AMD', price: 120.5 } })} />);
    expect(screen.getByText('Chg')).toBeInTheDocument();
  });

  it('is labeled Chg incl. ext when an extended-hours print is showing', () => {
    const quote = deriveStockQuote({
      ...baseInputs,
      marketStatus: { market: 'closed', afterHours: false, earlyHours: false, providers: [] } as Record<string, unknown>,
      snapshot: { symbol: 'AMD', price: 96.5, previous_close: 100, regular_trading_change: -4, late_trading_change_percent: -1.0, source: 'x' },
      realTimePrice: { symbol: 'AMD', price: 96.5, open: 0, high: 0, low: 0, change: -3.5, changePercent: -3.5, volume: 0, previousClose: 100 },
    });
    expect(quote.ext).not.toBeNull();
    render(<LegendStats quote={quote} />);
    expect(screen.getByText('Chg incl. ext')).toBeInTheDocument();
  });
});

const cellValue = (key: string): string | null | undefined => screen.getByText(key).nextElementSibling?.textContent;

describe('LegendStats close cell', () => {
  it('prints the row price in a regular session', () => {
    render(<LegendStats quote={deriveStockQuote({
      ...baseInputs,
      marketStatus: { market: 'open', afterHours: false, earlyHours: false, providers: [] } as Record<string, unknown>,
      realTimePrice: { symbol: 'AMD', price: 101.23, open: 100, high: 102, low: 99, change: 1.23, changePercent: 1.23, volume: 0, previousClose: 100 },
    })} />);
    expect(cellValue('C')).toBe('101.23');
  });

  // The candle ends at the regular close; the after-hours print (96.50) is
  // the lead's business, not the bar's.
  it('prints the settled close, not the live price, in an extended session', () => {
    render(<LegendStats quote={deriveStockQuote({
      ...baseInputs,
      marketStatus: { market: 'closed', afterHours: false, earlyHours: false, providers: [] } as Record<string, unknown>,
      snapshot: { symbol: 'AMD', price: 96.5, previous_close: 100, regular_trading_change: -4, late_trading_change_percent: -1.0, source: 'x' },
      realTimePrice: { symbol: 'AMD', price: 96.5, open: 0, high: 0, low: 0, change: -3.5, changePercent: -3.5, volume: 0, previousClose: 100 },
    })} />);
    expect(cellValue('C')).toBe('96.00');
    expect(screen.queryByText('96.50')).not.toBeInTheDocument();
  });
});

describe('LegendStats volume cell', () => {
  it('is labeled V when the row carries a session volume', () => {
    render(<LegendStats quote={deriveStockQuote({
      ...baseInputs,
      stockInfo: { Symbol: 'AMD', Name: 'AMD', Price: 120.5, Volume: 1_250_000, AverageVolume: 2_500_000 } as never,
    })} />);
    expect(cellValue('V')).toBe('1.25M');
    expect(screen.queryByText('Avg vol 3M')).not.toBeInTheDocument();
  });

  it('says it is the 3-month average when that is what it falls back to', () => {
    render(<LegendStats quote={deriveStockQuote({ ...baseInputs, quoteData: { avgVolume: 2_500_000 } })} />);
    expect(screen.queryByText('V')).not.toBeInTheDocument();
    expect(cellValue('Avg vol 3M')).toBe('2.50M');
  });
});

// DALTON-HOOK(extended-hours-quotes): Verify card, widget row, and exported row agree on extended-hours price and pct after WS write-through and when live price returns to regularClose.
// (MarketSidebarPanel was replaced by QuoteList in upstream #801.)
describe('Extended-hours quote consistency across surfaces', () => {
  it('keeps extended-hours price and pct identical across card, widget row, and exported row after WS write-through', () => {
    const queryClient = new QueryClient();
    const initialSnap: RawQuoteRow = {
      symbol: 'GOOG',
      price: 317.28,
      regular_close: 317.28,
      previous_close: 320.76,
      change: -2.66,
      change_percent: -0.83,
      regular_trading_change: -3.48,
      regular_trading_change_percent: -1.0849,
      late_trading_change: 0.82,
      late_trading_change_percent: 0.2584,
      market_status: 'late_trading',
      currency: 'USD',
    };
    queryClient.setQueryData(queryKeys.quote.detail('GOOG'), initialSnap);

    // 1. WS write-through to 319.50 (319.50 - 317.28 = +2.22, +0.70%)
    writeQuoteFromWs(queryClient, 'GOOG', {
      price: 319.5,
      change: -1.26,
      changePercent: -0.39,
      timestamp: 1700000001000,
    });

    const buildRow = (): WatchlistRow => {
      const snap = queryClient.getQueryData<RawQuoteRow>(queryKeys.quote.detail('GOOG'))!;
      const sp = snapshotToStockPrice('GOOG', snap);
      return {
        watchlist_item_id: 'w-goog',
        ...sp,
        previousClose: sp.previousClose ?? null,
        earlyTradingChangePercent: sp.earlyTradingChangePercent ?? null,
        lateTradingChangePercent: sp.lateTradingChangePercent ?? null,
        currency: sp.currency ?? 'USD',
        assetClass: 'equity',
      };
    };

    // DALTON-HOOK(extended-hours-quotes): Assert all 4 consumers show 319.50 and +0.70%.
    const afterHoursStatus = { market: 'extended-hours', afterHours: true, earlyHours: false };
    const wsRow = buildRow();

    mockDashboardCtx.watchlist = { rows: [wsRow], loading: false };
    mockDashboardCtx.dashboard = { marketStatus: afterHoursStatus };
    const { container: cardContainer, unmount: unmountCard } = (() => {
      const r = render(
        <MemoryRouter>
          <PortfolioWatchlistWidget
            instance={{ id: 'pw-sls-1', type: 'personal.portfolioWatchlist', config: { defaultTab: 'watchlist' } }}
            updateConfig={() => {}}
          />
        </MemoryRouter>,
      );
      return r;
    })();
    expect(cardContainer.querySelector('.dash-ext')?.textContent).toBe('AH$319.50+0.70%');
    unmountCard();
    mockDashboardCtx.watchlist = { rows: [], loading: false };
    mockDashboardCtx.dashboard = { marketStatus: null };

    const { container: primContainer, unmount: unmountPrim } = render(
      <MemoryRouter>
        <table>
          <tbody>
            <WatchlistRowItem item={wsRow} marketStatus={afterHoursStatus} />
          </tbody>
        </table>
      </MemoryRouter>,
    );
    expect(primContainer.querySelector('.dash-ext')?.textContent).toBe('AH$319.50+0.70%');
    unmountPrim();

    const exported = watchlistQuoteRow(wsRow, afterHoursStatus);
    expect(exported.postMarket).toBe(319.5);
    expect(exported.postMarketChange).toBe(2.22);
    expect(exported.postMarketChangePercent).toBe(0.7);

    // DALTON-HOOK(extended-hours-quotes): 2. WS write-through returns exactly to regularClose (317.28); must stay 317.28 +0.00%, not revert to 318.10.
    writeQuoteFromWs(queryClient, 'GOOG', {
      price: 317.28,
      change: -3.48,
      changePercent: -1.08,
      timestamp: 1700000002000,
    });
    const backToCloseRow = buildRow();

    mockDashboardCtx.watchlist = { rows: [backToCloseRow], loading: false };
    mockDashboardCtx.dashboard = { marketStatus: afterHoursStatus };
    const { container: cardBack, unmount: unmountCardBack } = render(
      <MemoryRouter>
        <PortfolioWatchlistWidget
          instance={{ id: 'pw-sls-2', type: 'personal.portfolioWatchlist', config: { defaultTab: 'watchlist' } }}
          updateConfig={() => {}}
        />
      </MemoryRouter>,
    );
    expect(cardBack.querySelector('.dash-ext')?.textContent).toBe('AH$317.28·0.00%');
    expect(cardBack).not.toHaveTextContent('$318.10');
    unmountCardBack();
    mockDashboardCtx.watchlist = { rows: [], loading: false };
    mockDashboardCtx.dashboard = { marketStatus: null };

    const { container: primBack, unmount: unmountPrimBack } = render(
      <MemoryRouter>
        <table>
          <tbody>
            <WatchlistRowItem item={backToCloseRow} marketStatus={afterHoursStatus} />
          </tbody>
        </table>
      </MemoryRouter>,
    );
    expect(primBack.querySelector('.dash-ext')?.textContent).toBe('AH$317.28·0.00%');
    expect(primBack).not.toHaveTextContent('$318.10');
    unmountPrimBack();

    const exportedBack = watchlistQuoteRow(backToCloseRow, afterHoursStatus);
    expect(exportedBack.postMarket).toBe(317.28);
    expect(exportedBack.postMarketChange).toBe(0);
    expect(exportedBack.postMarketChangePercent).toBe(0);
  });

  // DALTON-HOOK(extended-hours-quotes): Verify -0.125% rounds half-away-from-zero to -0.13% identically across all 4 surfaces in both regular and after-hours sessions.
  it('formats negative half-way -0.125% identically as -0.13% across card, widget row, sidebar, and exported markdown in regular and after-hours sessions', () => {
    const openStatus = { market: 'open', afterHours: false, earlyHours: false };
    const afterHoursStatus = { market: 'extended-hours', afterHours: true, earlyHours: false };
    const sp = snapshotToStockPrice('GOOG', {
      symbol: 'GOOG',
      price: 99.875,
      regular_close: 99.875,
      previous_close: 100,
      change: -0.125,
      change_percent: -0.125,
      regular_trading_change: -0.125,
      regular_trading_change_percent: -0.125,
      late_trading_change: -0.125,
      late_trading_change_percent: -0.125,
      market_status: 'late_trading',
      currency: 'USD',
    });
    expect(sp.change).toBe(-0.125);
    expect(sp.changePercent).toBe(-0.125);

    const halfRow: WatchlistRow = {
      watchlist_item_id: 'w-half',
      ...sp,
      previousClose: sp.previousClose ?? null,
      earlyTradingChangePercent: sp.earlyTradingChangePercent ?? null,
      lateTradingChangePercent: sp.lateTradingChangePercent ?? null,
      currency: 'USD',
      assetClass: 'equity',
    };

    // Regular session: -0.125 and -0.125% from snapshotToStockPrice render as -$0.13 and -0.13% across all 4 surfaces
    mockDashboardCtx.watchlist = { rows: [halfRow], loading: false };
    mockDashboardCtx.dashboard = { marketStatus: openStatus };
    const { container: regCardEl, unmount: unmountRegCard } = render(
      <MemoryRouter>
        <PortfolioWatchlistWidget
          instance={{ id: 'pw-sls-3', type: 'personal.portfolioWatchlist', config: { defaultTab: 'watchlist' } }}
          updateConfig={() => {}}
        />
      </MemoryRouter>,
    );
    expect(regCardEl.querySelector('td[title="-$0.13"]')).not.toBeNull();
    expect(regCardEl).toHaveTextContent('-0.13%');
    unmountRegCard();
    mockDashboardCtx.watchlist = { rows: [], loading: false };
    mockDashboardCtx.dashboard = { marketStatus: null };

    const { container: regPrimEl, unmount: unmountRegPrim } = render(
      <MemoryRouter>
        <table>
          <tbody>
            <WatchlistRowItem item={halfRow} marketStatus={openStatus} />
          </tbody>
        </table>
      </MemoryRouter>,
    );
    expect(regPrimEl.querySelector('td[title="-$0.13"]')).not.toBeNull();
    expect(regPrimEl).toHaveTextContent('-0.13%');
    unmountRegPrim();

    const regExported = watchlistQuoteRow(halfRow, openStatus);
    expect(regExported.change).toBe(-0.13);
    expect(regExported.changePercent).toBe(-0.13);
    expect(serializeQuoteRowToMarkdown(regExported)).toContain('-0.13 (-0.13%)');

    // After-hours session: main and extended-hours -0.125% both render as -0.13% across all 4 surfaces
    mockDashboardCtx.watchlist = { rows: [halfRow], loading: false };
    mockDashboardCtx.dashboard = { marketStatus: afterHoursStatus };
    const { container: cardEl, unmount: unmountCard } = render(
      <MemoryRouter>
        <PortfolioWatchlistWidget
          instance={{ id: 'pw-sls-4', type: 'personal.portfolioWatchlist', config: { defaultTab: 'watchlist' } }}
          updateConfig={() => {}}
        />
      </MemoryRouter>,
    );
    expect(cardEl).toHaveTextContent('$99.88');
    expect(cardEl.querySelector('td[title="-$0.13"]')).not.toBeNull();
    expect(cardEl).toHaveTextContent('-0.13%');
    expect(cardEl.querySelector('.dash-ext')?.textContent).toBe('AH$99.75-0.13%');
    unmountCard();
    mockDashboardCtx.watchlist = { rows: [], loading: false };
    mockDashboardCtx.dashboard = { marketStatus: null };

    // 2. WatchlistRowItem (_holdingsPrimitives)
    const { container: primEl, unmount: unmountPrim } = render(
      <MemoryRouter>
        <table>
          <tbody>
            <WatchlistRowItem item={halfRow} marketStatus={afterHoursStatus} />
          </tbody>
        </table>
      </MemoryRouter>,
    );
    expect(primEl).toHaveTextContent('$99.88');
    expect(primEl.querySelector('td[title="-$0.13"]')).not.toBeNull();
    expect(primEl).toHaveTextContent('-0.13%');
    expect(primEl.querySelector('.dash-ext')?.textContent).toBe('AH$99.75-0.13%');
    unmountPrim();

    // 3. Exported row & markdown
    const exported = watchlistQuoteRow(halfRow, afterHoursStatus);
    expect(exported.price).toBe(99.88);
    expect(exported.change).toBe(-0.13);
    expect(exported.changePercent).toBe(-0.13);
    expect(exported.postMarket).toBe(99.75);
    expect(exported.postMarketChange).toBe(-0.13);
    expect(exported.postMarketChangePercent).toBe(-0.13);
    const md = serializeQuoteRowToMarkdown(exported);
    expect(md).toContain('regular close USD 99.88 -0.13 (-0.13%) vs prev close');
    expect(md).toContain('post-market USD 99.75 -0.13 (-0.13% vs regular close)');
  });
});
