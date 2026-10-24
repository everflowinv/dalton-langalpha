/**
 * A watchlist mixes venues and indexes, so each row prices itself: in the
 * currency its quote came in, and an index level with no currency at all.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';

// DALTON-HOOK(extended-hours-quotes): Mock DashboardDataContext for WatchlistWidget snapshot export test.
const { mockDashboardCtx } = vi.hoisted(() => ({
  mockDashboardCtx: {
    watchlist: { rows: [] as WatchlistRow[], loading: false },
    watchlistHandlers: { onAdd: () => {}, onDelete: () => {} },
    portfolio: { rows: [] as PortfolioRow[], loading: false, hasRealHoldings: true },
    portfolioHandlers: { onAdd: () => {}, onEdit: () => {}, onDelete: () => {} },
    dashboard: { marketStatus: null as Record<string, unknown> | null },
  },
}));
vi.mock('../../framework/DashboardDataContext', () => ({
  useDashboardContext: () => mockDashboardCtx,
}));

import { WatchlistRowItem, PortfolioRowItem } from '../_holdingsPrimitives';
import WatchlistWidget from '../WatchlistWidget';
import PortfolioWidget, { rowToQuote } from '../PortfolioWidget';
import PortfolioWatchlistWidget, { portfolioRowToQuote } from '../PortfolioWatchlistWidget';
import PortfolioWatchlistCard from '../../../components/PortfolioWatchlistCard'; // DALTON-HOOK(extended-hours-quotes)
import { QueryClient } from '@tanstack/react-query'; // DALTON-HOOK(extended-hours-quotes)
import { queryKeys } from '@/lib/queryKeys';
import { snapshotToStockPrice } from '@/lib/quotes/quoteAdapters';
import { writeQuoteFromWs } from '@/lib/quotes/quoteWriteThrough';
import type { QuoteRow as RawQuoteRow } from '@/lib/quotes/quoteBatcher';
import {
  __resetWidgetContextRegistryForTests,
  getWidgetContextSnapshot,
} from '../../framework/contextSnapshot';
import { watchlistQuoteRow, type WatchlistRow } from '../../../hooks/useWatchlistData';
import type { PortfolioRow } from '../../../hooks/usePortfolioData';

function row(over: Partial<WatchlistRow>): WatchlistRow {
  return {
    watchlist_item_id: 'w1', symbol: 'ACME', price: 100, change: 1, changePercent: 1,
    isPositive: true, quoteAvailable: true, previousClose: 99,
    earlyTradingChangePercent: null, lateTradingChangePercent: null,
    currency: null, assetClass: null,
    ...over,
  };
}

function renderRow(item: WatchlistRow) {
  return render(
    <MemoryRouter>
      <WatchlistRowItem item={item} index={0} marketStatus={null} />
    </MemoryRouter>,
  );
}

describe('WatchlistRowItem price', () => {
  it('prints an index level bare, whatever currency its row names', () => {
    renderRow(row({ symbol: '^GSPC', price: 5000, currency: 'USD', assetClass: 'index' }));
    expect(screen.getByText('5,000.00')).toBeInTheDocument();
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
  });

  it('reads an index from its code when the row carries no asset class', () => {
    renderRow(row({ symbol: '000300.SH', price: 4000, currency: 'CNY' }));
    expect(screen.getByText('4,000.00')).toBeInTheDocument();
    expect(screen.queryByText(/¥/)).not.toBeInTheDocument();
  });

  it('prices a listing in the currency its quote came in', () => {
    renderRow(row({ symbol: '600519.SH', price: 1500, currency: 'CNY', assetClass: 'stock' }));
    expect(screen.getByText('CN¥1,500.00')).toBeInTheDocument();
  });

  // DALTON-HOOK(extended-hours-quotes): Lock GOOG after-hours, pre-market, and regular-hours display on both watchlist components.
  it('renders GOOG after-hours card with regular close 317.28, badge -1.08%, and after-hours 318.10', () => {
    const googSnap = {
      symbol: 'GOOG',
      price: 317.28,
      regular_close: 317.28,
      previous_close: 320.76,
      change: -2.66,
      change_percent: -0.83,
      regular_trading_change: -3.48,
      regular_trading_change_percent: -1.0849,
      late_trading_change_percent: 0.2584,
      market_status: 'late_trading',
      currency: 'USD',
    };
    const stockPrice = snapshotToStockPrice('GOOG', googSnap);
    const watchlistItem: WatchlistRow = {
      watchlist_item_id: 'w-goog',
      ...stockPrice,
      previousClose: stockPrice.previousClose ?? null,
      earlyTradingChangePercent: stockPrice.earlyTradingChangePercent ?? null,
      lateTradingChangePercent: stockPrice.lateTradingChangePercent ?? null,
      currency: stockPrice.currency ?? 'USD',
      assetClass: 'equity',
    };
    const afterHoursStatus = { market: 'extended-hours', afterHours: true, earlyHours: false };

    const { unmount } = render(
      <MemoryRouter>
        <PortfolioWatchlistCard
          watchlistRows={[watchlistItem]}
          marketStatus={afterHoursStatus}
        />
      </MemoryRouter>,
    );

    // DALTON-HOOK(extended-hours-quotes): Assert PortfolioWatchlistCard after-hours values.
    const cardRow = screen.getByTestId('watchlist-row-GOOG');
    expect(cardRow).toHaveTextContent('$317.28');
    expect(cardRow).not.toHaveTextContent('$320.76');
    expect(cardRow).toHaveTextContent('-$3.48');
    expect(cardRow).toHaveTextContent('-1.08%');
    expect(cardRow).toHaveTextContent('$318.10 +0.26%');
    unmount();

    const { container } = render(
      <MemoryRouter>
        <WatchlistRowItem item={watchlistItem} index={0} marketStatus={afterHoursStatus} />
      </MemoryRouter>,
    );
    expect(container).toHaveTextContent('$317.28');
    expect(container).not.toHaveTextContent('$320.76');
    expect(container).toHaveTextContent('-$3.48');
    expect(container).toHaveTextContent('-1.08%');
    expect(container).toHaveTextContent('$318.10 +0.26%');
  });

  // DALTON-HOOK(extended-hours-quotes): Lock GOOG pre-market display on PortfolioWatchlistCard and WatchlistRowItem.
  it('renders GOOG pre-market card with previous close 320.76, change -3.46, badge -1.07%, and pre-market 322.26', () => {
    const googPreSnap = {
      symbol: 'GOOG',
      price: 320.76,
      previous_close: 320.76,
      regular_close: 320.76,
      change: 1.5,
      change_percent: 0.4676,
      regular_trading_change: -3.46,
      regular_trading_change_percent: -1.0672,
      early_trading_change: 1.5,
      early_trading_change_percent: 0.4676,
      market_status: 'early_trading',
      currency: 'USD',
    };
    const stockPrice = snapshotToStockPrice('GOOG', googPreSnap);
    const watchlistItem: WatchlistRow = {
      watchlist_item_id: 'w-goog-pre',
      ...stockPrice,
      previousClose: stockPrice.previousClose ?? null,
      earlyTradingChangePercent: stockPrice.earlyTradingChangePercent ?? null,
      lateTradingChangePercent: stockPrice.lateTradingChangePercent ?? null,
      currency: stockPrice.currency ?? 'USD',
      assetClass: 'equity',
    };
    const preMarketStatus = { market: 'extended-hours', afterHours: false, earlyHours: true };

    // DALTON-HOOK(extended-hours-quotes): Assert PortfolioWatchlistCard pre-market values.
    const { unmount } = render(
      <MemoryRouter>
        <PortfolioWatchlistCard
          watchlistRows={[watchlistItem]}
          marketStatus={preMarketStatus}
        />
      </MemoryRouter>,
    );

    const cardRow = screen.getByTestId('watchlist-row-GOOG');
    expect(cardRow).toHaveTextContent('$320.76');
    expect(cardRow).toHaveTextContent('-$3.46');
    expect(cardRow).toHaveTextContent('-1.07%');
    expect(cardRow).toHaveTextContent('$322.26 +0.47%');
    unmount();

    const { container } = render(
      <MemoryRouter>
        <WatchlistRowItem item={watchlistItem} index={0} marketStatus={preMarketStatus} />
      </MemoryRouter>,
    );
    expect(container).toHaveTextContent('$320.76');
    expect(container).toHaveTextContent('-$3.46');
    expect(container).toHaveTextContent('-1.07%');
    expect(container).toHaveTextContent('$322.26 +0.47%');
  });

  // DALTON-HOOK(extended-hours-quotes): Regular-session display unchanged.
  it('keeps regular-session display unchanged on PortfolioWatchlistCard and WatchlistRowItem', () => {
    const regularItem: WatchlistRow = row({
      symbol: 'GOOG',
      price: 317.28,
      change: -3.48,
      changePercent: -1.08,
      isPositive: false,
      previousClose: 320.76,
      regularClose: 317.28,
      regularTradingChange: -3.48,
      regularTradingChangePercent: -1.08,
      lateTradingChangePercent: 0.2584,
      currency: 'USD',
    });
    const openStatus = { market: 'open', afterHours: false, earlyHours: false };

    const { unmount } = render(
      <MemoryRouter>
        <PortfolioWatchlistCard
          watchlistRows={[regularItem]}
          marketStatus={openStatus}
        />
      </MemoryRouter>,
    );

    // DALTON-HOOK(extended-hours-quotes): Assert regular session card values.
    const cardRow = screen.getByTestId('watchlist-row-GOOG');
    expect(cardRow).toHaveTextContent('$317.28');
    expect(cardRow).toHaveTextContent('-$3.48');
    expect(cardRow).toHaveTextContent('-1.08%');
    expect(cardRow).not.toHaveTextContent('$318.10');
    unmount();

    const { container } = render(
      <MemoryRouter>
        <WatchlistRowItem item={regularItem} index={0} marketStatus={openStatus} />
      </MemoryRouter>,
    );
    expect(container).toHaveTextContent('$317.28');
    expect(container).toHaveTextContent('-$3.48');
    expect(container).toHaveTextContent('-1.08%');
    expect(container).not.toHaveTextContent('$318.10');
  });

  // DALTON-HOOK(extended-hours-quotes): Lock portfolio row pre-market main price and extended-hours price.
  it('renders portfolio row in pre-market with main price 320.76 and pre-market 322.26', () => {
    const portfolioItem: PortfolioRow = {
      user_portfolio_id: 'p-goog',
      symbol: 'GOOG',
      price: 320.76,
      previousClose: 320.76,
      regularClose: 320.76,
      earlyTradingChange: 1.5,
      earlyTradingChangePercent: 0.4676,
      lateTradingChangePercent: null,
      quantity: 10,
      average_cost: 300,
      currency: 'USD',
      marketValue: 3207.6,
      unrealizedPl: 207.6,
      unrealizedPlPercent: 6.92,
      isPositive: true,
      quoteAvailable: true,
    };
    const preMarketStatus = { market: 'extended-hours', afterHours: false, earlyHours: true };

    const { container } = render(
      <MemoryRouter>
        <PortfolioRowItem
          item={portfolioItem}
          index={0}
          marketStatus={preMarketStatus}
          valuesHidden={false}
        />
      </MemoryRouter>,
    );
    expect(container).toHaveTextContent('$320.76');
    expect(container).toHaveTextContent('+6.92%');
    expect(container).toHaveTextContent('$322.26 +0.47%');
  });

  // DALTON-HOOK(extended-hours-quotes): Lock agent watchlistQuoteRow, markdown, and WatchlistWidget row label across after-hours, pre-market, and regular sessions.
  it('exports regular-session change and changePercent in watchlistQuoteRow and WatchlistWidget row snapshot', async () => {
    __resetWidgetContextRegistryForTests();
    const afterHoursStatus = { market: 'extended-hours', afterHours: true, earlyHours: false };
    const afterHoursRow: WatchlistRow = row({
      watchlist_item_id: 'w-goog',
      symbol: 'GOOG',
      price: 317.28,
      change: -2.66,
      changePercent: -0.83,
      previousClose: 320.76,
      regularClose: 317.28,
      regularTradingChange: -3.48,
      regularTradingChangePercent: -1.0849,
      lateTradingChange: 0.82,
      lateTradingChangePercent: 0.2584,
      currency: 'USD',
      assetClass: 'equity',
    });

    // DALTON-HOOK(extended-hours-quotes): Assert watchlistQuoteRow and WatchlistWidget after-hours snapshot label and markdown.
    expect(watchlistQuoteRow(afterHoursRow, afterHoursStatus)).toEqual({
      symbol: 'GOOG',
      session: 'post',
      price: 317.28,
      change: -3.48,
      changePercent: -1.08,
      postMarket: 318.1,
      postMarketChange: 0.82,
      postMarketChangePercent: 0.26,
      currency: 'USD',
    });

    mockDashboardCtx.watchlist = { rows: [afterHoursRow], loading: false };
    mockDashboardCtx.dashboard = { marketStatus: afterHoursStatus };

    const { unmount } = render(
      <MemoryRouter>
        <WatchlistWidget
          instance={{ id: 'wl-test', type: 'watchlist.list', config: {} }}
          updateConfig={() => {}}
        />
      </MemoryRouter>,
    );

    // DALTON-HOOK(extended-hours-quotes): Verify after-hours label, data, and markdown identity strings.
    const rowSnap = await getWidgetContextSnapshot('wl-test', 'w-goog');
    expect(rowSnap?.label).toBe('GOOG · -1.08%');
    expect(rowSnap?.data).toEqual({
      row: {
        symbol: 'GOOG',
        session: 'post',
        price: 317.28,
        change: -3.48,
        changePercent: -1.08,
        postMarket: 318.1,
        postMarketChange: 0.82,
        postMarketChangePercent: 0.26,
        currency: 'USD',
      },
    });
    expect(rowSnap?.text).toContain(
      [
        '**GOOG**',
        'regular close USD 317.28 -3.48 (-1.08%) vs prev close',
        'post-market USD 318.10 +0.82 (+0.26% vs regular close)',
      ].join('\n'),
    );
    unmount();
  });

  // DALTON-HOOK(extended-hours-quotes): Lock pre-market and regular-session WatchlistWidget row snapshot labels and markdown.
  it('exports pre-market and regular-session WatchlistWidget row snapshot labels and markdown', async () => {
    __resetWidgetContextRegistryForTests();
    const preMarketStatus = { market: 'extended-hours', afterHours: false, earlyHours: true };
    const preMarketRow: WatchlistRow = row({
      watchlist_item_id: 'w-goog-pre',
      symbol: 'GOOG',
      price: 320.76,
      change: 1.5,
      changePercent: 0.4676,
      previousClose: 320.76,
      regularClose: 320.76,
      regularTradingChange: -3.46,
      regularTradingChangePercent: -1.0672,
      earlyTradingChange: 1.5,
      earlyTradingChangePercent: 0.4676,
      currency: 'USD',
      assetClass: 'equity',
    });

    // DALTON-HOOK(extended-hours-quotes): Verify pre-market row snapshot label and markdown.
    mockDashboardCtx.watchlist = { rows: [preMarketRow], loading: false };
    mockDashboardCtx.dashboard = { marketStatus: preMarketStatus };

    const { unmount: unmountPre } = render(
      <MemoryRouter>
        <WatchlistWidget
          instance={{ id: 'wl-pre', type: 'watchlist.list', config: {} }}
          updateConfig={() => {}}
        />
      </MemoryRouter>,
    );

    const preSnap = await getWidgetContextSnapshot('wl-pre', 'w-goog-pre');
    expect(preSnap?.label).toBe('GOOG · -1.07%');
    expect(preSnap?.text).toContain(
      [
        '**GOOG**',
        'prev close USD 320.76 -3.46 (-1.07% prior session)',
        'pre-market USD 322.26 +1.50 (+0.47% vs prev close)',
      ].join('\n'),
    );
    unmountPre();

    // DALTON-HOOK(extended-hours-quotes): Verify regular-session row snapshot label.
    const openStatus = { market: 'open', afterHours: false, earlyHours: false };
    const regularRow: WatchlistRow = row({
      watchlist_item_id: 'w-goog-reg',
      symbol: 'GOOG',
      price: 317.28,
      change: -3.48,
      changePercent: -1.08,
      previousClose: 320.76,
      regularClose: 317.28,
      regularTradingChange: -3.48,
      regularTradingChangePercent: -1.0849,
      currency: 'USD',
      assetClass: 'equity',
    });

    mockDashboardCtx.watchlist = { rows: [regularRow], loading: false };
    mockDashboardCtx.dashboard = { marketStatus: openStatus };

    const { unmount: unmountReg } = render(
      <MemoryRouter>
        <WatchlistWidget
          instance={{ id: 'wl-reg', type: 'watchlist.list', config: {} }}
          updateConfig={() => {}}
        />
      </MemoryRouter>,
    );

    // DALTON-HOOK(extended-hours-quotes): Assert regular-session snapshot label and markdown.
    const regSnap = await getWidgetContextSnapshot('wl-reg', 'w-goog-reg');
    expect(regSnap?.label).toBe('GOOG · -1.08%');
    expect(regSnap?.text).toContain(['**GOOG**', 'USD 317.28 -3.48 (-1.08%)'].join('\n'));
    unmountReg();
  });

  // DALTON-HOOK(extended-hours-quotes): Regression for WebSocket write-through in regular session and after-hours.
  it('propagates WebSocket write-through prices into exported quote rows in regular session and after-hours', async () => {
    __resetWidgetContextRegistryForTests();
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
      currency: 'USD',
    };
    queryClient.setQueryData(queryKeys.quote.detail('GOOG'), initialSnap);

    // DALTON-HOOK(extended-hours-quotes): Simulate WS write-through during regular trading hours.
    writeQuoteFromWs(queryClient, 'GOOG', {
      price: 325.0,
      change: 4.24,
      changePercent: 1.32,
    });
    const wsOpenSnap = queryClient.getQueryData<RawQuoteRow>(queryKeys.quote.detail('GOOG'))!;
    const openStockPrice = snapshotToStockPrice('GOOG', wsOpenSnap);
    const openRow: WatchlistRow = {
      watchlist_item_id: 'w-goog-open-ws',
      ...openStockPrice,
      previousClose: openStockPrice.previousClose ?? null,
      earlyTradingChangePercent: openStockPrice.earlyTradingChangePercent ?? null,
      lateTradingChangePercent: openStockPrice.lateTradingChangePercent ?? null,
      currency: openStockPrice.currency ?? 'USD',
      assetClass: 'equity',
    };
    const openStatus = { market: 'open', afterHours: false, earlyHours: false };

    // DALTON-HOOK(extended-hours-quotes): In regular session, exported price/change/changePercent are the live WS values.
    expect(watchlistQuoteRow(openRow, openStatus)).toEqual({
      symbol: 'GOOG',
      price: 325.0,
      change: 4.24,
      changePercent: 1.32,
      currency: 'USD',
    });

    mockDashboardCtx.watchlist = { rows: [openRow], loading: false };
    mockDashboardCtx.dashboard = { marketStatus: openStatus };
    const { unmount } = render(
      <MemoryRouter>
        <WatchlistWidget
          instance={{ id: 'wl-open-ws', type: 'watchlist.list', config: {} }}
          updateConfig={() => {}}
        />
      </MemoryRouter>,
    );
    const openRowSnap = await getWidgetContextSnapshot('wl-open-ws', 'w-goog-open-ws');
    expect(openRowSnap?.label).toBe('GOOG · +1.32%');
    unmount();

    // DALTON-HOOK(extended-hours-quotes): Simulate WS write-through during after-hours; price stays regularClose while postMarket uses live WS price.
    writeQuoteFromWs(queryClient, 'GOOG', {
      price: 319.5,
      change: -1.26,
      changePercent: -0.39,
    });
    const wsPostSnap = queryClient.getQueryData<RawQuoteRow>(queryKeys.quote.detail('GOOG'))!;
    const postStockPrice = snapshotToStockPrice('GOOG', wsPostSnap);
    const postRow: WatchlistRow = {
      watchlist_item_id: 'w-goog-post-ws',
      ...postStockPrice,
      previousClose: postStockPrice.previousClose ?? null,
      earlyTradingChangePercent: postStockPrice.earlyTradingChangePercent ?? null,
      lateTradingChangePercent: postStockPrice.lateTradingChangePercent ?? null,
      currency: postStockPrice.currency ?? 'USD',
      assetClass: 'equity',
    };
    const afterHoursStatus = { market: 'extended-hours', afterHours: true, earlyHours: false };

    // DALTON-HOOK(extended-hours-quotes): Assert after-hours WS write-through keeps regular close 317.28 and updates postMarket to 319.5.
    expect(watchlistQuoteRow(postRow, afterHoursStatus)).toEqual({
      symbol: 'GOOG',
      session: 'post',
      price: 317.28,
      change: -3.48,
      changePercent: -1.08,
      postMarket: 319.5,
      postMarketChange: 2.22,
      postMarketChangePercent: 0.7,
      currency: 'USD',
    });
  });

  // DALTON-HOOK(extended-hours-quotes): Omit extended-hours subline when after-hours regularClose is missing (even if previousClose is present) and export portfolio holdings with separate quote move vs unrealized P/L.
  it('omits extended-hours subline when after-hours regularClose is missing and exports portfolio holdings with separate quote move and unrealized P/L', async () => {
    __resetWidgetContextRegistryForTests();
    const afterHoursStatus = { market: 'extended-hours', afterHours: true, earlyHours: false };

    // Missing regularClose (and regularTradingChange), even with previousClose: 320.76 and lateTradingChangePercent: 0.2584 -> must not show $321.59 or $0.00
    const missingRegCloseWatchlist: WatchlistRow = row({
      watchlist_item_id: 'w-no-reg-close',
      symbol: 'GOOG',
      price: 318.1,
      change: -2.66,
      changePercent: -0.83,
      previousClose: 320.76,
      regularClose: null,
      regularTradingChange: null,
      lateTradingChangePercent: 0.2584,
      currency: 'USD',
    });
    const missingRegClosePortfolio: PortfolioRow = {
      user_portfolio_id: 'p-no-reg-close',
      symbol: 'GOOG',
      price: 318.1,
      previousClose: 320.76,
      regularClose: null,
      regularTradingChange: null,
      lateTradingChangePercent: 0.2584,
      quantity: 10,
      average_cost: 300,
      currency: 'USD',
      marketValue: 3181,
      unrealizedPl: 181,
      unrealizedPlPercent: 6.03,
      isPositive: true,
      quoteAvailable: true,
    };

    const { container: cardEl, unmount: unmountCard } = render(
      <MemoryRouter>
        <PortfolioWatchlistCard
          watchlistRows={[missingRegCloseWatchlist]}
          portfolioRows={[missingRegClosePortfolio]}
          marketStatus={afterHoursStatus}
        />
      </MemoryRouter>,
    );
    expect(cardEl).not.toHaveTextContent('$321.59');
    expect(cardEl).not.toHaveTextContent('$0.00');
    expect(cardEl).not.toHaveTextContent('+0.26%');
    unmountCard();

    const { container: primWlEl, unmount: unmountPrimWl } = render(
      <MemoryRouter>
        <WatchlistRowItem item={missingRegCloseWatchlist} index={0} marketStatus={afterHoursStatus} />
      </MemoryRouter>,
    );
    expect(primWlEl).not.toHaveTextContent('$321.59');
    expect(primWlEl).not.toHaveTextContent('$0.00');
    expect(primWlEl).not.toHaveTextContent('+0.26%');
    unmountPrimWl();

    const { container: primPfEl, unmount: unmountPrimPf } = render(
      <MemoryRouter>
        <PortfolioRowItem
          item={missingRegClosePortfolio}
          index={0}
          marketStatus={afterHoursStatus}
          valuesHidden={false}
        />
      </MemoryRouter>,
    );
    expect(primPfEl).not.toHaveTextContent('$321.59');
    expect(primPfEl).not.toHaveTextContent('$0.00');
    expect(primPfEl).not.toHaveTextContent('+0.26%');
    unmountPrimPf();

    // DALTON-HOOK(extended-hours-quotes): Verify portfolioRowToQuote and rowToQuote separate session move (-3.48 / -1.08%) from holding unrealizedPlPercent (+5.76%).
    const holdingPostRow: PortfolioRow = {
      user_portfolio_id: 'p-goog-post',
      symbol: 'GOOG',
      price: 317.28,
      change: -2.66,
      changePercent: -0.83,
      previousClose: 320.76,
      regularClose: 317.28,
      regularTradingChange: -3.48,
      regularTradingChangePercent: -1.0849,
      lateTradingChange: 0.82,
      lateTradingChangePercent: 0.2584,
      earlyTradingChangePercent: null,
      quantity: 10,
      average_cost: 300,
      currency: 'USD',
      marketValue: 3172.8,
      unrealizedPl: 172.8,
      unrealizedPlPercent: 5.76,
      isPositive: true,
      quoteAvailable: true,
    };

    const expectedHoldingQuote = {
      symbol: 'GOOG',
      session: 'post',
      price: 317.28,
      change: -3.48,
      changePercent: -1.08,
      shares: 10,
      marketValue: 3172.8,
      unrealizedPlPercent: 5.76,
      postMarket: 318.1,
      postMarketChange: 0.82,
      postMarketChangePercent: 0.26,
      currency: 'USD',
    };
    expect(portfolioRowToQuote(holdingPostRow, afterHoursStatus)).toEqual(expectedHoldingQuote);
    expect(rowToQuote(holdingPostRow, afterHoursStatus)).toEqual(expectedHoldingQuote);

    mockDashboardCtx.portfolio = { rows: [holdingPostRow], loading: false, hasRealHoldings: true };
    mockDashboardCtx.dashboard = { marketStatus: afterHoursStatus };

    const { unmount: unmountPfWidget } = render(
      <MemoryRouter>
        <PortfolioWidget
          instance={{ id: 'pf-test', type: 'portfolio.holdings', config: {} }}
          updateConfig={() => {}}
        />
        <PortfolioWatchlistWidget
          instance={{ id: 'pw-test', type: 'personal.portfolioWatchlist', config: { defaultTab: 'portfolio' } }}
          updateConfig={() => {}}
        />
      </MemoryRouter>,
    );

    const expectedHoldingMarkdown = [
      '**GOOG**',
      'regular close USD 317.28 -3.48 (-1.08%) vs prev close',
      'post-market USD 318.10 +0.82 (+0.26% vs regular close)',
      'shares 10',
      'mkt val USD 3172.80',
      '持仓浮盈 +5.76%',
    ].join('\n');

    const pfRowSnap = await getWidgetContextSnapshot('pf-test', 'p-goog-post');
    expect(pfRowSnap?.label).toBe('GOOG · +5.76%');
    expect(pfRowSnap?.data).toEqual({ row: expectedHoldingQuote });
    expect(pfRowSnap?.text).toContain(expectedHoldingMarkdown);

    const pwRowSnap = await getWidgetContextSnapshot('pw-test', 'p-goog-post');
    expect(pwRowSnap?.label).toBe('GOOG · +5.76%');
    expect(pwRowSnap?.data).toEqual({ row: expectedHoldingQuote, tab: 'portfolio' });
    expect(pwRowSnap?.text).toContain(expectedHoldingMarkdown);
    unmountPfWidget();
  });
});
