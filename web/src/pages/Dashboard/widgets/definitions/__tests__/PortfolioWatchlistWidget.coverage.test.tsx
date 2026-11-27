import { describe, it, expect, vi, beforeEach } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook } from '@testing-library/react';
import type React from 'react';
import {
  MAX_DASHBOARD_COVERAGE_QUOTES,
  useCoverageWatchlistData,
  type CoverageWatchlistRow,
} from '../../../hooks/useCoverageWatchlistData';
import type { mutateWatchlist, previewWatchlistMutation } from '@/lib/dalton/watchlist';
import { PortfolioWatchlistConfigSchema } from '../../framework/configSchemas';

const mocks = vi.hoisted(() => ({
  useQuotesCalls: [] as string[][],
  mutateWatchlistMock: vi.fn<typeof mutateWatchlist>(),
  previewWatchlistMutationMock: vi.fn<typeof previewWatchlistMutation>(),
  toastMock: vi.fn(),
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

vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: mocks.toastMock }),
}));

vi.mock('@/lib/dalton/watchlist', async (importActual) => {
  const actual = await importActual<typeof import('@/lib/dalton/watchlist')>();
  return {
    ...actual,
    mutateWatchlist: (...args: Parameters<typeof mutateWatchlist>) => mocks.mutateWatchlistMock(...args),
    previewWatchlistMutation: (...args: Parameters<typeof previewWatchlistMutation>) =>
      mocks.previewWatchlistMutationMock(...args),
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

  it('r4: filters by tier BEFORE slicing the 50-symbol quote cap so radar stocks beyond 50 core rows still render & subscribe quotes', () => {
    const core60 = Array.from({ length: 60 }, (_, i) => ({
      ticker: `CORE${String(i).padStart(2, '0')}`,
      name: `Core ${i}`,
      tier: 'core' as const,
      is_private: false,
      is_pinned: false,
    }));
    const radar5 = Array.from({ length: 5 }, (_, i) => ({
      ticker: `RADAR${i}`,
      name: `Radar ${i}`,
      tier: 'radar' as const,
      is_private: false,
      is_pinned: false,
    }));
    mocks.dashboardCtx = {
      ...mocks.dashboardCtx,
      coverage: {
        rows: core60.slice(0, 50).map((c) => makeCoverageRow(c.ticker, 'core')),
        allCompanies: [...core60, ...radar5],
        quotesBySymbol: {},
        totalCount: 65,
        quotedLimit: MAX_DASHBOARD_COVERAGE_QUOTES,
        counts: { core: 60, watch: 0, radar: 5, total: 65, groups: 0 },
        loading: false,
        loaded: true,
        refetch: vi.fn(),
      },
    };

    renderWidget({ valuesHidden: false });
    // Switch tier chip to Radar
    const radarBtn = screen.getByRole('button', { name: /雷达|Radar/i });
    fireEvent.click(radarBtn);

    // All 5 radar rows MUST be rendered (even though 60 core rows precede them!)
    expect(screen.getByTestId('coverage-row-RADAR0')).toBeTruthy();
    expect(screen.getByTestId('coverage-row-RADAR4')).toBeTruthy();
    // Live quotes subscribed for the filtered radar symbols!
    expect(mocks.useQuotesCalls.some((syms) => syms.includes('RADAR0'))).toBe(true);
    // Export reflects filtered radar totalCount (5), not full 65
    const exp = mocks.exported['pw-1'].full() as { data: { totalCount: number; rows: unknown[] } };
    expect(exp.data.totalCount).toBe(5);
    expect(exp.data.rows.length).toBe(5);
  });

  it('b2: right-click on Watchlist and Portfolio rows adds ticker to personal watchlist (core/watch/radar) without mutating source tables', async () => {
    mocks.mutateWatchlistMock.mockReset().mockResolvedValue({ ok: true, added_tickers: ['AAPL'] });
    const onWatchlistDelete = vi.fn();
    const onPortfolioDelete = vi.fn();
    const onPortfolioEdit = vi.fn();

    mocks.dashboardCtx = {
      ...mocks.dashboardCtx,
      watchlist: {
        rows: [
          {
            watchlist_item_id: 'wl-1',
            symbol: 'AAPL',
            price: 220,
            change: 2,
            changePercent: 0.9,
            currency: 'USD',
            quoteAvailable: true,
          },
        ],
        loading: false,
        loaded: true,
      },
      watchlistHandlers: { onAdd: vi.fn(), onDelete: onWatchlistDelete },
      portfolio: {
        rows: [
          {
            user_portfolio_id: 'pf-1',
            symbol: 'MSFT',
            quantity: 10,
            marketValue: 4200,
            unrealizedPlPercent: 12.5,
            currency: 'USD',
            quoteAvailable: true,
          },
        ],
        loading: false,
        loaded: true,
        hasRealHoldings: true,
      },
      portfolioHandlers: { onAdd: vi.fn(), onEdit: onPortfolioEdit, onDelete: onPortfolioDelete },
    };

    // 1. Watchlist ("自选股") tab -> right-click AAPL -> add to Core
    renderWidget({ defaultTab: 'watchlist', valuesHidden: false });
    const aaplCell = screen.getByText('AAPL');
    fireEvent.contextMenu(aaplCell);
    const addCoreItem = await screen.findByTestId('quick-add-coverage-AAPL-core');
    fireEvent.click(addCoreItem);
    await waitFor(() => {
      expect(mocks.mutateWatchlistMock).toHaveBeenCalledWith({
        action: 'add',
        tickers: ['AAPL'],
        tier: 'core',
      });
    });
    expect(onWatchlistDelete).not.toHaveBeenCalled();
    cleanup();

    // 2. Portfolio ("持仓") tab -> right-click MSFT -> add to Radar
    renderWidget({ defaultTab: 'portfolio', valuesHidden: false });
    const msftCell = await screen.findByText('MSFT');
    fireEvent.contextMenu(msftCell);
    const addRadarItem = await screen.findByTestId('quick-add-coverage-MSFT-radar');
    fireEvent.click(addRadarItem);
    await waitFor(() => {
      expect(mocks.mutateWatchlistMock).toHaveBeenCalledWith({
        action: 'add',
        tickers: ['MSFT'],
        tier: 'radar',
      });
    });
    expect(onPortfolioDelete).not.toHaveBeenCalled();
    expect(onPortfolioEdit).not.toHaveBeenCalled();
  });

  it('b2-r2: 409 on quick-add previews & commits as `add` for non-roster ticker, uses `set_tier` only if already in roster, and does not toast success on ok:false', async () => {
    mocks.toastMock.mockReset();
    mocks.mutateWatchlistMock.mockReset();
    mocks.previewWatchlistMutationMock.mockReset();

    mocks.dashboardCtx = {
      ...mocks.dashboardCtx,
      watchlist: {
        rows: [
          {
            watchlist_item_id: 'wl-1',
            symbol: 'READD',
            price: 100,
            change: 1,
            changePercent: 1.0,
            currency: 'USD',
            quoteAvailable: true,
          },
          {
            watchlist_item_id: 'wl-2',
            symbol: 'BADSYM',
            price: 10,
            change: 0,
            changePercent: 0,
            currency: 'USD',
            quoteAvailable: true,
          },
        ],
        loading: false,
        loaded: true,
      },
      watchlistHandlers: { onAdd: vi.fn(), onDelete: vi.fn() },
    };

    // Case 1: Re-adding a previously removed ticker with paused workspace tasks -> 409 -> previews as `add` (old_tier=null) and commits as `add`!
    const err409 = Object.assign(new Error('409'), { response: { status: 409 } });
    mocks.mutateWatchlistMock
      .mockRejectedValueOnce(err409)
      .mockResolvedValueOnce({ ok: true, added_tickers: ['READD'] });
    mocks.previewWatchlistMutationMock.mockResolvedValueOnce({
      ok: true,
      action: 'watchlist_set_tier',
      raw_action: 'add',
      snapshot_id: 'snap-add-readd',
      requires_confirmation: true,
      target_tickers: ['READD'],
      target_tier: 'core',
      affected_tickers: [{ ticker: 'READD', name: 'Readd Corp', old_tier: null, new_tier: 'core' }],
      affected_workspaces: [],
      task_summary: { resume_count: 1 },
      card_title: '确认加入 READD',
      card_description: '将恢复已暂停的任务',
    });

    renderWidget({ defaultTab: 'watchlist', valuesHidden: false });
    fireEvent.contextMenu(screen.getByText('READD'));
    fireEvent.click(await screen.findByTestId('quick-add-coverage-READD-core'));

    // Preview was called with original action 'add' (NOT 'set_tier')!
    await waitFor(() => {
      expect(mocks.previewWatchlistMutationMock).toHaveBeenCalledWith({
        action: 'add',
        tickers: ['READD'],
        tier: 'core',
      });
    });

    // Confirm modal commits with action: 'add' + snapshot_id
    const confirmBtn = await screen.findByRole('button', { name: /完成|Done/i });
    fireEvent.click(confirmBtn);
    await waitFor(() => {
      expect(mocks.mutateWatchlistMock).toHaveBeenLastCalledWith({
        action: 'add',
        snapshot_id: 'snap-add-readd',
      });
    });

    // Case 2: HTTP 200 with ok:false & empty added_tickers -> destructive toast, NOT success!
    mocks.toastMock.mockReset();
    mocks.mutateWatchlistMock.mockResolvedValueOnce({
      ok: false,
      added: [],
      added_tickers: [],
      not_found: ['BADSYM'],
    });
    fireEvent.contextMenu(screen.getByText('BADSYM'));
    fireEvent.click(await screen.findByTestId('quick-add-coverage-BADSYM-watch'));
    await waitFor(() => {
      expect(mocks.toastMock).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'destructive' }),
      );
    });
  });

  it('b2-r3: rows without watchlist_item_id / user_portfolio_id delegate right-click menu to Dalton RowExtraContextMenu', async () => {
    mocks.mutateWatchlistMock.mockReset().mockResolvedValue({ ok: true, added_tickers: ['NOIDWL'] });
    mocks.dashboardCtx = {
      ...mocks.dashboardCtx,
      watchlist: {
        rows: [
          {
            symbol: 'NOIDWL',
            price: 55,
            change: 1,
            changePercent: 1.8,
            currency: 'USD',
            quoteAvailable: true,
          },
        ],
        loading: false,
        loaded: true,
      },
      watchlistHandlers: { onAdd: vi.fn(), onDelete: vi.fn() },
      portfolio: {
        rows: [
          {
            symbol: 'NOIDPF',
            quantity: 5,
            marketValue: 500,
            unrealizedPlPercent: 5.0,
            currency: 'USD',
            quoteAvailable: true,
          },
        ],
        loading: false,
        loaded: true,
        hasRealHoldings: true,
      },
      portfolioHandlers: { onAdd: vi.fn(), onEdit: vi.fn(), onDelete: vi.fn() },
    };

    renderWidget({ defaultTab: 'watchlist', valuesHidden: false });
    fireEvent.contextMenu(screen.getByText('NOIDWL'));
    fireEvent.click(await screen.findByTestId('quick-add-coverage-NOIDWL-core'));
    await waitFor(() => {
      expect(mocks.mutateWatchlistMock).toHaveBeenCalledWith({
        action: 'add',
        tickers: ['NOIDWL'],
        tier: 'core',
      });
    });
    cleanup();

    mocks.mutateWatchlistMock.mockReset().mockResolvedValue({ ok: true, added_tickers: ['NOIDPF'] });
    renderWidget({ defaultTab: 'portfolio', valuesHidden: false });
    fireEvent.contextMenu(await screen.findByText('NOIDPF'));
    fireEvent.click(await screen.findByTestId('quick-add-coverage-NOIDPF-watch'));
    await waitFor(() => {
      expect(mocks.mutateWatchlistMock).toHaveBeenCalledWith({
        action: 'add',
        tickers: ['NOIDPF'],
        tier: 'watch',
      });
    });
  });
});
