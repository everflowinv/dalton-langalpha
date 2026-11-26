import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook } from '@testing-library/react';
import type React from 'react';
import {
  MAX_DASHBOARD_COVERAGE_QUOTES,
  useCoverageWatchlistData,
  type CoverageWatchlistRow,
} from '../../../hooks/useCoverageWatchlistData';
import { PortfolioWatchlistConfigSchema } from '../../framework/configSchemas';

const mocks = vi.hoisted(() => ({
  useQuotesCalls: [] as string[][],
  personalSummary: {
    ok: true,
    core: [] as Array<{ ticker: string; name: string; tier: 'core'; is_private?: boolean; is_pinned?: boolean }>,
    watch: [] as Array<{ ticker: string; name: string; tier: 'watch'; is_private?: boolean; is_pinned?: boolean }>,
    radar: [] as Array<{ ticker: string; name: string; tier: 'radar'; is_private?: boolean; is_pinned?: boolean }>,
    groups: [],
    stale_tickers: [],
    counts: { core: 0, watch: 0, radar: 0, total: 0, groups: 0 },
  },
  exported: {} as Record<string, { full: () => unknown; rows?: (id: string) => unknown }>,
  dashboardCtx: {} as Record<string, unknown>,
}));

vi.mock('@/config/brand', () => ({
  DALTON_PROFILE: 'core',
  isCoreProfileBuild: true,
  BRAND_NAME: 'Dalton',
  BRAND_MARK: 'langalpha',
  isDaltonBuild: true,
  faviconHref: () => '/logo-favicon.svg',
}));

vi.mock('@/lib/dalton/watchlist', async (importActual) => {
  const actual = await importActual<typeof import('@/lib/dalton/watchlist')>();
  return {
    ...actual,
    usePersonalWatchlist: () => ({
      data: mocks.personalSummary,
      isLoading: false,
      isSuccess: true,
      refetch: vi.fn(),
    }),
  };
});

vi.mock('@/lib/quotes', async (importActual) => {
  const actual = await importActual<typeof import('@/lib/quotes')>();
  return {
    ...actual,
    useQuotes: (symbols: string[]) => {
      mocks.useQuotesCalls.push([...symbols]);
      return { quotes: {}, isLoading: false, refetch: vi.fn() };
    },
  };
});

vi.mock('../../framework/DashboardDataContext', () => ({
  useDashboardContext: () => mocks.dashboardCtx,
}));

vi.mock('../../framework/contextSnapshot', () => ({
  useWidgetContextExport: (id: string, spec: { full: () => unknown; rows?: (id: string) => unknown }) => {
    mocks.exported[id] = spec;
  },
}));

vi.mock('../../../components/RowAttachButton', () => ({
  RowAttachButton: ({ rowId }: { rowId: string }) => <span data-testid={`attach-${rowId}`} />,
}));

import PortfolioWatchlistWidget from '../PortfolioWatchlistWidget';

function makeCoverageRow(symbol: string, tier: 'core' | 'watch' | 'radar' = 'core', isPrivate = false): CoverageWatchlistRow {
  return {
    symbol,
    name: `${symbol} Corp`,
    tier,
    is_private: isPrivate,
    is_pinned: false,
    workspace_id: null,
    price: isPrivate ? 0 : 120,
    change: 2.5,
    changePercent: 2.1,
    isPositive: true,
    quoteAvailable: !isPrivate,
    previousClose: 117.5,
    earlyTradingChangePercent: null,
    lateTradingChangePercent: null,
    currency: 'USD',
    assetClass: 'equity',
  };
}

function LocationProbe() {
  const loc = useLocation();
  return <span data-testid="dash-loc">{loc.pathname}</span>;
}

function renderWidget(config: { defaultTab?: 'coverage' | 'watchlist' | 'portfolio'; valuesHidden?: boolean }) {
  const updateConfig = vi.fn();
  render(
    <MemoryRouter initialEntries={['/dashboard']}>
      <LocationProbe />
      <Routes>
        <Route
          path="/dashboard"
          element={
            <PortfolioWatchlistWidget
              instance={{ id: 'pw-1', type: 'personal.portfolioWatchlist', config }}
              updateConfig={updateConfig}
            />
          }
        />
        <Route path="/market" element={<div data-testid="market-page" />} />
      </Routes>
    </MemoryRouter>,
  );
  return { updateConfig };
}

describe('Dashboard PortfolioWatchlistWidget coverage tab & 50-quote cap', () => {
  beforeEach(() => {
    mocks.useQuotesCalls = [];
    mocks.exported = {};
    mocks.dashboardCtx = {
      dashboard: { marketStatus: null },
      coverage: {
        rows: [makeCoverageRow('NVDA', 'core'), makeCoverageRow('OPENAI', 'core', true)],
        totalCount: 65,
        quotedLimit: MAX_DASHBOARD_COVERAGE_QUOTES,
        counts: { core: 20, watch: 30, radar: 15, total: 65, groups: 0 },
        loading: false,
        loaded: true,
        refetch: vi.fn(),
      },
      watchlist: { rows: [], loading: false, loaded: true },
      watchlistHandlers: { onAdd: vi.fn(), onDelete: vi.fn() },
      portfolio: { rows: [], loading: false, loaded: true, hasRealHoldings: false },
      portfolioHandlers: { onAdd: vi.fn(), onDelete: vi.fn(), onEdit: vi.fn() },
    };
  });

  it('keeps tab order [Coverage, Watchlist, Holdings] and preserves saved defaultTab="watchlist"', () => {
    // Zod schema preserves saved 'watchlist' and leaves omitted defaultTab as undefined
    expect(PortfolioWatchlistConfigSchema.parse({ defaultTab: 'watchlist' }).defaultTab).toBe('watchlist');
    expect(PortfolioWatchlistConfigSchema.parse({}).defaultTab).toBeUndefined();

    renderWidget({ defaultTab: 'watchlist', valuesHidden: false });
    const tabButtons = screen.getAllByRole('button', { pressed: true }).concat(
      screen.getAllByRole('button', { pressed: false }),
    );
    const chipLabels = screen
      .getAllByRole('group')[0]
      .querySelectorAll('button');
    expect(chipLabels.length).toBe(3);
    expect(chipLabels[0].textContent).toMatch(/关注列表|Coverage/);
    expect(chipLabels[1].textContent).toMatch(/自选股|Watchlist/);
    expect(chipLabels[2].textContent).toMatch(/持仓|Holdings/);
    // Saved defaultTab='watchlist' remains selected (no jump)
    expect(chipLabels[1].getAttribute('aria-pressed')).toBe('true');
    expect(tabButtons.length).toBeGreaterThan(0);
  });

  it('auto-selects coverage tab for new card without defaultTab, exports context, and navigates to /market', () => {
    renderWidget({ valuesHidden: false });
    const chipLabels = screen.getAllByRole('group')[0].querySelectorAll('button');
    expect(chipLabels[0].getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByTestId('coverage-row-NVDA')).toBeTruthy();
    expect(screen.getByTestId('coverage-row-OPENAI')).toBeTruthy();

    // Context export covers both full coverage list and single-row attach
    const fullExport = mocks.exported['pw-1'].full() as { text: string; data: { tab: string; totalCount: number } };
    expect(fullExport.data.tab).toBe('coverage');
    expect(fullExport.data.totalCount).toBe(65);
    expect(fullExport.text).toContain('NVDA');

    const rowExport = mocks.exported['pw-1'].rows?.('NVDA') as { text: string; data: { tab: string } };
    expect(rowExport.data.tab).toBe('coverage');
    expect(rowExport.text).toContain('NVDA');

    // View all link navigates to /market
    fireEvent.click(screen.getByRole('button', { name: /查看全部|View all/i }));
    expect(screen.getByTestId('dash-loc').textContent).toBe('/market');
  });

  it('caps live useQuotes subscriptions at 50 listed symbols and skips private companies', () => {
    const core = Array.from({ length: 65 }, (_, i) => ({
      ticker: `SYM${String(i).padStart(3, '0')}`,
      name: `Company ${i}`,
      tier: 'core' as const,
      is_private: i === 0, // 1 private + 64 listed
      is_pinned: false,
    }));
    mocks.personalSummary = {
      ok: true,
      core,
      watch: [],
      radar: [],
      groups: [],
      stale_tickers: [],
      counts: { core: 65, watch: 0, radar: 0, total: 65, groups: 0 },
    };

    const qc = new QueryClient();
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useCoverageWatchlistData(), { wrapper });

    expect(result.current.totalCount).toBe(65);
    expect(result.current.rows.length).toBe(50);
    expect(mocks.useQuotesCalls.length).toBeGreaterThan(0);
    const subscribed = mocks.useQuotesCalls[mocks.useQuotesCalls.length - 1];
    expect(subscribed.length).toBe(50);
    expect(subscribed).not.toContain('SYM000'); // private company excluded from live quotes
  });
});
