import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type React from 'react';
import { queryKeys } from '@/lib/queryKeys';
import { BATCH1_WATCHLIST_COLUMNS } from '@/lib/dalton/watchlist';

const mocks = vi.hoisted(() => ({
  toast: vi.fn(),
  ws: {
    prices: new Map(),
    connectionStatus: 'connected',
    dataLevel: null,
    ginlixDataEnabled: false,
    subscribe: vi.fn(),
    unsubscribe: vi.fn(),
  },
  watchlistSummary: {
    ok: true,
    core: [
      {
        ticker: 'NVDA',
        name: 'NVIDIA Corp',
        tier: 'core' as const,
        workspace_id: 'ws-nvda',
        is_pinned: true,
        is_private: false,
      },
      {
        ticker: 'OPENAI',
        name: 'OpenAI',
        tier: 'core' as const,
        workspace_id: 'ws-oai',
        is_pinned: false,
        is_private: true,
      },
    ],
    watch: [
      {
        ticker: 'AVGO',
        name: 'Broadcom',
        tier: 'watch' as const,
        workspace_id: null,
        is_pinned: false,
        is_private: false,
      },
    ],
    radar: [
      {
        ticker: 'NET',
        name: 'Cloudflare',
        tier: 'radar' as const,
        workspace_id: null,
        is_pinned: false,
        is_private: false,
      },
    ],
    groups: [],
    stale_tickers: [],
    counts: { core: 2, watch: 1, radar: 1, total: 4, groups: 0 },
    pending_reconcile_count: 0,
  },
  watchlistMetrics: {
    ok: true,
    items: {
      NVDA: {
        ticker: 'NVDA',
        is_private: false,
        status: 'ok' as const,
        price: 138.4,
        currency: 'USD',
        chg_1d: 2.45,
        chg_1w: 6.1,
        chg_1m: 12.3,
        chg_3m: 25.0,
        chg_ytd: 40.5,
        chg_1y: 88.2,
        high_52w: 150,
        low_52w: 90,
        position_52w: 80.7,
        rsi_14d: 64.2,
        rsi_30d: 58.1,
        vol_ratio_5_20: 1.25,
        market_cap: 3_400_000_000_000,
        next_earnings: '2026-12-15',
      },
      OPENAI: {
        ticker: 'OPENAI',
        is_private: true,
        status: 'unlisted' as const,
        price: null,
        currency: null,
        chg_1d: null,
        chg_1w: null,
        chg_1m: null,
        chg_3m: null,
        chg_ytd: null,
        chg_1y: null,
        high_52w: null,
        low_52w: null,
        position_52w: null,
        rsi_14d: null,
        rsi_30d: null,
        vol_ratio_5_20: null,
        market_cap: null,
        next_earnings: null,
      },
    },
    pending_symbols: [],
    as_of: '2026-11-26T10:00:00Z',
  },
  capturedChatProps: [] as Array<{ watchlistContext?: string | null; scope: string; symbol: string }>,
  activeWorkspaceObj: { workspace_id: 'home-ws', name: 'Home', is_home: true, is_shared: false } as Record<string, unknown>,
  isColleague: false,
}));

vi.mock('@/config/brand', () => ({
  DALTON_PROFILE: 'core',
  isCoreProfileBuild: true,
  BRAND_NAME: 'Dalton',
  BRAND_MARK: 'langalpha',
  isDaltonBuild: true,
  faviconHref: () => '/logo-favicon.svg',
}));

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => false,
  getIsMobileSnapshot: () => false,
}));

vi.mock('@/components/ui/use-toast', () => ({
  toast: mocks.toast,
  useToast: () => ({ toast: mocks.toast }),
}));

vi.mock('@/lib/framer', async (importActual) => ({
  ...(await importActual<Record<string, unknown>>()),
  AnimatePresence: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
}));

vi.mock('@/lib/dalton/watchlist', async (importActual) => {
  const actual = await importActual<typeof import('@/lib/dalton/watchlist')>();
  return {
    ...actual,
    fetchPersonalWatchlist: vi.fn(async () => mocks.watchlistSummary),
    fetchPersonalWatchlistMetrics: vi.fn(async () => mocks.watchlistMetrics),
    usePersonalWatchlist: () => ({
      data: mocks.watchlistSummary,
      isLoading: false,
      isError: false,
      isSuccess: true,
      refetch: vi.fn(),
    }),
    usePersonalWatchlistMetrics: () => ({
      data: mocks.watchlistMetrics,
      isLoading: false,
      isError: false,
      isSuccess: true,
    }),
  };
});

vi.mock('@/hooks/useWorkspace', () => ({
  useWorkspace: () => ({ data: mocks.activeWorkspaceObj }),
}));

vi.mock('../../ChatAgent/components/dalton/TeamSharing', () => ({
  useTeamReadOnly: () => ({ isColleague: mocks.isColleague, isReadOnly: mocks.isColleague }),
}));

vi.mock('../components/StockHeader', () => ({ default: () => <div data-testid="stock-header" /> }));
vi.mock('../components/MarketChart', () => ({
  default: ({ symbol, interval, toolbarLead }: { symbol: string; interval: string; toolbarLead?: React.ReactNode }) => (
    <div data-testid="market-chart" data-symbol={symbol} data-interval={interval}>
      {toolbarLead}
    </div>
  ),
}));
vi.mock('../components/MarketChatPanel', () => ({
  default: (props: { watchlistContext?: string | null; scope: string; symbol: string; onReturnToChat?: () => void }) => {
    mocks.capturedChatProps.push({
      watchlistContext: props.watchlistContext,
      scope: props.scope,
      symbol: props.symbol,
    });
    return (
      <div
        data-testid="market-chat-panel"
        data-has-watchlist-ctx={String(Boolean(props.watchlistContext))}
        data-has-return={String(Boolean(props.onReturnToChat))}
      />
    );
  },
}));
vi.mock('../components/quoteList/QuoteList', () => ({ QuoteList: () => null }));
vi.mock('../components/quoteList/QuoteListDrawer', () => ({ QuoteListDrawer: () => null }));
vi.mock('../components/CompanyOverviewPanel', () => ({ default: () => null }));
vi.mock('../contexts/MarketDataWSContext', () => ({
  MarketDataWSProvider: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
  useMarketDataWSContext: () => mocks.ws,
}));
vi.mock('../hooks/useStockData', () => ({
  useStockData: () => ({
    stockInfo: null,
    realTimePrice: null,
    snapshotData: null,
    overviewData: null,
    overviewLoading: false,
    overlayData: null,
    marketStatus: null,
  }),
}));
vi.mock('../hooks/useStockQuoteModel', () => ({ useStockQuoteModel: () => ({}) }));
vi.mock('../hooks/useChartLegendSlots', () => ({ useChartLegendSlots: () => ({ toolbarLead: null }) }));
vi.mock('../hooks/useRestoredWorkspace', () => ({
  useRestoredWorkspace: ({ linkedId }: { linkedId?: string | null }) => ({
    selectedWorkspaceId: linkedId || 'ws-1',
    pending: false,
    select: vi.fn(),
    workspaces: [{ workspace_id: 'ws-1', name: 'Workspace 1' }],
  }),
}));
vi.mock('@/hooks/useWorkspaces', () => ({
  useWorkspaces: () => ({
    data: { workspaces: [{ workspace_id: 'ws-1', name: 'Workspace 1' }], total: 1 },
    isFetchedAfterMount: true,
    isSuccess: true,
  }),
}));
vi.mock('@/pages/ChatAgent/utils/api', async (importActual) => ({
  ...(await importActual<Record<string, unknown>>()),
  getSkills: vi.fn().mockResolvedValue([]),
  getModelMetadata: vi.fn().mockResolvedValue({}),
  getHomeWorkspace: vi.fn().mockResolvedValue({ workspace_id: 'home-ws', status: 'running', is_home: true }),
}));

import MarketView from '../MarketView';
import { savePref } from '../utils/prefs';

function LocationProbe() {
  const loc = useLocation();
  const nav = useNavigate();
  return (
    <div>
      <span data-testid="loc-display">{loc.pathname + loc.search}</span>
      <button type="button" data-testid="history-back" onClick={() => nav(-1)}>back</button>
      <button type="button" data-testid="history-forward" onClick={() => nav(1)}>forward</button>
    </div>
  );
}

function renderAt(initialEntries: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(queryKeys.workspaces.home(), { workspace_id: 'home-ws', is_home: true });
  qc.setQueryData(['portfolioHoldings'], [{ symbol: 'NVDA' }]);
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={initialEntries}>
        <LocationProbe />
        <Routes>
          <Route path="/market" element={<MarketView />} />
          <Route path="/chat/*" element={<div data-testid="chat-route" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('MarketView personal watchlist route & deep-link navigation', () => {
  beforeEach(() => {
    mocks.capturedChatProps = [];
    mocks.isColleague = false;
    mocks.activeWorkspaceObj = { workspace_id: 'home-ws', name: 'Home', is_home: true, is_shared: false };
    localStorage.clear();
    sessionStorage.clear();
  });

  it('opens full WatchlistWorkspace at /market, navigates to /market?symbol=NVDA, and supports Back/Forward', async () => {
    renderAt(['/market']);

    // Full watchlist view renders without auto-jumping to a chart
    expect(await screen.findByTestId('watchlist-workspace')).toBeTruthy();
    expect(screen.queryByTestId('market-chart')).toBeNull();
    expect(await screen.findByTestId('watchlist-row-NVDA')).toBeTruthy();
    expect(screen.getByTestId('watchlist-row-OPENAI')).toBeTruthy();
    expect(screen.getByTestId('watchlist-row-NET')).toBeTruthy();

    // Private chat receives watchlist context automatically
    const chatPanel = screen.getByTestId('market-chat-panel');
    expect(chatPanel.getAttribute('data-has-watchlist-ctx')).toBe('true');

    // Column picker only exposes Batch-1 base columns (no FY1 P/E etc.)
    fireEvent.click(screen.getByRole('button', { name: /列配置|Columns/i }));
    const checkboxes = screen.getAllByRole('checkbox');
    expect(checkboxes.length).toBe(BATCH1_WATCHLIST_COLUMNS.length);

    // Clicking NVDA pushes `/market?symbol=NVDA` and opens K-line view with `?symbol=NVDA` preserved
    fireEvent.click(screen.getByTestId('watchlist-row-NVDA'));
    await waitFor(() => {
      expect(screen.getByTestId('market-chart')).toBeTruthy();
    });
    expect(screen.getByTestId('market-chart').getAttribute('data-symbol')).toBe('NVDA');
    expect(screen.getByTestId('loc-display').textContent).toBe('/market?symbol=NVDA');

    // Browser Back returns to `/market` full watchlist; Forward returns to `/market?symbol=NVDA`
    fireEvent.click(screen.getByTestId('history-back'));
    await waitFor(() => {
      expect(screen.getByTestId('watchlist-workspace')).toBeTruthy();
    });
    expect(screen.getByTestId('loc-display').textContent).toBe('/market');

    fireEvent.click(screen.getByTestId('history-forward'));
    await waitFor(() => {
      expect(screen.getByTestId('market-chart')).toBeTruthy();
    });
    expect(screen.getByTestId('loc-display').textContent).toBe('/market?symbol=NVDA');
  });

  it('preserves symbol, tf, and thread while consuming ws and returnTo on deep-link', async () => {
    renderAt(['/market?symbol=NVDA&tf=1hour&ws=ws-7&thread=th-9&returnTo=/chat/t/th-9']);

    await waitFor(() => {
      expect(screen.getByTestId('market-chart')).toBeTruthy();
    });
    expect(screen.getByTestId('market-chart').getAttribute('data-symbol')).toBe('NVDA');
    expect(screen.getByTestId('market-chart').getAttribute('data-interval')).toBe('1hour');
    expect(screen.getByTestId('market-chat-panel').getAttribute('data-has-return')).toBe('true');

    await waitFor(() => {
      const locText = screen.getByTestId('loc-display').textContent || '';
      expect(locText).toContain('symbol=NVDA');
      expect(locText).toContain('tf=1hour');
      expect(locText).toContain('thread=th-9');
      expect(locText).not.toContain('ws=');
      expect(locText).not.toContain('returnTo=');
    });
  });

  it('withholds watchlistContext from MarketChatPanel in shared/colleague sessions', async () => {
    savePref('mode', 'ptc');
    mocks.isColleague = true;
    mocks.activeWorkspaceObj = { workspace_id: 'ws-shared', name: 'Team WS', is_shared: true };

    renderAt(['/market']);
    expect(await screen.findByTestId('watchlist-row-NVDA')).toBeTruthy();
    const chatPanel = screen.getByTestId('market-chat-panel');
    expect(chatPanel.getAttribute('data-has-watchlist-ctx')).toBe('false');
  });
});
