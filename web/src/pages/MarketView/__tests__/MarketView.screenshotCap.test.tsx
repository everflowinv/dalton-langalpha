/**
 * Desktop MarketView screenshot cap: `handleCaptureChartForContext` stays
 * current as confirmed region screenshots are added and removed, so removing
 * one of 10 confirmed crops lets the next chart capture succeed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type React from 'react';
import { queryKeys } from '@/lib/queryKeys';
import { MAX_FILES } from '@/components/ui/chat-input.helpers';
import { chartSelectionStore } from '../stores/chartSelectionStore';

const mocks = vi.hoisted(() => ({
  toast: vi.fn(),
  captureChartAsDataUrl: vi.fn(),
  ws: {
    prices: new Map(),
    connectionStatus: 'connected',
    dataLevel: null,
    ginlixDataEnabled: false,
    subscribe: vi.fn(),
    unsubscribe: vi.fn(),
  },
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

vi.mock('../components/StockHeader', () => ({ default: () => <div data-testid="stock-header" /> }));
vi.mock('../components/MarketChart', async () => {
  const { useImperativeHandle } = await import('react');
  return {
    default: function ChartStub({ ref }: { ref?: React.Ref<unknown> }) {
      useImperativeHandle(ref, () => ({
        captureChartAsDataUrl: mocks.captureChartAsDataUrl,
        getChartMetadata: () => null,
      }));
      return <div data-testid="market-chart" />;
    },
  };
});
vi.mock('../components/MarketChatPanel', () => ({
  default: ({ onCaptureChart, chartImage }: { onCaptureChart: () => void; chartImage: string | null }) => (
    <div data-testid="desktop-chat-panel" data-chart-image={chartImage ?? ''}>
      <button type="button" onClick={onCaptureChart}>capture-chart</button>
    </div>
  ),
}));
vi.mock('../components/MarketSidebarPanel', () => ({ default: () => <div data-testid="sidebar" /> }));
vi.mock('../components/CompanyOverviewPanel', () => ({ default: () => <div data-testid="overview" /> }));

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
vi.mock('../hooks/useChartLegendSlots', () => ({ useChartLegendSlots: () => ({}) }));
vi.mock('../hooks/useRestoredWorkspace', () => ({
  useRestoredWorkspace: () => ({
    selectedWorkspaceId: 'ws-1',
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
vi.mock('@/hooks/usePreferences', async () => {
  const { useQuery } = await import('@tanstack/react-query');
  const { queryKeys: keys } = await import('@/lib/queryKeys');
  return {
    usePreferences: () => {
      const { data } = useQuery({
        queryKey: keys.user.preferences(),
        queryFn: () => null,
        enabled: false,
      });
      return { preferences: data ?? null, isLoading: false, isLoaded: true };
    },
  };
});
vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => ({
    models: {},
    slotModels: {},
    claudeCodeState: 'off',
    payers: {},
    validModelNames: new Set<string>(),
    catalogModelNames: new Set<string>(),
    metadata: {},
    isLoading: false,
    systemDefaults: { default_model: 'model-default', background_model: '' },
    offPlanModels: new Set(),
    platform: { model_tier: 0, byok_providers: [], oauth_providers: [] },
    rawApiResponse: {},
  }),
}));

import MarketView from '../MarketView';

describe('Desktop MarketView screenshot callback reactivity', () => {
  beforeEach(() => {
    localStorage.clear();
    chartSelectionStore.clearAll();
    mocks.toast.mockReset();
    mocks.captureChartAsDataUrl.mockReset();
    mocks.captureChartAsDataUrl.mockResolvedValue('data:image/png;base64,FULLCHART');
  });

  it('updates handleCaptureChartForContext when confirmed region crops drop below MAX_FILES', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(queryKeys.user.preferences(), { other_preference: {} });

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/market?symbol=GOOGL&interval=1day']}>
          <Routes>
            <Route path="/market" element={<MarketView />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const selectionIds: string[] = [];
    act(() => {
      for (let i = 0; i < MAX_FILES; i += 1) {
        const id = chartSelectionStore.beginDraft({
          symbol: 'GOOGL',
          timeframe: '1day',
          selectionType: 'region',
          timeStart: '2026-01-01T00:00:00Z',
          timeEnd: '2026-01-05T00:00:00Z',
          priceLow: 100 + i,
          priceHigh: 110 + i,
          bars: [],
          barsTruncated: false,
          croppedImage: `data:image/jpeg;base64,CROP${i}`,
        });
        chartSelectionStore.confirm(id, `crop-${i}`);
        selectionIds.push(id);
      }
    });

    // At 10 confirmed crops, capturing the full chart is refused with toast.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'capture-chart' }));
    });
    expect(mocks.captureChartAsDataUrl).not.toHaveBeenCalled();
    expect(mocks.toast).toHaveBeenCalledWith(expect.objectContaining({ variant: 'destructive' }));

    // Delete 1 crop -> now 9 confirmed crops -> capturing succeeds!
    mocks.toast.mockReset();
    act(() => {
      chartSelectionStore.remove(selectionIds[0]);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'capture-chart' }));
    });
    await waitFor(() => {
      expect(screen.getByTestId('desktop-chat-panel')).toHaveAttribute(
        'data-chart-image',
        'data:image/png;base64,FULLCHART',
      );
    });
    expect(mocks.captureChartAsDataUrl).toHaveBeenCalledTimes(1);
    expect(mocks.toast).not.toHaveBeenCalled();
  });
});
