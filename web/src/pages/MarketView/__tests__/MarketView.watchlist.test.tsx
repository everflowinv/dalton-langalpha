import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type React from 'react';
import { queryKeys } from '@/lib/queryKeys';
import {
  BATCH1_WATCHLIST_COLUMNS,
  buildPersonalWatchlistContextText,
  flattenWatchlistCompanies,
} from '@/lib/dalton/watchlist';
import { WatchlistAddModal } from '../components/watchlist/WatchlistAddModal';

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
  previewMutateMock: vi.fn(),
  mutateWatchlistMock: vi.fn(),
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
    previewWatchlistMutation: (...args: unknown[]) => mocks.previewMutateMock(...args),
    mutateWatchlist: (...args: unknown[]) => mocks.mutateWatchlistMock(...args),
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
  default: ({
    symbol,
    interval,
    onIntervalChange,
    toolbarLead,
  }: {
    symbol: string;
    interval: string;
    onIntervalChange?: (iv: string) => void;
    toolbarLead?: React.ReactNode;
  }) => (
    <div data-testid="market-chart" data-symbol={symbol} data-interval={interval}>
      {toolbarLead}
      <button type="button" data-testid="switch-tf-4h" onClick={() => onIntervalChange?.('4hour')}>
        4h
      </button>
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
      <span data-testid="loc-display">{loc.pathname + loc.search + loc.hash}</span>
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
          <Route path="/settings" element={<div data-testid="settings-route" />} />
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

  it('r4: normalizes nested workspace.workspace_id, persists search/sort/tf URL, and restores list via Back button', async () => {
    // 1. Nested workspace.workspace_id normalized into top-level workspace_id
    const flat = flattenWatchlistCompanies({
      ...mocks.watchlistSummary,
      watch: [
        {
          ticker: 'TSM',
          name: 'TSMC',
          tier: 'watch',
          workspace: { workspace_id: 'ws-nested-tsm', status: 'active' },
        },
      ],
    });
    expect(flat.find((c) => c.ticker === 'TSM')?.workspace_id).toBe('ws-nested-tsm');

    // 2. Full-roster summary included in watchlist context beyond 120 rows
    const ctxText = buildPersonalWatchlistContextText(
      flat,
      mocks.watchlistMetrics.items,
      'all',
      {
        total_count: 150,
        listed_count: 149,
        private_count: 1,
        tier_counts: { core: 50, watch: 50, radar: 50 },
        lowest_rsi_14d: [{ ticker: 'SNOW', rsi_14d: 21.3 }],
        highest_rsi_14d: [{ ticker: 'NVDA', rsi_14d: 78.1 }],
        top_gainers_1d: [],
        top_losers_1d: [],
        upcoming_earnings: [{ ticker: 'SNOW', date: '2026-12-03' }],
      },
    );
    expect(ctxText).toContain('SNOW(21.3)');
    expect(ctxText).toContain('SNOW(2026-12-03)');

    // 3. Search filter & tf URL persist across list <-> chart navigation
    renderAt(['/market']);
    const searchInput = await screen.findByRole('searchbox');
    fireEvent.change(searchInput, { target: { value: 'NVDA' } });
    expect(screen.getByTestId('watchlist-row-NVDA')).toBeTruthy();
    expect(screen.queryByTestId('watchlist-row-NET')).toBeNull();

    // Jump to chart, switch timeframe -> `?tf=4hour` written to URL
    fireEvent.click(screen.getByTestId('watchlist-row-NVDA'));
    await waitFor(() => expect(screen.getByTestId('market-chart')).toBeTruthy());
    fireEvent.click(screen.getByTestId('switch-tf-4h'));
    await waitFor(() => {
      expect(screen.getByTestId('loc-display').textContent).toContain('tf=4hour');
    });

    // Click BackToWatchlistButton -> returns to /market with search query restored!
    fireEvent.click(screen.getByTestId('back-to-watchlist-btn'));
    await waitFor(() => expect(screen.getByTestId('watchlist-workspace')).toBeTruthy());
    expect((screen.getByRole('searchbox') as HTMLInputElement).value).toBe('NVDA');
  });

  it('r4: WatchlistAddModal submits raw request without snapshot_id on exempt add, and snapshot_id on confirmable add', async () => {
    mocks.previewMutateMock.mockReset();
    mocks.mutateWatchlistMock.mockReset();

    // Exempt 1-ticker add: no snapshot_id sent!
    mocks.previewMutateMock.mockResolvedValueOnce({
      ok: true,
      action: null,
      raw_action: 'add',
      snapshot_id: null,
      requires_confirmation: false,
      target_tickers: ['PLTR'],
      target_tier: 'watch',
      affected_tickers: [{ ticker: 'PLTR', new_tier: 'watch' }],
      affected_workspaces: [],
      task_summary: {},
    });
    mocks.mutateWatchlistMock.mockResolvedValueOnce({ ok: true, added_tickers: ['PLTR'] });

    const onSuccess = vi.fn();
    const onClose = vi.fn();
    const { rerender } = render(<WatchlistAddModal open={true} onClose={onClose} onSuccess={onSuccess} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'PLTR' } });
    fireEvent.submit(screen.getByRole('textbox').closest('form')!);
    await waitFor(() => expect(mocks.mutateWatchlistMock).toHaveBeenCalledTimes(1));
    expect(mocks.mutateWatchlistMock).toHaveBeenCalledWith({
      action: 'add',
      tickers: ['PLTR'],
      tier: 'watch',
    });

    // Confirmable add (>5 tickers): submits ONLY action + server snapshot_id
    mocks.previewMutateMock.mockResolvedValueOnce({
      ok: true,
      action: 'watchlist_batch_add',
      raw_action: 'add',
      snapshot_id: 'snap-uuid-123',
      requires_confirmation: true,
      target_tickers: ['A1', 'A2', 'A3', 'A4', 'A5', 'A6'],
      target_tier: 'watch',
      affected_tickers: [{ ticker: 'A1', new_tier: 'watch' }],
      affected_workspaces: [],
      task_summary: {},
    });
    mocks.mutateWatchlistMock.mockResolvedValueOnce({ ok: true, added_tickers: ['A1'] });
    rerender(<WatchlistAddModal open={true} onClose={onClose} onSuccess={onSuccess} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'A1,A2,A3,A4,A5,A6' } });
    fireEvent.submit(screen.getByRole('textbox').closest('form')!);
    const confirmBtn = await screen.findByRole('button', { name: /确认执行|Confirm/i });
    fireEvent.click(confirmBtn);
    await waitFor(() => expect(mocks.mutateWatchlistMock).toHaveBeenCalledTimes(2));
    expect(mocks.mutateWatchlistMock).toHaveBeenLastCalledWith({
      action: 'add',
      snapshot_id: 'snap-uuid-123',
    });
  });

  it('b2: toolbar Tier Policies button navigates to /settings?tab=coverage#dalton-tier-policies and renders all 7 valuation states', async () => {
    const prevSummary = mocks.watchlistSummary;
    const prevMetrics = mocks.watchlistMetrics;
    (mocks as unknown as Record<string, unknown>).watchlistSummary = {
      ok: true,
      core: [
        { ticker: 'NVDA', name: 'NVIDIA', tier: 'core', is_private: false },
        { ticker: 'LOSS', name: 'Loss Co', tier: 'core', is_private: false },
        { ticker: 'XYZADR', name: 'Unverified ADR', tier: 'core', is_private: false },
        { ticker: 'STALECO', name: 'Stale Co', tier: 'core', is_private: false },
        { ticker: 'LIMIT429', name: 'Rate Limited', tier: 'core', is_private: false },
        { ticker: 'NOCOV', name: 'No Coverage', tier: 'core', is_private: false },
        { ticker: 'SPCX', name: 'SpaceX', tier: 'core', is_private: true },
      ],
      watch: [],
      radar: [],
      groups: [],
      stale_tickers: [],
      counts: { core: 7, watch: 0, radar: 0, total: 7, groups: 0 },
      pending_reconcile_count: 0,
    };
    (mocks as unknown as Record<string, unknown>).watchlistMetrics = {
      ok: true,
      pending_symbols: [],
      as_of: '2026-11-26T12:00:00Z',
      items: {
        NVDA: {
          ticker: 'NVDA', is_private: false, status: 'ok', price: 135, currency: 'USD',
          chg_1d: 1, chg_1w: 2, chg_1m: 3, chg_3m: 4, chg_ytd: 5, chg_1y: 10,
          high_52w: 150, low_52w: 90, position_52w: 75, rsi_14d: 60, rsi_30d: 58,
          vol_ratio_5_20: 1.1, market_cap: 3e12, next_earnings: '2026-12-01',
          pe_fy1: 30.0, pe_fy2: 24.5, eps_growth_fy2: 22.4,
          valuation: {
            fy1_eps: 4.5, fy2_eps: 5.51, fy1_end: '2027-01-31', fy2_end: '2028-01-31',
            estimate_currency: 'USD', quote_currency: 'USD', shares_per_quote_unit: 1,
            unit_verified: true, analyst_count: 42, provider: 'yfinance', as_of: '2026-11-26T12:00:00Z',
            status: 'ok', pe_fy1: 30.0, pe_fy2: 24.5, eps_growth_fy2: 22.4,
            pe_fy1_status: 'ok', pe_fy2_status: 'ok', eps_growth_fy2_status: 'ok',
          },
        },
        LOSS: {
          ticker: 'LOSS', is_private: false, status: 'ok', price: 20, currency: 'USD',
          chg_1d: -1, chg_1w: 0, chg_1m: 0, chg_3m: 0, chg_ytd: 0, chg_1y: 0,
          high_52w: 30, low_52w: 10, position_52w: 50, rsi_14d: 45, rsi_30d: 45,
          vol_ratio_5_20: 1, market_cap: 1e9, next_earnings: null,
          pe_fy1: null, pe_fy2: null, eps_growth_fy2: null,
          valuation: {
            fy1_eps: -1.5, fy2_eps: -0.5, fy1_end: '2026-12-31', fy2_end: '2027-12-31',
            estimate_currency: 'USD', quote_currency: 'USD', shares_per_quote_unit: 1,
            unit_verified: true, analyst_count: 5, provider: 'yfinance', as_of: '2026-11-26T12:00:00Z',
            status: 'loss', pe_fy1: null, pe_fy2: null, eps_growth_fy2: null,
            pe_fy1_status: 'loss', pe_fy2_status: 'loss', eps_growth_fy2_status: 'loss',
          },
        },
        XYZADR: {
          ticker: 'XYZADR', is_private: false, status: 'ok', price: 25, currency: 'USD',
          chg_1d: 0, chg_1w: 0, chg_1m: 0, chg_3m: 0, chg_ytd: 0, chg_1y: 0,
          high_52w: 30, low_52w: 20, position_52w: 50, rsi_14d: 50, rsi_30d: 50,
          vol_ratio_5_20: 1, market_cap: 5e9, next_earnings: null,
          pe_fy1: null, pe_fy2: null, eps_growth_fy2: null,
          valuation: {
            fy1_eps: 10, fy2_eps: 12, fy1_end: '2026-12-31', fy2_end: '2027-12-31',
            estimate_currency: 'JPY', quote_currency: 'USD', shares_per_quote_unit: null,
            unit_verified: false, analyst_count: 3, provider: 'yfinance', as_of: '2026-11-26T12:00:00Z',
            status: 'unit_unverified', pe_fy1: null, pe_fy2: null, eps_growth_fy2: null,
            pe_fy1_status: 'unit_unverified', pe_fy2_status: 'unit_unverified', eps_growth_fy2_status: 'unit_unverified',
          },
        },
        STALECO: {
          ticker: 'STALECO', is_private: false, status: 'ok', price: 100, currency: 'USD',
          chg_1d: 0, chg_1w: 0, chg_1m: 0, chg_3m: 0, chg_ytd: 0, chg_1y: 0,
          high_52w: 110, low_52w: 90, position_52w: 50, rsi_14d: 50, rsi_30d: 50,
          vol_ratio_5_20: 1, market_cap: 5e9, next_earnings: null,
          pe_fy1: 18.2, pe_fy2: 15.6, eps_growth_fy2: 16.7,
          valuation: {
            fy1_eps: 5.5, fy2_eps: 6.4, fy1_end: '2026-12-31', fy2_end: '2027-12-31',
            estimate_currency: 'USD', quote_currency: 'USD', shares_per_quote_unit: 1,
            unit_verified: true, analyst_count: 10, provider: 'yfinance', as_of: '2026-11-20T00:00:00Z',
            status: 'stale', pe_fy1: 18.2, pe_fy2: 15.6, eps_growth_fy2: 16.7,
            pe_fy1_status: 'stale', pe_fy2_status: 'stale', eps_growth_fy2_status: 'stale',
          },
        },
        LIMIT429: {
          ticker: 'LIMIT429', is_private: false, status: 'ok', price: 50, currency: 'USD',
          chg_1d: 0, chg_1w: 0, chg_1m: 0, chg_3m: 0, chg_ytd: 0, chg_1y: 0,
          high_52w: 60, low_52w: 40, position_52w: 50, rsi_14d: 50, rsi_30d: 50,
          vol_ratio_5_20: 1, market_cap: 5e9, next_earnings: null,
          pe_fy1: null, pe_fy2: null, eps_growth_fy2: null,
          valuation: {
            fy1_eps: null, fy2_eps: null, fy1_end: null, fy2_end: null,
            estimate_currency: null, quote_currency: 'USD', shares_per_quote_unit: null,
            unit_verified: false, analyst_count: null, provider: null, as_of: null,
            status: 'rate_limited', pe_fy1: null, pe_fy2: null, eps_growth_fy2: null,
            pe_fy1_status: 'rate_limited', pe_fy2_status: 'rate_limited', eps_growth_fy2_status: 'rate_limited',
          },
        },
        NOCOV: {
          ticker: 'NOCOV', is_private: false, status: 'ok', price: 12, currency: 'HKD',
          chg_1d: 0, chg_1w: 0, chg_1m: 0, chg_3m: 0, chg_ytd: 0, chg_1y: 0,
          high_52w: 15, low_52w: 10, position_52w: 40, rsi_14d: 50, rsi_30d: 50,
          vol_ratio_5_20: 1, market_cap: 1e9, next_earnings: null,
          pe_fy1: null, pe_fy2: null, eps_growth_fy2: null,
          valuation: {
            fy1_eps: null, fy2_eps: null, fy1_end: null, fy2_end: null,
            estimate_currency: null, quote_currency: 'HKD', shares_per_quote_unit: null,
            unit_verified: false, analyst_count: null, provider: 'yfinance', as_of: null,
            status: 'no_coverage', pe_fy1: null, pe_fy2: null, eps_growth_fy2: null,
            pe_fy1_status: 'no_coverage', pe_fy2_status: 'no_coverage', eps_growth_fy2_status: 'no_coverage',
          },
        },
        SPCX: {
          ticker: 'SPCX', is_private: true, status: 'unlisted', price: null, currency: null,
          chg_1d: null, chg_1w: null, chg_1m: null, chg_3m: null, chg_ytd: null, chg_1y: null,
          high_52w: null, low_52w: null, position_52w: null, rsi_14d: null, rsi_30d: null,
          vol_ratio_5_20: null, market_cap: null, next_earnings: null,
          pe_fy1: null, pe_fy2: null, eps_growth_fy2: null,
          valuation: {
            fy1_eps: null, fy2_eps: null, fy1_end: null, fy2_end: null,
            estimate_currency: null, quote_currency: null, shares_per_quote_unit: null,
            unit_verified: false, analyst_count: null, provider: null, as_of: null,
            status: 'unlisted', pe_fy1: null, pe_fy2: null, eps_growth_fy2: null,
          },
        },
      },
    };

    try {
      const { container } = renderAt(['/market']);
      await waitFor(() => expect(screen.getByTestId('watchlist-row-NVDA')).toBeTruthy());

      // Valuation cells render ok, loss (NM), unit_unverified, stale, rate_limited, no_coverage, unlisted
      expect(screen.getByTestId('watchlist-row-NVDA').textContent).toContain('30.0x');
      expect(screen.getByTestId('watchlist-row-NVDA').textContent).toContain('24.5x');
      expect(screen.getByTestId('watchlist-row-LOSS').textContent).toContain('NM');
      expect(container.querySelector('[data-valuation-status="unit_unverified"]')).toBeTruthy();
      expect(container.querySelector('[data-valuation-status="stale"]')).toBeTruthy();
      expect(container.querySelector('[data-valuation-status="rate_limited"]')).toBeTruthy();
      expect(container.querySelector('[data-valuation-status="no_coverage"]')).toBeTruthy();

      // Click Tier Policies shortcut -> navigates to /settings?tab=coverage#dalton-tier-policies
      fireEvent.click(screen.getByTestId('watchlist-tier-policies-link'));
      await waitFor(() => {
        expect(screen.getByTestId('loc-display').textContent).toBe(
          '/settings?tab=coverage#dalton-tier-policies',
        );
      });
    } finally {
      (mocks as unknown as Record<string, unknown>).watchlistSummary = prevSummary;
      (mocks as unknown as Record<string, unknown>).watchlistMetrics = prevMetrics;
    }
  });
});
